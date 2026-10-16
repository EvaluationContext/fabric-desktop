# Fabric Desktop user guide

Fabric Desktop runs your data work on your own PC and keeps the results in Microsoft Fabric's OneLake. It looks and works like Fabric, so most pages will feel familiar. This guide walks through every page: what it is for, how to use it, and what happens behind it.

The app's **Learn more** links open the sections of this page. Two areas have their own, longer guides:

- **[Notebooks and SQL](notebooks.md):** writing and running notebooks on your PC, Fabric code that works here, publishing, Run in Fabric and schedules, and the SQL query tab.
- **[Metadata framework](metadata-guide.md):** building and running medallion layers from config, step by step, with a reference for every setting.

New here? Install first ([install.md](install.md)). Setting it up for others? Start with [Configure Azure and Fabric](azure-setup.md).

**Where things run.** Unless a button says Fabric, work runs on your PC and uses no Fabric capacity: notebooks, SQL queries, metadata runs in an environment set to **This PC**, profiling, comparing and testing. Buttons that say **Fabric** (Run in Fabric, Fabric schedules, Optimize in Test or Prod, semantic models, environments set to **Runs on: Fabric**) use your workspace's capacity.

## The window

| Part | Where | What it does |
| --- | --- | --- |
| **Left rail** | Left edge | The pages: Home, Create, Workspaces, OneLake, Metadata, Fabric, Monitor, and Settings at the bottom. Below them are the items you opened recently (a workspace, a SQL query, a notebook), each with an **×** to close it. Closing a notebook saves it first. |
| **Search box** | Top bar | Opens the command bar (below). When you pick **Show only items matching ...**, the current page is filtered by that text until you clear the box. |
| **Environment picker** | Top bar, on Metadata pages only | Dev, Test or Prod. Every Metadata page shows the environment picked here. The pill next to it says where that environment runs, for example "Runs in Fabric · Sales - Test"; click it to change that. |
| **Bell** | Top right | The activity tray (below). The number on it counts what is running plus new problems; it turns red when there's a problem you haven't seen. |
| **Account circle** | Top right | Your initial, or **?** when signed out. Opens the account menu. |

### Command bar (Ctrl+K)

Press **Ctrl+K** anywhere, or click the search box. Type a few letters to find:

- **Pages**, such as Monitor or Settings.
- **Actions**, such as New notebook, New lakehouse, Run all entities, Metadata: Surrogate keys, OneLake shortcuts, Report a problem, What's new or Sign out.
- **Notebooks** on this PC, **workspaces** you can open, and **entities** in your metadata project.

Use the arrow keys and **Enter**, or click. **Esc** closes it. Actions for preview features only show when preview features are on.

### The bell: activity

The bell keeps a list of what happened in this app on this PC:

- **Runs that are still going**, including ones in Fabric. It follows a Fabric run until it ends, even if you go to another page, and tells you how it ended.
- **Results you can reopen**, such as the result of a Run all.
- **Every error**, explained in a sentence ("Your sign-in expired", "You don't have permission for that in this workspace"), with the technical text folded under **Details**. When you report a problem, the most recent errors are included for you.

### Account menu

Click your initial at the top right:

