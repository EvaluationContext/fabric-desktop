"""Unattended sign-in for the headless agent: a service principal or a managed identity.

The desktop app signs in as you (:class:`fabric_desktop.auth.Authenticator`). An agent has
nobody to sign in, so it uses an app identity instead. Both classes here answer the same
calls the rest of the engine makes on ``Authenticator`` (``fabric_token``, ``storage_token``,
``_token(scopes)``, ``silent_token``, ``signed_in_user``, ``signed_in_tenant``), so every
store, client and mixin works with either without change.

App identities can't hold delegated scopes such as ``Item.ReadWrite.All``; they get what
their roles in each workspace allow. So each request asks for the scope's resource with
``/.default``, and access comes from adding the principal to the workspace (Contributor or
higher) and from the Fabric tenant setting "Service principals can use Fabric APIs".
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import msal

from .auth import AUTHORITY, FABRIC_SCOPES, STORAGE_SCOPES


class Credential(Protocol):
    def fabric_token(self) -> str: ...
    def storage_token(self) -> str: ...
    def _token(self, scopes: list[str]) -> str: ...
    def silent_token(self, scopes: list[str]) -> str | None: ...
    def signed_in_user(self) -> str | None: ...
    def signed_in_tenant(self) -> str | None: ...


def resource_of(scope: str) -> str:
    """The resource a delegated scope belongs to: ``https://api.fabric.microsoft.com/Item.ReadWrite.All``
    is ``https://api.fabric.microsoft.com``; ``https://analysis.windows.net/powerbi/api/Tenant.Read.All``
    is ``https://analysis.windows.net/powerbi/api``."""
    parts = urlsplit(scope)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"{scope!r} isn't a scope URL")
    path = parts.path.rstrip("/")
    head, _, last = path.rpartition("/")
    path = head if last else path
    return f"{parts.scheme}://{parts.netloc}{path}"


def default_scope(scopes: list[str]) -> str:
    resources = {resource_of(s) for s in scopes}
    if len(resources) != 1:
        raise ValueError(f"One token can't cover several resources: {sorted(resources)}")
    return resources.pop() + "/.default"


class _AppCredential:
    """What both app identities share: one token per resource, cached by MSAL until near expiry."""

    tenant_id: str | None = None
    name = "app"

    def __init__(self) -> None:
        self._lock = threading.Lock()  # MSAL apps aren't safe to call from several threads at once

    def _acquire(self, resource_scope: str) -> dict[str, Any]:
        raise NotImplementedError

    def _token(self, scopes: list[str]) -> str:
        scope = default_scope(scopes)
        with self._lock:
            result = self._acquire(scope)
        if "access_token" not in result:
            raise RuntimeError(f"{self.name} couldn't get a token for {scope}: "
                               f"{result.get('error')}: {result.get('error_description')}")
        return result["access_token"]

    def fabric_token(self) -> str:
        return self._token(FABRIC_SCOPES)

    def storage_token(self) -> str:
        return self._token(STORAGE_SCOPES)

    def silent_token(self, scopes: list[str]) -> str | None:
        try:
            return self._token(scopes)
        except Exception:
            return None

    def signed_in_user(self) -> str | None:
        return self.name

    def signed_in_tenant(self) -> str | None:
        return self.tenant_id


def _certificate(path: Path, password: str | None) -> dict[str, Any]:
    """MSAL's client_credential for a certificate: a PEM with the private key and the certificate,
    or a PFX/P12 file."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.serialization import pkcs12

    data = path.read_bytes()
    pw = password.encode() if password else None
    if path.suffix.lower() in (".pfx", ".p12"):
        key, cert, _ = pkcs12.load_key_and_certificates(data, pw)
        if key is None or cert is None:
            raise ValueError(f"{path} has no private key and certificate")
    else:
        key = serialization.load_pem_private_key(data, pw)
        cert = x509.load_pem_x509_certificate(data)
    pem_key = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    return {"private_key": pem_key, "thumbprint": cert.fingerprint(hashes.SHA1()).hex(),  # noqa: S303 (Entra uses SHA-1)
            "public_certificate": cert.public_bytes(serialization.Encoding.PEM).decode()}


class ServicePrincipal(_AppCredential):
    """An Entra app registration signing in as itself, with a certificate (preferred) or a secret."""

    def __init__(self, tenant_id: str, client_id: str, certificate_path: str = "",
                 certificate_password_env: str = "", client_secret_env: str = "",
                 app: Any | None = None) -> None:
        super().__init__()
        if not tenant_id or not client_id:
            raise ValueError("A service principal needs tenant_id and client_id")
        self.tenant_id, self.client_id = tenant_id, client_id
        self.name = f"service principal {client_id}"
        if app is not None:  # tests
            self.app = app
            return
        if certificate_path:
            password = os.environ.get(certificate_password_env) if certificate_password_env else None
            credential: Any = _certificate(Path(certificate_path), password)
        elif client_secret_env:
            credential = os.environ.get(client_secret_env, "")
            if not credential:
                raise ValueError(f"The environment variable {client_secret_env} (the client secret) is empty")
        else:
            raise ValueError("A service principal needs certificate_path or client_secret_env")
        authority = AUTHORITY.rsplit("/", 1)[0] + "/" + tenant_id
        self.app = msal.ConfidentialClientApplication(client_id, authority=authority, client_credential=credential)

    def _acquire(self, resource_scope: str) -> dict[str, Any]:
        return self.app.acquire_token_for_client(scopes=[resource_scope])


class ManagedIdentity(_AppCredential):
    """The Azure VM's or Arc server's managed identity: no secret on the machine at all."""

    def __init__(self, client_id: str = "", tenant_id: str = "", client: Any | None = None) -> None:
        super().__init__()
        self.tenant_id = tenant_id or None
        self.name = f"managed identity {client_id}" if client_id else "system-assigned managed identity"
        if client is not None:  # tests
            self.client = client
            return
        import requests
        identity = msal.UserAssignedManagedIdentity(client_id=client_id) if client_id \
            else msal.SystemAssignedManagedIdentity()
        self.client = msal.ManagedIdentityClient(identity, http_client=requests.Session())

    def _acquire(self, resource_scope: str) -> dict[str, Any]:
        return self.client.acquire_token_for_client(resource=resource_scope.removesuffix("/.default"))


def from_config(auth: dict[str, Any]) -> Credential:
    """The credential an agent.toml ``[auth]`` table describes."""
    kind = auth.get("type", "")
    if kind == "service_principal":
        return ServicePrincipal(auth.get("tenant_id", ""), auth.get("client_id", ""),
                                auth.get("certificate_path", ""), auth.get("certificate_password_env", ""),
                                auth.get("client_secret_env", ""))
    if kind == "managed_identity":
        return ManagedIdentity(auth.get("client_id", ""), auth.get("tenant_id", ""))
    if kind == "user":
        from . import app_settings
        from .auth import Authenticator
        return Authenticator(auth.get("client_id", "") or app_settings.value("client_id"),
                             interactive=False)
    raise ValueError('[auth] type is "service_principal", "managed_identity" or "user"')
