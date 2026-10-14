import base64
import json
import subprocess
import sys
from pathlib import Path

import polars as pl
import pyarrow as pa
import pytest
from deltalake import DeltaTable, write_deltalake

from fabric_desktop import lakehouse
from fabric_desktop.notebook import Kernel, NotebookStore, from_ipynb, to_ipynb
from fabric_desktop.onelake import FileStore
from fabric_desktop.service import Service
from test_fabric_api import Resp, client

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
LH = {"workspace_id": "ws1", "lakehouse_id": "lh1", "name": "SalesLakehouse"}


def test_kernel_keeps_state_and_shows_last_expression():
    k = Kernel()
    first = k.run("x = 20\nprint('hello')")
    assert first["outputs"] == [{"type": "stream", "text": "hello\n"}]
    second = k.run("x + 1")
    assert second["execution_count"] == 2
    assert second["outputs"] == [{"type": "text", "text": "21"}]


def test_kernel_shows_data_frames_as_tables():
    out = Kernel().run("import polars as pl\npl.DataFrame({'region': ['East', 'West'], 'total': [960, 1350]})")
    assert out["outputs"] == [{"type": "table", "columns": ["region", "total"],
                               "rows": [["East", 960], ["West", 1350]], "total_rows": 2}]


def test_kernel_reports_errors_and_survives():
    k = Kernel()
    out = k.run("def f():\n    return 1 / 0\nf()")
    assert out["status"] == "error"
    err = out["outputs"][0]
    assert err["ename"] == "ZeroDivisionError"
    assert "<cell>" in err["traceback"] and "notebook.py" not in err["traceback"]
    assert k.run("raise SystemExit")["status"] == "error"
    assert k.run("input()")["status"] == "error"
    assert k.run("1")["status"] == "ok"


def test_ipynb_round_trip_keeps_cells_and_lakehouse():
    cells = [{"type": "markdown", "source": "# Sales\nby region"}, {"type": "code", "source": "x = 1\nx"}]
    nb = json.loads(json.dumps(to_ipynb(cells, LH)))
    assert nb["metadata"]["dependencies"]["lakehouse"]["default_lakehouse"] == "lh1"
    assert from_ipynb(nb) == {"cells": cells, "lakehouse": LH}


def test_notebook_store_names_and_renames(tmp_path):
    store = NotebookStore(tmp_path)
    assert store.new()["name"] == "Notebook 1"
    assert store.new()["name"] == "Notebook 2"
    store.rename("Notebook 1", "Sales by region")
    assert {n["name"] for n in store.list()} == {"Notebook 2", "Sales by region"}
    with pytest.raises(ValueError):
        store.path("bad/name")


def test_notebook_reads_local_files_and_writes_tables(tmp_path, monkeypatch):
    def local_publish(data, ref, mode, token):
        path = tmp_path / "onelake" / ref.schema / ref.table
        write_deltalake(str(path), data, mode=mode)
        return DeltaTable(str(path))

    monkeypatch.setattr(lakehouse, "publish", local_publish)
    service = Service(client_id="x", root=tmp_path)
    service._auth = type("Auth", (), {"storage_token": lambda self: "tok"})()
    code = (
        "import polars as pl\nfrom fabric_desktop import lakehouse\n"
        f"sales = lakehouse.read_csv({str(EXAMPLES / 'data' / 'sales.csv')!r})\n"
        "by_region = sales.group_by('region').agg(pl.col('amount').sum()).sort('region')\n"
        "lakehouse.write_table(by_region, 'sales_by_region')"
    )
    out = service.run_cell(code, LH)
    assert out["status"] == "ok", out
    assert out["outputs"] == [{"type": "text", "text": "Wrote 3 rows to dbo.sales_by_region (Delta version 0). New table: refresh the lakehouse in Fabric if it shows under Unidentified"}]
    assert service.history()[0]["rows"] == 3


def test_notebook_without_lakehouse_explains_how_to_attach(tmp_path):
    out = Service(client_id="x", root=tmp_path).run_cell(
        "from fabric_desktop import lakehouse\nlakehouse.read_csv('Files/raw/sales.csv')", None)
    assert "Add data items" in out["outputs"][0]["evalue"]


