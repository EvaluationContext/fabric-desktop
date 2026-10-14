# Fabric Desktop

Do your Microsoft Fabric data engineering on your own Windows PC: notebooks, Spark SQL queries, jobs and a metadata-driven medallion framework. Results are written straight into OneLake as Delta tables, so no Fabric capacity is spent on the transform work.

![Fabric Desktop architecture](docs/architecture.svg)

- **Local compute:** notebooks run in a Python kernel on the PC, SQL runs in DuckDB, and Python transforms run in Polars.
- **Direct to OneLake:** delta-rs writes each table straight to the lakehouse. It commits the Delta log only after the data files are written, so Fabric never sees a half-written version.
- **Fabric-safe tables:** every table is checked against a conservative Delta profile (reader 1, writer 2, classic checkpoints, no deletion vectors or column mapping). This keeps tables readable by the SQL analytics endpoint and Direct Lake.
- **Signs in as you:** the app uses delegated MSAL sign-in, with no service principal. Your token cache lives in `%LOCALAPPDATA%\FabricDesktop`.
- **Fabric's own pages inside the app:** the Monitoring hub and the OneLake catalog are embedded, and their sign-in is kept between launches.

How the pieces fit together, how a request flows and how writes reach OneLake: see [docs/architecture.md](docs/architecture.md).

## Layout

| Path | What it is |
| --- | --- |
| `sidecar/` | Python package `fabric_desktop`: auth, Fabric REST client, OneLake I/O, notebook kernel, SQL engine, metadata framework, job runner and the JSON-lines server |
| `app/` | Tauri 2 shell (`src-tauri/`) and the Fabric-style UI (`ui/`, plain HTML and JavaScript). The shell starts the sidecar and forwards UI actions to it |
| `docs/` | Architecture overview and diagram |
| `examples/` | Sample jobs (`sales_by_region.toml` for DuckDB, `top_products.toml` for Polars) and sample data |

## One-time setup: Entra app registration

1. In the Entra admin center, go to **App registrations > New registration**. Add a **Public client** redirect URI of `http://localhost`.
2. Under **API permissions**, add these delegated permissions:
   - Power BI Service (Fabric): `Workspace.ReadWrite.All` and `Item.ReadWrite.All`.
   - Azure Storage: `user_impersonation`.
3. Copy the Application (client) ID and set it for your user:
   `setx FABRIC_DESKTOP_CLIENT_ID <client-id>`

Your tenant may require admin consent for these permissions.

## Run it

```powershell
cd sidecar
python -m pip install -e ".[dev]"
python -m pytest -q                         # local tests, no Fabric needed
fabric-desktop preview ..\examples\sales_by_region.toml
fabric-desktop sign-in
fabric-desktop run ..\examples\sales_by_region.toml   # set workspace_id in the job first
```

### Desktop app

