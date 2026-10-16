"""App settings: policy over environment over the person's choice over the built-in app ID."""

import json

import pytest

from fabric_desktop import app_settings
from fabric_desktop.service import Service

APP = "11111111-2222-3333-4444-555555555555"
OTHER = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture
def dirs(monkeypatch, tmp_path):
    monkeypatch.setenv("ProgramData", str(tmp_path / "pd"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "la"))
    monkeypatch.delenv("FABRIC_DESKTOP_CLIENT_ID", raising=False)
    monkeypatch.delenv("FABRIC_DESKTOP_BUILTIN_CLIENT_ID", raising=False)
    policy = tmp_path / "pd" / "FabricDesktop" / "policy.json"
    policy.parent.mkdir(parents=True)
    return policy


def test_defaults(dirs):
    s = app_settings.load()
    assert s["client_id"] == {"value": "", "source": "default"}
    assert s["channel"] == {"value": "stable", "source": "default"}
    assert s["show_preview"]["value"] is True


def test_client_id_order(dirs, monkeypatch):
    monkeypatch.setenv("FABRIC_DESKTOP_BUILTIN_CLIENT_ID", APP)
    assert app_settings.load()["client_id"] == {"value": APP, "source": "built_in"}
    app_settings.save({"client_id": OTHER})
    assert app_settings.load()["client_id"] == {"value": OTHER, "source": "user"}
    monkeypatch.setenv("FABRIC_DESKTOP_CLIENT_ID", APP)
    assert app_settings.load()["client_id"]["source"] == "environment"
    dirs.write_text(json.dumps({"client_id": OTHER}))
    assert app_settings.load()["client_id"] == {"value": OTHER, "source": "policy"}


def test_policy_locks_a_setting(dirs):
    dirs.write_text(json.dumps({"channel": "preview", "support_url": "mailto:help@contoso.com"}))
    s = app_settings.load()
    assert s["channel"] == {"value": "preview", "source": "policy"}
    assert s["support_url"]["value"] == "mailto:help@contoso.com"
    with pytest.raises(ValueError, match="organization"):
        app_settings.save({"channel": "stable"})


def test_save_checks_values(dirs):
    with pytest.raises(ValueError, match="isn't an app"):
        app_settings.save({"client_id": "not-a-guid"})
    with pytest.raises(ValueError, match="channel"):
        app_settings.save({"channel": "nightly"})
    with pytest.raises(ValueError, match="can change"):
        app_settings.save({"support_url": "https://x"})
    assert app_settings.save({"channel": "preview", "show_preview": False})["show_preview"]["value"] is False
    assert app_settings.save({"client_id": ""})["client_id"]["source"] == "default"


def test_service_uses_the_current_app_id(dirs, tmp_path):
    svc = Service(root=tmp_path / "app")
    with pytest.raises(ValueError, match="Settings > Account"):
        svc.auth
    svc.save_app_settings({"client_id": APP})
    assert svc.auth.client_id == APP
    svc.save_app_settings({"client_id": OTHER})
    assert svc.auth.client_id == OTHER  # a new ID means a new sign-in
    assert svc.sign_out() == {"user": None}


def test_leaves_the_sidecars_own_settings_alone(dirs, tmp_path):
    own = tmp_path / "la" / "FabricDesktop" / "settings.json"
    own.parent.mkdir(parents=True)
    own.write_text(json.dumps({"project": "C:/metadata", "teams_webhook": "https://x"}))
    app_settings.save({"channel": "preview", "show_preview": False})
    assert json.loads(own.read_text()) == {"project": "C:/metadata", "teams_webhook": "https://x"}
