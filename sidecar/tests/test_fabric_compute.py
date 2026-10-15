"""Metadata runs on Fabric capacity: the bundle, the runner notebook, and the app side."""

import ast
import json
import types
from pathlib import Path

import pytest

from fabric_desktop.fabric_compute import RUNNER, bundle_files, bundle_hash, run_bundle, run_parameters, runner_ipynb
from fabric_desktop.meta import LocalStore, Project
from fabric_desktop.meta.templates import init_project
from test_cloud import CloudService, FakeFabric, FolderOneLake


def write_bundle(files: dict, dest: Path) -> Path:
    for rel, data in files.items():
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return dest


def test_bundle_has_the_engine_and_the_project_but_no_local_data(tmp_path):
    init_project(tmp_path / "proj", "3", "Chris", True)
    files = bundle_files(tmp_path / "proj", {"env": "test"})
    assert "package/fabric_desktop/meta/engine.py" in files
    assert "package/fabric_desktop/fabric_compute.py" in files
    assert "project/layers.toml" in files and "project/metadata/entities/silver.orders.toml" in files
    assert not any("/.local/" in p or "__pycache__" in p for p in files)
    assert json.loads(files["config.json"]) == {"env": "test"}
    assert bundle_hash(files) == bundle_hash(dict(files))
    assert bundle_hash(files) != bundle_hash(files | {"config.json": b"{}"})


def test_runner_notebook_is_a_parameterized_python_notebook():
    nb = runner_ipynb("ws1", "lhmeta")
    assert nb["metadata"]["kernel_info"]["name"] == "jupyter"
    assert nb["metadata"]["dependencies"]["lakehouse"]["default_lakehouse"] == "lhmeta"
    code = [c for c in nb["cells"] if c["cell_type"] == "code"]
    assert code[0]["metadata"]["tags"] == ["parameters"]
    for c in code:
        ast.parse("".join(c["source"]))
    assert "/lakehouse/default/Files/_fabric_desktop/current" in "".join(code[1]["source"])
    params = run_parameters(["silver.orders"], "full", True, "fd-1")["parameters"]
    assert params["entities"] == {"value": "silver.orders", "type": "string"}
    assert params["cascade"] == {"value": True, "type": "bool"}


def test_run_bundle_runs_the_same_engine_from_the_uploaded_copy(tmp_path):
    proj = tmp_path / "proj"
    init_project(proj, "3", "Chris", True)
    bundle = write_bundle(bundle_files(proj, {"env": "dev"}), tmp_path / "bundle")
    stores = (LocalStore(proj / ".local" / "dev", "dev"), LocalStore(proj / ".local" / "keys-dev", "dev"))
    out = run_bundle(bundle, stores=stores, results_dir=tmp_path / "results", request_id="fd-1")
    assert [r["status"] for r in out["results"]] == ["ok"] * 6
    assert out["version"] == Project(proj).version  # the bundle is the same metadata version
    saved = json.loads((tmp_path / "results" / "fd-1.json").read_text())
    assert saved["results"][0]["entity"] == out["results"][0]["entity"]
    out["raise_if_failed"]()
    # One entity, by name; scheduled runs (no request id) get a dated file.
    again = run_bundle(bundle, entities="gold.fact_sales", stores=stores, results_dir=tmp_path / "results")
    assert [r["entity"] for r in again["results"]] == ["gold.fact_sales"]
    assert again["request_id"].startswith("scheduled-")


def test_failed_run_fails_the_fabric_job(tmp_path):
    proj = tmp_path / "proj"
    init_project(proj, "3", "Chris", True)
    bundle = write_bundle(bundle_files(proj, {"env": "dev"}), tmp_path / "bundle")
    empty = LocalStore(tmp_path / "nothing", "dev")  # no landing files, so bronze fails
    out = run_bundle(bundle, entities="bronze.orders", stores=(empty, empty), results_dir=tmp_path / "r")
    with pytest.raises(RuntimeError, match="bronze.orders"):
        out["raise_if_failed"]()


def test_environments_file_is_not_part_of_the_metadata_version(tmp_path):
    proj = tmp_path / "proj"
    p = init_project(proj, "3", "Chris", True)
    before = p.version
    (proj / "environments.toml").write_text('[test]\ncompute = "fabric"\n', encoding="utf-8")
    assert Project(proj).version == before


