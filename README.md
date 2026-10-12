# Fabric Desktop

Run data jobs on your own Windows machine and publish the results to Microsoft Fabric as Delta tables, so no Fabric capacity is spent on the transform work.

- **Local compute:** SQL jobs run in DuckDB and Python jobs run in Polars.
- **Direct to OneLake:** delta-rs writes each table straight to `Tables/<schema>/<table>` in a schema-enabled lakehouse. It commits the Delta log only after the data files are written, so Fabric never sees a half-written version.
- **Fabric-safe tables:** every table is checked against a conservative Delta profile (reader 1, writer 2, classic checkpoints, no deletion vectors or column mapping). This keeps tables readable by the SQL analytics endpoint and Direct Lake.
- **Signs in as you:** the app uses delegated MSAL sign-in. Your token cache lives in `%LOCALAPPDATA%\FabricDesktop`.

The architecture and MVP plan are in the [project plan doc](https://claude.ai/code/artifact/284becf8-61f3-4329-a8f5-0a28c3059efe).

## Layout

| Path | What it is |
| --- | --- |
| `sidecar/` | Python package `fabric_desktop`: auth, Fabric REST client, OneLake addressing, job runner, publisher, JSON-lines server |
| `app/` | Tauri 2 shell (`src-tauri/`) and the Fabric-style UI (`ui/`, plain HTML and JavaScript). The shell starts the sidecar and forwards UI actions to it |
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

Download [fabric-desktop.exe](https://github.com/KratosDataGod/fabric-desktop/releases/download/latest/fabric-desktop.exe) from the **Releases** section of the repository page and double-click it. Every green build of `main` replaces it with a fresh copy. Windows SmartScreen may warn because the exe is not signed yet; choose **More info > Run anyway**. The app starts the sidecar with the `python` on your PATH, so install the sidecar first (above). To use a different interpreter, set `FABRIC_DESKTOP_PYTHON` to its full path.

To build it yourself instead, install Rust and the [Tauri prerequisites](https://v2.tauri.app/start/prerequisites/), then:

```powershell
$env:FABRIC_DESKTOP_PYTHON = (Get-Command python).Source   # the Python that has the sidecar installed
cd app\src-tauri
cargo run
```

## Notebooks

The app looks and works like the Fabric service: the same left rail (Home, Create, Workspaces, OneLake, Monitor), and Python notebooks with a ribbon, an Explorer pane and **Add data items** to attach a lakehouse. Cells run in a Python kernel on this PC, so no Fabric capacity is used.

```python
import polars as pl
from fabric_desktop import lakehouse

sales = lakehouse.read_csv("Files/raw/sales.csv")      # downloaded from the attached lakehouse
by_region = sales.group_by("region").agg(pl.col("amount").sum())
lakehouse.write_table(by_region, "dbo.sales_by_region")  # Delta table written straight to OneLake
```

- `read_csv`, `read_parquet` and `read_json` read `Files/...` paths from the attached lakehouse; any other path is a file on this PC.
- `read_table("schema.table")` and `write_table(df, "schema.table", mode="overwrite" | "append")` work with Delta tables. The schema defaults to `dbo`. Writes are checked against the Fabric-safe Delta profile and show up in **Monitor**.
- A cell's last expression is shown below it. Data frames (Polars, pandas, Arrow, DuckDB) show as a table of the first 100 rows.
- **Stop** ends the running cell by restarting the kernel, which clears its variables.
- Notebooks autosave as `.ipynb` files in `%LOCALAPPDATA%\FabricDesktop\notebooks`. **Publish to OneLake** also saves the notebook as a notebook item in the attached lakehouse's workspace.

Icons are [Fluent UI System Icons](https://github.com/microsoft/fluentui-system-icons) (MIT).

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

Merge (upsert) writes, a bundled Python runtime and installer, scheduling, pipelines, and running published notebooks in Fabric (they use the local `fabric_desktop.lakehouse` API).
