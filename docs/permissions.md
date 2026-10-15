# Permissions

Fabric Desktop uses two kinds of identity, and they need different setup:

- **The desktop app** signs in as you. Its Entra app registration needs the **delegated** API permissions below.
- **The headless agent** signs in as a service principal or managed identity. It needs **no API permissions**. Its access comes from a Fabric tenant setting and workspace roles (see [The headless agent](#the-headless-agent)).

Every list here is in alphabetical order: APIs by the name Entra shows, then permissions by name. Add them top to bottom and you visit each API once.

## The desktop app: delegated permissions

In the Entra admin center: **App registrations > your app > API permissions > Add a permission**. Every API here is under **Microsoft APIs**, or under **APIs my organization uses** if you search for it by name. Choose **Delegated permissions** each time.

| API (as Entra shows it) | Permission | Needed for | Status |
| --- | --- | --- | --- |
| Azure Key Vault | `user_impersonation` | Notebooks reading secrets with `notebookutils.credentials.getSecret` on this PC | Optional |
| Azure Service Management | `user_impersonation` | Pausing or resuming an F-SKU capacity | Planned |
| Azure SQL Database | `user_impersonation` | Querying a SQL analytics endpoint or warehouse directly | Planned |
| Azure Storage | `user_impersonation` | Reading and writing OneLake: tables, files, the Fabric runner bundle | **Required** |
| Microsoft Graph | `GroupMember.Read.All` | Showing who is in a group during access review (needs admin consent) | Planned |
| Microsoft Graph | `User.ReadBasic.All` | Finding a person by email when granting access | Required for Access |
| Power BI Service | `Capacity.Read.All` | Listing capacities when you create a workspace, and capacity size for the cost view | Required for creating workspaces |
| Power BI Service | `Connection.Read.All` | Cloud connections when you create a shortcut | Required for Shortcuts |
| Power BI Service | `Connection.ReadWrite.All` | Creating connections | Planned |
| Power BI Service | `Dataset.ReadWrite.All` | Semantic model refresh | Planned |
| Power BI Service | `Gateway.Read.All` | Report impact across gateways | Planned |
| Power BI Service | `Item.Execute.All` | Running or scheduling notebooks, the Fabric runner and table maintenance in Fabric | Required for Fabric runs |
| Power BI Service | `Item.ReadWrite.All` | Creating and updating lakehouses, notebooks, semantic models, variable libraries | **Required** |
| Power BI Service | `OneLake.ReadWrite.All` | OneLake shortcuts and data access roles | Required for Shortcuts and Access |
| Power BI Service | `Pipeline.Deploy` | Deployment pipelines | Planned |
| Power BI Service | `Pipeline.ReadWrite.All` | Deployment pipelines | Planned |
| Power BI Service | `Report.Read.All` | Report impact | Planned |
| Power BI Service | `Tenant.Read.All` | Showing endorsement in the catalog; used only when you are a Fabric admin | Optional (admin consent) |
| Power BI Service | `Workspace.GitCommit.All` | Fabric Git | Planned |
| Power BI Service | `Workspace.GitUpdate.All` | Fabric Git | Planned |
| Power BI Service | `Workspace.ReadWrite.All` | Listing and creating workspaces, role assignments | **Required** |

"Planned" permissions aren't used yet. Add them now if you want to avoid another consent round when those features arrive. The Fabric permissions are listed under **Power BI Service** in Entra, even though the app calls `api.fabric.microsoft.com`.

### Step by step

1. **Register the app.** **App registrations > New registration**, any name (for example `Fabric Desktop`), single tenant. Under **Authentication > Add a platform > Mobile and desktop applications**, add the redirect URI `http://localhost`.
2. **Make sure your tenant has every API.** Do this before adding permissions; see [the next section](#could-not-grant-admin-consent-your-organization-does-not-have-a-subscription-or-service-principal). It's the step that usually goes wrong.
3. **Add the permissions** in the table, top to bottom.
4. **Grant admin consent.** **Grant admin consent for <your tenant>** on the API permissions page. You need Global Administrator, Privileged Role Administrator or Cloud Application Administrator. Without it, each user consents for themselves at first sign-in, if your tenant allows that.
5. **Give the app's id to Fabric Desktop.** Copy the **Application (client) ID** and set it for your Windows user, then restart the app:

   ```powershell
   setx FABRIC_DESKTOP_CLIENT_ID <application-client-id>
   ```

6. **Sign in again** so your token carries the new permissions: close the app, delete `%LOCALAPPDATA%\FabricDesktop\msal_cache.bin`, and open it again. It asks you to sign in once. (Without this, the new permissions take effect within an hour, when the saved token is renewed.)

### "Could not grant admin consent. Your organization does not have a subscription (or service principal)..."

This means your tenant has never used those Microsoft APIs, so Entra has no local record (service principal) of them to grant consent against. New and developer tenants often lack Azure Storage, Azure Key Vault and Azure Service Management, and sometimes Microsoft Graph and Power BI Service too. Create the missing ones once, then grant consent again.

With the Azure CLI, signed in as an admin (`az login --allow-no-subscriptions`), run one line per API from the error message:

| API | Command |
| --- | --- |
| Azure Key Vault | `az ad sp create --id cfa8b339-82a2-471a-a3c9-0fc0be7a4093` |
| Azure Service Management | `az ad sp create --id 797f4846-ba00-4fd7-ba43-dac1f8f63013` |
| Azure SQL Database | `az ad sp create --id 022907d3-0f1b-48f7-badc-1ba6abab6d66` |
| Azure Storage | `az ad sp create --id e406a681-f3d4-42a8-90b6-c2b029497af1` |
| Microsoft Graph | `az ad sp create --id 00000003-0000-0000-c000-000000000000` |
| Power BI Service | `az ad sp create --id 00000009-0000-0000-c000-000000000000` |

Or with Microsoft Graph PowerShell (`Connect-MgGraph -Scopes Application.ReadWrite.All`):

```powershell
New-MgServicePrincipal -AppId e406a681-f3d4-42a8-90b6-c2b029497af1   # Azure Storage; repeat with each id above
```

A command that says the service principal already exists is fine: that API is already there. Creating them needs Application Administrator, Cloud Application Administrator or Global Administrator. Then go back to **API permissions** and select **Grant admin consent** again.

If you'd rather not add an API yet (for example Azure Key Vault or Azure Service Management), remove its permissions from the app registration instead. Consent then succeeds for the rest, and you can add it later.

### When a feature says a permission is missing

The app names the permission it needed in **Activity** (the bell) under Details. Find it in the table, add it, grant consent, and sign in again (step 6).

## The headless agent

The agent's identity (a service principal or managed identity) needs no entries on the **API permissions** page. App identities can't use delegated permissions; Fabric gives them what their workspace roles allow. Set up, in alphabetical order:

| Where | Setting | Why |
| --- | --- | --- |
| Entra admin center | An app registration with a certificate (service principal), or a managed identity on the server | The agent's identity. Steps in [agent.md](agent.md#set-up-the-identity) |
| Entra admin center | A security group containing that identity | The Fabric tenant setting below is granted to a group |
| Fabric admin portal | **Tenant settings > Developer settings > Service principals can use Fabric APIs**, enabled for that group | Without it, every Fabric and OneLake call from the agent is refused |
| Fabric workspace (each environment the agent runs) | **Manage access > Add people or groups**: the identity as **Contributor** or higher | Reading and writing the layer lakehouses and `LH_Meta` |
| Fabric workspace (Keys, if the project uses surrogate keys) | The identity as **Contributor** or higher | Reading and writing `LH_Keys` |
| Key Vault (only if the agent reads secrets, roadmap M2) | **Key Vault Secrets User** role for the identity | Reading source secrets |

## Keeping this page right

When code asks for a new scope (`sidecar/fabric_desktop/auth.py`) or the agent needs a new role, add it to the table here in its alphabetical place, in the same pull request.
