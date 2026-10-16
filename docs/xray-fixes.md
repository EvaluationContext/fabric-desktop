# Estate X-ray: what each finding means and how to fix it

The Estate X-ray on Home reads every lakehouse you can reach and lists what needs attention. It only reads: the folders under `Tables`, each Delta table's log, workspace roles, and the cost of runs this app started in Fabric. It changes nothing until you press a fix.

Each finding links here. Every section says what the finding means, why it matters, what the button in the app does, and how to fix it by hand in Fabric if you'd rather.

| Finding | Severity | Button in the app |
| --- | --- | --- |
| [Small files](#small-files) | Fix first (200 or more files), otherwise Worth fixing | **Optimize** |
| [Unidentified tables](#unidentified-tables) | Worth fixing | **Open in Fabric** |
| [Access](#access) | Worth fixing | **Review access** |
| [Cost](#cost) | Worth fixing ($10 or more in 30 days), otherwise Good to know | **Open notebook** or **See cost** |
| [Not changing](#not-changing) | Good to know | **Open in Fabric** |
| [Never vacuumed](#never-vacuumed) | Good to know | **Optimize** |
| [Empty lakehouses](#empty-lakehouses) | Good to know | **Open in Fabric** |

The scan reads at most 500 tables. Scans run when you press **Scan now** or **Scan again** (the very first one on a PC starts by itself), and **Pause** holds a scan where it is.

## Small files

**What it means.** The table has 20 or more data files averaging under 32 MB each. Each small append, streaming micro-batch or row-by-row load adds files, and nothing has combined them.

**Why it matters.** Every query opens every file. Hundreds of small files make Spark, the SQL analytics endpoint and Direct Lake slower, and cost more capacity, than a few large ones holding the same rows.

**What Optimize does.** It runs Fabric's own table maintenance job on the table, on your Fabric capacity:

1. `OPTIMIZE` with V-Order: rewrites the small files into large ones, sorted and compressed the way Direct Lake and the SQL endpoint read fastest.
2. `VACUUM` keeping 7 days: deletes files no version from the last 7 days needs.

Follow the job in **Monitor**. Old versions within those 7 days stay readable (time travel and restore still work).

**By hand.** In Fabric, open the lakehouse, select **...** next to the table, then **Maintenance**. Tick **Run OPTIMIZE command** and **Apply V-Order**, tick **Run VACUUM command** with a retention of 7 days, and select **Run now**. Or in a notebook:

```sql
OPTIMIZE my_schema.my_table VORDER;
VACUUM my_schema.my_table RETAIN 168 HOURS;
```

**To stop it coming back.** Load in larger batches. For tables loaded by Fabric Desktop's metadata framework, Test and Prod already optimize after each run (`optimize_after_run` in `environments.toml`, on by default).

## Unidentified tables

**What it means.** A folder under `Tables` isn't a Delta table: it has no `_delta_log` folder. Usually it holds CSV or Parquet files copied straight into `Tables`.

**Why it matters.** Fabric lists it under **Unidentified**. No engine can query it as a table, and the SQL endpoint and semantic models can't see it.

**What the button does.** **Open in Fabric** opens the lakehouse so you can look at the folder.

**By hand.** Pick one:

- **Files belong in Files.** In the lakehouse explorer, move the folder from `Tables` to `Files`.
- **It should be a table.** Load the files into a Delta table: right-click the folder in `Files` and choose **Load to Tables**, or in a notebook read them and write with `df.write.format("delta").saveAsTable("my_table")`. Then delete the original folder under `Tables`.
- **A table written from outside Fabric** (for example by Fabric Desktop) that is Delta but still shows as Unidentified needs a refresh of the lakehouse in Fabric. In that case the X-ray doesn't list it, since it does have a `_delta_log`.

## Access

**What it means.** One of these, from workspace roles:

- **A guest** (an account from another organization, `#EXT#`) has a role in one or more workspaces.
- **An app** (a service principal) is **Admin** in a workspace.

The X-ray can only read roles in workspaces where you are Admin.

**Why it matters.** Guests keep access after a project ends unless someone removes them. Admin lets an app delete the workspace and change who has access; most apps need Contributor at most.

**What the button does.** **Review access** opens **OneLake > Access**, which lists everyone with a role in every workspace, so you can change or remove it.

**By hand.** In Fabric, open the workspace, select **Manage access**, then change the person's role or remove them. For an app that runs pipelines or notebooks, **Contributor** is enough.

## Cost

**What it means.** A notebook this app ran in Fabric used enough capacity in the last 30 days to be worth noticing. The estimate comes from the run times in Monitor's **Cost** panel and your capacity price (set in **Monitor > Cost**).

**Why it matters.** The same notebook run on this PC uses no Fabric capacity.

**What the buttons do.** **Open notebook** opens it here (when the notebook is on this PC); set **Runs on** to **This PC** in the ribbon and later runs cost nothing. **See cost** opens **Monitor > Cost** with every item's numbers.

**By hand.** Nothing to do in Fabric. If the notebook must stay in Fabric, look for a smaller Spark pool or a shorter session timeout in its environment.

The estimate leaves out Spark session start-up. Fabric's **Capacity Metrics** app has the billed numbers.

## Not changing

**What it means.** The table's last write was 30 or more days ago.

**Why it matters.** If something should still be loading it, that load has stopped, and every report built on it is showing old data. If nothing should, the table may be safe to retire, which saves storage.

**What the button does.** **Open in Fabric** opens the lakehouse.

**By hand.** Find what used to write it: in Fabric, the lakehouse's **Lineage** view shows the pipelines and notebooks connected to it, and the table's history (`DESCRIBE HISTORY my_table` in a notebook) shows the last operation and when. Restart that load, or delete the table if nothing uses it. For metadata tables, a **freshness promise** on the entity alerts you (and Teams) when it goes late.

## Never vacuumed

**What it means.** The table has 30 or more versions and has never been vacuumed.

**Why it matters.** Every write leaves the files it replaced in storage. Until `VACUUM` removes them, you pay OneLake storage for every old version.

**What Optimize does.** The same table maintenance as for [small files](#small-files): `OPTIMIZE` with V-Order, then `VACUUM` keeping 7 days.

**By hand.** As for small files, with only **Run VACUUM command** ticked, or `VACUUM my_table RETAIN 168 HOURS;` in a notebook. Don't go below 7 days: a shorter retention can break queries running at the time, and you lose restore to those versions.

## Empty lakehouses

**What it means.** The lakehouse has no tables and no files.

**Why it matters.** It doesn't cost storage, but it clutters the workspace and the catalog, and people may write to it by mistake.

**What the button does.** **Open in Fabric** opens the lakehouse.

**By hand.** If nothing will use it, in Fabric select **...** next to the lakehouse in the workspace, then **Delete**. Deleting also removes its SQL analytics endpoint and default semantic model. Check first that no shortcut in another lakehouse points at it.
