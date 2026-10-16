# Metadata framework guide

The metadata framework builds and runs your medallion layers (Bronze, Silver, Gold, or any 2 to 10 layers you choose) from configuration instead of one-off notebooks. You describe each table once in a small file: where its data comes from, its columns, how to clean and check it, and how to load it. Fabric Desktop runs every table the same way, checks it, writes it to its layer's lakehouse, and logs what happened.

This guide goes from an empty folder to Prod. The [reference](#reference) at the end lists every setting.

> **Tested and not yet tested.** Everything that runs on your PC, including Dev on the PC, the four gates and every load mode, is covered by automated tests on every build. Features that talk to Fabric (promote to Test and Prod, runs in Fabric, Optimize, the semantic model, history and restore, the variable library) are built to Fabric's documented APIs but haven't yet been tried on a live tenant. Several are tagged **Preview** in the app.

## Ideas in one page

| Idea | What it means |
| --- | --- |
| **Project** | A folder you keep in Git. Every table is a file in it. |
| **Layer** | A stage of your data (Bronze, Silver, Gold). Each layer is its own schema-enabled lakehouse, `LH_<Layer>`, in every environment. Layers never mix. |
| **Entity** | One managed table, described by `metadata/entities/<layer>.<table>.toml`. |
| **Environment** | Dev, Test and Prod. Each has its own workspace (Dev can stay on your PC). |
| **Run** | Reading, checking and writing one or more entities in one environment. Every run goes through the same steps: read, contract, surrogate keys, clean, derive, hooks, lookups, checks, write, log. |
| **Gates** | Rules enforced at four points (save, write, promote, after) so the standards are followed without anyone policing them. |
| **Promote** | Moving a tested metadata version from Dev to Test, or Test to Prod. |
| **LH_Meta** | A lakehouse per environment that holds the run log, check results, watermarks, schema history and deployments. |
| **LH_Keys** | A lakehouse per environment, in a shared Keys workspace, that issues surrogate keys. |

## 1. Set up a project

1. Open **Metadata** in the left rail. With no project yet, it shows **Set up a metadata project**.
2. **Folder:** where the project goes. The default is `Documents\FabricDesktop\metadata`. Choose a folder inside a Git repository if you have one.
3. **Layers:** pick 2 (raw, curated), 3 (bronze, silver, gold) or 5 (landing, bronze, silver, gold, semantic). You can add, remove, rename and reorder later.
4. **Add the sample tables:** with 3 layers, tick this to get customers and orders from Bronze to Gold with sample CSV files, so you can try everything straight away.
5. **Create project.**

To open a project someone else made (for example, after `git clone`), use **Or open an existing project** and give the folder that has `layers.toml`.

A new project looks like this:

```
layers.toml                     your layers, in order, and each one's rules
environments.toml               Dev, Test and Prod: workspace, where it runs
exceptions.toml                 approved exceptions to a rule
metadata/entities/*.toml        one file per table
metadata/keys/*.toml            one file per business key, and source_systems.toml
transforms/                     your Python: hooks, custom checks, custom steps
tests/                          tests for that Python
.local/                         Dev's tables on this PC (in .gitignore)
```

## 2. Run Dev on your PC

A new project's Dev keeps its tables in the `.local` folder, so you can build and run everything with no Fabric workspace at all.

1. Pick **Dev** in the top bar's **Environment** picker.
2. **Run all** (top right) runs every entity in layer order. A dialog shows each table's result: rows written, rows quarantined, or why it failed.
3. **Details** on a result, or any row on the **Runs** tab, shows each step with rows and time, and each check with how many rows failed.

With the sample project, the first Run all loads customers and orders through Silver into a Type 2 customer dimension and a sales fact, quarantines a returned order whose amount isn't negative, and warns about an order whose customer doesn't exist yet.

When you're ready to use Fabric for Dev, assign it a workspace (step 7) and switch **Tables in** to **Workspace**.

## 3. Design your layers

**Metadata > Design > Layers** lists your layers in order, with each one's rules. Change them, then **Save layers**.

| Column | What it sets |
| --- | --- |
| **Layer** | The name. The lakehouse is `LH_<Name>` in every environment. |
| **Can read from** | **Any earlier layer**, or **Only** one earlier layer. The first layer reads files and sources. |
| **Schema drift** | What happens when a source has a column the contract doesn't: **Keep new columns**, **Ignore new columns**, or **Stop on new columns**. An entity can override this. |
| **Required checks** | **Key not null** and **Key unique**: every table in the layer must have these checks on its key. |
| **Surrogate keys: Required** | Every table in the layer must take its keys from LH_Keys. |
| **Keep history** | How many days back a table can be restored (at least 7). VACUUM keeps files this long. |
| **↑ ↓** and **Remove** | Reorder, or remove a layer with no entities. |

**Add layer** adds one at the end (up to 10). Reordering is refused if a table would end up reading a later layer.

Rules that always hold: a table writes only to its own layer's lakehouse; it reads from earlier layers, or from its own layer when that makes no loop (a fact reading its dimensions); reading a later layer is always rejected.

## 4. Add entities

**New entity** (on any Metadata page, or Ctrl+K) asks for:

| Field | What to put |
| --- | --- |
| **Template** | What kind of table (below). The file it writes already meets your layer's standards. |
| **Layer** and **Table name** | Together they make the entity name, such as `silver.orders`. |
| **Source** | For files: a pattern under the layer's lakehouse `Files` area, like `landing/orders/*.csv`. Otherwise another entity, like `bronze.orders`. |
| **Key columns** | The business key, comma separated. |

| Template | Makes |
| --- | --- |
| Ingest files | Reads CSV, Parquet or JSON as they arrived. Columns come from the first matching file in Dev, if there is one. |
| Clean | Cleans, deduplicates and merges changed rows from another table. Incremental when the source has a `modified_at`-style column. |
| Type 1 dimension | Overwrites changes, with integer surrogate keys that are never renumbered. |
| Type 2 dimension | Keeps history with valid from and to dates. |
| Fact | Looks up each dimension's surrogate key as of the fact's date; unknowns get -1. |
| Custom Python step | Creates `transforms/<table>.py` and a test. You write the transform; the framework checks and writes the result. |

The entity page opens. From there:

- **What happens on every run** shows the steps for this table. Grey steps are the framework; purple is your code, at a fixed point.
- **Config** is the TOML file. Edit and **Save** (or **Ctrl+S**). Saving runs the save gate: problems show above the editor and the table can't run or be promoted until they're fixed.
- **Contract** lists the output columns and marks keys, surrogate keys, derived columns and columns your hooks add.
- **Lineage** shows what it **Reads**, the table itself, and what it **Feeds**. Click a table to open it.
- **Recent runs** shows its last runs in every environment.
- **Data in <environment>** shows a sample of the table as written.
- **Run in Dev** (or the picked environment) saves and runs just this table. **Rebuild with downstream** reprocesses it in full and then every table that reads it, in order.
- **Delete** removes the file. The Delta table stays in the lakehouse until you delete it there.

### Where custom logic goes

Simplest first. The framework still runs every other step, checks the result and writes it.

1. **An expression:** `[[derive]]` adds a column from a SQL expression; `[[check]]` with `expr` is a row-level rule.
2. **A hook:** `[hooks] after_clean` or `before_write` names a function `(df, ctx)` that returns rows. Declare the tables it `reads` and the columns it `adds`.
3. **A custom check:** `[[check]] python = "transforms.checks:valid_email"` returns true for each good row.
4. **A custom step:** `[step] custom = "transforms.sales:build"` replaces this table's transform.

Every hook, custom check and custom step needs an `owner` and a `test`, or the save gate stops it. Shared helpers go in `transforms/common.py`.

## 5. Load modes and decisions

| Mode | `[load] mode` | What a run does |
| --- | --- | --- |
| Full reload | `full` | Replaces the table with what the source has now. |
| Incremental | `incremental` | Reads only rows whose `watermark` column is past the last run's, and merges them on the key. |
| Type 1 dimension | `scd1` | Merges on the key, overwriting changes. Surrogate keys are issued once and never renumbered. An Unknown member is at -1. |
| Type 2 dimension | `scd2` | A change in a `tracked` column closes the current row and adds a new version with `valid_from`, `valid_to` and `is_current`. Columns in `type1` are overwritten on every version. |

Facts use `[[lookup]]` to take a dimension's surrogate key as it was on the fact's date. A fact row whose dimension member doesn't exist gets -1.

**When a table's logic changes**, an incremental run stops and asks you to choose:

- **Apply to new rows only:** rows already loaded keep the old logic. `_run_id` shows which rows used which version.
- **Rebuild it and everything downstream** (recommended): reprocesses the full source, then every table that reads it, in order.
- **Migrate to Type 2** (when a dimension changes from Type 1 to Type 2): keeps every surrogate key and starts each row's history at 1900-01-01.

The table shows **Needs a decision** on Home and in Runs until you choose.

## 6. Quality: checks, quarantine and the four gates

Checks run before every write. Each has an `on_fail`:

| `on_fail` | What happens to failing rows |
| --- | --- |
| `stop` (default) | Nothing is written. The run fails with the check's name and how many rows failed. |
| `quarantine` | Failing rows go to `quarantine.<schema>_<table>` in the same lakehouse, with the check that held each one. The rest are written. |
| `warn` | Everything is written; the failures are recorded. |

**The four gates:**

1. **Save:** every entity is checked against its layer's standards. Columns must exist upstream, so renames and deletes show what breaks. Custom code needs an owner and a test.
2. **Write:** checks run before every commit. Only the framework writes a managed table; notebooks get an error and can use `scratch_` tables.
3. **Promote:** Dev to Test and Test to Prod need a clean save gate, passing tests, and a green last run of every table's current logic.
4. **After:** `meta.run_log`, `meta.dq_results`, `meta.watermarks`, `meta.schema_history` and `meta.deployments` in `LH_Meta`, plus `_run_id` and `_loaded_at` on every row and the run id and metadata version on every Delta commit.

**Exceptions.** When a rule genuinely doesn't fit, add an approved exception to `exceptions.toml` with the entity, the rule, a reason, an approver and an expiry date (or `permanent`). An expired exception fails the save gate again.

```toml
[[exception]]
entity = "gold.fact_sales"
rule = "custom_code_test"
reason = "Signed off by finance; test due next sprint"
approver = "Chris"
expires = "2026-11-01"
```

### Tools on the entity page

| Button | What it does |
| --- | --- |
| **Profile** | Reads what the entity reads, every row, on your PC, before cleaning: nulls, distinct values, ranges, lengths, most common values and what looks wrong. It suggests `[[clean]]` rules (trim, turn `N/A` into nulls, fold `Shipped`/`SHIPPED`, parse dates stored as text) and `[[check]]` rules (not null, unique, accepted values, ranges). Tick cleaning rules to see rows before and after; **Add to entity** appends the ticked blocks through the save gate. **Profile its table instead** profiles what was written. |
| **Compare** | Puts the table in two places side by side: Dev against Test or Prod, or now against an earlier Delta version. Row counts, keys only on one side, columns that changed value for the same key, and how each number column's total moved. |
| **History** (Preview) | Every Delta version: when, what happened, which run. **Restore** brings back an earlier version (as a new version, so it can be undone too), moves an incremental table's watermark back, and offers to run the tables that read it. |
| **Schema drift** card | Reads the source now and lists new, gone and retyped columns. Add a new column to the contract, mark a gone column as renamed (`[source] rename`), take a new type, or set this table's drift policy. **Save to config** writes it through the save gate. |
| **Freshness** card | See [Freshness promises](#freshness-promises). |
| **Quarantine** card | Shows held-back rows and the check that held each. Fix values in place, or fix the check and save, then **Replay selected**: they go through the contract, derived columns and checks again, and are written as a `replay` run without moving the watermark. **Discard selected** removes rows that should never load. After a replay the app offers to rebuild downstream tables. |
| **Cell 1: surrogate keys** card | The Spark SQL MERGE that issues this table's keys, for running in Fabric. **Create template notebook** makes a notebook that starts with it. |

### Freshness promises

Freshness says when a table should have had a successful run, in the environments you pick (Test and Prod when left out):

```toml
[freshness]
within_hours = 26     # a successful run in the last 26 hours
by = "07:00"          # and one by 7:00 each day, this PC's clock
envs = ["prod"]
```

Late tables show on Home and in the bell. To get them in Teams, **Settings > Notifications > Teams alerts** (or **Set up Teams alerts** on the card) takes the address of a Teams Workflows webhook ("Post to a channel when a webhook request is received"). Each late or failed table is posted once. The app checks at startup and every 10 minutes while it's open.

### Tests (Preview)

**Metadata > Quality > Tests** runs the `test_*.py` files in the project's `tests` folder. A test is a function named `test_something` that uses `assert`, as in pytest; it can take `tmp_path` for a scratch folder. Each run imports `transforms/` and `tests/` fresh, so it tests what you just saved. Promote runs them first and won't promote while one fails.

### Table health (Preview)

**Metadata > Quality > Table health** reads every managed table's Delta log in the picked environment: files, size, average file size, V-Order, versions since the last VACUUM and the last OPTIMIZE, with advice.

- In **Test and Prod**, **Optimize** runs Fabric's table maintenance job on the workspace's capacity: OPTIMIZE with V-Order (what Direct Lake and the SQL endpoint read fastest), then VACUUM keeping the layer's history days. Tables written from a PC have no V-Order until it runs, so by default every table a Test or Prod run writes is optimized right after the run. Untick **Optimize every table a run writes** to stop that.
- In **Dev on this PC**, **Compact** does the same with delta-rs, without V-Order.

## 7. Environments and promote

**Metadata > Deploy > Environments and promote** has a card for Dev, Test and Prod.

| On the card | What it does |
| --- | --- |
| **Assign workspace** / **Change** | Picks the environment's workspace, or **Create a new workspace** on a capacity you choose. The layer lakehouses and `LH_Meta` live there. |
| **Tables in: Workspace / This PC only** | Whether the environment reads and writes the workspace's lakehouses or the project's `.local` folder. |
| **Runs on: This PC / Fabric** | Where its runs execute. See below. |
| **Schedules** (Preview) | Runs every entity on a schedule, on this PC or in Fabric. |

Below the cards are **Promote Dev to Test** and **Promote Test to Prod**. Each shows whether the promote gate passes and, if not, what to do first. It also lists the tables whose logic differs from what last ran in the target, each with **Compare**, so you see what a promote does to the numbers before you press it.

**Promote to Test** (or Prod):

1. Checks the gate: the project passes the save gate, the tests pass, and every table's last run in the source environment is green on this exact metadata version.
2. Creates any missing lakehouses (`LH_<Layer>` for each layer, and `LH_Meta`) in the target workspace, and `LH_Keys` with any missing key tables in the Keys workspace.
3. Syncs source systems to `LH_Keys` and publishes the `VL_Sources` variable library.
4. Pins the target to this metadata version, recorded in `meta.deployments`.
5. If the target runs in Fabric, uploads the new version for its runner.

Promote copies **configuration**, never data and never keys. Each environment issues its own surrogate keys from its own `LH_Keys`, and Test and Prod load from their own sources. After a promote, run the target to load it.

### This PC or Fabric runs each environment

Each environment card has **Runs on: This PC | Fabric**, saved as `compute` in `environments.toml`. Fabric needs the tables in OneLake, so an environment with **This PC only** tables runs on the PC until you point it at a workspace.

With **Fabric** (Preview), **Run all** (and running one entity, backfills and rebuilds) uploads the metadata project and the app's engine to `LH_Meta/Files/_fabric_desktop/current`, and starts one notebook in the workspace, `nb_fabric_desktop_runner`. It's a Fabric Python notebook that runs the same engine against the same lakehouses. The upload is skipped when nothing changed. The app follows the job and shows the results when it finishes; the run log, checks and quarantine land in the same `LH_Meta` tables, so it doesn't matter where a run happened. A Fabric schedule runs every entity even when your PC is off, from the project as it was last uploaded.

## 8. Surrogate keys

Surrogate keys are issued in one place per environment: `LH_Keys`, in a dedicated Keys workspace (Dev keeps it on the PC). Each business key has its own key table, `sk.<name>`, with three columns: `<name>_sk` (INT), `<name>_bk` (the business key, unique) and `create_date`. Customer ABC gets one key, say 123, and every source that has Customer ABC maps to 123.

**Metadata > Design > Surrogate keys:**

- **New key table** adds one, for example `customer`.
- **Pick Keys workspace** (or **Change**) sets where `LH_Keys` lives for the picked environment.
- The list shows each key table's key count, next key, and which tables use it. Select one to **look up a business key**, see its attributes, and copy the block that makes an entity use it.
- **Sync to LH_Keys** creates any missing key tables and writes the source system configuration.

An entity takes keys with a `[[keys]]` block. The first step of every run MERGEs business keys it hasn't seen into the key table (continuing from the highest key, never reusing one), then adds `<name>_sk` to every row. Rows with no business key get -1 (the Unknown member). A composite key is a list, stored as `"a||b"`.

```toml
[[keys]]
table = "customer"
business_key = "customer_id"     # or ["source_code", "customer_id"]
```

## 9. Source systems and the variable library

**Metadata > Design > Source systems** keeps every source's fixed id, code (like `CRM`), name, type, owner and active flag, and its connection in Dev, Test and Prod: server, port, database, schema, base URL, file path, authentication, tenant, username, Key Vault URL and secret name, Fabric connection id, gateway and extra options. Switch environment with the Dev / Test / Prod buttons above the connection fields.

**Passwords and keys never go here.** Give the Key Vault URL and the secret's name; the notebook reads the secret when it runs.

**Sync to LH_Keys** writes them to `config.source_system` and `config.source_connection`, one row per source per environment, so notebooks and pipelines can look up connection values by code.

**Variable library VL_Sources** (on the same tab) is one Fabric variable library per environment workspace, with that environment's values active. **Publish to Dev / Test / Prod** creates or updates it; promote updates it in the target. Notebooks read the same names in Fabric and on your PC:

```python
lib = notebookutils.variableLibrary.getLibrary("VL_Sources")
lib.CRM_server_host
```

## 10. Semantic model (Preview)

**Metadata > Deploy > Semantic model** builds a Direct Lake semantic model over a layer's tables (the last layer, usually Gold) in Test or Prod. It reads the layer's lakehouse through its SQL analytics endpoint, with no import and no refresh schedule.

- Each entity is a table; dimensions and facts are marked. Facts get a row count measure.
- Relationships come from the metadata: a fact's `[[lookup]]` names the dimension and key, and any other column named like a dimension's surrogate key joins to it.
- Surrogate keys, `_run_id` and `_loaded_at` are hidden.
- **Create model** makes `SM_<Layer>` in the environment's workspace (rename it if you like). **Update model** updates it in place and keeps every lineage tag, so reports built on it keep working.

Dev on the PC has no Direct Lake model: switch the environment to Test or Prod.

## 11. Bring existing tables under management

To manage tables that already exist in a lakehouse, use **Adopt** on the lakehouse in the Estate X-ray on Home (Preview). It writes an entity for each table with `[source] table` pointing at the existing table and a contract from its columns. Review the files, then run them.

## 12. Run on a schedule or a server

- **On this PC or in Fabric:** **Schedule** at the top of Metadata, or **Schedules** on an environment card (Preview). Listed in **Monitor > Schedules**.
- **On a server, unattended:** the [headless agent](agent.md) runs the same engine on Windows or Linux as a service principal or managed identity, with a durable queue and retries. It hasn't yet run against a live tenant.

## Reference

### Entity file

`metadata/entities/<layer>.<table>.toml`. An example with the common sections:

```toml
name = "silver.orders"
owner = "data-team@contoso.com"
description = "Orders, cleaned"

[source]
entity = "bronze.orders"          # or files = "landing/orders/*.csv", or table = "<abfss uri>"
rename = { "cust_id" = "customer_id" }

[load]
mode = "incremental"              # full | incremental | scd1 | scd2
watermark = "modified_at"

[contract]
keys = ["order_id"]
drift = "ignore_new"              # evolve | ignore_new | strict; overrides the layer

[contract.columns]
order_id = "int64"
customer_id = "int64"
order_date = "date"
amount = "float64"
status = "string"
modified_at = "date"
net_amount = "float64"

[[clean]]
column = "status"
trim = true
case = "lower"

[[clean]]
dedupe_keep = "newest:modified_at"

[[derive]]
column = "net_amount"
expr = "amount - coalesce(discount, 0)"

[[check]]
column = "order_id"
not_null = true
unique = true
on_fail = "stop"

[[keys]]
table = "order"
business_key = "order_id"
```

| Section | Key | Meaning |
| --- | --- | --- |
| top level | `name`, `owner`, `description` | The entity's name (`layer.table`), who owns it, and what it is |
| `[source]` | `files` | A file pattern under the layer's lakehouse `Files` |
| | `entity` | Another entity it reads |
| | `table` | An existing Delta table, by OneLake URI (adopted tables) |
| | `rename` | Source column names mapped to the contract's names |
| `[load]` | `mode` | `full`, `incremental`, `scd1` or `scd2` |
| | `watermark` | The column incremental loads move forward on |
| `[contract]` | `keys` | The business key columns |
| | `drift` | This table's schema drift policy |
| `[contract.columns]` | `<column> = "<type>"` | `string`, `int64`, `float64`, `bool`, `date`, `datetime`, `decimal(p,s)` |
| `[[clean]]` | `column` plus `trim`, `case` (`lower`, `upper`, `title`), `replace` (`{ "old" = "new" }`), `null_if` (a list), `parse_date` (formats like `"dd/MM/yyyy"`), `fill_null`, `round` | Cleaning rules, applied in order |
| | `dedupe_keep` | `"newest:<column>"` or `"oldest:<column>"`: keep one row per key |
| `[[derive]]` | `column`, `expr` | A new column from a SQL expression |
| `[[check]]` | `column` with `not_null`, `unique`, `accepted_values` (a list), `range` (`{ min = 0, max = 100 }`) or `exists_in` (`"layer.table.column"`) | Column checks |
| | `name`, `expr` | A row rule as a SQL expression |
| | `python`, `owner`, `test` | A custom check |
| | `on_fail` | `stop`, `quarantine` or `warn` |
| `[[keys]]` | `table`, `business_key` | Surrogate keys from `LH_Keys` |
| `[[lookup]]` | `dim`, `on`, `as`, `at` | A dimension's surrogate key as of the fact's date |
| `[scd]` | `surrogate_key`, `tracked`, `type1` | Dimension settings |
| `[hooks]` | `after_clean`, `before_write`, `reads`, `adds`, `owner`, `test` | Your Python at a fixed point |
| `[step]` | `custom`, `reads`, `owner`, `test` | Your Python replaces the transform |
| `[params]` | any | Values your code reads from `ctx.params` |
| `[freshness]` | `within_hours`, `by`, `envs` | When a successful run is due |

Every written row also gets `_run_id` and `_loaded_at`.

### layers.toml

An optional `[rules]` section (`same_layer_reads = "dag"` lets a table read its own layer when that makes no loop; `"never"` forbids it), then one `[[layer]]` per layer, in order, with `name`, `description`, `drift`, `required_checks` (`key_not_null`, `key_unique`), `reads_from`, `require_keys` and `retention_days`. The Layers tab writes this file for you.

### environments.toml

One section per environment (`[dev]`, `[test]`, `[prod]`):

| Key | Meaning |
| --- | --- |
| `workspace_id`, `workspace_name` | The environment's workspace |
| `target` | `local` (the `.local` folder) or `onelake` (the workspace) |
| `compute` | `local` (this PC) or `fabric` |
| `keys_workspace_id`, `keys_workspace_name` | Where `LH_Keys` lives |
| `optimize_after_run` | Optimize every table a run writes (Test and Prod) |

Changing this file doesn't change the metadata version, so it never forces a re-promote.

### Columns and tables the framework adds

| Where | What |
| --- | --- |
| Every row | `_run_id`, `_loaded_at` |
| Type 2 dimensions | `valid_from`, `valid_to`, `is_current` |
| Dimensions | The surrogate key column, and an Unknown member at -1 |
| `LH_Meta` | `meta.run_log`, `meta.dq_results`, `meta.watermarks`, `meta.schema_history`, `meta.deployments` |
| Each layer lakehouse | `quarantine.<schema>_<table>` for held-back rows |
| `LH_Keys` | `sk.<name>` per key table; `config.source_system`, `config.source_connection` |
