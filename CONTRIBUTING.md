# Contributing to Fabric Desktop

Thanks for helping. There are several ways in, and most don't need code.

| You want to | Do this |
| --- | --- |
| Suggest a feature | Post it in the [Ideas forum](https://github.com/KratosDataGod/fabric-desktop/discussions/categories/ideas), or vote for an existing one. How ideas are prioritized: [docs/ideas.md](docs/ideas.md). |
| Report a bug | **Report a problem** in the app, then paste it into a [bug report](https://github.com/KratosDataGod/fabric-desktop/issues/new/choose). Check [troubleshooting](docs/troubleshooting.md) first. |
| Ask a question | [Q&A](https://github.com/KratosDataGod/fabric-desktop/discussions/categories/q-a) |
| Try new builds | Switch to the **Preview** channel in **Settings > Updates**, and tick "Would you try an early version?" on ideas you care about. |
| Fix the docs | Edit the file under `docs/` and open a pull request. Small fixes are always welcome. |
| Change the code | Read on. For anything bigger than a small fix, start from an idea or issue so the approach is agreed first. |

## Set up

You need Windows for the desktop app. The Python engine and its tests also run on Linux and macOS.

```powershell
git clone https://github.com/KratosDataGod/fabric-desktop
cd fabric-desktop\sidecar
python -m pip install -e ".[dev]"     # Python 3.11 or later
python -m pytest -q                   # every test runs without Fabric
```

To run the desktop app from source, install Rust and the [Tauri prerequisites](https://v2.tauri.app/start/prerequisites/), then:

```powershell
$env:FABRIC_DESKTOP_PYTHON = (Get-Command python).Source
cd app\src-tauri
cargo run
```

The UI is plain HTML and JavaScript in `app/ui/` with no build step; reload the window to see a change. How the pieces fit is in [docs/architecture.md](docs/architecture.md).

## Rules for changes

- **Tests pass.** `python -m pytest -q` in `sidecar/` before you push. CI also builds the exe and the engine on Windows.
- **New UI goes where people already look.** Read [docs/ui-guidelines.md](docs/ui-guidelines.md): register pages and commands in `features.js`, keep the left rail at 8 items or fewer, put new features in an existing page.
- **Untested on a tenant means Preview.** A feature that hasn't run against a live Fabric tenant is added to `PREVIEW_FEATURES` and shows a Preview tag.
- **Docs in the same pull request.** A new feature updates [docs/user-guide.md](docs/user-guide.md) (and the notebooks or metadata guide if it belongs there), and its page's **Learn more** link points at that section.
- **Permissions in one place.** A change that asks for a new scope (`sidecar/fabric_desktop/auth.py`) or needs a new agent role adds it to [docs/permissions.md](docs/permissions.md) in alphabetical order, in the same pull request.
- **Plain words.** Errors and labels say what happened and what to do, in words people use. Errors go through `friendlyError` in `activity.js`.
- **No secrets.** Never commit tokens, client secrets, tenant data or real connection strings, including in tests and screenshots.

## Pull requests

1. Branch from `main`.
2. Keep the change focused; one feature or fix per pull request.
3. Describe what a user sees before and after, and how you tested it. Screenshots help for UI changes.
4. Link the idea or issue it comes from.

Merges to `main` go to the **Preview** channel straight away. A maintainer moves a build to **Stable** with the **Promote to stable** workflow once it has been tried on Preview.
