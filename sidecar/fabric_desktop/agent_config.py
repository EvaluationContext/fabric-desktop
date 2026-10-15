"""The headless agent's settings: ``agent.toml``.

    [agent]
    name = "etl-01"                       # this worker's name in run logs; the host name if left out
    project = "/srv/fabric/sales-metadata" # the metadata project folder (a Git checkout)
    environments = ["test", "prod"]       # the environments this agent may run
    concurrency = 1                       # jobs at once
    workspaces = ["<workspace id>"]       # optional allowlist: refuse to write anywhere else

    [auth]
    type = "service_principal"            # or "managed_identity", or "user" (your cached sign-in)
    tenant_id = "<tenant id>"
    client_id = "<app id>"
    certificate_path = "/etc/fabric-desktop/agent.pem"

    [retry]
    max_attempts = 3
    backoff_seconds = 60

    [alerts]
    teams_webhook_env = "FABRIC_DESKTOP_TEAMS_WEBHOOK"

    [[schedule]]
    id = "prod-nightly"
    env = "prod"
    daily = ["02:00"]                     # or every_minutes = 15, or weekly = { days = [...], times = [...] }

Secrets never go in this file: a certificate is a path, a secret or webhook is the name of an
environment variable that holds it.
"""

from __future__ import annotations

import os
import re
import socket
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .fabric_run import schedule_configuration
from .local_schedules import local_configuration
from .meta.project import ENVIRONMENTS

# Keys whose value would be a secret itself. Give the name of an environment variable instead.
_SECRET_KEYS = {"client_secret", "password", "secret", "certificate_password", "teams_webhook", "token", "api_key"}
_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class ConfigError(ValueError):
    def __init__(self, path: Path | str, errors: list[str]):
        self.errors = errors
        super().__init__(f"{path} has {len(errors)} problem{'s' if len(errors) != 1 else ''}:\n- " + "\n- ".join(errors))


def default_home() -> Path:
    if os.environ.get("FABRIC_DESKTOP_HOME"):
        return Path(os.environ["FABRIC_DESKTOP_HOME"])
    if sys.platform == "win32":
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "FabricDesktop" / "agent"
    return Path("/var/lib/fabric-desktop")


def default_config_path() -> Path:
    if os.environ.get("FABRIC_DESKTOP_AGENT_CONFIG"):
        return Path(os.environ["FABRIC_DESKTOP_AGENT_CONFIG"])
    if sys.platform == "win32":
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "FabricDesktop" / "agent.toml"
    return Path("/etc/fabric-desktop/agent.toml")


@dataclass(frozen=True)
class Schedule:
    id: str
    env: str
    entities: list[str]
    config: dict[str, Any]  # the same shape local_schedules.next_run reads
    backfill: str = ""


@dataclass(frozen=True)
class AgentConfig:
    path: Path
    name: str
    home: Path
    project: Path
    environments: list[str]
    auth: dict[str, Any]
    concurrency: int = 1
    poll_seconds: float = 5.0
    job_lease_seconds: float = 300.0
    table_lease_seconds: float = 120.0
    max_attempts: int = 3
    backoff_seconds: float = 60.0
    max_backoff_seconds: float = 1800.0
    workspaces: list[str] = field(default_factory=list)
    teams_webhook_env: str = ""
    log_level: str = "INFO"
    schedules: list[Schedule] = field(default_factory=list)

    @property
    def teams_webhook(self) -> str:
        return os.environ.get(self.teams_webhook_env, "") if self.teams_webhook_env else ""


def _secret_keys(table: Any, where: str) -> list[str]:
    found = []
    if isinstance(table, dict):
        for k, v in table.items():
            if k.lower() in _SECRET_KEYS:
                found.append(f"{where}{k}: secrets don't go in agent.toml; put it in an environment variable "
                             f"and give its name as {k}_env")
            found += _secret_keys(v, f"{where}{k}.")
    elif isinstance(table, list):
        for i, v in enumerate(table):
            found += _secret_keys(v, f"{where}[{i}].")
    return found


