# Troubleshooting

When something goes wrong, Fabric Desktop says what happened in a sentence and keeps the technical text in **Activity** (the bell) under **Details**. Find the sentence you saw below. If it isn't here, use **Report a problem** in the account menu: it collects the build and recent errors for you, without your data.

## Starting the app

| What you see | What it means | What to do |
| --- | --- | --- |
| "Windows protected your PC" | SmartScreen doesn't know the exe yet because it isn't code-signed. | **More info > Run anyway**. |
| A blank white window | The WebView2 runtime is missing or broken. | Install the [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) and start again. |
| **Windows Smart App Control blocked it** or **An App Control policy on this PC blocked it** | Windows refused the engine's unsigned Python packages. Reinstalling won't help. | See [Install: If Windows blocks the engine](install.md#if-windows-blocks-the-engine). |
| "Could not check for a newer build" | GitHub didn't answer, or its limit of 60 checks an hour from your network was reached. | Nothing. The app runs the build you have and checks again next start. |
| "The update didn't take" | The new exe couldn't replace the old one, usually because another copy was still running or antivirus held the file. | Close every Fabric Desktop window, then download `fabric-desktop.exe` again from the Releases page. |
| "The engine could not be installed" | The engine download or unpack failed. The log is under the message. | Check the internet connection and disk space, then **Retry**. |
| "The local engine stopped while doing this" | The Python engine crashed. It restarts on your next action. | Try again. If it keeps happening, report it; `%LOCALAPPDATA%\FabricDesktop\sidecar.log` has the reason. |

## Signing in

| What you see | What it means | What to do |
| --- | --- | --- |
| **Set up sign-in** asks for an App (client) ID | Nobody has given this PC the app ID yet. | Paste the ID your admin gave you, or **Send the guide to my admin**. Admins can set it for everyone ([azure-setup.md](azure-setup.md#4-give-the-app-id-to-everyone)). |
| "Your organization hasn't approved Fabric Desktop yet" | No admin consent for the app registration, and users can't consent themselves. | Copy the link in **Settings > Account > Admin approval** and send it to a Microsoft Entra admin. |
| "Your organization hasn't approved the permission this needs" | The feature needs a permission the app registration doesn't have, or that hasn't been consented. | **Details** names the permission. An admin adds it ([permissions.md](permissions.md)), grants consent, and you sign in again. |
| "Could not grant admin consent ... does not have a subscription (or service principal)" (admin, in Entra) | The tenant has no service principal for one of the APIs. | Create it with `az ad sp create --id <id>` ([permissions.md](permissions.md#could-not-grant-admin-consent-your-organization-does-not-have-a-subscription-or-service-principal)). |
| "Your sign-in expired" | The saved token couldn't be renewed. | Sign in again. If it repeats, close the app, delete `%LOCALAPPDATA%\FabricDesktop\msal_cache.bin`, and start again. |
| A new permission still isn't working after consent | Your saved token was issued before the permission was added. | Close the app, delete `msal_cache.bin`, start again and sign in. Or wait up to an hour. |
| The Fabric page or Monitoring hub asks you to sign in every time | The saved Fabric sign-in couldn't be restored. | Sign in once more. If it keeps asking, delete `%LOCALAPPDATA%\FabricDesktop\fabric-view-signin.bin` and sign in again. |

## Workspaces, lakehouses and data

| What you see | What it means | What to do |
| --- | --- | --- |
| Workspaces is empty | Your account has no role on any workspace. | Ask a workspace admin to add you. |
| "You don't have permission for that in this workspace" | You need a higher workspace role for that action. | Contributor or above to write; Admin to change access. |
| "Skipped N workspaces" on OneLake | Those workspaces couldn't be read with your access. | Usually fine. Ask for a role if you need them. |
| Every read or write in OneLake is refused, but Fabric works in the browser | The tenant setting **Users can access data stored in OneLake with apps external to Fabric** is off for you. | A Fabric admin turns it on ([azure-setup.md](azure-setup.md#1-check-your-fabric-tenant-settings)). |
| "The workspace's Fabric capacity is paused or full" | Creating items, or anything that runs in Fabric, needs an active capacity. | Resume or scale the capacity, or wait. Work on your PC isn't affected. |
| "Fabric is busy and asked the app to slow down" | Fabric throttled the requests. | Wait a minute and retry. |
| A new table shows under **Unidentified** in Fabric | Fabric hasn't picked up a table written from outside yet. | Refresh the lakehouse in Fabric. See also [xray-fixes.md](xray-fixes.md#unidentified-tables). |
| Endorsed items is greyed out | Endorsement needs a Fabric administrator account and the `Tenant.Read.All` permission. | Nothing to fix unless you're an admin. |
| **Couldn't list capacities** when creating a workspace | The app registration lacks `Capacity.Read.All`. | An admin adds it ([permissions.md](permissions.md)). |

## Notebooks and SQL

| What you see | What it means | What to do |
| --- | --- | --- |
| "This notebook only runs in Fabric" | A cell uses something the PC doesn't have: RDDs, UDFs, streaming, Scala or R, or SQL MERGE/UPDATE/DELETE. The note names the cell. | Rewrite that cell, or set **Runs on** to Fabric. |
| "This notebook can't run in Fabric yet" | No lakehouse is attached, or a cell reads a path on your PC like `C:\data`. | Attach a lakehouse, or read the file from the lakehouse's `Files`. |
| A `%pip install` line is skipped | The engine's packages are fixed. | Use the packages the engine has (Polars, DuckDB, pandas, pyarrow, deltalake and more), or run the notebook in Fabric. |
| Writing a table says it is managed by the framework | The table belongs to a metadata entity. | Change the entity instead, or write to a `scratch_` table. |
| **Cancel** doesn't stop a cell | The code is stuck inside a library call that can't be interrupted. | **Restart kernel**. Variables are cleared. |
| A SQL query fails on a Spark-only feature | Not every Spark SQL function has a DuckDB match. | The error names the part it couldn't run. Rewrite that part, or run the query in Fabric. |

## Metadata

| What you see | What it means | What to do |
| --- | --- | --- |
| "N problems to fix" banner | The project doesn't pass the save gate. Runs and promote stay blocked. | Fix each listed problem in its entity file. An approved exception in `exceptions.toml` is the escape hatch. |
| A run **Needs a decision** | The table's logic changed and it loads incrementally. | Choose **Apply to new rows only** or **Rebuild it and everything downstream** ([guide](metadata-guide.md#5-load-modes-and-decisions)). |
| "check ... failed on N rows, so nothing was written" | A check with `on_fail = "stop"` failed. | Open the run's details, fix the data or the rule, or change `on_fail` to `quarantine`. |
| "no files match ..." | The source pattern found no files in the layer's lakehouse `Files`. | Check the path and that files were delivered. In Dev on the PC, files go in `.local`. |
| "... has not been loaded in <env> yet" | The table this one reads hasn't been run in this environment. | Run upstream first, or **Run all**, which runs in layer order. |
| Promote says "things to do first" | The promote gate isn't met. | Each line says what: a failing test, a table whose last run isn't green on this version, or a save gate problem. |
| "Assign a workspace before Test can run" | The environment has no workspace. | **Environments and promote > Assign workspace**. |
| A Fabric run "stopped before it saved any results" | The runner notebook failed in Fabric. | Open it from **Monitor** to see the notebook's output in Fabric. |
| Fabric schedule or Run in Fabric fails with 403 | The app registration lacks `Item.Execute.All`, or you aren't Contributor on the workspace. | Add the permission and sign in again ([permissions.md](permissions.md)). |
| A scheduled run on this PC didn't happen | The PC was off, asleep or nobody was signed in to Windows; or the app was signed out. | Use a Fabric schedule for runs that must happen. Monitor > Schedules shows each run and why it was skipped. |

## Still stuck

1. Open the bell and expand **Details** on the error.
2. **Report a problem** (account menu). It copies the build, channel, page and recent errors, and opens your organization's support contact or a new GitHub issue.
3. If you were asked for logs, attach `%LOCALAPPDATA%\FabricDesktop\sidecar.log`. Check it first: it can contain table and workspace names.
