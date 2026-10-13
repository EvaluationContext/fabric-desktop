import json
import os
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pytest
from deltalake import DeltaTable, write_deltalake

from fabric_desktop import delta_profile
from fabric_desktop.jobs import Job, run_local
from fabric_desktop.onelake import TableRef, check_name
from fabric_desktop.publish import stage_locally
from fabric_desktop.server import handle

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def test_duckdb_job_runs_locally():
    data = run_local(Job.load(EXAMPLES / "sales_by_region.toml"))
    assert data.column("region").to_pylist() == ["East", "North", "West"]
    assert data.column("total_amount").to_pylist() == [960, 400, 1350]


def test_polars_job_runs_locally():
    data = run_local(Job.load(EXAMPLES / "top_products.toml"))
    assert data.column("product").to_pylist()[0] == "Bikes"
    assert data.num_rows == 3


def test_staged_table_matches_fabric_profile(tmp_path):
    data = run_local(Job.load(EXAMPLES / "sales_by_region.toml"))
    table = stage_locally(data, tmp_path / "stage")
    assert table.protocol().min_reader_version == 1
    assert table.to_pyarrow_table().num_rows == 3


def test_profile_rejects_v2_checkpoints(tmp_path):
    write_deltalake(str(tmp_path), pa.table({"a": [1]}), configuration={"delta.checkpointPolicy": "v2"})
    with pytest.raises(delta_profile.ProfileViolation):
        delta_profile.check_table(DeltaTable(str(tmp_path)))


def test_table_uri_uses_schema_folder():
    ref = TableRef("ws-guid", "lh-guid", "sales", "orders")
    assert ref.uri == "abfss://ws-guid@onelake.dfs.fabric.microsoft.com/lh-guid/Tables/sales/orders"


@pytest.mark.parametrize("name", ["bad#name", "a:b", "", " padded", "q?"])
def test_bad_names_rejected(name):
    with pytest.raises(ValueError):
        check_name("table", name)


def test_job_validation(tmp_path):
    job = tmp_path / "job.toml"
    job.write_text('name="x"\nengine="spark"\nsql="select 1"\n[target]\nworkspace_id="w"\nlakehouse="l"\ntable="t"\n')
    with pytest.raises(ValueError, match="engine"):
        Job.load(job)


def test_server_reports_errors_without_crashing():
    class Fake:
        def history(self, limit=50):
            return [{"job": "x"}]

        def list_workspaces(self):
            raise RuntimeError("boom")

    assert handle(Fake(), json.dumps({"id": 1, "method": "history"})) == {"id": 1, "result": [{"job": "x"}]}
    assert "boom" in handle(Fake(), json.dumps({"id": 2, "method": "list_workspaces"}))["error"]
    assert "unknown" in handle(Fake(), json.dumps({"id": 3, "method": "rm_rf"}))["error"]



@pytest.mark.skipif(os.name == "nt", reason="closes fd 2 in the child, which needs POSIX")
def test_server_starts_without_stderr(tmp_path):
    """The windowed exe can start Python with no stderr; the server must still answer."""
    out = subprocess.run([sys.executable, "-m", "fabric_desktop.server"], input='{"id": 1, "method": "environment"}\n',
                         stdout=subprocess.PIPE, text=True, timeout=60, env=os.environ | {"LOCALAPPDATA": str(tmp_path)},
                         preexec_fn=lambda: os.close(2))
    assert out.returncode == 0
    assert json.loads(out.stdout)["id"] == 1


def test_signed_in_tenant_comes_from_the_msal_account():
    from fabric_desktop.auth import Authenticator

    class App:
        def __init__(self, accounts):
            self.accounts = accounts

        def get_accounts(self):
            return self.accounts

    auth = Authenticator.__new__(Authenticator)
    auth.app = App([{"username": "chris@contoso.com", "home_account_id": "oid-1.72f988bf-86f1-41af-91ab-2d7cd011db47"}])
    assert auth.signed_in_tenant() == "72f988bf-86f1-41af-91ab-2d7cd011db47"
    auth.app = App([])
    assert auth.signed_in_tenant() is None


def test_catalog_lists_lakehouses_across_workspaces_and_keeps_favorites(tmp_path, monkeypatch):
    from fabric_desktop import service as service_mod
    from fabric_desktop.service import Service

    class Auth:
        def fabric_token(self):
            return "tok"

        def silent_token(self, scopes):
            return None

    class Client:
        def __init__(self, token):
            pass

        def list_workspaces(self):
            return [{"id": "w1", "displayName": "Sales"}, {"id": "w2", "displayName": "Paused"}]

        def list_lakehouses(self, ws):
            if ws == "w2":
                raise RuntimeError("capacity is paused")
            return [{"id": "lh1", "displayName": "LH_Gold"}]

    monkeypatch.setattr(service_mod, "FabricClient", Client)
    svc = Service(client_id="x", root=tmp_path)
    svc._auth = Auth()
    assert svc.set_favorite("lh1", True) == ["lh1"]
    catalog = svc.catalog()
    assert catalog["items"] == [{"workspace_id": "w1", "workspace_name": "Sales", "lakehouse_id": "lh1", "name": "LH_Gold"}]
    assert catalog["skipped"] == ["Paused"]
    assert catalog["favorites"] == ["lh1"]
    assert svc.set_favorite("lh1", False) == []
    end = svc.endorsements(["w1"])
    assert end["available"] is False and end["pending"] is False and "admin" in end["reason"]
