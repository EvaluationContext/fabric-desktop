# Fabric Desktop user guide

Fabric Desktop runs your data work on your own PC and keeps the results in Microsoft Fabric's OneLake. It looks and works like Fabric, so most pages will feel familiar. This guide covers what each page is for and where to find things.

Press **Ctrl+K** anywhere to search pages, actions, notebooks, workspaces and entities.

## Get started

1. **Sign in** with your work account, the same one you use for Fabric. The first time, Fabric Desktop may ask for an app (client) ID; your Fabric admin has it. If sign-in says your organization needs to approve Fabric Desktop, send your admin the link from **Settings > Account > Admin approval**.
2. **Open a lakehouse** from **OneLake** or **Workspaces** to browse its tables and files, or query them with SQL.
3. **Create a notebook** from **Create**. Cells run on this PC, not on your Fabric capacity.
4. **Set up Metadata** when you want Fabric Desktop to build and run your medallion layers for you.

Home shows a **Get started** list with these steps until you have a metadata project. You can hide it.

## Home

Home answers "what happened since I was last here": problems first, then what is running, what runs next, and how each environment stands.

## Create

New notebooks and lakehouses. Notebooks run on this PC and read and write OneLake.

## Workspaces

Your Fabric workspaces. Expand one to see its items, star favorites, and pin the ones you use most. Pinned workspaces stay at the top.

## OneLake

The OneLake catalog: every item you can reach, with search, filters and favorites. **Shortcuts** and **Access** are preview features for OneLake shortcuts and data access roles.

## Metadata

Metadata describes each table of your medallion layers once, in a file, and Fabric Desktop builds and runs them. Its tabs are in four groups:

| Group | What's in it |
| --- | --- |
| Design | Entities, Layers, Surrogate keys, Source systems |
| Runs | Every run, with its steps, rows and problems |
| Quality | Tests and Table health |
| Deploy | Environments and promote, Semantic model |

The **Environment** picker in the top bar (Dev, Test or Prod) only shows on Metadata pages.

## Monitor

Everything Fabric Desktop ran or started, on this PC and in Fabric, with Fabric's own Monitoring hub below. **Schedules** and **Cost** are preview features.

## Settings

Open Settings from the gear at the bottom of the left rail, or from the account menu (your initial, top right).

- **Account:** who you're signed in as, Sign out, and the app (client) ID Fabric Desktop signs in with.
- **Updates:** the update channel (see below).
- **Preview features:** show or hide features that are still in preview.
- **Notifications:** Teams alerts for late and failed tables.
- **Help and about:** this guide, Report a problem, and the build you're running.

Anything your organization has set shows "Set by your organization" and can't be changed there.

## Updates

Fabric Desktop checks for an update each time it starts and installs it before opening. After an update, **What's new** lists what changed. You can open it again from the account menu.

There are two channels:

- **Stable** (the default): builds that have been checked and released.
- **Preview**: every new build as soon as it's ready. For testers; things may change or break.

A channel change takes effect the next time Fabric Desktop starts.

## Preview features

Features marked **Preview** work but haven't been tried on many Fabric tenants yet, so they may change. Today they are Run in Fabric, Schedules, Cost, OneLake shortcuts, OneLake access, Table history and restore, Table health, Semantic model, Tests, and the Estate X-ray. To hide them all, turn off **Settings > Preview features**.

## Report a problem

Account menu or Settings > **Report a problem**. Describe what happened; Fabric Desktop adds its build and the recent errors from Activity (the bell). It never adds your data, file contents or passwords. **Send** copies the report and opens your organization's support contact, or a new GitHub issue.

## For IT admins

Setting Fabric Desktop up for an organization, so nobody has to type an app ID: see [Rolling out to your organization](permissions.md#rolling-out-to-your-organization).