Download [fabric-desktop.exe](https://github.com/KratosDataGod/fabric-desktop/releases/download/latest/fabric-desktop.exe) from the **Releases** section of the repository page and double-click it. Every green build of `main` replaces it with a fresh copy. Windows SmartScreen may warn because the exe is not signed yet; choose **More info > Run anyway**. The app runs the sidecar with the `python` on your PATH (Python 3.11 or later). To use a different interpreter, set `FABRIC_DESKTOP_PYTHON` to its full path.

Each time it starts, the app checks its components before anything else runs:

- **Fabric Desktop:** if a newer build has been released, it downloads it straight away, checks the file really is that build, replaces the exe in place and restarts. The account menu shows which build is running.
- **Python:** it must be 3.11 or later.
- **Local engine (the sidecar):** the app installs the sidecar from the same commit the exe was built from, with pip, so the two always match. A missing or older sidecar is installed or updated automatically, with pip's output shown while it runs. An editable (`pip install -e`) install is left alone.

Exes you build yourself skip the update check and leave the sidecar as you installed it.

To build it yourself instead, install Rust and the [Tauri prerequisites](https://v2.tauri.app/start/prerequisites/), then:

```powershell
$env:FABRIC_DESKTOP_PYTHON = (Get-Command python).Source   # the Python that has the sidecar installed
cd app\src-tauri
cargo run
```

## Notebooks

The app looks and works like the Fabric service: the same left rail (Home, Create, Workspaces, OneLake, Metadata, Monitor), and Python notebooks with a ribbon, an Explorer pane and **Add data items** to attach a lakehouse. Cells run in a Python kernel on this PC, so no Fabric capacity is used.

**Add data items** opens a OneLake catalog: every lakehouse in every workspace you can open, in one list you can filter, with each lakehouse's tables and files shown before you attach it. The left side filters by Favorites, Endorsed or one workspace. Favorites are the stars you click in the list. They are saved on this PC, because Fabric has no public API for its own favorites. Endorsed (Certified or Promoted) comes from Fabric's admin workspace scan, so it works only when your account is a Fabric administrator and the app registration has the Power BI `Tenant.Read.All` delegated permission with consent. Otherwise the filter is greyed out with the reason.

```python
import polars as pl
from fabric_desktop import lakehouse

sales = lakehouse.read_csv("Files/raw/sales.csv")      # downloaded from the attached lakehouse
by_region = sales.group_by("region").agg(pl.col("amount").sum())
lakehouse.write_table(by_region, "dbo.sales_by_region")  # Delta table written straight to OneLake
```

- `read_csv`, `read_parquet` and `read_json` read `Files/...` paths from the attached lakehouse; any other path is a file on this PC.
- `sql("SELECT ...")` runs Spark SQL over the attached lakehouse's tables on this PC and returns a Polars DataFrame.
- `read_table("schema.table")` and `write_table(df, "schema.table", mode="overwrite" | "append")` work with Delta tables. The schema defaults to `dbo`. Writes are checked against the Fabric-safe Delta profile and show up in **Monitor**. A new table can show under **Unidentified** in Fabric until you refresh the lakehouse there.
- A cell's last expression is shown below it. Data frames (Polars, pandas, Arrow, DuckDB) show as a table of the first 100 rows.
- **Stop** ends the running cell by restarting the kernel, which clears its variables.
- Notebooks autosave as `.ipynb` files in `%LOCALAPPDATA%\FabricDesktop\notebooks`. **Publish to OneLake** also saves the notebook as a notebook item in the attached lakehouse's workspace.

**OneLake** is the catalog page. The top lists every item in every workspace you can open (lakehouses, warehouses, notebooks, semantic models, reports and the rest). Search matches names, workspaces, types, descriptions and lakehouse table names. Filter by Favorites, Endorsed, one workspace or an item type, and sort by any column. Lakehouse rows expand to show their tables and files, and open in a notebook. **Show in Fabric** opens the item in the bottom half, which is Fabric's own OneLake catalog in the same embedded Fabric view that Monitor uses.

**SQL query** works like Fabric's SQL query tab. Open it from a lakehouse page, or from **SQL query** on a lakehouse row in OneLake. Write Spark SQL against the lakehouse's tables by the names Fabric uses (`SELECT * FROM sales`, or `schema.table`). The query runs on this PC: sqlglot translates it to DuckDB SQL and DuckDB reads only the columns and files it needs from OneLake, so no Fabric capacity is used. Ctrl+Enter runs the query, or just the selected text. Results show the first 1,000 rows. **Save as table** writes the full result to the lakehouse, **Download CSV** saves it to your Downloads folder, and **Open in notebook** turns the query into `lakehouse.sql(...)` in a new notebook. Query tabs are kept per lakehouse between launches. Queries only read; nothing in the lakehouse changes unless you save a table.

**Monitor** has two halves. The top lists what ran on this PC: metadata runs in Dev and notebook table writes, newest first. The bottom is Fabric's own Monitoring hub, the live page from the Fabric service shown in a second webview inside the app window, so Fabric Desktop adds no monitoring code of its own there. That view signs in with your Windows account when it is the same work account you use in the app, so there is no password prompt. It opens in the tenant of your app sign-in, which skips Fabric's email page. If Windows is signed in with another account, Fabric asks you to sign in once. The app saves that sign-in's cookies, encrypted for your Windows account (DPAPI), in `%LOCALAPPDATA%\FabricDesktop\fabric-view-signin.bin` and restores them on the next launch, so you don't sign in again each time. Delete that file to forget the sign-in. Links that leave Fabric open in your default browser.

Icons are [Fluent UI System Icons](https://github.com/microsoft/fluentui-system-icons) (MIT).

## Metadata framework

**Metadata** in the left rail manages Bronze to Gold (or any 2 to 10 layers) from config instead of one-off notebooks. A metadata project is a folder you keep in Git:

| File or folder | What goes there |
| --- | --- |
| `layers.toml` | Your layers, in order. Each layer is its own schema-enabled lakehouse, `LH_<Layer>`, in every environment. |
| `metadata/entities/<layer>.<table>.toml` | One file per table: source, contract (columns, types, keys), cleaning rules, derived columns, checks, load mode. |
| `transforms/` | Your Python: hooks, custom checks and custom steps. Shared helpers go in `transforms/common.py`. |
| `tests/` | Tests for your Python. Every hook and custom step must name one. |
| `exceptions.toml` | Approved exceptions to a rule, each with a reason, an approver and an expiry. |
| `environments.toml` | Where Dev, Test and Prod write. Dev writes Delta tables to the project's `.local` folder by default. |

Every run goes through the same steps: read, contract (schema drift and types), clean, derive, hooks, lookups, checks, write, log. Load modes are full, incremental (watermark plus merge on the key), Type 1 dimension and Type 2 dimension. Dimensions get integer surrogate keys that are never renumbered and an Unknown member at -1; facts look up the key that was current on the fact's date.

Four gates keep the rules followed:

1. **Save:** every entity is checked against its layer's standards. Tables can read earlier layers, or their own layer when there's no loop, never a later one. Columns must exist upstream, so renames and deletes show what breaks. Custom code needs an owner and a test.
2. **Write:** checks run before every commit. `stop` writes nothing, `quarantine` moves failing rows to `quarantine.<schema>_<table>` with the reason, `warn` only records it. Only the framework writes a managed table; notebooks get an error and can use `scratch_` tables.
3. **Promote:** Dev to Test and Test to Prod need a clean save gate and a green last run of every table's current logic. Promote creates the `LH_<Layer>` lakehouses and `LH_Meta`, and pins the environment to that metadata version.
4. **After:** `meta.run_log`, `meta.dq_results`, `meta.watermarks`, `meta.schema_history` and `meta.deployments` in `LH_Meta`, plus `_run_id` and `_loaded_at` on every row and the run ID and metadata version on every Delta commit.

When a table's logic changes, an incremental run stops and asks: apply to new rows only, or rebuild the table and everything downstream. Changing a dimension from Type 1 to Type 2 offers a migration that keeps every key.

Where custom logic goes, simplest first: an expression (`[[derive]]` or a `[[check]]` with `expr`), a hook (`[hooks] after_clean` or `before_write`, a `(df, ctx)` function that declares the tables it `reads` and the columns it `adds`), a custom check (`[[check]] python = ...`), or a custom step (`[step] custom = ...`) that replaces one table's transform while the framework still checks and writes the result.

## Job files

```toml
name = "sales_by_region"
engine = "duckdb"            # or "polars" with script = "file.py" defining transform()
sql = "select region, sum(amount) as total from read_csv_auto('data/sales.csv') group by region"

[target]
workspace_id = "<workspace guid>"
lakehouse = "Sales"          # created as a schema-enabled lakehouse if missing
schema = "dbo"
table = "sales_by_region"
mode = "overwrite"           # or "append"
```

Relative paths in a job resolve from the job file's folder.

## Not yet built

A bundled Python runtime and installer, scheduling, pipelines, and running published notebooks or metadata runs on Fabric compute (Test and Prod runs execute on this PC and write to OneLake). From the metadata plan: profiling with suggested cleaning rules, per-step performance suggestions, re-pointing -1 facts when a late customer arrives, replaying quarantined rows, and running hook tests on promote.