def test_file_store_lists_children_without_lakehouse_prefix():
    class Session:
        def get(self, url, params, headers, timeout):
            assert params["directory"] == "lh1/Files/raw"
            return Resp(200, {"paths": [
                {"name": "lh1/Files/raw/sales.csv", "contentLength": "120"},
                {"name": "lh1/Files/raw/archive", "isDirectory": "true"},
            ]})

    entries = FileStore(lambda: "tok", Session()).list("ws1", "lh1", "Files/raw")
    assert [(e["name"], e["path"], e["is_dir"]) for e in entries] == [
        ("archive", "Files/raw/archive", True), ("sales.csv", "Files/raw/sales.csv", False)]


def test_save_notebook_creates_then_updates_the_fabric_item():
    c, s = client([Resp(200, {"value": []}), Resp(201, {"id": "nb1", "displayName": "Sales"})])
    assert c.save_notebook("ws1", "Sales", b"{}")["id"] == "nb1"
    part = s.calls[1][2]["definition"]["parts"][0]
    assert s.calls[1][:2] == ("POST", "https://api.fabric.microsoft.com/v1/workspaces/ws1/notebooks")
    assert base64.b64decode(part["payload"]) == b"{}"

    c, s = client([Resp(200, {"value": [{"id": "nb1", "displayName": "Sales"}]}), Resp(200, {})])
    c.save_notebook("ws1", "Sales", b"{}")
    assert s.calls[1][1].endswith("/notebooks/nb1/updateDefinition")


def test_server_keeps_prints_out_of_the_protocol_and_speaks_utf8(tmp_path):
    request = json.dumps({"id": 1, "method": "run_cell", "params": {
        "code": "import os\nos.write(1, b'native noise')\nprint('h\u00e9llo \u2713')\n2 * 21"}}, ensure_ascii=False)
    proc = subprocess.run([sys.executable, "-m", "fabric_desktop.server"], input=request + "\n",
                          capture_output=True, encoding="utf-8", timeout=120,
                          env={"LOCALAPPDATA": str(tmp_path), **_base_env()})
    response = json.loads(proc.stdout)  # exactly one JSON line, nothing else
    assert response["result"]["outputs"] == [{"type": "stream", "text": "h\u00e9llo \u2713\n"}, {"type": "text", "text": "42"}]
    assert "native noise" in proc.stderr


def _base_env():
    import os
    return {k: v for k, v in os.environ.items() if k != "LOCALAPPDATA"}


def _attach(tmp_path, schemas):
    lakehouse.attach(lakehouse.Attached("ws1", "lh1", "Fabric_Lakehouse", lambda: "tok", tmp_path,
                                        schemas_enabled=lambda: schemas))


def test_lakehouse_without_schemas_keeps_tables_under_tables_root(tmp_path):
    _attach(tmp_path, False)
    try:
        assert lakehouse.table_ref("dbo.sales").uri.endswith("/lh1/Tables/sales")
        assert lakehouse.table_ref("sales").uri.endswith("/lh1/Tables/sales")
        with pytest.raises(ValueError, match="without schemas"):
            lakehouse.table_ref("crm.sales")
        _attach(tmp_path, True)
        assert lakehouse.table_ref("sales").uri.endswith("/lh1/Tables/dbo/sales")
    finally:
        lakehouse.attach(None)


def test_schema_layout_comes_from_the_lakehouse_default_schema(tmp_path):
    service = Service(client_id="x", root=tmp_path)
    answers = {"lh1": {"properties": {"defaultSchema": "dbo"}}, "lh2": {"properties": {}}}
    service._auth = type("A", (), {"fabric_token": lambda s: "tok"})()
    import fabric_desktop.service as service_mod

    class Client:
        def __init__(self, token):
            pass

        def get_lakehouse(self, ws, lh):
            return answers[lh]

    original = service_mod.FabricClient
    service_mod.FabricClient = Client
    try:
        assert service._schemas_enabled("ws1", "lh1") is True
        assert service._schemas_enabled("ws1", "lh2") is False
    finally:
        service_mod.FabricClient = original


def test_display_shows_timestamps_with_a_time_zone():
    import datetime as dt
    from fabric_desktop.notebook import display
    table = pa.table({"ts": pa.array([dt.datetime(2024, 1, 1)], pa.timestamp("us", tz="UTC"))})
    assert display(table)["rows"] == [["2024-01-01 00:00:00+00:00"]]