def _schedule(raw: dict[str, Any], i: int, errors: list[str]) -> Schedule | None:
    where = f"schedule {raw.get('id') or i + 1}"
    sid, env = str(raw.get("id", "")), raw.get("env")
    if not _ID.fullmatch(sid):
        errors.append(f"{where}: id is letters, digits, . _ or -")
        return None
    if env not in ENVIRONMENTS:
        errors.append(f"{where}: env is one of {', '.join(ENVIRONMENTS)}")
        return None
    kinds = [k for k in ("every_minutes", "daily", "weekly") if k in raw]
    if len(kinds) != 1:
        errors.append(f"{where}: give exactly one of every_minutes, daily or weekly")
        return None
    try:
        if kinds[0] == "every_minutes":
            fabric_cfg = schedule_configuration("cron", every_minutes=raw["every_minutes"])
            schedule = {"kind": "cron", "every_minutes": raw["every_minutes"]}
        elif kinds[0] == "daily":
            fabric_cfg = schedule_configuration("daily", times=list(raw["daily"]))
            schedule = {"kind": "daily", "times": list(raw["daily"])}
        else:
            weekly = raw["weekly"] or {}
            fabric_cfg = schedule_configuration("weekly", times=list(weekly.get("times", [])),
                                                weekdays=list(weekly.get("days", [])))
            schedule = {"kind": "weekly", "times": list(weekly.get("times", [])), "weekdays": list(weekly.get("days", []))}
    except (ValueError, TypeError) as exc:
        errors.append(f"{where}: {exc}")
        return None
    del fabric_cfg  # validated; the agent keeps local time, as Task Scheduler does
    entities = [str(e) for e in raw.get("entities") or []]
    return Schedule(sid, env, entities, local_configuration(schedule), str(raw.get("backfill", "")))


def load(path: Path | str | None = None) -> AgentConfig:
    path = Path(path) if path else default_config_path()
    if not path.exists():
        raise FileNotFoundError(f"No agent config at {path}. Copy agent.example.toml there, or pass --config.")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(path, [f"not valid TOML: {exc}"]) from None
    return parse(raw, path)


def parse(raw: dict[str, Any], path: Path | str = "agent.toml") -> AgentConfig:
    errors = _secret_keys(raw, "")
    agent, auth, retry, alerts = (raw.get(k) or {} for k in ("agent", "auth", "retry", "alerts"))
    project = agent.get("project", "")
    if not project:
        errors.append("agent.project: the metadata project folder is required")
    elif not (Path(project) / "layers.toml").exists():
        errors.append(f"agent.project: {project} isn't a metadata project (no layers.toml)")
    envs = list(agent.get("environments") or [])
    if not envs:
        errors.append("agent.environments: list the environments this agent may run, e.g. [\"prod\"]")
    errors += [f"agent.environments: {e!r} isn't one of {', '.join(ENVIRONMENTS)}" for e in envs if e not in ENVIRONMENTS]
    if auth.get("type") not in ("service_principal", "managed_identity", "user"):
        errors.append('auth.type: "service_principal", "managed_identity" or "user"')
    if auth.get("type") == "service_principal":
        if not auth.get("tenant_id") or not auth.get("client_id"):
            errors.append("auth: a service principal needs tenant_id and client_id")
        if not auth.get("certificate_path") and not auth.get("client_secret_env"):
            errors.append("auth: give certificate_path (preferred) or client_secret_env")
        elif auth.get("certificate_path") and not Path(auth["certificate_path"]).exists():
            errors.append(f"auth.certificate_path: {auth['certificate_path']} doesn't exist")

    def number(table: dict[str, Any], key: str, default: float, low: float, where: str) -> float:
        value = table.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < low:
            errors.append(f"{where}.{key}: a number of at least {low:g}")
            return default
        return float(value)

    concurrency = int(number(agent, "concurrency", 1, 1, "agent"))
    schedules = [s for i, r in enumerate(raw.get("schedule") or []) if (s := _schedule(r, i, errors))]
    ids = [s.id for s in schedules]
    errors += [f"schedule {i}: the id is used twice" for i in sorted({i for i in ids if ids.count(i) > 1})]
    errors += [f"schedule {s.id}: {s.env} isn't in agent.environments" for s in schedules if s.env not in envs]
    cfg = AgentConfig(
        path=Path(path), name=str(agent.get("name") or socket.gethostname()),
        home=Path(agent["home"]) if agent.get("home") else default_home(),
        project=Path(project), environments=envs, auth=dict(auth), concurrency=concurrency,
        poll_seconds=number(agent, "poll_seconds", 5, 0.1, "agent"),
        job_lease_seconds=number(agent, "job_lease_seconds", 300, 30, "agent"),
        table_lease_seconds=number(agent, "table_lease_seconds", 120, 15, "agent"),
        max_attempts=int(number(retry, "max_attempts", 3, 1, "retry")),
        backoff_seconds=number(retry, "backoff_seconds", 60, 0, "retry"),
        max_backoff_seconds=number(retry, "max_backoff_seconds", 1800, 0, "retry"),
        workspaces=[str(w) for w in agent.get("workspaces") or []],
        teams_webhook_env=str(alerts.get("teams_webhook_env", "")),
        log_level=str(agent.get("log_level", "INFO")).upper(), schedules=schedules)
    if errors:
        raise ConfigError(path, errors)
    return cfg
