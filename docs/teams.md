# Working across teams

Fabric Desktop runs on each person's PC, but the work it produces is shared: metadata projects in Git, workspaces and lakehouses in Fabric, keys in `LH_Keys`. This page is about setting that up so many people and several teams can work side by side without stepping on each other.

## Who does what

| Role | Typical person | Responsible for |
| --- | --- | --- |
| **Platform owner** | Data platform or BI lead | The app registration, the policy file, the update channel, the Keys workspaces, and the [ideas forum](ideas.md) triage. |
| **IT / Entra admin** | Identity team | Consent for the app registration, pushing `policy.json`, security groups. See [Configure Azure and Fabric](azure-setup.md). |
| **Project owner** | Lead engineer of a domain (Sales, Finance) | One metadata project: its repository, its Dev/Test/Prod workspaces, reviews, and who may promote. |
| **Data engineer** | Anyone building tables | Entities, transforms and tests in their branch; runs Dev on their own PC. |
| **Analyst** | Report builders, data scientists | Notebooks and SQL queries against lakehouses; the semantic model built on Gold. |

One person can hold several roles. The point is that each shared thing has one owner.

## One app for everyone

- **One app registration** for the whole organization (or one per tenant, if you serve several). Every team uses the same app ID. Don't let teams register their own; it multiplies consent requests and makes support harder.
- **Push the settings** with `%ProgramData%\FabricDesktop\policy.json` so nobody types an app ID ([Rolling out to your organization](permissions.md#rolling-out-to-your-organization)). Set `support_url` to your team's Teams channel or ticket form so **Report a problem** goes to you first, not to GitHub.
- **Update channels.** Keep most people on **Stable**. Put a small pilot group on **Preview** (Settings, or a separate policy file for that group) so new builds are tried by a few people before everyone gets them. A maintainer promotes to Stable after the pilot has used a build.
- **Preview features.** Consider `"show_preview": false` for most people until each feature has been tried on your tenant, and let the pilot group report what works.

## A metadata project is a Git repository

Every table is a text file, so the normal software workflow applies.

**Layout.** One metadata project per domain or team works best: `sales-metadata`, `finance-metadata`. A project maps to one set of Dev/Test/Prod workspaces. Very large domains can split, but keep tables that feed each other in one project, because lineage, rebuild-downstream and promote work within a project.

**Day to day:**

1. Clone the project's repository. In Fabric Desktop, **Metadata > Or open an existing project** and pick the folder.
2. Create a branch for your change.
3. Edit entities in the app (or in any editor). Each save runs the save gate, so you see problems before anyone reviews.
4. Run **Dev on your own PC**. A new project's Dev keeps tables in the project's `.local` folder, which is in `.gitignore`, so every engineer has their own Dev and nobody's runs collide.
5. Run **Tests** and fix anything red.
6. Commit and open a pull request. Reviewers read the TOML diff: a new check, a changed key, a new column.
7. After merge, the project owner (or whoever you agree promotes) pulls `main` and promotes Dev to Test, then Test to Prod.

**Ownership in the files:**

- Every entity has an `owner`. Use a team address or a group, not one person, so ownership survives people moving on.
- Every hook, custom check and custom step has its own `owner` and `test`; the save gate enforces it.
- A `CODEOWNERS` file in the repository can require the right reviewers per folder, for example `metadata/entities/gold.* @contoso/sales-bi`.
- Exceptions in `exceptions.toml` carry an approver and an expiry, so a shortcut taken today shows up again when it expires.

**What not to commit:** `.local/` (Dev's tables), `__pycache__/` and `.pytest_cache/`. A new project's `.gitignore` already lists them. Never put passwords or keys in `source_systems.toml`; it only holds Key Vault URLs and secret names.

## Environments and workspaces

| Environment | Who runs it | Where |
| --- | --- | --- |
| **Dev** | Each engineer, on their own PC | `.local` by default. A shared Dev workspace is optional; if you use one, agree who runs it. |
| **Test** | The project owner, or a schedule | A Test workspace per project |
| **Prod** | Only promotes and schedules (or the [agent](agent.md)), never ad hoc runs | A Prod workspace per project |

**Name workspaces predictably**, for example `Sales - Dev`, `Sales - Test`, `Sales - Prod`. The app suggests `<project folder> - <Environment>` when you create one.

**Use security groups for workspace roles**, one per project and environment:

| Group | Role on the workspace |
| --- | --- |
| `fd-sales-engineers` | Contributor on Dev and Test |
| `fd-sales-release` | Contributor on Test and Prod (the people who promote) |
| `fd-sales-readers` | Viewer on Prod, plus OneLake data access roles for the tables they may read |

**One runner per shared environment.** A metadata run writes a table by reading it, merging new rows and committing the result. Two runs of the same environment from two PCs at the same time can overwrite each other's commit. So for Test and Prod, run from one place: a schedule (on one PC, or in Fabric) or the headless agent, which takes a write lease per table before it writes. Ad hoc runs from a laptop belong in Dev.

**Promote is the only way into Test and Prod.** It checks the gate, records each promoted metadata version in `meta.deployments`, and keeps environments reproducible. Restrict Contributor on Prod to the release group so nobody can bypass it from Fabric.

## Shared keys and sources across teams

Surrogate keys and source systems are often shared by more than one project: Sales and Finance both need `customer`.

- **One Keys workspace per environment for the whole organization** (or per data domain), owned by the platform owner. Every project points its environments at it (**Metadata > Surrogate keys > Pick Keys workspace**). Then Customer ABC gets the same key in every project's tables.
- **Agree on key table definitions.** Each project has its own `metadata/keys/<name>.toml`. Projects that share a key table must define it the same way (same business key columns). The simplest way is a small shared repository of key and source system definitions that every project copies or includes as a Git submodule, changed by pull request with the platform owner as reviewer.
- **Keys are never renumbered or deleted.** Deleting a key table definition removes the file only; the table stays in `LH_Keys`, so no key is lost. Each environment issues its own keys; promote never copies keys between environments.
- **Source systems** have a fixed id and code that never change. Treat a new source system as a shared change and review it the same way.

## Notebooks across a team

Notebooks are files on each person's PC until they're published.

- **Keep notebooks you share in Git** next to the project, or publish them to a shared workspace with **Publish to OneLake**. Publishing remembers its workspace per notebook.
- **Scratch work** that writes next to managed tables uses a `scratch_` table name. Managed tables can only be written by the framework, so a notebook can't overwrite a Gold table by accident.
- **Schedules on a PC** run only while that PC is on and its owner is signed in to Windows. For anything a team depends on, use a Fabric schedule or the agent instead.

## Knowing what's going on

- **Monitor** shows runs from every environment, filtered by environment and by where they ran. **Home** puts failures and late tables first.
- **Freshness promises** on important tables, with **Teams alerts**, tell the whole team when a table is late or failed, without anyone watching the app ([freshness](metadata-guide.md#freshness-promises)).
- **Fabric's Monitoring hub** (bottom of Monitor) shows everything else in your workspaces.
- **`LH_Meta`** in each environment holds the run log, check results and deployments as Delta tables, so you can build a team dashboard on them in Fabric.

## Feedback from many people

With thousands of users, route feedback so it adds up instead of getting lost:

- **Problems:** **Report a problem** in the app, to your `support_url`. Your platform team triages and files real bugs on GitHub.
- **Ideas:** the public [ideas forum](ideas.md), where people vote and maintainers prioritize. Encourage people to vote on an existing idea rather than post a duplicate.
