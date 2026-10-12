"""Thin client for the Fabric REST API (workspaces, lakehouses and notebooks)."""

from __future__ import annotations

import base64
import time
from typing import Any, Callable

import requests

API = "https://api.fabric.microsoft.com/v1"


class FabricError(RuntimeError):
    pass


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
            raise FabricError(f"{method} {url} failed with {resp.status_code}: {resp.text}")
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
