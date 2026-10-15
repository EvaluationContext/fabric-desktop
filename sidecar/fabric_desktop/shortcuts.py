"""OneLake shortcuts: tables and folders in a lakehouse that point at data somewhere else.

A shortcut sits at ``Tables/<schema>`` (a table) or under ``Files`` (a folder) and points at
another lakehouse in OneLake, ADLS Gen2, Amazon S3 or another store. Fabric reads through it
as if the data were in the lakehouse, so nothing is copied.
"""

from __future__ import annotations

import re
from typing import Any

TARGETS = {"oneLake": "OneLake", "adlsGen2": "ADLS Gen2", "amazonS3": "Amazon S3",
           "googleCloudStorage": "Google Cloud Storage", "s3Compatible": "S3 compatible",
           "azureBlobStorage": "Azure Blob Storage", "dataverse": "Dataverse"}
_NAME = re.compile(r"^[^\\/:*?\"<>|]{1,256}$")


def describe(s: dict[str, Any]) -> dict[str, Any]:
    """One shortcut as the app shows it: where it is, what kind of target, and where that is."""
    target = s.get("target") or {}
    key = next((k for k in TARGETS if k in target), None)
    t = target.get(key) or {} if key else {}
    if key == "oneLake":
        where = t.get("path", "")
        detail = {"workspace_id": t.get("workspaceId"), "item_id": t.get("itemId"), "target_path": where}
    else:
        where = "/".join(x.strip("/") for x in (t.get("location", ""), t.get("subpath", "")) if x)
        detail = {"location": t.get("location"), "subpath": t.get("subpath"), "connection_id": t.get("connectionId")}
    path = (s.get("path") or "").strip("/")
    return {"name": s.get("name"), "path": path, "full_path": f"{path}/{s.get('name')}",
            "kind": "table" if path.startswith("Tables") else "folder",
            "target_type": TARGETS.get(key or "", target.get("type") or "Other"), "target": where, **detail}


def location(where: str) -> str:
    """Where the shortcut goes: "Tables/<schema>" or a folder under Files."""
    where = (where or "").strip().strip("/")
    if where in ("Tables", ""):
        return "Tables/dbo"
    if not where.startswith(("Tables/", "Files")):
        raise ValueError("A shortcut goes under Tables/<schema> (a table) or Files (a folder).")
    if where.startswith("Tables/") and where.count("/") != 1:
        raise ValueError("A table shortcut goes directly in a schema, like Tables/dbo.")
    return where


def target(source: dict[str, Any]) -> dict[str, Any]:
    """The REST API's target from the dialog: OneLake (workspace, item, path) or cloud storage."""
    kind = source.get("type")
    if kind == "oneLake":
        path = (source.get("path") or "").strip("/")
        if not (source.get("workspace_id") and source.get("item_id") and path):
            raise ValueError("Pick the lakehouse and the table or folder the shortcut points at.")
        return {"oneLake": {"workspaceId": source["workspace_id"], "itemId": source["item_id"], "path": path}}
    if kind in ("adlsGen2", "amazonS3"):
        loc = (source.get("location") or "").strip().rstrip("/")
        if not loc.startswith("https://"):
            raise ValueError("The location is the storage account or bucket URL, starting with https://")
        if not source.get("connection_id"):
            raise ValueError("Pick the cloud connection Fabric uses to reach that storage.")
        return {kind: {"location": loc, "subpath": "/" + (source.get("subpath") or "").strip("/"),
                       "connectionId": source["connection_id"]}}
    raise ValueError("A shortcut points at OneLake, ADLS Gen2 or Amazon S3.")


def check_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME.match(name):
        raise ValueError("Give the shortcut a name without \\ / : * ? \" < > |")
    return name


def used_by(shortcut: dict[str, Any], entities: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Managed tables that read files through this shortcut, or that a table shortcut would clash with.

    ``entities``: [{name, files, schema, table}] for the lakehouse the shortcut is in."""
    out = []
    for e in entities:
        if shortcut["kind"] == "folder" and e.get("files"):
            if f"Files/{e['files'].strip('/')}".startswith(shortcut["full_path"] + "/"):
                out.append({"entity": e["name"], "how": "reads its files"})
        elif shortcut["kind"] == "table" and shortcut["path"] == f"Tables/{e['schema']}" and shortcut["name"] == e["table"]:
            out.append({"entity": e["name"], "how": "has the same name, so its runs write into the shortcut's target"})
    return out


def connection_rows(connections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cloud connections a storage shortcut can use."""
    wanted = {"AzureDataLakeStorage": "adlsGen2", "AmazonS3": "amazonS3"}
    out = []
    for c in connections:
        details = c.get("connectionDetails") or {}
        kind = wanted.get(details.get("type", ""))
        if kind:
            out.append({"id": c.get("id"), "name": c.get("displayName") or c.get("id"), "type": kind,
                        "path": details.get("path", "")})
    return sorted(out, key=lambda c: str(c["name"]).lower())
