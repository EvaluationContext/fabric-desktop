# Notebooks and SQL

Notebooks in Fabric Desktop look and work like Fabric's, but every cell runs in a Python kernel on your PC. Reading and writing lakehouse data goes straight to OneLake, so your work uses no Fabric capacity. This page covers how to write, run, publish and schedule notebooks, which Fabric notebook code works here, and the SQL query tab.

## Your first notebook

1. **Create > Notebook** (or **New notebook** on Home, or Ctrl+K "New notebook"). The notebook opens with one empty code cell.
2. **Add data items** in the ribbon opens a OneLake catalog of every lakehouse you can reach. Select one to see its tables and files on the right, then **Attach** (or double-click it). The attached lakehouse shows in the **Explorer** on the left. **New lakehouse** at the bottom creates one and attaches it.
3. In the Explorer, click a table to insert code that loads it, or a file to insert code that reads it.
4. Run the cell with **Shift+Enter**. Output shows below it.
5. Write a result back:

   ```python
   import polars as pl
   from fabric_desktop import lakehouse

   sales = lakehouse.read_csv("Files/raw/sales.csv")          # from the attached lakehouse
   by_region = sales.group_by("region").agg(pl.col("amount").sum())
   lakehouse.write_table(by_region, "dbo.sales_by_region")     # a Delta table in OneLake
   ```

6. The notebook saves itself a second or two after each change. **Ctrl+S** saves right away. Rename it by clicking its name in the breadcrumb at the top.

Notebooks are `.ipynb` files in `%LOCALAPPDATA%\FabricDesktop\notebooks`. They stay on your PC until you publish them.

## The lakehouse module

`from fabric_desktop import lakehouse` is the notebook's way to the attached lakehouse.

| Call | What it does |
| --- | --- |
| `lakehouse.read_csv(path)`, `read_parquet(path)`, `read_json(path)` | Read a file. `Files/...` paths read from the attached lakehouse; any other path is a file on this PC. Returns a Polars DataFrame. |
| `lakehouse.read_table("schema.table")` | Read a Delta table. The schema defaults to `dbo`. |
| `lakehouse.write_table(df, "schema.table", mode="overwrite")` | Write a Delta table. `mode` is `"overwrite"` or `"append"`. |
| `lakehouse.sql("SELECT ...")` | Run Spark SQL over the attached lakehouse's tables, on this PC, and return a Polars DataFrame. |

Every write:

- is checked against the **Fabric-safe Delta profile** (reader version 1, writer version 2, classic checkpoints, no deletion vectors or column mapping), so the SQL analytics endpoint and Direct Lake can read it;
- writes the data files first and the Delta log last, so Fabric never sees a half-written version;
- shows in **Monitor** as a notebook write, with rows, mode and Delta version.

A table Fabric hasn't seen before can show under **Unidentified** in the lakehouse until you refresh the lakehouse in Fabric.

Tables managed by the [metadata framework](metadata-guide.md) can't be written from a notebook; you'll get an error. Use a `scratch_` table name for experiments next to managed tables.

## Fabric notebook code on this PC

Most notebooks written for Fabric run here unchanged.

**`spark` is ready in every cell.** It is DuckDB's PySpark-compatible API, wired to the attached lakehouse. There's no Spark cluster; everything runs on this PC.

```python
df = spark.read.table("dbo.sales")
df = spark.sql("SELECT region, SUM(amount) AS total FROM sales GROUP BY region")
raw = spark.read.csv("Files/raw/sales.csv", header=True)
df.write.mode("overwrite").saveAsTable("dbo.sales_by_region")
df.createOrReplaceTempView("v_sales")
from pyspark.sql import functions as F
```

