# Fabric Desktop

Do your Microsoft Fabric data engineering on your own Windows PC: notebooks, Spark SQL queries, jobs and a metadata-driven medallion framework. Results are written straight into OneLake as Delta tables, so no Fabric capacity is spent on the transform work.

![Fabric Desktop in a Microsoft Fabric solution: develop locally, execute on your infrastructure, store and consume in Microsoft Fabric](docs/fabric-desktop-solution.png)

**Develop with Fabric Desktop. Execute on your infrastructure. Store and consume in Microsoft Fabric.** The picture above is where the product is headed. Today the desktop app and a single-node [headless agent](docs/agent.md) are built; multiple workers, resource governance and pipeline hand-off are later milestones in the [enterprise roadmap](docs/enterprise-roadmap.md).

**Why it exists:** Power BI took off because developers could build on their own machine and deploy to the cloud. Fabric Desktop brings that model to data engineering. Read the story in **[Bringing the Power BI Model to Data Engineering](docs/why-fabric-desktop.md)**.

![Fabric Desktop architecture](docs/architecture.svg)

- **Local compute:** notebooks run in a Python kernel on the PC, SQL runs in DuckDB, and Python transforms run in Polars.
- **Direct to OneLake:** delta-rs writes each table straight to the lakehouse. It commits the Delta log only after the data files are written, so Fabric never sees a half-written version.
- **Fabric-safe tables:** every table is checked against a conservative Delta profile (reader 1, writer 2, classic checkpoints, no deletion vectors or column mapping). This keeps tables readable by the SQL analytics endpoint and Direct Lake.
- **Signs in as you:** the app uses delegated MSAL sign-in. Your token cache lives in `%LOCALAPPDATA%\FabricDesktop`.
- **Headless agent for servers:** the same engine runs unattended on Windows or Linux servers as a service principal or managed identity, with a durable job queue, retries, crash recovery and per-table write leases. See [docs/agent.md](docs/agent.md).
- **Fabric's own pages inside the app:** the Monitoring hub and the OneLake catalog are embedded, and their sign-in is kept between launches.

How the pieces fit together, how a request flows and how writes reach OneLake: see [docs/architecture.md](docs/architecture.md).

## Documentation

| Document | What it covers |
| --- | --- |
| [docs/why-fabric-desktop.md](docs/why-fabric-desktop.md) | Why Fabric Desktop is needed: bringing Power BI's build-locally, deploy-to-the-cloud model to data engineering |
| [docs/architecture.md](docs/architecture.md) | How the app, the sidecar and OneLake fit together |
| [docs/permissions.md](docs/permissions.md) | Every Entra permission the app uses, in alphabetical order; setup steps; fixing admin consent errors; what the agent needs instead |
| [docs/xray-fixes.md](docs/xray-fixes.md) | Each Estate X-ray finding: what it means, what the app's fix button does, and how to fix it by hand in Fabric |
| [docs/agent.md](docs/agent.md) | The headless agent: identity setup, `agent.toml`, installing on Linux (systemd or container) and Windows, operating it, and what to verify on your tenant |
| [docs/enterprise-roadmap.md](docs/enterprise-roadmap.md) | The enterprise local execution assessment: feature inventory, gap analysis, architecture changes, roadmap milestones M0 to M6 and work items |
| [deploy/agent.example.toml](deploy/agent.example.toml) | Every agent setting, with comments |

## Layout

| Path | What it is |
| --- | --- |
| `sidecar/` | Python package `fabric_desktop`: auth, Fabric REST client, OneLake I/O, notebook kernel, SQL engine, metadata framework, job runner and the JSON-lines server |
| `app/` | Tauri 2 shell (`src-tauri/`) and the Fabric-style UI (`ui/`, plain HTML and JavaScript). The shell starts the sidecar and forwards UI actions to it |
| `docs/` | Architecture overview and diagram, agent guide, enterprise roadmap, and the KratosBI logo files (`docs/brand/`) |
| `deploy/` | Headless agent packaging: example `agent.toml`, systemd unit and Dockerfile (`linux/`), and the Windows install script (`windows/`) |
| `examples/` | Sample jobs (`sales_by_region.toml` for DuckDB, `top_products.toml` for Polars) and sample data |

