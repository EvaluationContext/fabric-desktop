# Fabric Desktop documentation

Fabric Desktop runs Microsoft Fabric data engineering work on your own PC (notebooks, Spark SQL, and a metadata-driven medallion framework) and writes the results straight into OneLake. Start with the guide for what you're doing.

## Start here

| I want to | Read |
| --- | --- |
| Install the app on my PC | [Install](install.md) |
| Set it up in Azure and Fabric for my organization | [Configure Azure and Fabric](azure-setup.md), then [Permissions](permissions.md) |
| Learn what every page does | [User guide](user-guide.md) |
| Write and run notebooks and SQL on my PC | [Notebooks and SQL](notebooks.md) |
| Build Bronze, Silver and Gold from config | [Metadata framework guide](metadata-guide.md) |
| Set it up for several teams | [Working across teams](teams.md) |
| Fix an error | [Troubleshooting](troubleshooting.md) |
| Suggest a feature or vote on one | [Submitting ideas](ideas.md) |

## All documents

| Document | For | What it covers |
| --- | --- | --- |
| [install.md](install.md) | Everyone | Requirements, download, first start, Windows blocking the engine, updates and channels, where files live, uninstalling |
| [azure-setup.md](azure-setup.md) | Admins | Fabric tenant settings, the Entra app registration, consent, giving everyone the app ID, workspaces and capacity, access, optional extras, a check that it works |
| [permissions.md](permissions.md) | Admins | Every Entra permission, in alphabetical order; fixing admin consent errors; the policy file; what the agent needs |
| [user-guide.md](user-guide.md) | Everyone | Every page: Home, Create, Workspaces, OneLake (catalog, shortcuts, access), Metadata, Fabric, Monitor, Settings, the command bar and the bell |
| [notebooks.md](notebooks.md) | Engineers, analysts | Notebooks on your PC, Fabric code that works here, publishing, Run in Fabric, schedules, SQL query |
| [metadata-guide.md](metadata-guide.md) | Engineers | The metadata framework step by step, from a new project to Prod, and a reference for every setting |
| [teams.md](teams.md) | Team leads, platform owners | Roles, Git workflow, environments and workspaces, shared keys, feedback at scale |
| [troubleshooting.md](troubleshooting.md) | Everyone | Each error message, what it means and what to do |
| [ideas.md](ideas.md) | Everyone | Posting ideas, voting, and how ideas are prioritized and shipped |
| [xray-fixes.md](xray-fixes.md) | Engineers | Each Estate X-ray finding and how to fix it |
| [agent.md](agent.md) | Platform owners | The headless agent on Windows or Linux servers |
| [architecture.md](architecture.md) | Contributors | How the app, the engine and OneLake fit together |
| [ui-guidelines.md](ui-guidelines.md) | Contributors | Where new features go in the UI |
| [enterprise-roadmap.md](enterprise-roadmap.md) | Platform owners | Milestones for running at enterprise scale |
| [why-fabric-desktop.md](why-fabric-desktop.md) | Everyone | Why it exists: the Power BI model for data engineering |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | Contributors | Setting up, rules for changes, pull requests |

## What is tested and what isn't

Fabric Desktop is new, and it's honest about what has been proven.

**Tested on every build**, by automated tests on Windows and Linux, against local Delta tables:

- Notebooks, the `lakehouse` module, `spark` and `notebookutils` on the PC
- Spark SQL translation and the SQL query engine
- The metadata framework: every load mode, checks and quarantine, surrogate keys, the four gates, compare, schema drift, replay, freshness, profiling and tests
- Startup, updates and settings
- The headless agent's queue, retries and leases

**Used on the maintainer's own Fabric tenant:** sign-in and admin consent, listing workspaces and items, the OneLake catalog, the embedded Fabric pages (Fabric and the Monitoring hub) and their saved sign-in, and writing tables to OneLake from the PC. That is one tenant; yours may differ in settings and permissions.

**Built to Fabric's documented APIs but not yet tried on a live tenant.** These are tagged **Preview** in the app and can be hidden in **Settings > Preview features**:

| Preview feature | Where |
| --- | --- |
| Run in Fabric (notebooks and environments) | Notebook ribbon, Metadata environments |
| Schedules (on this PC and in Fabric) | Notebook ribbon, Metadata, Monitor |
| Cost | Monitor |
| OneLake shortcuts | OneLake |
| OneLake access | OneLake |
| Table history and restore | Entity page |
| Table health and Optimize | Metadata > Quality |
| Semantic model | Metadata > Deploy |
| Tests | Metadata > Quality |
| Estate X-ray | Home |

The headless agent hasn't run against a live tenant either ([agent.md](agent.md#verify-on-your-tenant)). Promote to Test and Prod creates lakehouses and writes through the same calls as the features above; try it on a non-production workspace first.

When a Preview feature has been tried on a tenant, its tag is removed. If you try one, say how it went in the [Ideas forum](ideas.md) or a bug report.
