"""Who can read what: workspace roles across environments, and OneLake data access roles.

Workspace Admins, Members and Contributors can read every table in the workspace. Viewers
read a lakehouse's tables only through its OneLake data access roles (when OneLake security
is on) or the SQL endpoint's permissions. This module turns Fabric's role lists into one
review: a person per row, an environment per column, with the things worth a second look.
"""

from __future__ import annotations

import re
from typing import Any

ROLES = ("Admin", "Member", "Contributor", "Viewer")
WRITE_ROLES = ("Admin", "Member", "Contributor")
_ROLE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,127}$")


def principal(p: dict[str, Any]) -> dict[str, Any]:
    details = p.get("userDetails") or {}
    upn = details.get("userPrincipalName") or ""
    kind = p.get("type") or ""
    return {"id": p.get("id"), "name": p.get("displayName") or upn or p.get("id"), "type": kind,
            "email": upn, "guest": "#EXT#" in upn.upper()}


def review(workspaces: list[dict[str, Any]]) -> dict[str, Any]:
    """``workspaces``: [{key, label, workspace_id, workspace_name, assignments | error}].

    Returns the people (one row each, with their role in every workspace) and the flags."""
    people: dict[str, dict[str, Any]] = {}
    for ws in workspaces:
        for a in ws.get("assignments") or []:
            p = principal(a.get("principal") or {})
            row = people.setdefault(p["id"], p | {"roles": {}, "flags": []})
            row["roles"][ws["key"]] = {"role": a.get("role"), "assignment_id": a.get("id")}
    prod_keys = [w["key"] for w in workspaces if w.get("env") == "prod"]
    dev_keys = [w["key"] for w in workspaces if w.get("env") == "dev"]
    for row in people.values():
        if row["guest"]:
            row["flags"].append("Guest account from another organization")
        for k in prod_keys:
            role = row["roles"].get(k, {}).get("role")
            if role in WRITE_ROLES and row["type"] == "User":
                row["flags"].append(f"Can change Prod directly ({role}); a group is easier to review")
            if role in WRITE_ROLES and row["type"] == "User" and dev_keys and not any(row["roles"].get(d) for d in dev_keys):
                row["flags"].append("Can change Prod but has no access to Dev")
        admins = [k for k, v in row["roles"].items() if v.get("role") == "Admin"]
        if row["type"] == "ServicePrincipal" and admins:
            row["flags"].append("An app with Admin rights")
    rows = sorted(people.values(), key=lambda r: (not r["flags"], r["type"] != "User", str(r["name"]).lower()))
    return {"workspaces": [{k: w.get(k) for k in ("key", "label", "env", "workspace_id", "workspace_name", "error")}
                           for w in workspaces],
            "people": rows, "flagged": sum(bool(r["flags"]) for r in rows)}


# ---- OneLake data access roles

def _paths(role: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for rule in role.get("decisionRules") or []:
        if rule.get("effect", "Permit") != "Permit":
            continue
        for perm in rule.get("permission") or []:
            if perm.get("attributeName") == "Path":
                out += list(perm.get("attributeValueIncludedIn") or [])
    return out


def describe_role(role: dict[str, Any]) -> dict[str, Any]:
    members = role.get("members") or {}
    return {"name": role.get("name"), "paths": _paths(role),
            "entra": [m.get("objectId") for m in members.get("microsoftEntraMembers") or []],
            "item_members": [", ".join(m.get("itemAccess") or []) for m in members.get("fabricItemMembers") or []]}


def make_role(name: str, paths: list[str], object_ids: list[str], tenant_id: str,
              keep: dict[str, Any] | None = None) -> dict[str, Any]:
    """A read role on ``paths`` ("*", "/Tables/dbo", "/Tables/dbo/orders", "/Files/raw") for these people.

    ``keep`` is the role being edited: its item members (such as DefaultReader's "everyone
    with ReadAll") are kept as they are."""
    if not _ROLE_NAME.match(name or ""):
        raise ValueError("A role name is letters and digits only, starting with a letter (like SalesReaders).")
    paths = [p if p == "*" else "/" + p.strip("/") for p in paths if p]
    if not paths:
        raise ValueError("Pick at least one table, schema or folder the role can read.")
    members: dict[str, Any] = {}
    if object_ids:
        members["microsoftEntraMembers"] = [{"tenantId": tenant_id, "objectId": o} for o in dict.fromkeys(object_ids)]
    if keep and (keep.get("members") or {}).get("fabricItemMembers"):
        members["fabricItemMembers"] = keep["members"]["fabricItemMembers"]
    if not members:
        raise ValueError("Add at least one person or group to the role.")
    return {"name": name, "decisionRules": [{"effect": "Permit", "permission": [
        {"attributeName": "Path", "attributeValueIncludedIn": paths},
        {"attributeName": "Action", "attributeValueIncludedIn": ["Read"]}]}], "members": members}


def covers(path: str, table: str) -> bool:
    """Does a role path reach this table ("/Tables/dbo/orders")?"""
    if path == "*":
        return True
    path = path.rstrip("/")
    return table == path or table.startswith(path + "/")


def who_can_read(table: str, assignments: list[dict[str, Any]], roles: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Everyone who can read one table of a lakehouse, and why."""
    out: dict[str, dict[str, Any]] = {}
    for a in assignments:
        p = principal(a.get("principal") or {})
        if a.get("role") in WRITE_ROLES:
            out[p["id"]] = p | {"how": f"Workspace {a.get('role')}"}
    names = {principal(a.get("principal") or {})["id"]: principal(a.get("principal") or {}) for a in assignments}
    for role in roles or []:
        d = describe_role(role)
        if not any(covers(p, table) for p in d["paths"]):
            continue
        for oid in d["entra"]:
            if oid not in out:
                out[oid] = (names.get(oid) or {"id": oid, "name": oid, "type": "", "email": "", "guest": False}) \
                    | {"how": f"Data access role {d['name']}"}
        for desc in d["item_members"]:
            key = f"item:{d['name']}:{desc}"
            out[key] = {"id": key, "name": f"Everyone with {desc or 'item'} permission", "type": "Item permission",
                        "email": "", "guest": False, "how": f"Data access role {d['name']}"}
    return sorted(out.values(), key=lambda r: (r["how"], str(r["name"]).lower()))
