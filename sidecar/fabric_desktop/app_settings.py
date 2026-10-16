"""App settings: what this person chose, what their organization set, and what the build ships with.

Three places, strongest first:

    policy    %ProgramData%\\FabricDesktop\\policy.json, pushed by IT (Intune, Group Policy files).
              Anything set here wins and shows as "Set by your organization".
    user      %LOCALAPPDATA%\\FabricDesktop\\app-settings.json, written by the Settings page (the
              sidecar's settings.json, beside it, holds other things).
    built in  engine.json next to the engine's python.exe (written by CI), or
              FABRIC_DESKTOP_BUILTIN_CLIENT_ID for builds that run a Python of their own.

The FABRIC_DESKTOP_CLIENT_ID environment variable still works, between policy and user, so
installs set up with setx keep signing in as before.

Keys:
    client_id        the Microsoft Entra app (client) ID the app signs in with
    channel          "stable" (default) or "preview": which release the app updates to
    show_preview     true to show features still in preview (default true)
    support_url      where "Report a problem" goes: an https link or a mailto: address
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

CHANNELS = ("stable", "preview")
DEFAULTS: dict[str, Any] = {"client_id": "", "channel": "stable", "show_preview": True, "support_url": ""}
USER_KEYS = ("client_id", "channel", "show_preview")  # support_url is for IT only
GUID_CHARS = set("0123456789abcdefABCDEF-")


def policy_path() -> Path:
    base = os.environ.get("ProgramData") or os.environ.get("PROGRAMDATA") or "/etc"
    return Path(base) / "FabricDesktop" / "policy.json"


def user_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "FabricDesktop" / "app-settings.json"


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if k in DEFAULTS} if isinstance(data, dict) else {}


def built_in_client_id() -> str:
    env = os.environ.get("FABRIC_DESKTOP_BUILTIN_CLIENT_ID", "")
    if env:
        return env
    info = _read_engine_info()
    return str(info.get("client_id") or "")


def _read_engine_info() -> dict[str, Any]:
    try:
        return json.loads(Path(sys.executable).with_name("engine.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def valid_client_id(value: str) -> bool:
    value = value.strip()
    return len(value) == 36 and set(value) <= GUID_CHARS and value.count("-") == 4


def load() -> dict[str, Any]:
    """Each setting's value and where it came from: policy, environment, user, built_in or default."""
    policy, user = _read(policy_path()), _read(user_path())
    out: dict[str, Any] = {}
    for key, default in DEFAULTS.items():
        candidates = [("policy", policy.get(key))]
        if key == "client_id":
            candidates.append(("environment", os.environ.get("FABRIC_DESKTOP_CLIENT_ID")))
        if key in USER_KEYS:
            candidates.append(("user", user.get(key)))
        if key == "client_id":
            candidates.append(("built_in", built_in_client_id()))
        value, source = default, "default"
        for where, candidate in candidates:
            if candidate not in (None, ""):
                value, source = candidate, where
                break
        if key == "channel" and value not in CHANNELS:
            value, source = default, "default"
        out[key] = {"value": value, "source": source}
    return out


def value(key: str) -> Any:
    return load()[key]["value"]


def save(changes: dict[str, Any]) -> dict[str, Any]:
    """Saves this person's choices. A setting the organization fixed can't be changed here."""
    current = load()
    user = _read(user_path())
    for key, new in changes.items():
        if key not in USER_KEYS:
            raise ValueError(f"{key} is not a setting you can change")
        if current[key]["source"] == "policy":
            raise ValueError(f"Your organization sets {key}, so it can't be changed here")
        if key == "channel" and new not in CHANNELS:
            raise ValueError(f"channel must be one of {', '.join(CHANNELS)}")
        if key == "client_id":
            new = str(new or "").strip()
            if new and not valid_client_id(new):
                raise ValueError("That isn't an app (client) ID. It looks like 00000000-0000-0000-0000-000000000000.")
        if key == "show_preview":
            new = bool(new)
        if new in (None, ""):
            user.pop(key, None)
        else:
            user[key] = new
    path = user_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(user, indent=2), encoding="utf-8")
    return load()
