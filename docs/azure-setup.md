# Configure Azure and Fabric

Fabric Desktop needs three things set up in your Microsoft cloud before people can use it: an app registration in Microsoft Entra ID, a few Fabric settings, and workspaces with capacity. This page walks through all of it in order. It is written for whoever administers Entra and Fabric in your organization. Most of it is done once.

The exact list of API permissions, in alphabetical order, lives in one place: **[permissions.md](permissions.md)**. This page tells you when to use it and what else to set up around it.

| Step | Who does it | Once or per person |
| --- | --- | --- |
| [1. Check your Fabric tenant settings](#1-check-your-fabric-tenant-settings) | Fabric administrator | Once |
| [2. Register the app in Entra](#2-register-the-app-in-entra) | Entra Application Administrator or Cloud Application Administrator | Once |
| [3. Add the permissions and grant consent](#3-add-the-permissions-and-grant-consent) | Entra Global Administrator, Privileged Role Administrator or Cloud Application Administrator | Once |
| [4. Give the app ID to everyone](#4-give-the-app-id-to-everyone) | IT, or the repository owner | Once |
| [5. Plan workspaces and capacity](#5-plan-workspaces-and-capacity) | Fabric or workspace admins | Once per project |
| [6. Give people access](#6-give-people-access) | Workspace admins | Per person or group |
| [7. Optional extras](#7-optional-extras) | Varies | As needed |
| [8. Check it works](#8-check-it-works) | One user | Once |

## 1. Check your Fabric tenant settings

In the **Fabric admin portal** (`app.fabric.microsoft.com` > gear > **Admin portal > Tenant settings**), check these. Most tenants already have them on.

| Setting | Where | Needed for |
| --- | --- | --- |
| **Users can access data stored in OneLake with apps external to Fabric** | OneLake settings | Everything that reads or writes tables and files. Fabric Desktop talks to OneLake from outside Fabric, so if this is off every read and write is refused. |
| **Users can create Fabric items** | Microsoft Fabric | Creating lakehouses, notebooks, variable libraries and semantic models. |
| **Create workspaces** | Workspace settings | Only if people will create Dev, Test or Prod workspaces from the app. |
| **Service principals can use Fabric APIs** | Developer settings | Only for the [headless agent](agent.md). Enable it for a security group that contains the agent's identity. |

Settings can be on for the whole organization or for named security groups. If you limit them to groups, make sure Fabric Desktop's users are in them.

## 2. Register the app in Entra

Fabric Desktop signs people in as themselves (delegated sign-in). It does that through one app registration that you own. You don't need a client secret: it is a public client.

1. Open the [Microsoft Entra admin center](https://entra.microsoft.com) > **Identity > Applications > App registrations > New registration**.
2. **Name:** anything people will recognize on the consent screen, for example `Fabric Desktop`.
3. **Supported account types:**
   - **Accounts in this organizational directory only** if only your organization uses it.
   - **Accounts in any organizational directory** if people from more than one organization will use the same app ID, such as partners or customers. Each of those organizations then approves it once ([permissions.md: People from more than one organization](permissions.md#rolling-out-to-your-organization)).
4. **Redirect URI:** choose **Public client/native (mobile & desktop)** and enter `http://localhost`.
5. Select **Register**.
6. On the app's **Overview** page, copy the **Application (client) ID**. That is the "app ID" people paste into Fabric Desktop.
7. Under **Authentication**, check that `http://localhost` is listed under **Mobile and desktop applications**.

## 3. Add the permissions and grant consent

Follow **[permissions.md](permissions.md)** from **Step by step** onward. In short:

1. Make sure your tenant has a service principal for each Microsoft API you will add (Azure Storage, Microsoft Graph, Power BI Service, and any optional ones). On new tenants this is the step that usually fails, with "Could not grant admin consent ... does not have a subscription (or service principal)". The fix is one `az ad sp create` line per API, listed on that page.
2. On **API permissions**, add the **delegated** permissions in the table, top to bottom. They're in alphabetical order so you visit each API once.
3. Select **Grant admin consent for <your tenant>**.

What you can leave out:

- Permissions marked **Planned** aren't used yet. Adding them now saves another consent round later; leaving them out is fine.
- Permissions marked **Optional** or **Required for <a feature>** only matter if people use that feature. Without them the feature says which permission it needs; everything else works.
- **Azure Key Vault** and **Azure Service Management** need their service principals to exist in your tenant. If you don't want them yet, leave them off rather than letting them block consent for the rest.

If you skip admin consent, each person is asked to consent the first time they sign in, if your tenant lets users consent to apps. Many organizations don't, which is why admin consent is recommended.

## 4. Give the app ID to everyone

Nobody should have to type the app ID or run a command. Pick one way; the first one found wins, in this order:

| Way | How | Best for |
| --- | --- | --- |
| **Policy file** | Push `%ProgramData%\FabricDesktop\policy.json` with Intune or Group Policy | IT-managed PCs. People see "Set by your organization" and can't change it. |
| **Environment variable** | `FABRIC_DESKTOP_CLIENT_ID` for the Windows user | Existing installs set up by script. |
| **Settings** | Each person pastes it in **Settings > Account**, or in the box at first sign-in | One person or a small pilot. |
| **Built in** | Set the repository variable `FABRIC_CLIENT_ID` (**Settings > Secrets and variables > Actions > Variables**). Every build after that carries it. | Everyone who downloads the app from this repository. |

A policy file that sets everything an organization usually wants:

```json
{
  "client_id": "00000000-0000-0000-0000-000000000000",
  "channel": "stable",
  "show_preview": false,
  "support_url": "https://teams.microsoft.com/l/channel/..."
}
```

| Key | Values | Effect |
| --- | --- | --- |
| `client_id` | The Application (client) ID | The app people sign in through |
| `channel` | `stable` or `preview` | Fixes the update channel |
| `show_preview` | `true` or `false` | Shows or hides every feature tagged Preview |
| `support_url` | An `https://` link or a `mailto:` address | Where **Report a problem** sends people, instead of GitHub |

Every key is optional. More detail is in [permissions.md: Rolling out to your organization](permissions.md#rolling-out-to-your-organization).

> With about 5,000 people, consider `"show_preview": false` for most of them and a pilot group with Preview features on. Preview features haven't been tried on a live tenant ([what is tested](README.md#what-is-tested-and-what-isnt)).

## 5. Plan workspaces and capacity

Notebooks and SQL queries can use any lakehouse a person can open. The **metadata framework** expects a particular shape, so plan it before a team starts.

| What | How many | Holds |
| --- | --- | --- |
| **Environment workspaces** | One each for Dev, Test and Prod, per project | One lakehouse per layer (`LH_Bronze`, `LH_Silver`, `LH_Gold`, or whatever layers the project has), plus `LH_Meta` for run logs, checks and watermarks. |
| **Keys workspace** | One per environment, shared by every project that shares business keys | `LH_Keys`: one table per business key (`sk.customer`, `sk.product`) and the source system configuration. |

Things to know:

- **Dev can stay on the PC.** A new project's Dev keeps its tables in a `.local` folder on the developer's PC. It needs no workspace until someone assigns one in **Metadata > Environments and promote**.
- **Test and Prod lakehouses are created by the app** on the first promote. You don't create them by hand.
- **Capacity.** Lakehouses need a workspace on a Fabric capacity (F SKU, or a trial). Runs on a PC use none of it. Features that deliberately run in Fabric do: Run in Fabric, Fabric schedules, environments set to **Runs on: Fabric**, Optimize (Fabric's table maintenance job), and semantic models. The Monitor **Cost** panel estimates how much.
- **Workspaces can be created from the app** (**Metadata > Environments and promote > Assign workspace > Create a new workspace**) if people are allowed to create workspaces and the app has `Capacity.Read.All`.

## 6. Give people access

Fabric Desktop never gets more access than the person using it. It uses their own Fabric workspace roles.

| Person | Role they need | On |
| --- | --- | --- |
| Someone exploring data and running notebooks | **Viewer** to read, **Contributor** to write tables | The workspaces whose lakehouses they use |
| A data engineer building a metadata project | **Contributor** or higher | Each environment workspace they run, and the Keys workspace |
| Someone who promotes to Test or Prod | **Contributor** or higher | The target environment's workspace and the Keys workspace |
| Someone who manages access from the app's **OneLake > Access** tab | **Admin** | The workspace |
| Someone who turns on endorsement in the catalog | **Fabric administrator** | The tenant (also needs `Tenant.Read.All`) |

Using security groups for these roles makes [working across teams](teams.md) much easier than adding people one by one.

## 7. Optional extras

| Extra | What to set up | Used by |
| --- | --- | --- |
| **Secrets in notebooks** | An Azure Key Vault; give people the **Key Vault Secrets User** role on it; add the Azure Key Vault permission to the app | `notebookutils.credentials.getSecret` on the PC |
| **Teams alerts** | In a Teams channel, add the **Workflows** template "Post to a channel when a webhook request is received" and copy its address | **Settings > Notifications** or Freshness on an entity: late and failed tables are posted to the channel |
| **OneLake shortcuts to ADLS Gen2 or Amazon S3** | A cloud connection in Fabric (**Settings > Manage connections and gateways**) | **OneLake > Shortcuts > New shortcut** |
| **OneLake security (data access roles)** | Turn on OneLake security on the lakehouse in Fabric | **OneLake > Access** table-level roles |
| **A headless agent on a server** | A separate app registration with a certificate, or a managed identity; the **Service principals can use Fabric APIs** tenant setting; Contributor on the workspaces | [agent.md](agent.md) |

## 8. Check it works

Have one person do this on a PC with the app installed:

1. Start Fabric Desktop and **Sign in**. Expected: Home says "Welcome back".
2. Open **Workspaces**. Expected: the workspaces they can open in Fabric. If it's empty, they have no workspace roles yet.
3. Open **OneLake**, expand a lakehouse, and choose **SQL query**. Run `SELECT * FROM <a table> LIMIT 10`. Expected: rows. This proves OneLake access from outside Fabric works.
4. **Create > Notebook**, **Add data items**, attach a lakehouse they can write to, and run:

   ```python
   import polars as pl
   from fabric_desktop import lakehouse
   lakehouse.write_table(pl.DataFrame({"ok": [1]}), "dbo.fabric_desktop_check")
   ```

   Expected: the table shows in Fabric (refresh the lakehouse there if it shows under **Unidentified**). Delete it afterwards.
5. If anything fails, the bell (Activity) has the error with its details. [Troubleshooting](troubleshooting.md) lists the common ones.
