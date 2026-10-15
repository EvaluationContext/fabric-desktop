"""Looking after the data estate, mixed into ``Service``: restore a table to an earlier version,
what runs cost in Fabric capacity, OneLake shortcuts, who has access, the tests that gate a
promote, and the VL_Sources variable library.

Each feature's logic lives in its own module (meta/restore, costs, shortcuts, access,
meta/testing, meta/varlib); this file only connects them to the project, Fabric and the UI.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import polars as pl
import requests
from deltalake.exceptions import TableNotFoundError

from . import access as access_mod
from . import costs as costs_mod
from . import shortcuts as shortcuts_mod
from .auth import CONNECTION_SCOPES, GRAPH_USER_SCOPES, ONELAKE_SCOPES
from .fabric_api import FabricClient
from .meta import impact
from .meta import restore as restore_mod
from .meta import testing as testing_mod
from .meta import varlib as varlib_mod
from .meta.engine import _wm_text
from .meta.project import ENVIRONMENTS
from .meta.store import OneLakeStore
from .onelake import TableCatalog

FABRIC_APP = "https://app.fabric.microsoft.com"
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _who(service: Any) -> str:
    try:
        return service.auth.signed_in_user() or ""
    except Exception:  # noqa: BLE001
        return os.environ.get("USERNAME") or os.environ.get("USER") or ""


class EstateMixin:

    def _scoped(self, scopes: list[str]) -> FabricClient:
        """A Fabric client for calls that need a scope beyond the sign-in's (asked for once)."""
        return FabricClient(lambda: self.auth._token(scopes))  # type: ignore[attr-defined]

    # ---- restore a table to an earlier version

    def _entity_uri(self, project: Any, name: str, env: str) -> tuple[Any, Any, str]:
        e = project.entities[name]
        store = self._store(project, env)  # type: ignore[attr-defined]
        return e, store, store.uri(project.layer(e.layer).lakehouse, e.schema, e.table)

    def meta_table_history(self, name: str, env: str = "dev", limit: int = 50) -> dict[str, Any]:
        """The table's versions with which can be restored, and earlier restores of it."""
        project = self._project()  # type: ignore[attr-defined]
        e, store, uri = self._entity_uri(project, name, env)
        days = project.layer(e.layer).retention_days
        try:
            versions = restore_mod.versions(uri, store.options(), days, limit)
        except TableNotFoundError:
            versions = []
        try:
            restores = json.loads(store.meta("restores").filter(pl.col("entity") == name).reverse().head(10).write_json())
        except Exception:  # noqa: BLE001 - the versions are what matter
            restores = []
        return {"entity": name, "env": env, "retention_days": days, "layer": e.layer, "versions": versions,
                "restores": restores, "downstream": impact(project, name),
                "incremental": e.mode == "incremental" and bool(e.spec.get("load", {}).get("watermark"))}

    def meta_restore(self, name: str, env: str, version: int, reason: str = "") -> dict[str, Any]:
        """Put the table back to ``version`` (a new Delta commit; later versions stay in history).

        An incremental table's watermark moves back to the restored data, so its next run loads
        again what came after. Tables downstream are not changed: rebuild them if they took in
        the bad load."""
        project = self._project()  # type: ignore[attr-defined]
        e, store, uri = self._entity_uri(project, name, env)
        result = restore_mod.restore(uri, store.options(), int(version),
                                     {"fabric_desktop_restored_to": str(version),
                                      "fabric_desktop_reason": (reason or "")[:500]})
        watermark = None
        wm_col = e.spec.get("load", {}).get("watermark")
        if e.mode == "incremental" and wm_col:
            df = store.read(project.layer(e.layer).lakehouse, e.schema, e.table)
            watermark = _wm_text(df[wm_col].max()) if df is not None and df.height and wm_col in df.columns else None
            store.meta_append("watermarks", [{"entity": name, "value": watermark,
                                              "run_id": f"restore-v{result['new_version']}"}])
        store.meta_append("restores", [{"entity": name, "env": env, "from_version": result["from_version"],
                                        "to_version": result["to_version"], "new_version": result["new_version"],
                                        "by": _who(self), "reason": reason or ""}])
        return result | {"entity": name, "env": env, "downstream": impact(project, name),
                         "watermark": watermark, "incremental": e.mode == "incremental" and bool(wm_col)}

    # ---- cost

    def _cost_settings(self) -> dict[str, Any]:
        return costs_mod.settings(self._settings().get("cost"))  # type: ignore[attr-defined]

    def set_cost_settings(self, price_per_cu_hour: float | None = None, currency: str | None = None,
                          python_cu: float | None = None, spark_cu: float | None = None) -> dict[str, Any]:
        cfg = self._cost_settings()
        for key, value in (("price_per_cu_hour", price_per_cu_hour), ("python_cu", python_cu), ("spark_cu", spark_cu)):
            if value not in (None, ""):
                if float(value) <= 0:
                    raise ValueError(f"{key.replace('_', ' ')} must be more than 0")
                cfg[key] = float(value)
        if currency:
            cfg["currency"] = str(currency).strip().upper()[:5]
        self._save_settings(cost=cfg)  # type: ignore[attr-defined]
        return cfg

    def _env_capacities(self) -> dict[str, dict[str, Any]]:
        """The capacity each environment's workspace is on, read only when already signed in."""
        try:
            project = self._project()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return {}
        fabric = self._quiet_fabric()  # type: ignore[attr-defined]
        if fabric is None:
            return {}
        cache = getattr(self, "_capacity_cache", None)
        if cache is None:
            try:
                cache = {c["id"]: c for c in fabric.list_capacities()}
            except Exception:  # noqa: BLE001
                return {}
            self._capacity_cache = cache
        out = {}
        for env in ENVIRONMENTS:
            ws = project.environments.get(env, {}).get("workspace_id")
            if not ws:
                continue
            try:
                cap = cache.get(fabric.get_workspace(ws).get("capacityId") or "")
            except Exception:  # noqa: BLE001
                continue
            if cap:
                out[env] = {"name": cap.get("displayName"), "sku": cap.get("sku")}
        return out

    def cost_report(self, days: int = 30) -> dict[str, Any]:
        """Estimated capacity use and cost of every run in the last ``days``, and what this PC saved."""
        cfg = self._cost_settings()
        meta = self.meta_runs_all(limit=100000)  # type: ignore[attr-defined]
        fabric = self.fabric_runs(limit=500, refresh=False)  # type: ignore[attr-defined]
        writes = self.history(limit=100000)  # type: ignore[attr-defined]
        rows = costs_mod.activities(meta["runs"], fabric, writes, cfg)
        return costs_mod.report(rows, cfg, int(days), capacities=self._env_capacities()) | {
            "unavailable": meta.get("unavailable", {})}

    # ---- OneLake shortcuts

    def _managed_in(self, workspace_id: str, lakehouse_name: str) -> list[dict[str, Any]]:
        """Managed tables that live in this lakehouse of an environment's workspace."""
        try:
            project = self._project()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return []
        if not any(project.environments.get(env, {}).get("workspace_id") == workspace_id for env in ENVIRONMENTS):
            return []
        return [{"name": e.name, "files": e.spec.get("source", {}).get("files"), "schema": e.schema, "table": e.table}
                for e in project.entities.values()
                if e.layer in project.layer_names() and project.layer(e.layer).lakehouse == lakehouse_name]

    def _shortcut_rows(self, api: FabricClient, workspace_id: str, item_id: str, lakehouse_name: str) -> list[dict[str, Any]]:
        managed = self._managed_in(workspace_id, lakehouse_name)
        rows = [shortcuts_mod.describe(s) for s in api.list_shortcuts(workspace_id, item_id)]
        for r in rows:
            r["used_by"] = shortcuts_mod.used_by(r, managed)
        return sorted(rows, key=lambda r: r["full_path"].lower())

    def list_shortcuts(self, workspace_id: str, item_id: str, lakehouse_name: str = "") -> list[dict[str, Any]]:
        return self._shortcut_rows(self._scoped(ONELAKE_SCOPES), workspace_id, item_id, lakehouse_name)

    def all_shortcuts(self, lakehouses: list[dict[str, str]], refresh: bool = False) -> dict[str, Any]:
        """Shortcuts in every lakehouse, read in the background: {"pending": True} until done."""
        lock = self.__dict__.setdefault("_shortcuts_lock", threading.Lock())
        with lock:
            cached = getattr(self, "_all_shortcuts", None)
            if cached is not None and not refresh:
                return dict(cached)
            token = self.auth._token(ONELAKE_SCOPES)  # type: ignore[attr-defined]  # here: MSAL stays on one thread
            self._all_shortcuts = {"pending": True, "items": {}, "errors": {}}

        def read(ref: dict[str, str]) -> tuple[str, list[dict[str, Any]] | None, str | None]:
            try:
                return ref["lakehouse_id"], self._shortcut_rows(FabricClient(lambda: token), ref["workspace_id"],
                                                                ref["lakehouse_id"], ref.get("name", "")), None
            except Exception as exc:  # noqa: BLE001 - a paused capacity, no access: list the rest
                return ref["lakehouse_id"], None, str(exc)

        def run() -> None:
            items, errors = {}, {}
            with ThreadPoolExecutor(max_workers=8) as pool:
                for lh, rows, err in pool.map(read, lakehouses):
                    if rows is None:
                        errors[lh] = err
                    else:
                        items[lh] = rows
            with lock:
                self._all_shortcuts = {"pending": False, "items": items, "errors": errors}

        threading.Thread(target=run, daemon=True).start()
        return {"pending": True, "items": {}, "errors": {}}

    def create_shortcut(self, workspace_id: str, item_id: str, where: str, name: str,
                        source: dict[str, Any], lakehouse_name: str = "") -> list[dict[str, Any]]:
        api = self._scoped(ONELAKE_SCOPES)
        api.create_shortcut(workspace_id, item_id, shortcuts_mod.location(where), shortcuts_mod.check_name(name),
                            shortcuts_mod.target(source))
        self._all_shortcuts = None
        return self._shortcut_rows(api, workspace_id, item_id, lakehouse_name)

    def delete_shortcut(self, workspace_id: str, item_id: str, path: str, name: str,
                        lakehouse_name: str = "") -> list[dict[str, Any]]:
        """Remove the shortcut only; the data it points at is not touched."""
        api = self._scoped(ONELAKE_SCOPES)
        api.delete_shortcut(workspace_id, item_id, path, name)
        self._all_shortcuts = None
        return self._shortcut_rows(api, workspace_id, item_id, lakehouse_name)

    def shortcut_connections(self) -> list[dict[str, Any]]:
        return shortcuts_mod.connection_rows(self._scoped(CONNECTION_SCOPES).list_connections())

    def meta_entity_shortcuts(self, name: str, env: str = "dev") -> list[dict[str, Any]]:
        """Shortcuts the entity reads its files through, for its lineage. Never asks to sign in."""
        project = self._project()  # type: ignore[attr-defined]
        e = project.entities[name]
        if not e.spec.get("source", {}).get("files"):
            return []
        token = self.auth.silent_token(ONELAKE_SCOPES)  # type: ignore[attr-defined]
        if not token:
            return []
        try:
            store = self._store(project, env)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(store, OneLakeStore):
            return []
        lh = project.layer(e.layer).lakehouse
        if lh not in store.lakehouse_ids:
            return []
        rows = self._shortcut_rows(FabricClient(lambda: token), store.workspace_id, store.lakehouse_ids[lh], lh)
        return [r for r in rows if any(u["entity"] == name for u in r["used_by"])]

    # ---- access

    def _review_targets(self, extra: list[dict[str, str]] | None) -> list[dict[str, Any]]:
        targets: list[dict[str, Any]] = []
        seen: set[str] = set()
        try:
            project = self._project()  # type: ignore[attr-defined]
            envs = project.environments
        except Exception:  # noqa: BLE001 - no metadata project: review the workspaces asked for
            envs = {}
        for env in ENVIRONMENTS:
            cfg = envs.get(env, {})
            for key, ws, name, label in ((env, cfg.get("workspace_id"), cfg.get("workspace_name"), env.title()),
                                         (f"{env}-keys", cfg.get("keys_workspace_id"), cfg.get("keys_workspace_name"),
                                          f"{env.title()} keys")):
                if ws and ws not in seen:
                    seen.add(ws)
                    targets.append({"key": key, "label": label, "env": env if key == env else None,
                                    "workspace_id": ws, "workspace_name": name or ""})
        for w in extra or []:
            if w.get("workspace_id") and w["workspace_id"] not in seen:
                seen.add(w["workspace_id"])
                targets.append({"key": w["workspace_id"], "label": w.get("workspace_name") or "Workspace", "env": None,
                                "workspace_id": w["workspace_id"], "workspace_name": w.get("workspace_name") or ""})
        return targets

    def access_review(self, extra: list[dict[str, str]] | None = None) -> dict[str, Any]:
        """Workspace roles across Dev, Test, Prod (and their Keys workspaces), one row per person."""
        targets = self._review_targets(extra)
        if targets:
            client = self.fabric  # type: ignore[attr-defined]
            for t in targets:
                try:
                    t["assignments"] = client.list_role_assignments(t["workspace_id"])
                except Exception as exc:  # noqa: BLE001 - e.g. a Viewer can't list roles
                    t["error"] = str(exc)
        return access_mod.review(targets) | {"roles": list(access_mod.ROLES)}

    def access_set_role(self, workspace_id: str, role: str = "", assignment_id: str = "", principal_id: str = "",
                        principal_type: str = "User") -> dict[str, Any]:
        """Give someone a workspace role, change it, or (role "") take it away."""
        if role and role not in access_mod.ROLES:
            raise ValueError(f"role must be one of {', '.join(access_mod.ROLES)}")
        fabric = self.fabric  # type: ignore[attr-defined]
        if assignment_id and role:
            fabric.update_role_assignment(workspace_id, assignment_id, role)
        elif assignment_id:
            fabric.delete_role_assignment(workspace_id, assignment_id)
        elif principal_id and role:
            fabric.add_role_assignment(workspace_id, principal_id, principal_type or "User", role)
        else:
            raise ValueError("Pick a person and a role.")
        return {"ok": True}

    def access_find_person(self, text: str) -> dict[str, Any]:
        """A person by email (Microsoft Graph), or a group or app by its object ID."""
        text = (text or "").strip()
        if _GUID.match(text):
            return {"id": text, "name": text, "email": "", "type": "Group"}
        if "@" not in text:
            raise ValueError("Type a person's email, or a group's or app's object ID.")
        token = self.auth._token(GRAPH_USER_SCOPES)  # type: ignore[attr-defined]
        r = requests.get(f"https://graph.microsoft.com/v1.0/users/{text}",
                         params={"$select": "id,displayName,userPrincipalName,mail"},
                         headers={"Authorization": f"Bearer {token}"}, timeout=30)
        if r.status_code == 404:
            r = requests.get("https://graph.microsoft.com/v1.0/users",
                             params={"$filter": f"mail eq '{text.replace(chr(39), '')}'",
                                     "$select": "id,displayName,userPrincipalName,mail"},
                             headers={"Authorization": f"Bearer {token}"}, timeout=30)
            found = (r.json().get("value") or []) if r.ok else []
            if not found:
                raise LookupError(f"No one with the email {text} in your organization.")
            user = found[0]
        elif not r.ok:
            raise RuntimeError(f"Microsoft Graph answered {r.status_code}: {r.text[:200]}")
        else:
            user = r.json()
        return {"id": user["id"], "name": user.get("displayName") or text,
                "email": user.get("userPrincipalName") or user.get("mail") or text, "type": "User"}

    def _lakehouse_paths(self, workspace_id: str, item_id: str) -> tuple[list[str], list[str]]:
        catalog = TableCatalog(self.auth.storage_token)  # type: ignore[attr-defined]
        schemas = catalog.list_schemas(workspace_id, item_id)
        tables = [f"/Tables/{s}/{t}" for s in schemas for t in catalog.list_tables(workspace_id, item_id, s)]
        return [f"/Tables/{s}" for s in schemas], tables

    def access_lakehouse(self, workspace_id: str, item_id: str) -> dict[str, Any]:
        """A lakehouse's OneLake data access roles, its tables, and the workspace's people."""
        people = []
        try:
            people = [access_mod.principal(a.get("principal") or {}) | {"role": a.get("role")}
                      for a in self.fabric.list_role_assignments(workspace_id)]  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - roles still show; names fall back to IDs
            pass
        try:
            schemas, tables = self._lakehouse_paths(workspace_id, item_id)
        except Exception:  # noqa: BLE001
            schemas, tables = [], []
        out: dict[str, Any] = {"workspace_id": workspace_id, "item_id": item_id, "people": people,
                               "schemas": schemas, "tables": tables, "enabled": True, "problem": None, "roles": []}
        try:
            out["roles"] = [access_mod.describe_role(r)
                            for r in self._scoped(ONELAKE_SCOPES).list_data_access_roles(workspace_id, item_id)]
        except Exception as exc:  # noqa: BLE001 - OneLake security not turned on for this lakehouse, or no rights
            out["enabled"], out["problem"] = False, str(exc)
        return out

    def access_save_role(self, workspace_id: str, item_id: str, name: str, paths: list[str], members: list[str],
                         original_name: str | None = None) -> dict[str, Any]:
        api = self._scoped(ONELAKE_SCOPES)
        roles = api.list_data_access_roles(workspace_id, item_id)
        names = {r.get("name") for r in roles}
        if (original_name is None or name != original_name) and name in names:
            raise ValueError(f"There is already a role named {name}.")
        keep = next((r for r in roles if r.get("name") == (original_name or name)), None)
        role = access_mod.make_role(name, paths, members, self.auth.signed_in_tenant() or "", keep)  # type: ignore[attr-defined]
        api.put_data_access_roles(workspace_id, item_id,
                                  [r for r in roles if r.get("name") not in (original_name, name)] + [role])
        return self.access_lakehouse(workspace_id, item_id)

    def access_delete_role(self, workspace_id: str, item_id: str, name: str) -> dict[str, Any]:
        api = self._scoped(ONELAKE_SCOPES)
        roles = api.list_data_access_roles(workspace_id, item_id)
        api.put_data_access_roles(workspace_id, item_id, [r for r in roles if r.get("name") != name])
        return self.access_lakehouse(workspace_id, item_id)

    def access_who_can_read(self, workspace_id: str, item_id: str, table: str) -> list[dict[str, Any]]:
        assignments = self.fabric.list_role_assignments(workspace_id)  # type: ignore[attr-defined]
        try:
            roles = self._scoped(ONELAKE_SCOPES).list_data_access_roles(workspace_id, item_id)
        except Exception:  # noqa: BLE001 - without OneLake security, the workspace roles decide
            roles = None
        return access_mod.who_can_read(table, assignments, roles)

    # ---- tests that gate a promote

    def _test_results_file(self, project: Any) -> Any:
        return project.local_data / "test_results.json"

    def _run_tests(self, project: Any) -> dict[str, Any]:
        result = testing_mod.run(project)
        path = self._test_results_file(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=1), encoding="utf-8")
        return result

    def meta_tests(self, run: bool = False) -> dict[str, Any]:
        """The project's tests and their last results; ``run`` runs them now."""
        project = self._project()  # type: ignore[attr-defined]
        if run:
            result = self._run_tests(project)
        else:
            try:
                result = json.loads(self._test_results_file(project).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                result = {"tests": [], "passed": 0, "failed": 0, "missing": [], "at": None, "version": None}
        files = [p.relative_to(project.root).as_posix() for p in testing_mod.discover(project)]
        return result | {"files": files, "references": testing_mod.referenced(project),
                         "stale": result.get("version") not in (None, project.version)}

    def _test_gate(self, project: Any) -> tuple[list[str], dict[str, Any]]:
        result = self._run_tests(project)
        return testing_mod.gate_errors(result), {k: result[k] for k in ("passed", "failed", "at")}

    # ---- VL_Sources variable library

    def meta_varlib(self) -> dict[str, Any]:
        project = self._project()  # type: ignore[attr-defined]
        envs = {env: {"workspace_id": project.environments.get(env, {}).get("workspace_id", ""),
                      "workspace_name": project.environments.get(env, {}).get("workspace_name", ""),
                      "value_set": varlib_mod.active_set(env)} for env in ENVIRONMENTS}
        return {"library": varlib_mod.LIBRARY, "variables": varlib_mod.values(project), "environments": envs,
                "published": self._settings().get("varlib_published", {})}  # type: ignore[attr-defined]

    def _publish_varlib(self, project: Any, env: str) -> dict[str, Any]:
        cfg = project.environments.get(env, {})
        ws = cfg.get("workspace_id")
        if not ws:
            raise LookupError(f"Assign a workspace to {env.title()} first (Metadata > Environments and promote).")
        fabric = self.fabric  # type: ignore[attr-defined]
        item = fabric.save_variable_library(ws, varlib_mod.LIBRARY, varlib_mod.definition(project),
                                            "Source system connections, managed by Fabric Desktop")
        if item.get("id"):
            fabric.set_active_value_set(ws, item["id"], varlib_mod.active_set(env))
        published = self._settings().get("varlib_published", {})  # type: ignore[attr-defined]
        published[env] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        self._save_settings(varlib_published=published)  # type: ignore[attr-defined]
        return {"env": env, "workspace_id": ws, "workspace_name": cfg.get("workspace_name", ""),
                "created": bool(item.get("created")), "value_set": varlib_mod.active_set(env),
                "variables": len(varlib_mod.values(project)),
                "url": f"{FABRIC_APP}/groups/{ws}/variablelibraries/{item.get('id')}?experience=fabric-developer"
                if item.get("id") else None}

    def meta_publish_varlib(self, env: str) -> dict[str, Any]:
        """Create or update VL_Sources in the environment's workspace, with its value set active."""
        if env not in ENVIRONMENTS:
            raise ValueError(f"environment must be one of {', '.join(ENVIRONMENTS)}")
        return self._publish_varlib(self._project(), env)  # type: ignore[attr-defined]

    def _notebook_variables(self, library: str) -> dict[str, str] | None:
        """notebookutils.variableLibrary on this PC: VL_Sources for the attached lakehouse's environment."""
        if library != varlib_mod.LIBRARY:
            return None
        try:
            project = self._project()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return None
        from . import lakehouse
        ws = lakehouse._attached.workspace_id if lakehouse._attached else None
        env = next((env for env in ENVIRONMENTS if ws and project.environments.get(env, {}).get("workspace_id") == ws), "dev")
        return varlib_mod.resolve(project, env)
