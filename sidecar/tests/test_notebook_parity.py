"""Fabric notebook parity on this PC: spark, notebookutils, live output, Cancel, rich results."""

import time

import pytest
from deltalake import DeltaTable, write_deltalake

from fabric_desktop import lakehouse
from fabric_desktop.notebook import Kernel, NotebookStore
from test_notebook import EXAMPLES, LH, _sql_service

SALES = str(EXAMPLES / "data" / "sales.csv")


def _wait(k, version=-1):
    while True:
        snap = k.poll(version, wait=0.5)
        if snap["status"] != "running":
            return snap


def test_spark_reads_files_and_groups_like_pyspark():
    k = Kernel()
    out = k.run(
        "from pyspark.sql import functions as F\n"
        f"df = spark.read.csv({SALES!r}, header=True, inferSchema=True)\n"
        "df.groupBy('region').agg(F.sum('amount').alias('total')).orderBy('region')")
    assert out["status"] == "ok", out
    table = out["outputs"][-1]
    assert table["type"] == "table" and table["columns"] == ["region", "total"]
    assert table["total_rows"] == 3


def test_spark_session_builder_and_temp_views_and_sql_cells():
    k = Kernel()
    assert k.run("from pyspark.sql import SparkSession\ns = SparkSession.builder.appName('x').getOrCreate()\n"
                 "s is spark")["outputs"] == [{"type": "text", "text": "True"}]
    k.run("spark.createDataFrame([(1, 'a'), (2, 'b')], ['id', 'v']).createOrReplaceTempView('t')")
    out = k.run("%%sql\nSELECT count(*) AS n FROM t")
    assert out["status"] == "ok", out
    assert out["outputs"][0]["rows"] == [[2]]


def test_spark_reads_and_writes_lakehouse_tables(tmp_path, monkeypatch):
    service, opened = _sql_service(tmp_path, monkeypatch)
    written = {}

    def write_table(data, table, mode="overwrite"):
        written[table] = (data.num_rows, mode)
        return f"Wrote {data.num_rows} rows to {table}"

    monkeypatch.setattr(lakehouse, "write_table", write_table)
    out = service.run_cell(
        "df = spark.read.table('dbo.sales')\n"
        "df.filter(df.Country == 'US').write.mode('overwrite').saveAsTable('dbo.us_sales')\n"
        "spark.sql('SELECT Country, count(*) AS n FROM sales GROUP BY Country ORDER BY n DESC')", LH)
    assert out["status"] == "ok", out
    assert written["dbo.us_sales"] == (2, "overwrite")
    assert out["outputs"][0] == {"type": "stream", "text": "Wrote 2 rows to dbo.us_sales\n"}
    assert out["outputs"][1]["rows"][0] == ["US", 2]
    out = service.run_cell("%%sql\nINSERT INTO dbo.us_sales SELECT * FROM sales WHERE Country = 'UK'", LH)
    assert out["status"] == "ok", out
    assert written["dbo.us_sales"] == (1, "append")
    out = service.run_cell("%%sql\nMERGE INTO dbo.x AS t USING dbo.y AS s ON t.id = s.id WHEN MATCHED THEN DELETE", LH)
    assert "runs in Fabric" in out["outputs"][0]["text"]
    assert "dbo.sales" in opened


def test_fabric_paths_resolve_to_the_attached_lakehouse(tmp_path):
    lakehouse.attach(lakehouse.Attached("ws1", "lh1", "Sales", lambda: "tok", tmp_path, workspace_name="Dev"))
    try:
        assert lakehouse.relative_path("/lakehouse/default/Files/raw/a.csv") == "Files/raw/a.csv"
        assert lakehouse.relative_path("abfss://Dev@onelake.dfs.fabric.microsoft.com/Sales.Lakehouse/Files/a.csv") == "Files/a.csv"
        assert lakehouse.relative_path("abfss://ws1@onelake.dfs.fabric.microsoft.com/lh1/Tables/dbo/x") == "Tables/dbo/x"
        assert lakehouse.relative_path("C:/data/a.csv") is None
        with pytest.raises(ValueError, match="another lakehouse"):
            lakehouse.relative_path("abfss://ws9@onelake.dfs.fabric.microsoft.com/lh9/Files/a.csv")
    finally:
        lakehouse.attach(None)


def test_notebookutils_fs_runtime_and_exit(tmp_path):
    k = Kernel()
    folder = (tmp_path / "out").as_posix()  # forward slashes: the path goes inside a Python string
    out = k.run(
        f"notebookutils.fs.mkdirs('file:{folder}')\n"
        f"notebookutils.fs.put('file:{folder}/a.txt', 'hello', True)\n"
        f"print([f.name for f in mssparkutils.fs.ls('file:{folder}')])\n"
        f"print(notebookutils.fs.head('file:{folder}/a.txt'))\n"
        "notebookutils.runtime.context['isLocal']")
    assert out["status"] == "ok", out
    assert out["outputs"] == [{"type": "stream", "text": "['a.txt']\nhello\n"}, {"type": "text", "text": "True"}]
    out = k.run("x = 1\nnotebookutils.notebook.exit('done')\nx = 2")
    assert out["status"] == "ok"
    assert k.namespace["x"] == 1
    assert "exited with value: done" in out["outputs"][0]["text"]


