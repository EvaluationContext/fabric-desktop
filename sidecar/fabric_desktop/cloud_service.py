"""UI actions that use Fabric capacity on purpose, mixed into ``Service``.

Development runs on this PC; these hand finished work to Fabric: run a published notebook
there now or on a schedule, keep tables V-Ordered and vacuumed with Fabric's table
maintenance job, and put a Direct Lake semantic model over the last layer. Profiling is
here too: it runs locally and turns what it finds into cleaning rules and checks.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import polars as pl

from . import maintenance, profiling, semantic
from .auth import FABRIC_SCOPES, STORAGE_SCOPES
from .fabric_api import FabricClient
from .fabric_compute import BUNDLE_DIR, RESULTS_DIR, RUNNER, bundle_files, bundle_hash, run_parameters, runner_ipynb
from .fabric_run import TERMINAL, describe_schedule, fabric_ipynb, schedule_configuration, uses_local_module, uses_spark
from .meta.engine import Engine, RunResult, apply_clean
from .meta.project import ENVIRONMENTS, GateError, validate
from .meta.store import META_LAKEHOUSE, META_SCHEMAS, LocalStore, OneLakeStore
from .onelake import FileStore

NOTEBOOK_JOB = "RunNotebook"
FABRIC_APP = "https://app.fabric.microsoft.com"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class CloudMixin:
    root: Path

    # ---- the log of jobs started from this app in Fabric (notebook runs, table maintenance)

    @property
    def fabric_runs_file(self) -> Path:
        return self.root / "fabric_runs.jsonl"

    def _read_fabric_runs(self) -> list[dict[str, Any]]:
        try:
            lines = self.fabric_runs_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return [json.loads(line) for line in lines if line.strip()]

    def _write_fabric_runs(self, runs: list[dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.fabric_runs_file.write_text("".join(json.dumps(r) + "\n" for r in runs[-500:]), encoding="utf-8")

    def _log_fabric_run(self, record: dict[str, Any]) -> dict[str, Any]:
        record = {"status": "NotStarted", "started_at": _now(), "finished_at": None, "message": ""} | record
        self._write_fabric_runs(self._read_fabric_runs() + [record])
        return record

    def fabric_runs(self, limit: int = 50, refresh: bool = True) -> list[dict[str, Any]]:
        """Jobs this app started in Fabric, newest first; unfinished ones are asked for their status."""
        runs = self._read_fabric_runs()
        if refresh:
            open_runs = [r for r in runs[-limit:] if r.get("status") not in TERMINAL]
            fabric = self._quiet_fabric() if open_runs else None
            if fabric is not None:
                for r in open_runs:
                    try:
                        job = fabric.job_instance(r["workspace_id"], r["item_id"], r["job_id"])
                    except Exception as exc:  # offline or gone: keep the last known status
                        r["message"] = r.get("message") or f"Status not available: {exc}"
                        continue
                    r["status"] = job.get("status", r["status"])
                    r["finished_at"] = job.get("endTimeUtc") or r.get("finished_at")
                    reason = job.get("failureReason") or {}
                    r["message"] = reason.get("message", "") if isinstance(reason, dict) else str(reason)
                self._write_fabric_runs(runs)
        return runs[-limit:][::-1]

    def _quiet_fabric(self) -> FabricClient | None:
        """A Fabric client only if the user is already signed in: Monitor must never pop a sign-in."""
        try:
            token = self.auth.silent_token(FABRIC_SCOPES)  # type: ignore[attr-defined]
        except Exception:  # no client ID configured
            return None
        return FabricClient(lambda: token) if token else None

    def meta_runs_all(self, limit: int = 200) -> dict[str, Any]:
        """Metadata runs from every environment, newest first, for Monitor and Home.

        Environments in OneLake are read only when the user is already signed in, so this never
        pops a sign-in; ``unavailable`` says why an environment was left out."""
        try:
            project = self._project()  # type: ignore[attr-defined]
        except Exception:  # no metadata project yet
            return {"runs": [], "unavailable": {}}
        signed_in = None
        runs: list[dict[str, Any]] = []
        unavailable: dict[str, str] = {}
        for env in ENVIRONMENTS:
            cfg = self._env_cfg(project, env)  # type: ignore[attr-defined]
            local = cfg.get("target", "onelake" if env != "dev" else "local") == "local"
            if not local and not cfg.get("workspace_id"):
                continue  # not set up yet
            if not local:
                if signed_in is None:
                    try:
                        signed_in = bool(self.auth.silent_token(STORAGE_SCOPES)) and self._quiet_fabric() is not None  # type: ignore[attr-defined]
                    except Exception:
                        signed_in = False
                if not signed_in:
                    unavailable[env] = "Sign in to see its runs"
                    continue
            try:
                rows = self.meta_runs(env, limit)  # type: ignore[attr-defined]
            except Exception as exc:  # offline, or a lakehouse is gone
                unavailable[env] = str(exc)
                continue
            for r in rows:
                r["env"] = env
                r["compute"] = "fabric" if str(r.get("batch_id") or "").startswith("fabric-") else "local"
            runs += rows
        runs.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
        return {"runs": runs[:limit], "unavailable": unavailable}

    def cancel_fabric_run(self, workspace_id: str, item_id: str, job_id: str) -> dict[str, Any]:
        self.fabric_jobs.cancel_job(workspace_id, item_id, job_id)  # type: ignore[attr-defined]
        return {"cancelled": job_id}

    # ---- notebooks in Fabric

    def _publish_workspace(self, name: str, lakehouse_ref: dict[str, str] | None,
                           workspace: dict[str, str] | None = None) -> dict[str, str]:
        """Where the notebook is published: the workspace picked for it (passed now or saved with it),
        else the attached lakehouse's workspace."""
        if not (workspace and workspace.get("workspace_id")):
            path = self.notebooks.path(name)  # type: ignore[attr-defined]
            workspace = self.notebooks.load(name).get("workspace") if path.exists() else None  # type: ignore[attr-defined]
        if workspace and workspace.get("workspace_id"):
            return {"workspace_id": workspace["workspace_id"], "workspace_name": workspace.get("workspace_name", "")}
        if lakehouse_ref and lakehouse_ref.get("workspace_id"):
            return {"workspace_id": lakehouse_ref["workspace_id"], "workspace_name": lakehouse_ref.get("workspace_name", "")}
        raise ValueError(f"Pick a workspace to publish {name} to.")

    def _publish(self, name: str, cells: list[dict[str, Any]], lakehouse_ref: dict[str, str] | None,
                 compute: str | None = None, workspace: dict[str, str] | None = None) -> dict[str, Any]:
        ws = self._publish_workspace(name, lakehouse_ref, workspace)
        self.notebooks.save(name, cells, lakehouse_ref, compute, ws)  # type: ignore[attr-defined]
        schemas = (self._schemas_enabled(lakehouse_ref["workspace_id"], lakehouse_ref["lakehouse_id"])  # type: ignore[attr-defined]
                   if lakehouse_ref else True)
        ipynb = json.dumps(fabric_ipynb(cells, lakehouse_ref, schemas)).encode()
        item = self.fabric.save_notebook(ws["workspace_id"], name, ipynb)  # type: ignore[attr-defined]
        if not item.get("id"):  # a create can come back without the item; look it up
            item = next((n for n in self.fabric.list_notebooks(ws["workspace_id"])  # type: ignore[attr-defined]
                         if n["displayName"] == name), item)
        return {"name": name, "workspace_id": ws["workspace_id"], "workspace_name": ws["workspace_name"],
                "id": item.get("id"),
                "url": f"{FABRIC_APP}/groups/{ws['workspace_id']}/synapsenotebooks/{item.get('id')}"
                       "?experience=fabric-developer"}

    def run_notebook_in_fabric(self, name: str, cells: list[dict[str, Any]], lakehouse_ref: dict[str, str] | None,
                               compute: str | None = None, workspace: dict[str, str] | None = None) -> dict[str, Any]:
        """Publish the notebook, then run it on its workspace's Fabric capacity."""
        item = self._publish(name, cells, lakehouse_ref, compute, workspace)
        job_id = self.fabric_jobs.run_item_job(item["workspace_id"], item["id"], NOTEBOOK_JOB)  # type: ignore[attr-defined]
        # As published: a Python notebook, or Spark (what the cost view counts it as).
        engine = "python" if lakehouse_ref and uses_local_module(cells) and not uses_spark(cells) else "spark"
        return self._log_fabric_run({"kind": "Notebook run", "name": name, "workspace_id": item["workspace_id"],
                                     "workspace_name": item["workspace_name"], "engine": engine,
                                     "item_id": item["id"], "job_id": job_id, "url": item["url"]})

    def _notebook_item(self, name: str, workspace_id: str) -> dict[str, Any] | None:
        return next((n for n in self.fabric.list_notebooks(workspace_id)  # type: ignore[attr-defined]
                     if n["displayName"] == name), None)

    def notebook_schedules(self, name: str, lakehouse_ref: dict[str, str] | None = None,
                           workspace: dict[str, str] | None = None) -> dict[str, Any]:
        ws = self._publish_workspace(name, lakehouse_ref, workspace)
        item = self._notebook_item(name, ws["workspace_id"])
        if item is None:
            return {"published": False, "schedules": [], "workspace_id": ws["workspace_id"],
                    "workspace_name": ws["workspace_name"]}
        schedules = self.fabric.list_schedules(ws["workspace_id"], item["id"], NOTEBOOK_JOB)  # type: ignore[attr-defined]
        return {"published": True, "item_id": item["id"], "workspace_id": ws["workspace_id"],
                "workspace_name": ws["workspace_name"],
                "schedules": [s | {"text": describe_schedule(s)} for s in schedules]}

    def add_notebook_schedule(self, name: str, cells: list[dict[str, Any]], lakehouse_ref: dict[str, str] | None,
                              schedule: dict[str, Any], compute: str | None = None,
                              workspace: dict[str, str] | None = None) -> dict[str, Any]:
        """Publish the notebook (so the schedule runs what is on screen), then add a schedule to it."""
        config = schedule_configuration(schedule.get("kind", "daily"), schedule.get("times"),
                                        schedule.get("every_minutes"), schedule.get("weekdays"),
                                        schedule.get("time_zone") or "UTC")
        item = self._publish(name, cells, lakehouse_ref, compute, workspace)
        self.fabric_jobs.create_schedule(item["workspace_id"], item["id"], NOTEBOOK_JOB, config)  # type: ignore[attr-defined]
        return self.notebook_schedules(name, lakehouse_ref)

    def delete_notebook_schedule(self, name: str, schedule_id: str, lakehouse_ref: dict[str, str] | None = None,
                                 workspace: dict[str, str] | None = None) -> dict[str, Any]:
        ws = self._publish_workspace(name, lakehouse_ref, workspace)
        item = self._notebook_item(name, ws["workspace_id"])
        if item is None:
            raise LookupError(f"{name} isn't published in {ws['workspace_name'] or 'that workspace'}")
        self.fabric_jobs.delete_schedule(ws["workspace_id"], item["id"], NOTEBOOK_JOB, schedule_id)  # type: ignore[attr-defined]
        return self.notebook_schedules(name, lakehouse_ref)

    # ---- table health and maintenance for the metadata project's tables

    def _env_cfg(self, project: Any, env: str) -> dict[str, Any]:
        return project.environments.get(env, {})

    def _optimize_after_run(self, project: Any, env: str) -> bool:
        cfg = self._env_cfg(project, env)
        if cfg.get("target", "onelake" if env != "dev" else "local") == "local":
            return False
        return bool(cfg.get("optimize_after_run", True))

    def _managed_tables(self, project: Any, include_meta: bool = True) -> list[dict[str, str]]:
        rows = [{"entity": e.name, "lakehouse": project.layer(e.layer).lakehouse, "schema": e.schema,
                 "table": e.table, "layer": e.layer, "retention_days": project.layer(e.layer).retention_days}
                for e in sorted(project.entities.values(), key=lambda x: (project.layer_index(x.layer), x.name))
                if e.layer in project.layer_names()]
        if include_meta:
            rows += [{"entity": f"meta.{t}", "lakehouse": META_LAKEHOUSE, "schema": "meta", "table": t, "layer": "",
                      "retention_days": maintenance.RETENTION_HOURS // 24} for t in META_SCHEMAS]
        return rows

    def meta_health(self, env: str = "dev") -> dict[str, Any]:
        project = self._project()  # type: ignore[attr-defined]
        store = self._store(project, env)  # type: ignore[attr-defined]
        local = isinstance(store, LocalStore)
        tables = []
        for t in self._managed_tables(project):
            try:
                h = maintenance.health(store.uri(t["lakehouse"], t["schema"], t["table"]), store.options(), local=local)
            except LookupError as exc:  # the lakehouse isn't in this environment yet
                h = {"exists": False, "advice": [], "status": "missing", "message": str(exc)}
            tables.append(t | h)
        cfg = self._env_cfg(project, env)
        return {"env": env, "local": local, "optimize_after_run": self._optimize_after_run(project, env),
                "workspace_id": cfg.get("workspace_id", ""), "tables": tables,
                "maintenance": [r for r in self.fabric_runs(100, refresh=not local)
                                if r.get("kind") == "Table maintenance" and r.get("env") == env][:20]}

    def _maintain(self, store: Any, rows: list[dict[str, str]], env: str) -> list[dict[str, Any]]:
        out = []
        for t in rows:
            label = f"{t['schema']}.{t['table']}"
            try:
                days = t.get("retention_days") or maintenance.RETENTION_HOURS // 24
                if isinstance(store, LocalStore):
                    result = maintenance.compact_local(store.uri(t["lakehouse"], t["schema"], t["table"]), days)
                    out.append({"table": label, "status": "Completed", **result})
                    continue
                lh_id = store.lakehouse_ids[t["lakehouse"]]
                try:  # so Fabric's own engines keep the same history; the job below vacuums with it too
                    maintenance.set_retention(store.uri(t["lakehouse"], t["schema"], t["table"]), days, store.options())
                except Exception:  # noqa: BLE001 - the maintenance job still runs with the right retention
                    pass
                job_id = self.fabric_jobs.run_table_maintenance(  # type: ignore[attr-defined]
                    store.workspace_id, lh_id, t["table"], t["schema"], vacuum_retention=maintenance.fabric_retention(days))
                out.append(self._log_fabric_run({
                    "kind": "Table maintenance", "name": f"{t['lakehouse']} {label}", "env": env, "table": label,
                    "workspace_id": store.workspace_id, "item_id": lh_id, "job_id": job_id}))
            except Exception as exc:  # one table failing doesn't stop the rest
                out.append({"table": label, "status": "Failed", "message": f"{type(exc).__name__}: {exc}"})
        return out

    def meta_optimize(self, env: str = "dev", entities: list[str] | None = None) -> list[dict[str, Any]]:
        """OPTIMIZE and VACUUM tables: Fabric's maintenance job (with V-Order) in OneLake, delta-rs on this PC."""
        project = self._project()  # type: ignore[attr-defined]
        store = self._store(project, env)  # type: ignore[attr-defined]
        rows = [t for t in self._managed_tables(project) if entities is None or t["entity"] in entities]
        if isinstance(store, OneLakeStore):
            rows = [t for t in rows if t["lakehouse"] in store.lakehouse_ids]
        return self._maintain(store, rows, env)

    def meta_set_maintenance(self, env: str, optimize_after_run: bool) -> dict[str, Any]:
        project = self._project()  # type: ignore[attr-defined]
        envs = {k: dict(v) for k, v in project.environments.items()}
        envs.setdefault(env, {})["optimize_after_run"] = bool(optimize_after_run)
        self._write_environments(project, envs)  # type: ignore[attr-defined]
        return self.meta_health(env)

    def _after_run(self, project: Any, env: str, store: Any, results: list[dict[str, Any]]) -> None:
        """Queue Fabric table maintenance for every table a OneLake run wrote."""
        if not isinstance(store, OneLakeStore) or not self._optimize_after_run(project, env):
            return
        written = {r["entity"] for r in results if r.get("status") == "ok" and r.get("rows_written")}
        rows = [t for t in self._managed_tables(project, include_meta=False) if t["entity"] in written]
        jobs = {j["table"]: j for j in self._maintain(store, rows, env)}
        for r in results:
            e = project.entities.get(r["entity"])
            job = jobs.get(f"{e.schema}.{e.table}") if e else None
            if job:
                r["maintenance"] = job.get("status") if job.get("status") == "Failed" else "queued"
                r["maintenance_message"] = job.get("message", "")

    # ---- metadata runs on Fabric capacity (compute = "fabric" in environments.toml)

    def _compute(self, project: Any, env: str) -> str:
        return self._env_cfg(project, env).get("compute", "local")

    def meta_set_compute(self, env: str, compute: str) -> dict[str, Any]:
        """Run an environment's metadata runs on this PC or on its workspace's Fabric capacity."""
        if compute not in ("local", "fabric"):
            raise ValueError("compute is local (this PC) or fabric")
        project = self._project()  # type: ignore[attr-defined]
        cfg = self._env_cfg(project, env)
        if compute == "fabric" and cfg.get("target", "onelake" if env != "dev" else "local") == "local":
            raise ValueError(f"{env.title()} keeps its tables on this PC, which Fabric can't reach. "
                             f"Point {env.title()} at a Fabric workspace first, then run it in Fabric.")
        envs = {k: dict(v) for k, v in project.environments.items()}
        envs.setdefault(env, {})["compute"] = compute
        self._write_environments(project, envs)  # type: ignore[attr-defined]
        return self.meta_project()  # type: ignore[attr-defined]

    @property
    def _bundles_file(self) -> Path:
        return self.root / "fabric_bundles.json"

    def _fabric_bundle(self, project: Any, env: str, store: Any) -> dict[str, str]:
        """Upload the engine and the project to the environment's LH_Meta and publish the runner
        notebook, unless the same bundle is already there. Returns where the runner is."""
        if not isinstance(store, OneLakeStore):
            raise ValueError(f"{env.title()} keeps its tables on this PC, so it can't run in Fabric")
        ws = store.workspace_id
        meta_id = store.lakehouse_ids.get(META_LAKEHOUSE)
        if not meta_id:
            raise LookupError(f"{META_LAKEHOUSE} doesn't exist in the {env} workspace yet; promote to {env} first")
        config: dict[str, Any] = {"env": env, "workspace_id": ws, "lakehouse_ids": dict(store.lakehouse_ids),
                                  "optimize_after_run": self._optimize_after_run(project, env)}
        if self._uses_keys(project):  # type: ignore[attr-defined]
            keys_store = self._keys_store(project, env, create=env == "dev")  # type: ignore[attr-defined]
            if isinstance(keys_store, OneLakeStore):
                config |= {"keys_workspace_id": keys_store.workspace_id, "keys_lakehouse_ids": dict(keys_store.lakehouse_ids)}
        files = bundle_files(project.root, config)
        digest = bundle_hash(files)
        try:
            state = json.loads(self._bundles_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        key = f"{env}:{ws}"
        known = state.get(key, {})
        if known.get("hash") != digest or not known.get("runner_id"):
            token = self.auth.storage_token()  # type: ignore[attr-defined]  # once: MSAL isn't thread-safe
            fs = FileStore(lambda: token)
            fs.delete(ws, meta_id, BUNDLE_DIR)
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda item: fs.upload(ws, meta_id, f"{BUNDLE_DIR}/{item[0]}", item[1]), files.items()))
            fabric = self.fabric  # type: ignore[attr-defined]
            item = fabric.save_notebook(ws, RUNNER, json.dumps(runner_ipynb(ws, meta_id)).encode())
            runner_id = item.get("id") or (self._notebook_item(RUNNER, ws) or {}).get("id")
            if not runner_id:
                raise LookupError(f"{RUNNER} was published but Fabric didn't return its id; try again")
            known = {"hash": digest, "runner_id": runner_id, "at": _now(), "version": project.version}
            state[key] = known
            self.root.mkdir(parents=True, exist_ok=True)
            self._bundles_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return {"workspace_id": ws, "meta_lakehouse_id": meta_id, "runner_id": known["runner_id"]}

    def _check_runnable(self, project: Any, store: Any) -> None:
        """The gates a run checks first, here, so a blocked run fails at once rather than in Fabric."""
        errors = validate(project, dt.date.today())
        if errors:
            raise GateError("save", errors)
        Engine(project, store).check_deployed()

    def _run_in_fabric(self, project: Any, env: str, store: Any, names: list[str] | None,
                       backfill: str | None, cascade: bool) -> list[dict[str, Any]]:
        self._check_runnable(project, store)
        where = self._fabric_bundle(project, env, store)
        request_id = f"fd-{dt.datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
        label = ", ".join(names) if names else f"all {len(project.entities)} entities"
        job_id = self.fabric_jobs.run_item_job(where["workspace_id"], where["runner_id"], "RunNotebook",  # type: ignore[attr-defined]
                                          run_parameters(names, backfill, cascade, request_id))
        self._log_fabric_run({"kind": "Metadata run", "name": label, "env": env, "workspace_id": where["workspace_id"],
                              "workspace_name": self._env_cfg(project, env).get("workspace_name", ""),
                              "item_id": where["runner_id"], "job_id": job_id, "request_id": request_id,
                              "meta_lakehouse_id": where["meta_lakehouse_id"],
                              "url": f"{FABRIC_APP}/groups/{where['workspace_id']}/synapsenotebooks/{where['runner_id']}"
                                     "?experience=fabric-developer"})
        return [{"entity": label, "status": "submitted", "run_id": "", "env": env, "job_id": job_id,
                 "request_id": request_id, "message": f"Running in Fabric on the {env.title()} workspace's capacity"}]

    def meta_fabric_result(self, request_id: str) -> dict[str, Any]:
        """A Fabric run's status, and its per-entity results once the runner has saved them."""
        run = next((r for r in self.fabric_runs(200) if r.get("request_id") == request_id), None)
        if run is None:
            raise KeyError(f"no Fabric run {request_id}")
        out: dict[str, Any] = {"status": run["status"], "message": run.get("message", ""), "results": None}
        if run["status"] in TERMINAL:
            data = FileStore(lambda: self.auth.storage_token()).read_bytes(  # type: ignore[attr-defined]
                run["workspace_id"], run["meta_lakehouse_id"], f"{RESULTS_DIR}/{request_id}.json")
            if data:
                out["results"] = json.loads(data)["results"]
        return out

    def meta_schedules(self, env: str) -> dict[str, Any]:
        project = self._project()  # type: ignore[attr-defined]
        ws = self._env_cfg(project, env).get("workspace_id", "")
        if self._compute(project, env) != "fabric" or not ws:
            return {"env": env, "compute": "local", "published": False, "schedules": []}
        runner = self._notebook_item(RUNNER, ws)
        schedules = self.fabric.list_schedules(ws, runner["id"], "RunNotebook") if runner else []  # type: ignore[attr-defined]
        return {"env": env, "compute": "fabric", "published": runner is not None,
                "schedules": [s | {"text": describe_schedule(s)} for s in schedules]}

    def meta_add_schedule(self, env: str, schedule: dict[str, Any]) -> dict[str, Any]:
        """Schedule "run every entity" in Fabric, from the project as it is now."""
        project = self._project()  # type: ignore[attr-defined]
        if self._compute(project, env) != "fabric":
            raise ValueError(f"Set {env.title()} to run in Fabric first; scheduled runs need Fabric compute")
        config = schedule_configuration(schedule.get("kind", "daily"), schedule.get("times"),
                                        schedule.get("every_minutes"), schedule.get("weekdays"),
                                        schedule.get("time_zone") or "UTC")
        store = self._store(project, env, create=env == "dev")  # type: ignore[attr-defined]
        self._check_runnable(project, store)
        where = self._fabric_bundle(project, env, store)
        self.fabric_jobs.create_schedule(where["workspace_id"], where["runner_id"], "RunNotebook", config)  # type: ignore[attr-defined]
        return self.meta_schedules(env)

    def meta_delete_schedule(self, env: str, schedule_id: str) -> dict[str, Any]:
        project = self._project()  # type: ignore[attr-defined]
        ws = self._env_cfg(project, env).get("workspace_id", "")
        runner = self._notebook_item(RUNNER, ws) if ws else None
        if runner is None:
            raise LookupError(f"There is no runner notebook in the {env.title()} workspace")
        self.fabric_jobs.delete_schedule(ws, runner["id"], "RunNotebook", schedule_id)  # type: ignore[attr-defined]
        return self.meta_schedules(env)

    # ---- Direct Lake semantic model over a layer

    def _model_layer(self, project: Any, layer: str | None) -> str:
        layer = layer or project.layer_names()[-1]
        if layer not in project.layer_names():
            raise ValueError(f"{layer} is not a layer")
        return layer

    def meta_model_plan(self, env: str = "test", layer: str | None = None) -> dict[str, Any]:
        project = self._project()  # type: ignore[attr-defined]
        layer = self._model_layer(project, layer)
        tables, rels = semantic.plan(project, layer)
        cfg = self._env_cfg(project, env)
        local = cfg.get("target", "onelake" if env != "dev" else "local") == "local"
        existing = None
        name = f"SM_{layer.title()}"
        if not local and cfg.get("workspace_id"):
            try:
                existing = next((m for m in self.fabric.list_semantic_models(cfg["workspace_id"])  # type: ignore[attr-defined]
                                 if m["displayName"] == name), None)
            except Exception:
                existing = None
        return {"env": env, "layer": layer, "layers": project.layer_names(), "name": name, "local": local,
                "workspace_id": cfg.get("workspace_id", ""), "workspace_name": cfg.get("workspace_name", ""),
                "lakehouse": project.layer(layer).lakehouse,
                "existing": {"id": existing["id"], "url": self._model_url(cfg["workspace_id"], existing["id"])} if existing else None,
                "tables": [{"name": t.name, "entity": t.entity, "source": f"{t.schema}.{t.table}",
                            "kind": "dimension" if t.is_dimension else "fact"} for t in tables],
                "relationships": [r.__dict__ for r in rels]}

    @staticmethod
    def _model_url(workspace_id: str, model_id: str) -> str:
        return f"{FABRIC_APP}/groups/{workspace_id}/datasets/{model_id}/details?experience=fabric-developer"

    def meta_build_model(self, env: str = "test", layer: str | None = None, name: str | None = None) -> dict[str, Any]:
        """Create or update a Direct Lake semantic model over the layer's tables in the environment's workspace."""
        project = self._project()  # type: ignore[attr-defined]
        layer = self._model_layer(project, layer)
        store = self._store(project, env)  # type: ignore[attr-defined]
        if not isinstance(store, OneLakeStore):
            raise ValueError("Direct Lake reads OneLake, and this environment keeps its tables on this PC. "
                             "Build the model from Test or Prod after promoting.")
        lakehouse = project.layer(layer).lakehouse
        if lakehouse not in store.lakehouse_ids:
            raise LookupError(f"{lakehouse} doesn't exist in the {env} workspace yet; promote and run first")
        tables, rels = semantic.plan(project, layer)
        skipped = []
        for t in tables:
            try:
                fields = table_schema(store, lakehouse, t.schema, t.table)
            except Exception:
                fields = None
            if not fields:
                skipped.append(t.entity)
            t.columns = fields or []
        tables = [t for t in tables if t.columns]
        if not tables:
            raise LookupError(f"No {layer} tables have been written in {env} yet; run them first")
        names = {t.name for t in tables}
        rels = [r for r in rels if r.from_table in names and r.to_table in names
                and r.from_column in dict(next(t for t in tables if t.name == r.from_table).columns)
                and r.to_column in dict(next(t for t in tables if t.name == r.to_table).columns)]
        fabric: FabricClient = self.fabric  # type: ignore[attr-defined]
        props = (fabric.get_lakehouse(store.workspace_id, store.lakehouse_ids[lakehouse]).get("properties") or {})
        endpoint = props.get("sqlEndpointProperties") or {}
        if not endpoint.get("connectionString") or not endpoint.get("id"):
            raise LookupError(f"{lakehouse}'s SQL analytics endpoint isn't ready yet; try again in a minute")
        refreshed = True
        try:  # so the endpoint sees tables this app wrote; the model reads through it
            fabric.refresh_sql_endpoint(store.workspace_id, endpoint["id"])
        except Exception:
            refreshed = False
        parts = semantic.build(tables, rels, endpoint["connectionString"], endpoint["id"])
        name = (name or f"SM_{layer.title()}").strip()
        model = fabric.save_semantic_model(store.workspace_id, name, parts,
                                           f"Direct Lake over {lakehouse}, built by Fabric Desktop from the metadata project")
        model_id = model.get("id") or next((m["id"] for m in fabric.list_semantic_models(store.workspace_id)
                                            if m["displayName"] == name), "")
        return {"name": name, "id": model_id, "created": model.get("created", False), "workspace_id": store.workspace_id,
                "url": self._model_url(store.workspace_id, model_id) if model_id else "",
                "tables": [t.name for t in tables], "relationships": len(rels), "skipped": skipped,
                "endpoint_refreshed": refreshed}

    # ---- profiling

    def _profile_input(self, project: Any, env: str, name: str, source: bool) -> pl.DataFrame:
        e = project.entities[name]
        store = self._store(project, env)  # type: ignore[attr-defined]
        engine = Engine(project, store)
        if source:
            df = engine._read(e, RunResult("", name, "ok"), full=True)
        else:
            df = engine.read_entity(name)
            if df is None:
                raise LookupError(f"{name} has not been loaded in {env} yet")
        return df

    def meta_profile(self, name: str, env: str = "dev", source: bool = True) -> dict[str, Any]:
        """Profile what the entity reads (source=True), before any cleaning, or the table it writes."""
        project = self._project()  # type: ignore[attr-defined]
        e = project.entities[name]
        started = time.monotonic()
        df = self._profile_input(project, env, name, source)
        # The contract keys only: a [[keys]] business key can be another table's key (customer_id on orders).
        result = profiling.profile(df, list(e.keys), profiling.existing_rules(e.spec) if source else [])
        if not source:  # cleaning applies to what is read, so only checks make sense on the output
            result["suggestions"] = [s for s in result["suggestions"] if s["kind"] != "clean"]
        return result | {"entity": name, "env": env, "source": source, "seconds": round(time.monotonic() - started, 2)}

    def meta_profile_preview(self, name: str, rules: list[dict[str, Any]], env: str = "dev",
                             limit: int = 20) -> dict[str, Any]:
        """Before and after for the chosen cleaning rules: the rows they change, side by side."""
        project = self._project()  # type: ignore[attr-defined]
        e = project.entities[name]
        df = self._profile_input(project, env, name, True)
        cols = sorted({r["column"] for r in rules if r.get("column") in df.columns})
        after, changed = apply_clean(df, rules, list(e.keys))
        if not cols or after.height != df.height:
            return {"columns": cols, "rows": [], "changed": changed}
        diff = pl.lit(False)
        for c in cols:
            diff = diff | ~df[c].cast(pl.String).eq_missing(after[c].cast(pl.String))
        before = df.select(cols).with_row_index("__r")
        after_ = after.select([pl.col(c).alias(f"{c} (after)") for c in cols])
        both = pl.concat([before, after_], how="horizontal").filter(df.select(diff.alias("d"))["d"])
        return {"columns": cols, "changed": changed,
                "rows": json.loads(both.drop("__r").head(limit).cast({c: pl.String for c in both.columns if c != "__r"}).write_json())}

    def meta_add_rules(self, name: str, blocks: list[str]) -> dict[str, Any]:
        """Append the accepted [[clean]] and [[check]] blocks to the entity file, through the save gate."""
        project = self._project()  # type: ignore[attr-defined]
        text = project.entities[name].path.read_text(encoding="utf-8")
        return self.meta_save_entity(name, profiling.append_blocks(text, blocks))  # type: ignore[attr-defined]


def table_schema(store: Any, lakehouse: str, schema: str, table: str) -> list[tuple[str, str]]:
    """(column, Delta type) for a table in the store."""
    from deltalake import DeltaTable
    fields = DeltaTable(store.uri(lakehouse, schema, table), storage_options=store.options()).schema().fields
    return [(f.name, f.type.type if isinstance(f.type.type, str) else str(f.type)) for f in fields]