class FolderFiles:
    """OneLake's Files area kept in a folder, standing in for FileStore."""
    root: Path = None
    uploads = 0

    def __init__(self, token):
        pass

    def _p(self, ws, lh, path):
        return FolderFiles.root / ws / lh / path

    def delete(self, ws, lh, path):
        import shutil
        shutil.rmtree(self._p(ws, lh, path), ignore_errors=True)

    def upload(self, ws, lh, path, data):
        FolderFiles.uploads += 1
        p = self._p(ws, lh, path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def read_bytes(self, ws, lh, path):
        p = self._p(ws, lh, path)
        return p.read_bytes() if p.exists() else None


class ComputeFabric(FakeFabric):
    def __init__(self):
        super().__init__()
        self.notebooks, self.jobs, self.schedules = [], [], []

    def save_notebook(self, ws, name, ipynb):
        self.notebooks.append((ws, name, json.loads(ipynb)))
        return {"id": "runner1"}

    def list_notebooks(self, ws):
        return [{"id": "runner1", "displayName": RUNNER}] if self.notebooks else []

    def run_item_job(self, ws, item, job_type, execution_data=None):
        self.jobs.append((ws, item, job_type, execution_data))
        return f"job{len(self.jobs)}"

    def job_instance(self, ws, item, job):
        return {"status": "Completed"}

    def create_schedule(self, ws, item, job_type, config):
        self.schedules.append({"id": "s1", "enabled": True, "configuration": config})

    def list_schedules(self, ws, item, job_type):
        return self.schedules


@pytest.fixture
def svc(tmp_path, monkeypatch):
    s = CloudService(root=tmp_path / "app")
    s.fake = ComputeFabric()
    s._auth = types.SimpleNamespace(storage_token=lambda: "tok", silent_token=lambda scopes: "tok")
    s.meta_init(str(tmp_path / "proj"), "3")
    FolderFiles.root, FolderFiles.uploads = tmp_path / "files", 0
    monkeypatch.setattr("fabric_desktop.cloud_service.FileStore", FolderFiles)
    return s


def test_compute_needs_tables_in_onelake(svc):
    with pytest.raises(ValueError, match="keeps its tables on this PC"):
        svc.meta_set_compute("dev", "fabric")
    svc.meta_set_environment("test", "ws-test", "Sales - Test")
    assert svc.meta_set_compute("test", "fabric")["environments"]["test"]["compute"] == "fabric"
    # Pointing the environment back at this PC turns Fabric compute off with it.
    svc.meta_set_environment("dev", target="local")
    svc.meta_set_environment("test", target="local")
    assert "compute" not in svc._project().environments["test"]


def test_run_in_fabric_uploads_once_starts_the_runner_and_reads_results(svc, tmp_path, monkeypatch):
    project = svc._project()
    svc.meta_run()  # Dev on this PC, so there is something to promote
    store = FolderOneLake(tmp_path / "onelake", "dev", [l.lakehouse for l in project.layers] + ["LH_Meta", "LH_Keys"])
    monkeypatch.setattr(svc, "_store", lambda p, env, create=False: store)
    monkeypatch.setattr(svc, "_keys_store", lambda p, env, create=False: store)
    envs = {k: dict(v) for k, v in project.environments.items()}
    envs["dev"] = {"target": "onelake", "workspace_id": "ws-test", "compute": "fabric"}
    svc._write_environments(project, envs)

    out = svc.meta_run(names=["silver.orders"], env="dev")
    assert out[0]["status"] == "submitted" and out[0]["entity"] == "silver.orders"
    ws, item, job_type, data = svc.fake.jobs[0]
    assert (ws, item, job_type) == ("ws-test", "runner1", "RunNotebook")
    assert data["parameters"]["entities"]["value"] == "silver.orders"
    bundle = FolderFiles.root / "ws-test" / "id-LH_Meta" / "Files/_fabric_desktop/current"
    config = json.loads((bundle / "config.json").read_text())
    assert config["lakehouse_ids"]["LH_Gold"] == "id-LH_Gold" and config["keys_workspace_id"] == "ws-test"
    first = FolderFiles.uploads
    assert first > 20 and svc.fake.notebooks[0][1] == RUNNER

    svc.meta_run(env="dev")  # nothing changed: no second upload or publish
    assert FolderFiles.uploads == first and len(svc.fake.notebooks) == 1

    # The runner saves its results; the app reads them once the job is done.
    request_id = out[0]["request_id"]
    results = FolderFiles.root / "ws-test" / "id-LH_Meta" / "Files/_fabric_desktop/results"
    results.mkdir(parents=True)
    (results / f"{request_id}.json").write_text(json.dumps({"results": [{"entity": "silver.orders", "status": "ok"}]}))
    res = svc.meta_fabric_result(request_id)
    assert res["status"] == "Completed" and res["results"][0]["status"] == "ok"

    sched = svc.meta_add_schedule("dev", {"kind": "daily", "times": ["05:00"]})
    assert sched["compute"] == "fabric" and sched["schedules"][0]["text"].startswith("Daily at 05:00")


def test_fabric_runs_are_marked_in_the_run_log(tmp_path):
    proj = tmp_path / "proj"
    init_project(proj, "3", "Chris", True)
    bundle = write_bundle(bundle_files(proj, {"env": "dev"}), tmp_path / "bundle")
    stores = (LocalStore(proj / ".local" / "dev", "dev"), LocalStore(proj / ".local" / "keys-dev", "dev"))
    run_bundle(bundle, entities="bronze.orders", stores=stores, results_dir=tmp_path / "r")
    log = stores[0].meta("run_log")
    assert log["batch_id"][-1].startswith("fabric-")


def test_runs_from_every_environment(svc, tmp_path, monkeypatch):
    svc.meta_run()  # Dev, on this PC
    out = svc.meta_runs_all()
    assert {r["env"] for r in out["runs"]} == {"dev"} and all(r["compute"] == "local" for r in out["runs"])
    assert out["unavailable"] == {}  # Test and Prod have no workspace yet: left out quietly

    # Test in OneLake: read only when already signed in, never by asking for a sign-in.
    svc.meta_set_environment("test", "ws-test", "Sales - Test")
    project = svc._project()
    store = FolderOneLake(tmp_path / "onelake", "test", [l.lakehouse for l in project.layers] + ["LH_Meta"])
    store.meta_append("run_log", [{"run_id": "r1", "batch_id": "fabric-1", "entity": "silver.orders", "env": "test",
                                   "status": "failed", "started_at": "2099-01-01T00:00:00+00:00", "message": "check failed"}])
    orig = type(svc)._store
    monkeypatch.setattr(svc, "_store", lambda p, env, create=False: store if env == "test" else orig(svc, p, env, create))
    svc._auth = types.SimpleNamespace(storage_token=lambda: "tok", silent_token=lambda scopes: None)
    assert svc.meta_runs_all()["unavailable"] == {"test": "Sign in to see its runs"}
    svc._auth = types.SimpleNamespace(storage_token=lambda: "tok", silent_token=lambda scopes: "tok")
    out = svc.meta_runs_all()
    first = out["runs"][0]
    assert (first["env"], first["entity"], first["compute"], first["status"]) == ("test", "silver.orders", "fabric", "failed")


def test_runs_from_every_environment_without_a_project(tmp_path):
    s = CloudService(root=tmp_path / "app")
    assert s.meta_runs_all() == {"runs": [], "unavailable": {}}


def test_every_environment_keeps_a_workspace_and_can_run_on_this_pc(svc):
    class Creating:
        def create_workspace(self, name, capacity_id="", description=""):
            assert capacity_id == "cap1"
            return {"id": "ws-new", "displayName": name}
    svc.fake = Creating()
    res = svc.meta_create_workspace("dev", " Sales - Dev ", "cap1")
    dev = res["environments"]["dev"]
    assert res["created"]["displayName"] == "Sales - Dev"
    # Assigning a workspace points the environment at it; this PC still runs it.
    assert (dev["workspace_id"], dev["target"], dev.get("compute", "local")) == ("ws-new", "onelake", "local")
    # Going offline keeps the workspace assigned and turns Fabric compute off.
    svc.meta_set_environment("prod", "ws-prod", "Sales - Prod")
    svc.meta_set_compute("prod", "fabric")
    prod = svc.meta_set_environment("prod", target="local")["environments"]["prod"]
    assert (prod["workspace_id"], prod["target"]) == ("ws-prod", "local") and "compute" not in prod
    back = svc.meta_set_environment("prod", target="onelake")["environments"]["prod"]
    assert (back["workspace_id"], back["target"]) == ("ws-prod", "onelake")
    # Test has no workspace yet, so it can't point at one.
    with pytest.raises(LookupError, match="Assign a workspace to Test"):
        svc.meta_set_environment("test", target="onelake")