- `spark.read.csv/parquet/json/load("Files/...")`, `spark.read.table`, `spark.sql`, `saveAsTable`, temp views and `pyspark.sql.functions` work.
- `%%sql` cells use the same session, so they see temp views. `CREATE TABLE ... AS SELECT` and `INSERT INTO` write lakehouse tables.
- **Not available on this PC:** RDDs, UDFs, streaming, Scala and R cells, and `MERGE`, `UPDATE` or `DELETE` in SQL. A notebook that uses them runs in Fabric only (see [Where a notebook runs](#where-a-notebook-runs)).

**`notebookutils` (and `mssparkutils`)** cover:

| Area | What works here |
| --- | --- |
| `fs` | `ls`, `head`, `put`, `append`, `cp`, `mv`, `rm`, `mkdirs`, `exists`, over the attached lakehouse and local `file:` paths |
| `notebook` | `run`, `exit` and `runMultiple`, against the notebooks on this PC |
| `credentials` | `getToken`, and `getSecret` from Azure Key Vault as you (needs the Key Vault Secrets User role and the Azure Key Vault permission) |
| `runtime.context` | Works; `isLocal` is true here |
| `variableLibrary` | `getLibrary("VL_Sources")` reads the metadata project's source system values ([variable library](metadata-guide.md#9-source-systems-and-the-variable-library)) |

**Paths** can be written the Fabric way: `Files/...`, `/lakehouse/default/Files/...`, or the attached lakehouse's `abfss://` path.

**Magics:** `%run Other notebook` runs another notebook in this one's namespace. `%pip` lines are skipped with a note, because the engine's packages are fixed. `%%configure` does nothing here.

## Running cells

| Do this | How |
| --- | --- |
| Run a cell and move to the next | **Shift+Enter** |
| Run a cell and stay | **Ctrl+Enter** |
| Run a cell and add one below | **Alt+Enter** |
| Run everything | **Run all** (Home or Run tab) |
| Run the cells above, or this one and below | Run tab: **Run all above**, **Run cell and below** |
| Stop the running cell | **Cancel**. Variables from earlier cells are kept. |
| Start fresh | **Restart kernel** (clears all variables), or **Restart and run all** |

Printed output shows while a cell runs. Code stuck inside a library call can't always be interrupted; the app then offers **Restart kernel**.

**What output looks like:**

- A cell's last expression shows below it. `display(x)` shows a value mid-cell.
- Data frames (Spark, Polars, pandas, Arrow, DuckDB) show the first 1,000 rows with three views: **Table** (search, click a heading to sort), **Chart** (bar, line or scatter, with the X axis, values and aggregate you pick) and **Inspect** (missing, distinct, min, max, mean and a histogram per column). `display(df, summary=True)` opens Inspect.
- Matplotlib charts show as images. Objects with an HTML view (Plotly, `%%html` cells) render in a sandboxed frame.

## Editing

- Cells have line numbers, bracket matching, folding, find and replace (**Ctrl+F**), **Ctrl+/** to comment, and autocomplete (**Ctrl+Space**) from the running kernel's variables, Python names, and the attached lakehouse's table names inside strings and `%%sql` cells.
- Each cell has a language picker (Python, Spark SQL, HTML, Markdown) and **...** for more actions.
- **Command mode** (press **Esc**): **A** and **B** add a cell above or below, **DD** deletes, **Z** undoes a delete, **M** and **Y** switch between Markdown and code, **C**, **X** and **V** copy, cut and paste cells, **O** hides output, **Shift+L** toggles line numbers, and **Enter** goes back to editing.

The ribbon has four tabs:

| Tab | What's on it |
| --- | --- |
| **Home** | Run all, Cancel, **Runs on**, Add data items, Save, Schedule (Preview), **Publish to OneLake** |
| **Edit** | Add Code or Markdown cells, Move up or down, Split cell, Merge with below, Delete cell, Undo delete, **Parameter cell**, **Freeze**, Find and replace, Clear all outputs |
| **Run** | Run all, Run cell, Run all above, Run cell and below, Cancel, Runs on, Restart kernel, Restart and run all, Schedule |
| **View** | Explorer, **Outline** (Markdown headings; click to jump), **Variables** (name, type, size and value of each variable, secrets hidden; click one to display it), collapse or expand all inputs and outputs, line numbers |

A **Parameter cell** is the cell that pipelines and `notebookutils.notebook.run` pass values into, as in Fabric. A **frozen** cell can't be edited, run or deleted until you unfreeze it.

## Publish to OneLake

**Publish to OneLake** saves the notebook as a notebook item in a Fabric workspace, so it shows in Fabric and can run there.

1. The first time, it asks which workspace (starting from the attached lakehouse's). The choice is saved in the notebook, so later publishes go straight there.
2. The arrow next to **Publish** shows where it goes and has **Change workspace...**.
3. The attached lakehouse stays the notebook's default lakehouse in Fabric, even when it lives in another workspace.

How it is published:

- A notebook that uses `spark` or `%%sql` is published as a **Spark notebook**, where `spark` and `%%sql` are Fabric's own.
- A notebook that uses `fabric_desktop.lakehouse` is published as a **Fabric Python notebook** with a setup cell first. That cell defines the same `lakehouse` module from libraries Fabric's Python notebooks already have (Polars, deltalake, DuckDB), so the notebook runs there unchanged. Its `%%sql` cells become `lakehouse.sql(...)`. The setup cell is only in the published copy, not in your notebook.
- Other notebooks are published as they are.

## Where a notebook runs

**Runs on** in the ribbon picks where **Run all** runs the notebook. The choice is saved in the notebook.

| Choice | What happens |
| --- | --- |
| **This PC** | Every cell runs in the kernel on your PC. No capacity used. |
| **Fabric** (Preview) | Run all publishes the notebook and runs the whole thing on its workspace's Fabric capacity. Single cells can't run in Fabric; switch back to This PC to run one cell. Cell output shows in Fabric, and the bell tells you when it finishes. |
| **Same as Dev** (default) | Follows the environment picked on the Metadata pages and where that environment runs. |

A note above the cells tells you when a notebook can't run where it is set to, and which cell is the reason:

- Spark features this PC doesn't have (RDDs, UDFs, streaming, Scala or R cells, `%%sql` that MERGEs, UPDATEs or DELETEs) mean **Fabric only**.
- A notebook with no lakehouse attached, or one that reads a path on your PC like `C:\data`, means **this PC only**.

The note has a button to switch when the other place would work.

## Run in Fabric and schedules (Preview)

**Run in Fabric** publishes the notebook, then starts it on the workspace's Fabric capacity with Fabric's job scheduler. The status bar, the bell and **Monitor** follow it until it finishes. Attach a lakehouse first; the notebook runs in that lakehouse's workspace (or the workspace you publish to).

**Schedule** opens one dialog for both places:

- **On this PC:** Windows Task Scheduler runs the saved notebook, every cell in order, with its attached lakehouse. It runs even when the app is closed, as long as the PC is on and you're signed in to Windows. Save your changes first: it runs what's saved.
- **In Fabric:** publishes what's on screen and adds a Fabric schedule. It runs on capacity even when your PC is off.

For either, choose **Repeat** (Every day, Some days of the week, or Every few minutes), the **Times of day** (24-hour, comma between, for example `06:00, 18:00`), the **Days**, or **Minutes between runs**, and the **Time zone**. The same dialog lists the notebook's schedules and removes them. All schedules are also in **Monitor > Schedules**.

## SQL query

The SQL query tab works like Fabric's. Open it with **SQL query** on a lakehouse in **OneLake** or **Workspaces**, or **New SQL query** on a lakehouse page.

1. The **Explorer** lists the lakehouse's tables. Click a table to run `SELECT * FROM <table> LIMIT 100`. Click **▸** next to it to see its columns; click a column to type its name.
2. Write **Spark SQL** using the names Fabric uses: `SELECT * FROM sales`, or `schema.table` for schemas other than `dbo`.
3. **Run**, or **Ctrl+Enter**. Select some text first to run just that.
4. Results show the first 1,000 rows, with how many rows there were and how long it took.

| Button | What it does |
| --- | --- |
| **Copy** | Copies the shown rows, tab-separated, for pasting into Excel |
| **Download CSV** | Saves the full result to your Downloads folder |
| **Save as table** | Writes the full result to the lakehouse as a Delta table you name |
| **Open in notebook** | Turns the query into `lakehouse.sql(...)` in a new notebook |
| **+** or **New SQL query** | Adds a query tab. Double-click a tab to rename it. |

How it works: sqlglot translates your Spark SQL to DuckDB SQL, and DuckDB reads only the columns and files it needs from OneLake. Queries only read; nothing in the lakehouse changes unless you **Save as table**. Query tabs are kept per lakehouse between launches.

## Job files

For repeatable transforms without a notebook, a job file runs DuckDB SQL or a Polars script and publishes the result. See [Job files in the README](https://github.com/KratosDataGod/fabric-desktop/blob/main/README.md#job-files). From a terminal: `fabric-desktop preview job.toml` shows the result without writing it, and `fabric-desktop run job.toml` writes it.
