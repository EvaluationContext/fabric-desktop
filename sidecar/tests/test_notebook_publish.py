"""Publish to OneLake, Run in Fabric and schedules, called the way the app calls them."""

from __future__ import annotations

import json

import pytest

from fabric_desktop.server import handle
from fabric_desktop.service import Service

LAKEHOUSE = {"workspace_id": "ws-lh", "workspace_name": "Data", "lakehouse_id": "lh1", "name": "LH_Bronze"}
CELLS = [{"type": "code", "source": "print(1)", "meta": {}}]


class FakeFabric:
    def __init__(self):
        self.saved = []
        self.items = {}
        self.schedules = []

    def save_notebook(self, ws, name, ipynb):
        self.saved.append((ws, name, json.loads(ipynb)))
        self.items[(ws, name)] = {"id": f"nb-{ws}", "displayName": name}
        return self.items[(ws, name)]

    def list_notebooks(self, ws):
        return [v for (w, _), v in self.items.items() if w == ws]

    def get_lakehouse(self, ws, lh):
        return {"properties": {"defaultSchema": "dbo"}}

    def run_item_job(self, ws, item, job):
        return "job1"

    def create_schedule(self, ws, item, job, config):
        self.schedules.append((ws, item))

    def list_schedules(self, ws, item, job):
        return []


class NotebookService(Service):
    fake = None

    @property
    def fabric(self):
        return self.fake

    @property
    def fabric_jobs(self):
        return self.fake


@pytest.fixture
def svc(tmp_path):
    s = NotebookService(root=tmp_path)
    s.fake = FakeFabric()
    return s


def call(svc, method, **params):
    out = handle(svc, json.dumps({"id": 1, "method": method, "params": params}))
    assert "error" not in out, out["error"]
    return out["result"]


def payload(name="NB", lakehouse=LAKEHOUSE, compute="default", workspace=None):
    # Exactly what the notebook screen sends (notebookPayload in app.js).
    return {"name": name, "cells": CELLS, "lakehouse_ref": lakehouse, "compute": compute, "workspace": workspace}


def test_publish_takes_the_app_payload_and_defaults_to_the_lakehouse_workspace(svc):
    out = call(svc, "publish_notebook", **payload())
    assert out["workspace_id"] == "ws-lh" and out["workspace_name"] == "Data"
    assert svc.fake.saved[-1][0] == "ws-lh"
    # The workspace it went to is remembered with the notebook.
    assert svc.notebooks.load("NB")["workspace"] == {"workspace_id": "ws-lh", "workspace_name": "Data"}


def test_picked_workspace_is_saved_and_reused(svc):
    picked = {"workspace_id": "ws-pub", "workspace_name": "Publishing"}
    call(svc, "publish_notebook", **payload(workspace=picked, compute="fabric"))
    assert svc.fake.saved[-1][0] == "ws-pub"
    saved = svc.notebooks.load("NB")
    assert saved["workspace"] == picked and saved["compute"] == "fabric"
    # Later saves and publishes without a workspace keep using it.
    call(svc, "save_notebook", **payload(workspace=None))
    out = call(svc, "publish_notebook", **payload(workspace=None))
    assert out["workspace_id"] == "ws-pub" and svc.fake.saved[-1][0] == "ws-pub"
    # The default lakehouse still points at its own workspace.
    deps = svc.fake.saved[-1][2]["metadata"]["dependencies"]["lakehouse"]
    assert deps["default_lakehouse_workspace_id"] == "ws-lh"
    run = call(svc, "run_notebook_in_fabric", **payload())
    assert run["workspace_id"] == "ws-pub" and run["workspace_name"] == "Publishing"
    sched = call(svc, "add_notebook_schedule", **payload(), schedule={"kind": "daily", "times": ["06:00"]})
    assert sched["workspace_id"] == "ws-pub" and svc.fake.schedules == [("ws-pub", "nb-ws-pub")]
    assert call(svc, "notebook_schedules", name="NB", lakehouse_ref=LAKEHOUSE)["published"] is True


def test_publish_without_a_lakehouse_needs_a_workspace(svc):
    out = handle(svc, json.dumps({"id": 1, "method": "publish_notebook", "params": payload(lakehouse=None)}))
    assert out["error"] == "ValueError: Pick a workspace to publish NB to."
    picked = {"workspace_id": "ws-pub", "workspace_name": "Publishing"}
    assert call(svc, "publish_notebook", **payload(lakehouse=None, workspace=picked))["workspace_id"] == "ws-pub"


def test_open_notebook_returns_the_workspace(svc):
    call(svc, "save_notebook", **payload(workspace={"workspace_id": "w", "workspace_name": "W"}))
    assert call(svc, "open_notebook", name="NB")["workspace"] == {"workspace_id": "w", "workspace_name": "W"}
    call(svc, "save_notebook", **payload(workspace={}))  # forget it
    assert call(svc, "open_notebook", name="NB")["workspace"] is None
