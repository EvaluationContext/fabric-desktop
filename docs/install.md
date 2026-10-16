# Install Fabric Desktop

This page takes you from nothing to a running app on one Windows PC. If you are setting it up for a whole organization, read [Configure Azure and Fabric](azure-setup.md) first, then come back here.

> **Before you start:** Fabric Desktop is new. The parts that only talk to your own PC are tested on every build. Several features that talk to Fabric are marked **Preview** in the app because nobody has tried them on a live Fabric tenant yet. See [What is tested and what isn't](README.md#what-is-tested-and-what-isnt).

## What you need

| You need | Why |
| --- | --- |
| Windows 10 or 11 (64-bit) | The desktop app is Windows only. The [headless agent](agent.md) also runs on Linux. |
| Microsoft Edge WebView2 Runtime | The app's window is WebView2. It is already on every up-to-date Windows 11 and most Windows 10 PCs. If the app opens a blank window, install it from [Microsoft](https://developer.microsoft.com/microsoft-edge/webview2/). |
| A work account with Microsoft Fabric | You sign in with the same account you use at `app.fabric.microsoft.com`. |
| An app (client) ID | The Microsoft Entra app Fabric Desktop signs in through. Your admin creates it once for everyone ([Configure Azure and Fabric](azure-setup.md)). If the build you download already carries one, you never see this step. |
| A Fabric capacity on your workspaces | Only for creating lakehouses and for the features that run in Fabric. Work that runs on your PC uses no capacity. |
| Disk space | The engine takes several hundred MB, plus local copies of the tables you work on. |

You do **not** need Python, Spark, Java or admin rights on the PC. The app brings its own engine.

## Download and start

1. Download **[fabric-desktop.exe](https://github.com/KratosDataGod/fabric-desktop/releases/download/stable/fabric-desktop.exe)** (the Stable channel). It is also on the repository's **Releases** page.
2. Put it anywhere you like, for example a `Tools` folder or your Desktop. It is a single file; there is no installer.
3. Double-click it.
4. **Windows SmartScreen** may say "Windows protected your PC" because the exe isn't code-signed yet. Choose **More info > Run anyway**. You only see this the first time.
5. The first start installs the local engine. A **Checking components** window shows three lines:

   | Line | What happens |
   | --- | --- |
   | Fabric Desktop | Checks GitHub for a newer build on your channel and installs it straight away, then restarts. |
   | Python | Comes with the engine. Nothing to install. |
   | Local engine | Downloads `fabric-desktop-engine.zip` (Python plus every package) and unpacks it into `%LOCALAPPDATA%\FabricDesktop\engine`. |

   This takes a minute or two the first time. Later starts go straight to Home unless something is updating.
6. **Sign in.** Click **Sign in** on Home (or the **?** circle at the top right). A browser window asks for your work account. If the app asks for an **App (client) ID**, paste the one your admin gave you and choose **Save and sign in**. If you don't have one, choose **Send the guide to my admin**.

That's it. Home shows a **Get started** list until you set up a metadata project. Go on to the [user guide](user-guide.md).

## If Windows blocks the engine

On some PCs Windows refuses to load parts of the engine. The startup window then says **Windows Smart App Control blocked it** or **An App Control policy on this PC blocked it**, and names the blocked packages.

This isn't a broken install, so reinstalling won't help. The engine's Python packages (pyarrow, Polars, DuckDB and others) aren't code-signed by their makers, and these Windows features only let signed code load.

| What blocked it | What to do |
| --- | --- |
| **Smart App Control** (Windows 11 on a home or personal PC) | It can't allow just one app. Either use a PC without it, or turn it off in **Windows Security > App & browser control > Smart App Control settings** and click **Retry**. Some Windows versions can't turn it back on without a reset, so decide that first. |
| **An App Control policy** (set by your organization) | Ask IT to allow the Python packages in the folder the window names. They see each block in **Event Viewer > Microsoft > Windows > CodeIntegrity > Operational**. Then click **Retry**. |

The lasting fix is code signing: CI signs every file when the repository has an Azure Trusted Signing account set up ([README: Code signing](https://github.com/KratosDataGod/fabric-desktop/blob/main/README.md#code-signing)). That isn't in place yet.

## Updates

Fabric Desktop updates itself. Each time it starts it asks GitHub whether a newer build is out on your channel. If one is, it downloads it, checks it is the right file, swaps the exe in place and restarts. Then **What's new** lists what changed. You never run an old build by accident.

| Channel | Who it's for | What you get |
| --- | --- | --- |
| **Stable** (default) | Everyone | Builds a maintainer has tried and promoted with the **Promote to stable** workflow. |
| **Preview** | Testers | Every build of `main` as soon as CI is green. Things may change or break. |

Change it in **Settings > Updates**. The change takes effect the next time the app starts. An organization can fix the channel for everyone with a policy file ([Rolling out to your organization](permissions.md#rolling-out-to-your-organization)).

The update check uses GitHub's public API, which allows 60 checks an hour from one internet address. In a large office behind one address you may see "Could not check for a newer build" now and then. The app keeps working on the build you have and checks again at the next start.

## Where things are kept on your PC

| Path | What it is |
| --- | --- |
| The folder you put `fabric-desktop.exe` in | The app. The update replaces this file in place. |
| `%LOCALAPPDATA%\FabricDesktop\engine` | The local engine: Python and its packages. |
| `%LOCALAPPDATA%\FabricDesktop\notebooks` | Your notebooks, as `.ipynb` files. Back these up or keep them in Git. |
| `%LOCALAPPDATA%\FabricDesktop\msal_cache.bin` | Your sign-in token cache. Delete it to force a fresh sign-in. |
| `%LOCALAPPDATA%\FabricDesktop\fabric-view-signin.bin` | The saved sign-in of the embedded Fabric pages, encrypted for your Windows account. |
| `%LOCALAPPDATA%\FabricDesktop\app-settings.json` | What you chose in Settings. |
| `%LOCALAPPDATA%\FabricDesktop\sidecar.log` | The engine's log. Attach it to a problem report if asked. |
| `%LOCALAPPDATA%\FabricDesktop\*.json`, `*.jsonl` | Run history, favorites, pinned workspaces, schedules on this PC, the Estate X-ray's last scan. |
| `%ProgramData%\FabricDesktop\policy.json` | Settings your organization fixed, if any. |
| `Documents\FabricDesktop\metadata` | The default folder for a new metadata project. You can pick another, and you should keep it in Git. |

## Uninstall

1. If you added schedules on this PC, remove them in **Monitor > Schedules** first. They live in Windows Task Scheduler under the `FabricDesktop` folder.
2. Close the app and delete `fabric-desktop.exe`.
3. Delete `%LOCALAPPDATA%\FabricDesktop` to remove the engine, your notebooks, settings and sign-in. Copy the `notebooks` folder somewhere first if you want to keep them.

Nothing in Fabric is removed. Lakehouses, tables and notebooks you published stay where they are.

## For developers: run from source

To work on Fabric Desktop itself, see [CONTRIBUTING.md](https://github.com/KratosDataGod/fabric-desktop/blob/main/CONTRIBUTING.md). In short:

```powershell
cd sidecar
python -m pip install -e ".[dev]"
python -m pytest -q                                          # tests, no Fabric needed
$env:FABRIC_DESKTOP_PYTHON = (Get-Command python).Source    # the app uses this Python
cd ..\app\src-tauri
cargo run                                                    # needs Rust and the Tauri prerequisites
```

Builds you make yourself skip the update check.