| Item | What it does |
| --- | --- |
| Signed in as ... | Who you are signed in as |
| **Settings** | Opens [Settings](#settings) |
| **Help and user guide** | Opens this guide |
| **Report a problem** | See [Report a problem](#report-a-problem) |
| **What's new** | The release notes of the build you're running |
| Build and channel | Which build you're on, and Stable or Preview |
| **Sign out** | Forgets your sign-in on this PC, in the app and in the embedded Fabric pages. Notebooks and settings stay. |

## Get started

1. **Sign in** with your work account, the same one you use for Fabric. The first time, Fabric Desktop may ask for an **App (client) ID**; your Fabric admin has it. If sign-in says your organization needs to approve Fabric Desktop, send your admin the link from **Settings > Account > Admin approval**.
2. **Open a lakehouse** from **OneLake** or **Workspaces** to browse its tables and files, or query them with SQL.
3. **Create a notebook** from **Create**. Cells run on this PC, not on your Fabric capacity.
4. **Set up Metadata** when you want Fabric Desktop to build and run your medallion layers for you.

Home shows a **Get started** list with these steps until you have a metadata project. Each step has a **Start** button and gets a tick when done. **Hide** removes the list.

## Home

Home answers "what happened since I was last here". From the top:

| Section | What it shows |
| --- | --- |
| **Summary line** | "Nothing needs your attention", or how many things do, and how many are running. |
| **Needs your attention** | Tables whose latest run failed or is waiting for a decision, in any environment, with **Open entity** and **Open run**. Fabric runs that failed in the last day, with **Open in Fabric**. Tables that are late for their [freshness promise](metadata-guide.md#freshness-promises). |
| **Running now** | What this app is running, here or in Fabric. |
| **Coming up** | The next scheduled runs, on this PC and in Fabric. **All schedules** opens Monitor > Schedules. |
| **Environments** | Dev, Test and Prod: where each runs, and "All green", "2 failed", "Running" or "Never run". Click one to see its runs. Before you have a metadata project, this space shows **Get started** instead. |
| **Start something** | New notebook, New lakehouse, OneLake catalog. |
| **Recent notebooks** | Your last eight notebooks. Click to open. The section folds with its arrow. |
| **Estate X-ray** (Preview) | See below. |

### Estate X-ray (Preview)

The X-ray reads every lakehouse you can reach and lists what needs attention, worst first: too many small files, tables Fabric shows as Unidentified, wide access, cost, tables that stopped changing, tables never vacuumed and empty lakehouses. It only reads; nothing changes until you press a fix on a line.

- The first scan on a PC starts by itself after you sign in. After that, press **Scan again** when you want fresh results. **Pause** holds a scan where it is.
- The card folds to one line with the totals, and stays as you left it.
- **Your lakehouses** lists every lakehouse it read. **Adopt** on a lakehouse turns its tables into metadata entities, so the framework manages them from then on.
- Every finding and its fix is explained in [xray-fixes.md](xray-fixes.md).

## Create

New items:

- **Notebook:** a new Python notebook, saved on this PC. See [Notebooks](notebooks.md).
- **Lakehouse:** asks for a workspace and a name, then creates a schema-enabled lakehouse in Fabric (tables live under schemas, `dbo` by default) and opens it. Needs a workspace on a capacity, and Contributor or higher on it.

## Workspaces

Every Fabric workspace you can open.

- **+** next to a workspace lists its items. Each item opens in the [Fabric page](#fabric); lakehouses also have **Open** and **SQL query**.
- The **pin** keeps a workspace at the top, in the order you pinned them. The **star** marks a favorite. The chips at the top show **All**, **Favorites** or **Pinned**. Pins and favorites are kept on this PC.
- Click a workspace's row to open it: its lakehouses, with **New lakehouse**, **New notebook** and **Open in notebook**.
- **Open in Fabric** opens the workspace in the Fabric page.

### A lakehouse

Opening a lakehouse shows its **Tables** (by schema) and **Files** (folders and file sizes) as a tree, with **Open in new notebook** and **New SQL query**.

## OneLake

The OneLake page has three tabs: **Catalog**, and the preview tabs **Shortcuts** and **Access**.

### Catalog

Every item in every workspace you can open: lakehouses, warehouses, notebooks, semantic models, reports, pipelines and the rest.

- **Search** matches names, workspaces, types, descriptions and the table names inside lakehouses. Table names are read in the background, so search by table works a few seconds after the page opens.
- **The left side** narrows the list: All items, My workspace, Endorsed items, Favorites, or one workspace (**More workspaces...** shows the rest, and **Find a workspace** filters them).
- **Type** narrows by item type, grouped as Fabric groups them (Data, Insights, Processes, Solutions, Configurations). Pick a group or open it to pick one type. **Clear** resets it.
- **Click a column heading** to sort by it; click again to reverse.
- **Star** an item to make it a favorite. Fabric has no public API for its own favorites, so these are kept on this PC.
- **Endorsement** (Certified or Promoted) comes from Fabric's admin scan. It only shows when you are a Fabric administrator and the app has `Tenant.Read.All`. Otherwise Endorsed items is greyed out with the reason.
- **Layer** shows which metadata layer a lakehouse is, when it belongs to your metadata project.
- On a lakehouse row, **▸** shows its tables and files, **SQL query** opens a query tab, and **Open in notebook** starts a notebook with it attached. **Show in Fabric** (any item) opens it in the Fabric page.
- **Open Fabric's catalog** opens Fabric's own OneLake catalog.

Workspaces you can't read are skipped and named at the top.

### Shortcuts (Preview)

Shortcuts are tables or folders in a lakehouse that point at data somewhere else, without copying it.

- Pick **Every lakehouse** or one lakehouse on the left to list its shortcuts: name, where it lives (`Tables/<schema>` or `Files/...`), and what it points at.
- **New shortcut** (pick a lakehouse first): choose **Put it in** (a table under a schema, or a folder under Files), a **Name**, and what it **Points at**:
  - **OneLake:** another lakehouse's table, or a folder in it.
  - **ADLS Gen2** or **Amazon S3:** a Fabric cloud connection, the location URL and a subpath. Connections are made in Fabric (**Settings > Manage connections and gateways**).
- **Remove** deletes the shortcut, not the data it points at.
- When a metadata entity reads files that come through a shortcut, the entity's lineage shows "via shortcut".

Needs `OneLake.ReadWrite.All`, and `Connection.Read.All` for ADLS and S3 ([permissions](permissions.md)).

### Access (Preview)

Who can reach each environment's workspace, and who can read which tables. Changes apply in Fabric right away.

- **Workspace roles** is a grid: one row per person, group or app, one column per environment's workspace, with their role in each (No access, Viewer, Contributor, Member, Admin). Change a role with its drop-down; the app asks before it changes anything. The **Worth a look** column flags entries that deserve a check. **Add a workspace** adds any other workspace to the grid. Listing roles needs Member or Admin on the workspace.
- **Give access** adds a person, group or app to a workspace with a role. Finding someone by email asks you once to let the app read your directory's names.
- **Table access in a lakehouse** lists the lakehouse's OneLake data access roles. These decide which tables Viewers, and people outside the workspace, can read; Admins, Members and Contributors read everything. **New role** and **Edit** choose what the role can read (everything, or some tables) and its members. **DefaultReader** is the role that lets everyone with read permission see every table.
- **Who can read a table** answers that question for one table.
- If OneLake security isn't turned on for the lakehouse, the tab says so and offers **Open the lakehouse in Fabric** to turn it on (**Manage OneLake security**).

Changing roles needs workspace **Admin**. Needs `OneLake.ReadWrite.All`, `Workspace.ReadWrite.All` and Microsoft Graph `User.ReadBasic.All`.

## Metadata

Metadata describes each table of your medallion layers once, in a file, and Fabric Desktop builds and runs them. Its tabs are in four groups:

| Group | Tabs |
| --- | --- |
| Design | Entities, Layers, Surrogate keys, Source systems |
| Runs | Every run, with its steps, rows and problems |
| Quality | Tests and Table health |
| Deploy | Environments and promote, Semantic model |

The **Environment** picker in the top bar (Dev, Test or Prod) only shows on Metadata pages. Everything on these pages, from runs to table health, is for the environment picked there.

The full walkthrough, from setting up a project to promoting to Prod, and a reference for every setting, is in **[the metadata guide](metadata-guide.md)**.

## Fabric

The Fabric page is Fabric's own portal, `app.fabric.microsoft.com`, inside the app. Links across the app (**Open in Fabric**, **Show in Fabric**) open their item here. Buttons along the top: **Back**, **Home**, **OneLake catalog**, **Monitoring hub**, **Reload**, and **Open in browser**.

It signs in with your Windows account when that is the same work account you use in the app, so there's usually no password prompt. Otherwise sign in once; the app keeps that sign-in between launches, encrypted for your Windows account in `%LOCALAPPDATA%\FabricDesktop\fabric-view-signin.bin`. Delete that file to make it forget. Links that leave Fabric open in your default browser.

## Monitor

Monitor has two halves.

**From Fabric Desktop** (top) has three panels:

- **Runs:** everything this app ran or started, in every environment, on this PC and in Fabric: metadata runs, notebook table writes, notebook runs in Fabric, scheduled runs on this PC, and table maintenance jobs. Filter by environment (All, Dev, Test, Prod) and by where it ran (Anywhere, This PC, Fabric). **Problems first** puts failures at the top. Each failed row says why. Click a row for its details: a metadata run shows each step with rows and time, and each check with how many rows failed. Test and Prod runs show once you're signed in.
- **Schedules** (Preview): every schedule, on this PC and in Fabric, in one list, with how its last run went, when it runs next, and **Run now**, **On/Off**, **Open** and **Remove**. Schedules on this PC are started by Windows Task Scheduler even when the app is closed, as long as the PC is on and you are signed in to it. Fabric schedules run on capacity even when your PC is off.
- **Cost** (Preview): what runs in Fabric cost in capacity units (CU hours) and money, and what running on this PC saved, over 7, 30 or 90 days, by item and by environment, with each environment's share of its capacity. **Prices** sets your price per CU hour and currency. These are estimates from run time; Fabric's Capacity Metrics app has the billed numbers.

**In Fabric** (bottom) is Fabric's own Monitoring hub, for everything else in your workspaces, with **Back**, **Home**, **Reload** and **Open in browser**. **Hide** gives the top half the whole page; the app remembers it.

## Settings

Open Settings from the gear at the bottom of the left rail, or from the account menu.

| Section | What's there |
| --- | --- |
| **Account** | Who you're signed in as, **Sign out**, and the **App (client) ID** Fabric Desktop signs in with. Leave the ID empty to use the one that comes with the app. **Admin approval** copies the link a Microsoft Entra admin opens once to approve the app for your organization. |
| **Updates** | Stable or Preview channel. See [Updates](#updates). |
| **Preview features** | Show or hide every feature tagged Preview. |
| **Notifications** | **Teams alerts**: post late and failed tables to a Teams channel. See [Freshness promises](metadata-guide.md#freshness-promises). |
| **Help and about** | This guide, **Report a problem**, the **Guide for IT admins**, the build, the engine's Python version and where the organization settings file is. |

Anything your organization has set shows "Set by your organization" and can't be changed there.

## Updates

Fabric Desktop checks for an update each time it starts and installs it before opening. After an update, **What's new** lists what changed. You can open it again from the account menu.

There are two channels:

- **Stable** (the default): builds that have been checked and released.
- **Preview**: every new build as soon as it's ready. For testers; things may change or break.

A channel change takes effect the next time Fabric Desktop starts. More detail, including where files are kept, is in [install.md](install.md#updates).

## Preview features

Features marked **Preview** work but haven't been tried on many Fabric tenants yet, so they may change. Today they are Run in Fabric, Schedules, Cost, OneLake shortcuts, OneLake access, Table history and restore, Table health, Semantic model, Tests, and the Estate X-ray. To hide them all, turn off **Settings > Preview features**.

The [documentation home](README.md#what-is-tested-and-what-isnt) says what is tested and what isn't.

## Report a problem

Account menu or Settings > **Report a problem**. Describe what happened; Fabric Desktop adds its build, channel, the engine's Python version, your tenant id, the page you're on and the recent errors from Activity (the bell). It never adds your data, file contents or passwords, and you can read everything it adds before you send.

**Send** copies the report and opens your organization's support contact if it has one, or a new GitHub issue where you paste it. **Copy only** just copies it.

Ideas and feature requests go to the ideas forum instead: see [Submitting ideas](ideas.md).

## For IT admins

Setting Fabric Desktop up for an organization, so nobody has to type an app ID: see [Configure Azure and Fabric](azure-setup.md) and [Rolling out to your organization](permissions.md#rolling-out-to-your-organization). For teams sharing projects and environments, see [Working across teams](teams.md).
