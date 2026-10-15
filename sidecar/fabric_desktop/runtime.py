"""What the headless agent runs: the metadata engine and its OneLake stores, with no desktop UI.

``Runtime`` is built from the same mixins as the desktop ``Service`` (projects, environments,
stores, Fabric maintenance after a run, alerts), so a table runs exactly as it does from the
app. It leaves out the notebook kernel, the UI's settings file as the source of truth and the
interactive sign-in: the project and the identity come from ``agent.toml``.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

from .agent_config import AgentConfig
from .auth import EXECUTE_SCOPES
from .cloud_service import CloudMixin
from .credentials import Credential, from_config
from .dataops_service import DataOpsMixin
from .fabric_api import FabricClient
from .leases import Backend, Leases, LocalBackend, OneLakeBackend, table_key
from .meta import Engine, LocalStore, OneLakeStore, Project
from .meta.store import META_LAKEHOUSE, Store
from .meta_service import MetaMixin

# Settings that come from agent.toml and are never written to the agent's settings file.
_FROM_CONFIG = ("meta_project", "teams_webhook")


class Runtime(MetaMixin, CloudMixin, DataOpsMixin):
    def __init__(self, config: AgentConfig, credential: Credential | None = None):
        self.config = config
        self.root = config.home / "data"
        self._credential = credential

    # ---- identity and clients (the desktop Service has the same names)

    @property
    def auth(self) -> Credential:
        if self._credential is None:
            self._credential = from_config(self.config.auth)
        return self._credential

    @property
    def fabric(self) -> FabricClient:
        return FabricClient(self.auth.fabric_token)

    @property
    def fabric_jobs(self) -> FabricClient:
        return FabricClient(lambda: self.auth._token(EXECUTE_SCOPES))

    # ---- settings: the project and alert address come from agent.toml

    def _settings(self) -> dict[str, Any]:
        return super()._settings() | {"meta_project": str(self.config.project),
                                      "teams_webhook": self.config.teams_webhook}

    def _save_settings(self, **changes: Any) -> None:
        kept = {k: v for k, v in (MetaMixin._settings(self) | changes).items() if k not in _FROM_CONFIG}
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings_file.write_text(json.dumps(kept, indent=2), encoding="utf-8")

    # ---- guard rails

    def check_allowed(self, project: Project, env: str) -> None:
        if env not in self.config.environments:
            raise PermissionError(f"This agent may run {', '.join(self.config.environments)}, not {env} "
                                  f"(agent.environments in {self.config.path})")
        if not self.config.workspaces:
            return
        cfg = project.environments.get(env, {})
        for key in ("workspace_id", "keys_workspace_id"):
            ws = cfg.get(key)
            if ws and ws not in self.config.workspaces:
                raise PermissionError(f"{env.title()}'s {key} {ws} isn't in agent.workspaces, so this agent won't write there")

    def lease_backend(self, project: Project, env: str, store: Store) -> Backend:
        if isinstance(store, OneLakeStore):
            meta_id = store.lakehouse_ids.get(META_LAKEHOUSE)
            if not meta_id:
                raise LookupError(f"{META_LAKEHOUSE} doesn't exist in the {env} workspace yet; promote to {env} first")
            return OneLakeBackend(store.workspace_id, meta_id, self.auth.storage_token)
        if isinstance(store, LocalStore):
            return LocalBackend(store.root / "_leases")
        raise TypeError(f"no lease backend for {type(store).__name__}")

    # ---- running

    def run_environment(self, env: str, entities: list[str] | None = None, backfill: str | None = None,
                        cascade: bool = False, job: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Run an environment's entities on this machine, each table under a write lease. Returns
        one result per entity, as the desktop's Run does."""
        project = self._project()
        self.check_allowed(project, env)
        # The agent never creates lakehouses: an environment is set up by promoting to it.
        store = self._store(project, env)
        uses_keys = self._uses_keys(project, entities)
        keys_store = self._keys_store(project, env) if uses_keys else None
        leases = Leases(self.lease_backend(project, env, store), owner=self.config.name,
                        ttl=self.config.table_lease_seconds, wait=self.config.table_lease_seconds)
        job = job or {}
        engine = Engine(project, store, keys_store=keys_store, source_options=self._source_options,
                        table_lock=lambda e: leases.hold(table_key(project.layer(e.layer).lakehouse, e.schema, e.table)),
                        run_context={"worker": self.config.name, "host": socket.gethostname(),
                                     "attempt": int(job.get("attempts") or 1), "job_id": job.get("id", "")})
        engine.batch_prefix = "agent-"
        if cascade and entities and len(entities) == 1:
            results = engine.backfill(entities[0])
        else:
            results = engine.run_many(entities or None, backfill=backfill or None)
        out = [r.to_dict() for r in results]
        try:
            self._after_run(project, env, store, out)
        except Exception as exc:  # maintenance is a follow-up; the tables are written
            for r in out:
                r.setdefault("maintenance_message", f"{type(exc).__name__}: {exc}")
        return out

    def check(self) -> dict[str, Any]:
        """What ``fabric-desktop agent check`` reports: the project, and a token for each audience."""
        project = self._project()
        out: dict[str, Any] = {"worker": self.config.name, "home": str(self.config.home), "pid": os.getpid(),
                               "project": str(project.root), "metadata_version": project.version,
                               "entities": len(project.entities), "environments": {}}
        for env in self.config.environments:
            cfg = project.environments.get(env, {})
            out["environments"][env] = {"target": cfg.get("target", "onelake" if env != "dev" else "local"),
                                        "workspace_id": cfg.get("workspace_id", "")}
        needs_cloud = any(v["target"] != "local" for v in out["environments"].values())
        if needs_cloud:
            out["identity"] = self.auth.signed_in_user()
            out["fabric_token"] = bool(self.auth.fabric_token())
            out["storage_token"] = bool(self.auth.storage_token())
        return out
