"""VL_Sources: the source systems' connection settings as a Fabric variable library.

One variable per connection field that any environment fills in, named ``<CODE>_<field>``
(``CRM_server_host``), plus ``ENVIRONMENT`` and the Keys workspace. Dev's values are the
defaults; Test and Prod are value sets that override what differs. Each environment's
workspace gets its own copy with its own value set active, so a notebook reads

    notebookutils.variableLibrary.getLibrary("VL_Sources").CRM_server_host

and gets that environment's server, in Fabric and on this PC alike. Secrets never go in:
only the Key Vault secret's name, as in the source systems file.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .keys import CONNECTION_FIELDS, ENVIRONMENTS

if TYPE_CHECKING:
    from .project import Project

LIBRARY = "VL_Sources"
DEFAULT_SET = "Default value set"
VALUE_SETS = {"test": "Test", "prod": "Prod"}  # dev uses the default values
_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/variableLibrary/definition"


def values(project: Project) -> list[dict[str, Any]]:
    """Every variable with its value in each environment: [{name, note, dev, test, prod}]."""
    rows: list[dict[str, Any]] = [{
        "name": "ENVIRONMENT", "note": "Which environment this workspace is",
        **{env: env for env in ENVIRONMENTS}}]
    envs = project.environments
    for key, label in (("keys_workspace_id", "KEYS_WORKSPACE_ID"), ("keys_workspace_name", "KEYS_WORKSPACE_NAME")):
        if any(envs.get(env, {}).get(key) for env in ENVIRONMENTS):
            rows.append({"name": label, "note": "The workspace that holds LH_Keys",
                         **{env: str(envs.get(env, {}).get(key, "")) for env in ENVIRONMENTS}})
    for s in sorted(project.source_systems, key=lambda s: s.id):
        for f in CONNECTION_FIELDS:
            per_env = {env: str(s.connections.get(env, {}).get(f, "") or "") for env in ENVIRONMENTS}
            if any(per_env.values()):
                rows.append({"name": f"{s.code}_{f}", "note": f"{s.name or s.code}: {f.replace('_', ' ')}", **per_env})
    return rows


def definition(project: Project) -> dict[str, str]:
    """The library's definition files, as Fabric's REST API takes them."""
    rows = values(project)
    parts = {"variables.json": json.dumps({
        "$schema": f"{_SCHEMA}/variables/1.0.0/schema.json",
        "variables": [{"name": r["name"], "note": r["note"], "type": "String", "value": r["dev"]} for r in rows]},
        indent=2)}
    for env, set_name in VALUE_SETS.items():
        parts[f"valueSets/{set_name}.json"] = json.dumps({
            "$schema": f"{_SCHEMA}/valueSet/1.0.0/schema.json", "name": set_name,
            "variableOverrides": [{"name": r["name"], "value": r[env]} for r in rows if r[env] != r["dev"]]}, indent=2)
    parts["settings.json"] = json.dumps({"$schema": f"{_SCHEMA}/settings/1.0.0/schema.json",
                                         "valueSetsOrder": list(VALUE_SETS.values())}, indent=2)
    return parts


def active_set(env: str) -> str:
    return VALUE_SETS.get(env, DEFAULT_SET)


def resolve(project: Project, env: str) -> dict[str, str]:
    """The values a notebook in ``env`` reads: what the active value set gives each variable."""
    env = env if env in ENVIRONMENTS else "dev"
    return {r["name"]: r[env] for r in values(project)}
