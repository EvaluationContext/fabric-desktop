"""Thin client for the Fabric REST API (workspaces, lakehouses and notebooks)."""

from __future__ import annotations

import base64
import time
from typing import Any, Callable

import requests

API = "https://api.fabric.microsoft.com/v1"
POWERBI_ADMIN = "https://api.powerbi.com/v1.0/myorg/admin"


class FabricError(RuntimeError):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def _error(method: str, url: str, resp: requests.Response) -> FabricError:
    try:
        code = resp.json().get("errorCode")
    except ValueError:
        code = None
    if code == "CapacityNotActive":
        return FabricError("This workspace's Fabric capacity is paused or inactive. Resume the capacity, "
                           "or use a workspace on an active capacity.", code)
    return FabricError(f"{method} {url} failed with {resp.status_code}: {resp.text}", code)


class FabricClient:
    def __init__(self, token_provider: Callable[[], str], session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep, poll_timeout: float = 300):
        self._token = token_provider
        self._http = session or requests.Session()
        self._sleep = sleep
        self._poll_timeout = poll_timeout

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        if not url.startswith("http"):
            url = f"{API}{url}"
        headers = {"Authorization": f"Bearer {self._token()}"}
        for _ in range(5):
            resp = self._http.request(method, url, headers=headers, timeout=60, **kwargs)
            if resp.status_code != 429:
                break
            self._sleep(float(resp.headers.get("Retry-After", 5)))
        if resp.status_code >= 400:
            raise _error(method, url, resp)
        return resp

    def _paged(self, url: str, key: str = "value") -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while url:
            body = self._request("GET", url).json()
            items.extend(body.get(key, []))
            url = body.get("continuationUri")
        return items

    def _wait(self, resp: requests.Response) -> dict[str, Any]:
        """Follow a 202 long-running operation to its result."""
        if resp.status_code != 202:
            return resp.json() if resp.content else {}
        location = resp.headers["Location"]
        deadline = time.monotonic() + self._poll_timeout
        while True:
            self._sleep(float(resp.headers.get("Retry-After", 2)))
            resp = self._request("GET", location)
            status = resp.json().get("status")
            if status == "Succeeded":
                result = self._request("GET", f"{location.rstrip('/')}/result")
                return result.json() if result.content else {}
            if status in ("Failed", "Undefined"):
                raise FabricError(f"Operation failed: {resp.text}")
            if time.monotonic() > deadline:
                raise FabricError(f"Operation timed out: {location}")

    def list_workspaces(self) -> list[dict[str, Any]]:
        return self._paged("/workspaces")

    def list_capacities(self) -> list[dict[str, Any]]:
        """Capacities the signed-in user can assign workspaces to."""
        return self._paged("/capacities")

    def create_workspace(self, name: str, capacity_id: str = "", description: str = "") -> dict[str, Any]:
        """Create a workspace as the signed-in user, on a capacity so it can hold lakehouses."""
        body: dict[str, Any] = {"displayName": name}
        if capacity_id:
            body["capacityId"] = capacity_id
        if description:
            body["description"] = description
        try:
            return self._request("POST", "/workspaces", json=body).json()
        except FabricError as exc:
            if exc.code == "WorkspaceNameAlreadyExists":
                raise FabricError(f"A workspace named {name} already exists. Pick it from the list, "
                                  f"or use another name.", exc.code) from exc
            raise

    def list_lakehouses(self, workspace_id: str) -> list[dict[str, Any]]:
        return self._paged(f"/workspaces/{workspace_id}/lakehouses")

    def scan_endorsements(self, workspace_ids: list[str], timeout: float = 60) -> dict[str, str]:
        """Item id -> endorsement ("Certified" or "Promoted") from the admin workspace scan.

        Needs a Fabric admin token. The scan takes up to 100 workspaces per request."""
        found: dict[str, str] = {}
        for start in range(0, len(workspace_ids), 100):
            batch = workspace_ids[start:start + 100]
            scan = self._request("POST", f"{POWERBI_ADMIN}/workspaces/getInfo", json={"workspaces": batch}).json()
            deadline = time.monotonic() + timeout
            while True:
                status = self._request("GET", f"{POWERBI_ADMIN}/workspaces/scanStatus/{scan['id']}").json()
                if status.get("status") == "Succeeded":
                    break
                if status.get("status") not in ("NotStarted", "Running") or time.monotonic() > deadline:
                    raise FabricError(f"The admin scan did not finish: {status}")
                self._sleep(2)
            result = self._request("GET", f"{POWERBI_ADMIN}/workspaces/scanResult/{scan['id']}").json()
            for ws in result.get("workspaces", []):
                # Items come in one list per type (datasets, reports, Lakehouse, ...).
                for items in ws.values():
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        endorsement = isinstance(item, dict) and (item.get("endorsementDetails") or {}).get("endorsement")
                        if endorsement and endorsement != "None" and item.get("id"):
                            found[item["id"]] = endorsement
        return found

    def list_items(self, workspace_id: str) -> list[dict[str, Any]]:
        """Every item in the workspace: lakehouses, warehouses, notebooks, semantic models, ..."""
        return self._paged(f"/workspaces/{workspace_id}/items")

    def get_lakehouse(self, workspace_id: str, lakehouse_id: str) -> dict[str, Any]:
        return self._request("GET", f"/workspaces/{workspace_id}/lakehouses/{lakehouse_id}").json()

    def create_lakehouse(self, workspace_id: str, name: str, description: str = "") -> dict[str, Any]:
        """Create a schema-enabled lakehouse."""
        body = {"displayName": name, "creationPayload": {"enableSchemas": True}}
        if description:
            body["description"] = description
        return self._wait(self._request("POST", f"/workspaces/{workspace_id}/lakehouses", json=body))

    def find_lakehouse(self, workspace_id: str, name: str) -> dict[str, Any] | None:
        return next((lh for lh in self.list_lakehouses(workspace_id) if lh["displayName"] == name), None)

    def list_notebooks(self, workspace_id: str) -> list[dict[str, Any]]:
        return self._paged(f"/workspaces/{workspace_id}/notebooks")

    def save_notebook(self, workspace_id: str, name: str, ipynb: bytes) -> dict[str, Any]:
        """Create the notebook item, or replace its definition if one with this name exists."""
        definition = {"format": "ipynb", "parts": [{
            "path": "notebook-content.ipynb",
            "payload": base64.b64encode(ipynb).decode(),
            "payloadType": "InlineBase64",
        }]}
        existing = next((n for n in self.list_notebooks(workspace_id) if n["displayName"] == name), None)
        if existing is None:
            body = {"displayName": name, "definition": definition}
            return self._wait(self._request("POST", f"/workspaces/{workspace_id}/notebooks", json=body))
        url = f"/workspaces/{workspace_id}/notebooks/{existing['id']}/updateDefinition"
        self._wait(self._request("POST", url, json={"definition": definition}))
        return existing

    # ---- Fabric's job scheduler: run an item on Fabric capacity, now or on a schedule

    def run_item_job(self, workspace_id: str, item_id: str, job_type: str,
                     execution_data: dict[str, Any] | None = None, item_path: str = "items") -> str:
        """Start a job instance and return its id (the last part of the Location header)."""
        body = {"executionData": execution_data} if execution_data else None
        resp = self._request("POST", f"/workspaces/{workspace_id}/{item_path}/{item_id}/jobs/instances",
                             params={"jobType": job_type}, json=body)
        location = resp.headers.get("Location", "")
        if not location:
            raise FabricError(f"Fabric accepted the {job_type} job but returned no job instance")
        return location.rstrip("/").rsplit("/", 1)[-1]

    def job_instance(self, workspace_id: str, item_id: str, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/workspaces/{workspace_id}/items/{item_id}/jobs/instances/{job_id}").json()

    def cancel_job(self, workspace_id: str, item_id: str, job_id: str) -> None:
        self._request("POST", f"/workspaces/{workspace_id}/items/{item_id}/jobs/instances/{job_id}/cancel")

    def list_schedules(self, workspace_id: str, item_id: str, job_type: str) -> list[dict[str, Any]]:
        return self._paged(f"/workspaces/{workspace_id}/items/{item_id}/jobs/{job_type}/schedules")

    def create_schedule(self, workspace_id: str, item_id: str, job_type: str,
                        configuration: dict[str, Any], enabled: bool = True) -> dict[str, Any]:
        url = f"/workspaces/{workspace_id}/items/{item_id}/jobs/{job_type}/schedules"
        return self._request("POST", url, json={"enabled": enabled, "configuration": configuration}).json()

    def update_schedule(self, workspace_id: str, item_id: str, job_type: str, schedule_id: str,
                        enabled: bool, configuration: dict[str, Any]) -> dict[str, Any]:
        url = f"/workspaces/{workspace_id}/items/{item_id}/jobs/{job_type}/schedules/{schedule_id}"
        return self._request("PATCH", url, json={"enabled": enabled, "configuration": configuration}).json()

    def list_job_instances(self, workspace_id: str, item_id: str) -> list[dict[str, Any]]:
        """The item's job runs, newest first (Fabric keeps them for a while)."""
        runs = self._paged(f"/workspaces/{workspace_id}/items/{item_id}/jobs/instances")
        return sorted(runs, key=lambda r: r.get("startTimeUtc") or "", reverse=True)

    def delete_schedule(self, workspace_id: str, item_id: str, job_type: str, schedule_id: str) -> None:
        self._request("DELETE", f"/workspaces/{workspace_id}/items/{item_id}/jobs/{job_type}/schedules/{schedule_id}")

    def run_table_maintenance(self, workspace_id: str, lakehouse_id: str, table: str, schema: str | None = None,
                              v_order: bool = True, z_order_by: list[str] | None = None,
                              vacuum_retention: str | None = "7.01:00:00") -> str:
        """OPTIMIZE (with V-Order) and VACUUM one table on Fabric capacity. Returns the job id."""
        data: dict[str, Any] = {"tableName": table, "optimizeSettings": {"vOrder": "true" if v_order else "false"}}
        if schema:
            data["schemaName"] = schema
        if z_order_by:
            data["optimizeSettings"]["zOrderBy"] = list(z_order_by)
        if vacuum_retention:
            data["vacuumSettings"] = {"retentionPeriod": vacuum_retention}
        return self.run_item_job(workspace_id, lakehouse_id, "TableMaintenance", data, item_path="lakehouses")

    # ---- semantic models and the SQL analytics endpoint

    def list_semantic_models(self, workspace_id: str) -> list[dict[str, Any]]:
        return self._paged(f"/workspaces/{workspace_id}/semanticModels")

    def save_semantic_model(self, workspace_id: str, name: str, parts: dict[str, str],
                            description: str = "") -> dict[str, Any]:
        """Create the semantic model from TMDL files, or replace the definition of the one with this name."""
        definition = {"parts": [{"path": path, "payload": base64.b64encode(text.encode()).decode(),
                                 "payloadType": "InlineBase64"} for path, text in parts.items()]}
        existing = next((m for m in self.list_semantic_models(workspace_id) if m["displayName"] == name), None)
        if existing is None:
            body: dict[str, Any] = {"displayName": name, "definition": definition}
            if description:
                body["description"] = description
            created = self._wait(self._request("POST", f"/workspaces/{workspace_id}/semanticModels", json=body))
            return created | {"created": True}
        url = f"/workspaces/{workspace_id}/semanticModels/{existing['id']}/updateDefinition"
        self._wait(self._request("POST", url, json={"definition": definition}))
        return existing | {"created": False}

    def refresh_sql_endpoint(self, workspace_id: str, sql_endpoint_id: str) -> dict[str, Any]:
        """Ask the lakehouse's SQL analytics endpoint to pick up tables written from outside Fabric."""
        url = f"/workspaces/{workspace_id}/sqlEndpoints/{sql_endpoint_id}/refreshMetadata"
        return self._wait(self._request("POST", url, json={}))

    # ---- workspaces: who has access

    def get_workspace(self, workspace_id: str) -> dict[str, Any]:
        return self._request("GET", f"/workspaces/{workspace_id}").json()

    def list_role_assignments(self, workspace_id: str) -> list[dict[str, Any]]:
        """Workspace roles: [{id, principal: {id, displayName, type, userDetails...}, role}]."""
        return self._paged(f"/workspaces/{workspace_id}/roleAssignments")

    def add_role_assignment(self, workspace_id: str, principal_id: str, principal_type: str, role: str) -> dict[str, Any]:
        body = {"principal": {"id": principal_id, "type": principal_type}, "role": role}
        return self._request("POST", f"/workspaces/{workspace_id}/roleAssignments", json=body).json()

    def update_role_assignment(self, workspace_id: str, assignment_id: str, role: str) -> dict[str, Any]:
        url = f"/workspaces/{workspace_id}/roleAssignments/{assignment_id}"
        resp = self._request("PATCH", url, json={"role": role})
        return resp.json() if resp.content else {}

    def delete_role_assignment(self, workspace_id: str, assignment_id: str) -> None:
        self._request("DELETE", f"/workspaces/{workspace_id}/roleAssignments/{assignment_id}")

    # ---- OneLake security: data access roles on a lakehouse

    def list_data_access_roles(self, workspace_id: str, item_id: str) -> list[dict[str, Any]]:
        return self._paged(f"/workspaces/{workspace_id}/items/{item_id}/dataAccessRoles")

    def put_data_access_roles(self, workspace_id: str, item_id: str, roles: list[dict[str, Any]]) -> None:
        """Replace every data access role on the item with ``roles``."""
        self._request("PUT", f"/workspaces/{workspace_id}/items/{item_id}/dataAccessRoles", json={"value": roles})

    # ---- OneLake shortcuts

    def list_shortcuts(self, workspace_id: str, item_id: str) -> list[dict[str, Any]]:
        return self._paged(f"/workspaces/{workspace_id}/items/{item_id}/shortcuts")

    def create_shortcut(self, workspace_id: str, item_id: str, path: str, name: str,
                        target: dict[str, Any]) -> dict[str, Any]:
        """``path`` is where it goes in the lakehouse ("Tables/dbo" or "Files/..."); ``target`` is
        {"oneLake": {...}}, {"adlsGen2": {...}} or {"amazonS3": {...}}."""
        body = {"path": path, "name": name, "target": target}
        return self._request("POST", f"/workspaces/{workspace_id}/items/{item_id}/shortcuts",
                             params={"shortcutConflictPolicy": "Abort"}, json=body).json()

    def delete_shortcut(self, workspace_id: str, item_id: str, path: str, name: str) -> None:
        self._request("DELETE", f"/workspaces/{workspace_id}/items/{item_id}/shortcuts/{path.strip('/')}/{name}")

    def list_connections(self) -> list[dict[str, Any]]:
        """Cloud connections the user can use (ADLS Gen2 and S3 shortcuts need one)."""
        return self._paged("/connections")

    # ---- variable libraries

    def list_variable_libraries(self, workspace_id: str) -> list[dict[str, Any]]:
        return self._paged(f"/workspaces/{workspace_id}/variableLibraries")

    def save_variable_library(self, workspace_id: str, name: str, parts: dict[str, str],
                              description: str = "") -> dict[str, Any]:
        """Create the variable library from its definition files, or replace the one with this name."""
        definition = {"parts": [{"path": path, "payload": base64.b64encode(text.encode()).decode(),
                                 "payloadType": "InlineBase64"} for path, text in parts.items()]}
        existing = next((v for v in self.list_variable_libraries(workspace_id) if v["displayName"] == name), None)
        if existing is None:
            body: dict[str, Any] = {"displayName": name, "definition": definition}
            if description:
                body["description"] = description
            created = self._wait(self._request("POST", f"/workspaces/{workspace_id}/variableLibraries", json=body))
            if not created.get("id"):
                created = next((v for v in self.list_variable_libraries(workspace_id) if v["displayName"] == name), created)
            return created | {"created": True}
        url = f"/workspaces/{workspace_id}/variableLibraries/{existing['id']}/updateDefinition"
        self._wait(self._request("POST", url, json={"definition": definition}))
        return existing | {"created": False}

    def set_active_value_set(self, workspace_id: str, library_id: str, value_set: str) -> None:
        self._request("PATCH", f"/workspaces/{workspace_id}/variableLibraries/{library_id}",
                      json={"properties": {"activeValueSetName": value_set}})