def test_notebookutils_get_token_uses_the_sign_in():
    k = Kernel(token=lambda audience: f"token-for-{audience}")
    out = k.run("import notebookutils\nnotebookutils.credentials.getToken('storage')")
    assert out["outputs"] == [{"type": "text", "text": "'token-for-storage'"}]


def test_run_another_notebook_with_parameters_and_percent_run(tmp_path):
    store = NotebookStore(tmp_path)
    store.save("child", [
        {"type": "code", "source": "region = 'all'", "meta": {"tags": ["parameters"]}},
        {"type": "code", "source": "result = f'rows for {region}'\nnotebookutils.notebook.exit(result)"},
    ])
    store.save("helpers", [{"type": "code", "source": "def double(x):\n    return 2 * x"}])
    k = Kernel(load_notebook=lambda name: store.load(name)["cells"])
    out = k.run("notebookutils.notebook.run('child', 60, {'region': 'West'})")
    assert out["outputs"][-1] == {"type": "text", "text": "'rows for West'"}
    assert "region" not in k.namespace  # the child ran in its own namespace
    out = k.run("%run helpers\ndouble(21)")
    assert out["status"] == "ok", out
    assert out["outputs"][-1] == {"type": "text", "text": "42"}


def test_cell_tags_round_trip(tmp_path):
    store = NotebookStore(tmp_path)
    cells = [{"type": "code", "source": "a = 1", "meta": {"tags": ["parameters"], "frozen": True,
                                                          "source_hidden": True}},
             {"type": "code", "source": "a"}]
    store.save("nb", cells)
    assert store.load("nb")["cells"] == cells


def test_output_streams_while_a_cell_runs_and_cancel_keeps_variables():
    k = Kernel()
    k.run("kept = 'yes'")
    # A Python loop stops at once; native code that never returns to Python can't be stopped this way.
    first = k.start("import time\nprint('step 1', flush=True)\nfor _ in range(600):\n    time.sleep(0.05)\nprint('never')")
    assert first["status"] == "running"
    deadline = time.monotonic() + 10
    snap, outputs = first, first["outputs"]
    while time.monotonic() < deadline and not outputs:
        snap = k.poll(snap["version"], wait=0.5)
        outputs = snap["outputs"] or outputs
    assert snap["status"] == "running" and outputs == [{"type": "stream", "text": "step 1\n"}]
    with pytest.raises(RuntimeError, match="already running"):
        k.start("1")
    assert k.interrupt()["stopped"] is True
    done = _wait(k)
    assert done["status"] == "cancelled"
    assert k.run("kept")["outputs"] == [{"type": "text", "text": "'yes'"}]


def test_display_mid_cell_html_and_charts():
    k = Kernel()
    out = k.run("import polars as pl\ndisplay(pl.DataFrame({'a': [1, 2]}), summary=True)\nprint('after')")
    assert [o["type"] for o in out["outputs"]] == ["table", "stream"]
    assert out["outputs"][0]["summary"] is True
    out = k.run("%%html\n<b>hi</b>")
    assert out["outputs"] == [{"type": "html", "html": "\n<b>hi</b>"}]

    class Rich:
        def _repr_html_(self):
            return "<i>rich</i>"
    k.namespace["Rich"] = Rich
    assert k.run("Rich()")["outputs"] == [{"type": "html", "html": "<i>rich</i>"}]


def test_matplotlib_figures_show_as_images():
    pytest.importorskip("matplotlib")
    out = Kernel().run("import matplotlib.pyplot as plt\nplt.plot([1, 2, 3])\nplt.show()")
    images = [o for o in out["outputs"] if o["type"] == "image"]
    assert len(images) == 1 and images[0]["mime"] == "image/png"


def test_variables_hide_secrets_and_completion_lists_attributes():
    k = Kernel()
    k.run("import polars as pl\nsales = pl.DataFrame({'a': [1, 2, 3]})\napi_password = 'p'\nn = 5")
    rows = {r["name"]: r for r in k.variables()}
    assert set(rows) == {"sales", "api_password", "n"}
    assert rows["sales"]["size"] == "3 × 1" and rows["sales"]["type"] == "DataFrame"
    assert rows["api_password"]["value"] == "hidden"
    assert rows["n"]["value"] == "5"
    done = k.complete("sales.gro")
    assert {"label": "group_by", "type": "method"} in done["items"] and done["from"] == len("sales.")
    names = [i["label"] for i in k.complete("sa")["items"]]
    assert "sales" in names
    assert any(i["label"] == "read" for i in k.complete("spark.re")["items"])


def test_restart_clears_variables_and_gives_a_new_spark():
    k = Kernel()
    k.run("spark.createDataFrame([(1,)], ['a']).createOrReplaceTempView('t')\nx = 1")
    k.restart()
    assert "x" not in k.namespace
    assert k.run("spark.sql('select * from t')")["status"] == "error"