## One-time setup: Entra app registration

The desktop app signs in as you through your own Entra app registration. Every permission it needs, in alphabetical order, with step-by-step setup and the fix for "Could not grant admin consent ... does not have a subscription (or service principal)", is in **[docs/permissions.md](docs/permissions.md)**.

In short:

1. Register an app with the redirect URI `http://localhost` (Mobile and desktop applications).
2. Make sure your tenant has a service principal for each API (Azure Storage, Microsoft Graph, Power BI Service, and any optional ones).
3. Add the delegated permissions from the table, grant admin consent, and set the app's id for your user: `setx FABRIC_DESKTOP_CLIENT_ID <client-id>`.

The headless agent needs no API permissions; it needs a Fabric tenant setting and workspace roles, listed on the same page.

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

Download [fabric-desktop.exe](https://github.com/KratosDataGod/fabric-desktop/releases/download/latest/fabric-desktop.exe) from the **Releases** section of the repository page and double-click it. Every green build of `main` replaces it with a fresh copy. Windows SmartScreen may warn while the exe is not signed; choose **More info > Run anyway**. The app brings its own engine (Python plus the sidecar and its packages) in `%LOCALAPPDATA%\FabricDesktop\engine`, so the PC needs no Python. To run the sidecar with a Python of your own instead, set `FABRIC_DESKTOP_PYTHON` to its full path and pip-install the sidecar there.

Each time it starts, the app checks its components before anything else runs:

- **Fabric Desktop:** if a newer build has been released, it downloads it straight away, checks the file really is that build, replaces the exe in place and restarts. The account menu shows which build is running.
- **Local engine:** the engine built from the same commit as the exe, so the two always match. A missing or older engine is downloaded from the release (`fabric-desktop-engine.zip`) and unpacked automatically. With `FABRIC_DESKTOP_PYTHON` set, the sidecar is pip-installed into that Python instead (it must be 3.11 or later), and an editable (`pip install -e`) install is left alone.
- **Blocked by Windows:** if Smart App Control or an organization's App Control policy refuses the engine's DLLs, the check names the blocked packages and says what to do instead of reinstalling.

Exes you build yourself skip the update check and leave the sidecar as you installed it.

To build it yourself instead, install Rust and the [Tauri prerequisites](https://v2.tauri.app/start/prerequisites/), then:

```powershell
$env:FABRIC_DESKTOP_PYTHON = (Get-Command python).Source   # the Python that has the sidecar installed
cd app\src-tauri
cargo run
```

### Code signing

Smart App Control (and most App Control policies) only lets signed code load, and the Python packages the engine uses (pyarrow, Polars, DuckDB and others) are not signed by their makers. CI signs them, and the exe, with [Azure Trusted Signing](https://learn.microsoft.com/azure/trusted-signing/) when the repository has these settings (**Settings > Secrets and variables > Actions**):

| Kind | Name | Value |
|---|---|---|
| Secret | `AZURE_TENANT_ID` | Tenant of the app registration below |
| Secret | `AZURE_CLIENT_ID` | An app registration with the **Trusted Signing Certificate Profile Signer** role on the signing account |
| Secret | `AZURE_CLIENT_SECRET` | A client secret of that app registration |
| Variable | `TRUSTED_SIGNING_ENDPOINT` | The account's region endpoint, such as `https://eus.codesigning.azure.net/` |
| Variable | `TRUSTED_SIGNING_ACCOUNT` | The Trusted Signing account name |
| Variable | `TRUSTED_SIGNING_PROFILE` | The certificate profile name |

Without them, builds are unsigned and the signing steps are skipped.

### Knowing what's running

The top bar holds the **Environment** picker (Dev, Test or Prod), which every page uses, and a pill that says where that environment runs, such as "Runs in Fabric · Sales - Test". Click the pill to change it. **Home** starts with what needs attention (tables whose last run failed, in any environment), then what is running, the next scheduled runs, and each environment's state. The **bell** opens the activity tray: it follows runs in Fabric until they end, even if you leave the page, keeps results you can reopen, and keeps every error with its technical details folded underneath. Errors are explained in a sentence ("Your sign-in expired", "You don't have permission for that in this workspace"); the raw text is under **Details**. **Monitor** lists runs from every environment (Test and Prod once you are signed in), filtered by environment and by where they ran, with failures first and their reason in the row. The Fabric Monitoring hub below it can be hidden.

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
- A cell's last expression is shown below it, and `display(x)` shows a value mid-cell (`display(df, summary=True)` opens the column summary). Data frames (Spark, Polars, pandas, Arrow, DuckDB) show the first 1,000 rows with Fabric's views: **Table** (search, click a header to sort), **Chart** (bar, line or scatter, with the X axis, values and aggregate you pick) and **Inspect** (missing, distinct, min, max, mean and a histogram per column). Matplotlib charts show as images, and objects with an HTML view (Plotly, `%%html` cells) render in a sandboxed frame.
- Printed output shows while a cell runs. **Cancel** stops the running cell and keeps your variables. Code stuck inside a library call can't be interrupted; the app then offers **Restart kernel**, which clears variables.

### Fabric notebook code on this PC

Notebooks written for Fabric run here without changes in most cases:

- `spark` is ready in every cell. It is DuckDB's PySpark-compatible API wired to the attached lakehouse: `spark.read.table("dbo.sales")`, `spark.sql(...)`, `spark.read.csv/parquet/json/load("Files/...")`, `df.write.mode("overwrite").saveAsTable("dbo.out")`, temp views, and `from pyspark.sql import functions as F`. There is no Spark cluster; everything runs on this PC. `%%sql` cells use the same session, so they see temp views, and `CREATE TABLE ... AS SELECT` and `INSERT INTO` write lakehouse tables. RDDs, UDFs, streaming, Scala/R cells, and `MERGE`/`UPDATE`/`DELETE` still need Fabric.
- `notebookutils` (and `mssparkutils`) covers `fs` (ls, head, put, append, cp, mv, rm, mkdirs, exists) over the attached lakehouse and local `file:` paths, `notebook.run`/`exit`/`runMultiple` against the notebooks on this PC, `credentials.getToken` and `credentials.getSecret` (Key Vault, as you; needs the Key Vault Secrets User role), and `runtime.context` (with `isLocal`).
- Paths can be written the Fabric way: `Files/...`, `/lakehouse/default/Files/...` or the attached lakehouse's `abfss://` path.
- `%run Other notebook` runs another notebook in this one's namespace. `%pip` lines are skipped with a note (the engine's packages are fixed), and `%%configure` does nothing here.
- A notebook that uses Spark is published as a Spark notebook, where `spark` and `%%sql` are Fabric's own.

### Editing like Fabric

- Cells use a code editor with line numbers, bracket matching, folding, find and replace (Ctrl+F), Ctrl+/ to comment, and autocomplete (Ctrl+Space): variables and attributes from the running kernel, Python names, and the attached lakehouse's table names inside strings and `%%sql` cells.
- Each cell has a language picker (Python, Spark SQL, HTML, Markdown), and **...** for more actions.
- Keys as in Fabric: Shift+Enter runs and moves on, Ctrl+Enter runs, Alt+Enter runs and adds a cell. Esc switches to command mode, where A and B add a cell above or below, DD deletes, Z undoes a delete, M and Y switch between Markdown and code, C, X and V copy, cut and paste cells, O hides output, Shift+L toggles line numbers, and Enter edits.
- **Edit** tab: split and merge cells, undo delete, **Parameter cell** (the cell pipelines and `notebookutils.notebook.run` pass values into), **Freeze** (the cell can't be edited, run or deleted). Hide a cell's code or output from its toolbar or the **View** tab.
- **View** tab: **Variables** (name, type, size and value of each variable, with secrets hidden; click one to display it), **Outline** (Markdown headings; click to jump), and Explorer.
- Notebooks autosave as `.ipynb` files in `%LOCALAPPDATA%\FabricDesktop\notebooks`. **Publish to OneLake** also saves the notebook as a notebook item in a Fabric workspace. The first Publish asks which workspace (starting from the attached lakehouse's); the choice is saved in the notebook, so later publishes go straight there. The arrow next to Publish shows where it goes and has **Change workspace**. Run in Fabric and Schedule use the same workspace; the attached lakehouse stays the notebook's default lakehouse even when it lives in another workspace.

**Runs on** in the notebook ribbon picks where **Run all** runs each notebook: **This PC**, **Fabric**, or the default, **Same as** the environment picked in the top bar (Metadata > Environments). The choice is saved in the notebook. In Fabric, Run all publishes the notebook and runs the whole thing in its publish workspace; single cells only run on this PC. A note above the cells says when a notebook can't run where it is set to, and which cell is why: Spark features this PC doesn't have (RDDs, UDFs, streaming, Scala or R cells, `%%sql` that MERGEs, UPDATEs or DELETEs) run in Fabric only, and a notebook with no lakehouse, or one that reads a path like `C:\data`, runs on this PC only.

**Run in Fabric** and **Schedule** hand a finished notebook to Fabric. Run in Fabric publishes the notebook, then starts it on the workspace's Fabric capacity with Fabric's job scheduler; the status bar and **Monitor** follow the run until it finishes. Schedule publishes it and adds a Fabric schedule (every day at set times, some days of the week, or every few minutes, in a time zone you pick), so it runs even when this PC is off; the same dialog lists and removes the notebook's schedules. A notebook that uses `fabric_desktop.lakehouse` is published as a Fabric Python notebook with a setup cell first, which defines the same `lakehouse` module from the libraries Fabric's Python notebooks already have (Polars, deltalake, DuckDB), so the notebook runs there unchanged; its `%%sql` cells become `lakehouse.sql(...)`. The setup cell is only in the published copy. Notebooks that don't use the module, such as the surrogate key templates, are published as they are, for Spark.

**OneLake** is the catalog page. The top lists every item in every workspace you can open (lakehouses, warehouses, notebooks, semantic models, reports and the rest). Search matches names, workspaces, types, descriptions and lakehouse table names. Filter by Favorites, Endorsed, one workspace or an item type, and sort by any column. Lakehouse rows expand to show their tables and files, and open in a notebook. **Show in Fabric** opens the item in the bottom half, which is Fabric's own OneLake catalog in the same embedded Fabric view that Monitor uses.

**SQL query** works like Fabric's SQL query tab. Open it from a lakehouse page, or from **SQL query** on a lakehouse row in OneLake. Write Spark SQL against the lakehouse's tables by the names Fabric uses (`SELECT * FROM sales`, or `schema.table`). The query runs on this PC: sqlglot translates it to DuckDB SQL and DuckDB reads only the columns and files it needs from OneLake, so no Fabric capacity is used. Ctrl+Enter runs the query, or just the selected text. Results show the first 1,000 rows. **Save as table** writes the full result to the lakehouse, **Download CSV** saves it to your Downloads folder, and **Open in notebook** turns the query into `lakehouse.sql(...)` in a new notebook. Query tabs are kept per lakehouse between launches. Queries only read; nothing in the lakehouse changes unless you save a table.

**Monitor** has two halves. The top lists what Fabric Desktop ran: metadata runs in Dev, notebook table writes, and the notebook runs and table maintenance jobs it started in Fabric (with their live status), newest first. The bottom is Fabric's own Monitoring hub, the live page from the Fabric service shown in a second webview inside the app window, so Fabric Desktop adds no monitoring code of its own there. That view signs in with your Windows account when it is the same work account you use in the app, so there is no password prompt. It opens in the tenant of your app sign-in, which skips Fabric's email page. If Windows is signed in with another account, Fabric asks you to sign in once. The app saves that sign-in's cookies, encrypted for your Windows account (DPAPI), in `%LOCALAPPDATA%\FabricDesktop\fabric-view-signin.bin` and restores them on the next launch, so you don't sign in again each time. Delete that file to forget the sign-in. Links that leave Fabric open in your default browser.

Fabric Desktop is by [KratosBI](https://kratosbi.com). The KratosBI logo and app icon in `docs/brand/` and `app/ui/` belong to KratosBI. Interface icons are [Fluent UI System Icons](https://github.com/microsoft/fluentui-system-icons) (MIT).

## Metadata framework

**Metadata** in the left rail manages Bronze to Gold (or any 2 to 10 layers) from config instead of one-off notebooks. A metadata project is a folder you keep in Git:

| File or folder | What goes there |
| --- | --- |
| `layers.toml` | Your layers, in order. Each layer is its own schema-enabled lakehouse, `LH_<Layer>`, in every environment. |
| `metadata/entities/<layer>.<table>.toml` | One file per table: source, contract (columns, types, keys), cleaning rules, derived columns, checks, load mode. |
| `transforms/` | Your Python: hooks, custom checks and custom steps. Shared helpers go in `transforms/common.py`. |
| `tests/` | Tests for your Python. Every hook and custom step must name one. |
| `exceptions.toml` | Approved exceptions to a rule, each with a reason, an approver and an expiry. |
| `metadata/keys/<name>.toml` | One file per business key. Each is a key table `sk.<name>` in `LH_Keys`. |
| `metadata/keys/source_systems.toml` | Every source system (fixed id and code) and its connection in Dev, Test and Prod. |
| `environments.toml` | Where Dev, Test and Prod write, and the Keys workspace that holds each one's `LH_Keys`. Dev writes Delta tables to the project's `.local` folder by default. |

Every run goes through the same steps: read, contract (schema drift and types), surrogate keys, clean, derive, hooks, lookups, checks, write, log. Load modes are full, incremental (watermark plus merge on the key), Type 1 dimension and Type 2 dimension. Dimensions get integer surrogate keys that are never renumbered and an Unknown member at -1; facts look up the key that was current on the fact's date.

### Surrogate keys

Surrogate keys are managed in one place: a lakehouse `LH_Keys` in a dedicated Keys workspace per environment (Dev keeps it on this PC). Each business key has its own key table, `sk.<name>`, with three columns: `<name>_sk` (INT), `<name>_bk` (the business key, unique) and `create_date`. Customer ABC gets one key, say 123, and every source that has Customer ABC maps to 123. A table takes its keys with a `[[keys]]` block:

```toml
[[keys]]
table = "customer"
business_key = "customer_id"     # or a list for a composite key, stored as "a||b"
```

The first step of every run MERGEs the business keys it hasn't seen yet into the key table (continuing from the highest key, never reusing one), then adds `customer_sk` to every row. Rows with no business key get -1. A Type 1 dimension whose rows already carry the key uses it instead of issuing its own. **Metadata > Surrogate keys** lists the key tables with their counts and looks up a business key; an entity's page shows the same MERGE in Spark SQL and **Create template notebook** saves a notebook whose first cell is that MERGE, for running in Fabric. Tick **Surrogate keys: Required** on a layer (for example Silver) to make every table in it use them.

**Metadata > Source systems** keeps each source's id, code, type and owner, and its connection per environment: server, port, database, schema, base URL, file path, authentication, tenant, username, Key Vault URL and secret name, Fabric connection id, gateway and extra options. Passwords and keys never go in it. **Sync to LH_Keys** writes them to `config.source_system` and `config.source_connection` so notebooks and pipelines can look up connection values by code; promote does the same and creates any missing key table, but never copies keys between environments.

Four gates keep the rules followed:

1. **Save:** every entity is checked against its layer's standards. Tables can read earlier layers, or their own layer when there's no loop, never a later one. Columns must exist upstream, so renames and deletes show what breaks. Custom code needs an owner and a test.
2. **Write:** checks run before every commit. `stop` writes nothing, `quarantine` moves failing rows to `quarantine.<schema>_<table>` with the reason, `warn` only records it. Only the framework writes a managed table; notebooks get an error and can use `scratch_` tables.
3. **Promote:** Dev to Test and Test to Prod need a clean save gate and a green last run of every table's current logic. Promote creates the `LH_<Layer>` lakehouses and `LH_Meta`, and pins the environment to that metadata version.
4. **After:** `meta.run_log`, `meta.dq_results`, `meta.watermarks`, `meta.schema_history` and `meta.deployments` in `LH_Meta`, plus `_run_id` and `_loaded_at` on every row and the run ID and metadata version on every Delta commit.

When a table's logic changes, an incremental run stops and asks: apply to new rows only, or rebuild the table and everything downstream. Changing a dimension from Type 1 to Type 2 offers a migration that keeps every key.

Where custom logic goes, simplest first: an expression (`[[derive]]` or a `[[check]]` with `expr`), a hook (`[hooks] after_clean` or `before_write`, a `(df, ctx)` function that declares the tables it `reads` and the columns it `adds`), a custom check (`[[check]] python = ...`), or a custom step (`[step] custom = ...`) that replaces one table's transform while the framework still checks and writes the result.

### This PC or Fabric runs each environment

Each environment card in **Metadata > Environments and promote** has **Runs on: This PC | Fabric**, so Dev, Test and Prod can each run in either place (`compute` in `environments.toml`). Fabric needs the environment's tables in OneLake, so a Dev that writes to `.local` runs on this PC until you point it at a workspace.

With Fabric, **Run all** (and running one entity, backfills and rebuilds) uploads the metadata project and this app's engine to `LH_Meta/Files/_fabric_desktop/current` and starts one notebook in the workspace, `nb_fabric_desktop_runner`, a Fabric Python notebook that runs the same engine against the same lakehouses. The upload is skipped when nothing changed. The app follows the job and shows the results when it finishes; the run log, checks and quarantine land in the same `LH_Meta` tables, so **Runs**, promote and table health don't care where a run happened. The runner fails its Fabric job when a table fails, so Fabric's Monitoring hub shows it too. **Schedule** adds Fabric schedules that run every entity, even when this PC is off, from the project as it was last uploaded; promoting to an environment that runs in Fabric uploads the new version.

Changing settings in `environments.toml` no longer changes the metadata version, so after updating to 0.6.0 promote each environment once to pick up the new version number.

### Profile, table health and the semantic model

**Profile** on an entity's page reads what the entity reads, every row, on this PC, before any cleaning: nulls, distinct values, ranges and lengths, the most common values, and what looks wrong. It turns what it finds into rules the framework already runs: `[[clean]]` blocks to trim spaces, turn values like `N/A` into nulls, fold `Shipped`/`SHIPPED` into one case or parse dates stored as text, and `[[check]]` blocks for not null, unique, accepted values and ranges (warn by default, stop for the table's own key). Tick cleaning rules to see the rows they change before and after, then **Add to entity** appends the ticked blocks to the entity file through the save gate. Problems a rule already handles are marked as handled and not suggested again. **Profile its table instead** profiles what the entity wrote, for checks.

**Metadata > Table health** reads every managed table's Delta log in the chosen environment: files, size, average file size, versions since the last VACUUM, the last OPTIMIZE and whether the files are V-Ordered, with advice for each. In Test and Prod (OneLake), **Optimize** runs Fabric's table maintenance job on the workspace's capacity: OPTIMIZE with V-Order, which Direct Lake and the SQL analytics endpoint read fastest, then VACUUM keeping 7 days. Tables written from this PC have no V-Order until it runs, so by default every table a Test or Prod run writes is optimized right after the run; untick **Optimize every table a run writes** to turn that off for an environment (`optimize_after_run` in `environments.toml`). In Dev, **Compact** does the same on this PC with delta-rs, without V-Order.

**Metadata > Semantic model** builds a Direct Lake semantic model over a layer's tables (the last layer, usually Gold) in Test or Prod. It reads the layer's lakehouse through its SQL analytics endpoint, with no import and no refresh schedule. Each entity is a table, facts get a row count measure, and relationships come from the metadata: a fact's `[[lookup]]` names the dimension and its key, and any other column named like a dimension's surrogate key joins to it. Surrogate keys, `_run_id` and `_loaded_at` are hidden. **Create model** creates `SM_<Layer>` in the environment's workspace (you can rename it); building again updates it in place and keeps every lineage tag, so reports on it keep working. The app asks the SQL endpoint to pick up new tables first.

### Compare, schema drift, quarantine replay and freshness

**Compare** on an entity's page puts the table in two places side by side: Dev against Test or Prod, or the table now against an earlier Delta version. It shows row counts, keys only on one side, which columns changed value for the same key (with the first changed rows, old and new), and how each number column's total moved. Surrogate keys, run stamps and Type 2 dates are left out, since each environment issues its own. Each promote card lists the tables whose logic differs from what last ran in the target, with a Compare for each, so you see what a promote does to the numbers before you press it.

**Schema drift** on an entity's page reads its source now (the files, or the upstream table) and lists what differs from the contract: new columns, columns that are gone, and columns whose type changed. Tick a new column to add it to the contract, mark a gone column as renamed to a new one (written as `[source] rename = { "cust_id" = "customer_id" }`, applied as the source is read) or remove it from the contract, take a column's new type, and choose this table's own policy for new columns (`[contract] drift`, otherwise the layer's). **Save to config** writes the decisions into the entity file through the save gate.

When a check holds rows back, the entity page shows **Quarantine** with the rows and the check that held each one. Fix values in place, or fix the check in the config and save, then **Replay selected**: the rows go through the output contract, the derived columns and every check again, and are written like any run (logged as a `replay` run, without moving the watermark). Rows that still fail stay in quarantine with the new reason. Downstream tables read incrementally, so after a replay the app offers to rebuild them. **Discard selected** removes rows that should never load.

**Freshness** on an entity's page sets when the table should have a successful run: `within_hours`, a daily `by` time (this PC's clock), or both, in the environments you pick (Test and Prod when left out). It is saved in the entity file:

```toml
[freshness]
within_hours = 26
by = "07:00"
envs = ["prod"]
```

Late tables show on Home and in the bell. **Set up Teams alerts** takes the address of a Teams Workflows webhook ("Post to a channel when a webhook request is received"); each late or failed table is then posted once. The app checks at startup and every 10 minutes while it is open.

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

## Headless agent

Run your metadata project on a server with no one signed in. On Windows or Linux:

```bash
pip install ./sidecar
fabric-desktop agent --config agent.toml check            # validate the config and the sign-in
fabric-desktop agent --config agent.toml run              # the service: schedules, queue, retries
fabric-desktop agent --config agent.toml enqueue prod     # run Prod now
fabric-desktop agent --config agent.toml status
```

Setup, every setting, and install steps for systemd, Docker and Windows are in [docs/agent.md](docs/agent.md). The plan it belongs to is in [docs/enterprise-roadmap.md](docs/enterprise-roadmap.md). It has not yet run against a live Fabric tenant.

## Not yet built

An installer, and pipelines (the agent's pipeline hand-off is roadmap M4). From the metadata plan: per-step performance suggestions, re-pointing -1 facts when a late customer arrives, replaying quarantined rows, and running hook tests on promote. Source system connections are a table in `LH_Keys`, not yet a Fabric variable library item.
