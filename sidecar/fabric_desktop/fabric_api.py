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
