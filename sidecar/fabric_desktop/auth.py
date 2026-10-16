"""Delegated sign-in with MSAL.

One interactive sign-in yields two tokens: one for the Fabric REST API and
one for the Storage audience, which OneLake requires. The MSAL token cache is
persisted per user so later runs refresh silently.
"""

from __future__ import annotations

import os
from pathlib import Path

import msal

AUTHORITY = "https://login.microsoftonline.com/organizations"

FABRIC_SCOPES = [
    "https://api.fabric.microsoft.com/Workspace.ReadWrite.All",
    "https://api.fabric.microsoft.com/Item.ReadWrite.All",
]
# Running an item in Fabric (a notebook, the runner, table maintenance), now or on a schedule.
# Asked for the first time you run or schedule something there.
EXECUTE_SCOPES = FABRIC_SCOPES + ["https://api.fabric.microsoft.com/Item.Execute.All"]
STORAGE_SCOPES = ["https://storage.azure.com/user_impersonation"]
# Asked for the first time a feature needs them (one extra consent prompt, as you):
# OneLake shortcuts and data access roles, cloud connections, and finding a person by email.
ONELAKE_SCOPES = ["https://api.fabric.microsoft.com/OneLake.ReadWrite.All"]
CONNECTION_SCOPES = ["https://api.fabric.microsoft.com/Connection.Read.All"]
GRAPH_USER_SCOPES = ["https://graph.microsoft.com/User.ReadBasic.All"]
# Listing capacities (creating a workspace, the cost view's capacity size).
CAPACITY_SCOPES = ["https://api.fabric.microsoft.com/Capacity.Read.All"]
# Fabric admin read access, used only to show endorsement. Never asked for interactively:
# it works when the app registration has consent for it and the user is a Fabric admin.
ADMIN_SCOPES = ["https://analysis.windows.net/powerbi/api/Tenant.Read.All"]


def default_cache_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    return Path(base) / "FabricDesktop" / "msal_cache.bin"


class Authenticator:
    def __init__(self, client_id: str, cache_path: Path | None = None, interactive: bool = True):
        if not client_id:
            raise ValueError("Fabric Desktop doesn't know which app to sign in with yet. Open Settings > Account "
                             "and paste the app (client) ID your Fabric admin gave you.")
        self.client_id = client_id
        self.cache_path = cache_path or default_cache_path()
        self.interactive = interactive  # False for scheduled runs: nobody is there to sign in
        self.cache = msal.SerializableTokenCache()
        if self.cache_path.exists():
            self.cache.deserialize(self.cache_path.read_text())
        self.app = msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=self.cache)

    def _save(self) -> None:
        if self.cache.has_state_changed:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(self.cache.serialize())

    def _token(self, scopes: list[str]) -> str:
        result = None
        accounts = self.app.get_accounts()
        if accounts:
            result = self.app.acquire_token_silent(scopes, account=accounts[0])
        if not result and not self.interactive:
            raise RuntimeError("Your Fabric sign-in has expired. Open Fabric Desktop and sign in, "
                               "and scheduled runs will work again.")
        if not result:
            result = self.app.acquire_token_interactive(scopes)
        self._save()
        if "access_token" not in result:
            raise RuntimeError(f"Sign-in failed: {result.get('error')}: {result.get('error_description')}")
        return result["access_token"]

    def silent_token(self, scopes: list[str]) -> str | None:
        """A token for scopes the user has already consented to, without any prompt; None otherwise."""
        accounts = self.app.get_accounts()
        result = self.app.acquire_token_silent(scopes, account=accounts[0]) if accounts else None
        self._save()
        return result.get("access_token") if result else None

    def fabric_token(self) -> str:
        return self._token(FABRIC_SCOPES)

    def storage_token(self) -> str:
        return self._token(STORAGE_SCOPES)

    def sign_out(self) -> None:
        for account in self.app.get_accounts():
            self.app.remove_account(account)
        self._save()

    def signed_in_user(self) -> str | None:
        accounts = self.app.get_accounts()
        return accounts[0].get("username") if accounts else None

    def signed_in_tenant(self) -> str | None:
        """Directory (tenant) ID of the signed-in account; MSAL keeps it as "<object id>.<tenant id>"."""
        accounts = self.app.get_accounts()
        home = accounts[0].get("home_account_id", "") if accounts else ""
        return home.split(".", 1)[1] if "." in home else None
