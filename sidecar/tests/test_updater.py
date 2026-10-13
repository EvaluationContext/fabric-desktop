"""Tests for the startup updater the shell embeds (app/src-tauri/src/updater.py)."""

import importlib.util
import json
from importlib import metadata
from pathlib import Path

import pytest

SHA = "0123456789abcdef0123456789abcdef01234567"

spec = importlib.util.spec_from_file_location(
    "updater", Path(__file__).resolve().parents[2] / "app" / "src-tauri" / "src" / "updater.py")
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class FakeDist:
    def __init__(self, direct_url):
        self.version = "0.2.0"
        self._direct = direct_url

    def read_text(self, name):
        assert name == "direct_url.json"
        return None if self._direct is None else json.dumps(self._direct)


@pytest.mark.parametrize("direct, commit, editable", [
    ({"url": f"https://github.com/KratosDataGod/fabric-desktop/archive/{SHA}.zip", "archive_info": {}}, SHA, False),
    ({"url": "https://github.com/KratosDataGod/fabric-desktop/archive/refs/heads/main.zip", "archive_info": {}}, None, False),
    ({"url": "https://github.com/KratosDataGod/fabric-desktop.git", "vcs_info": {"vcs": "git", "commit_id": SHA}}, SHA, False),
    ({"url": "file:///C:/src/fabric-desktop/sidecar", "dir_info": {"editable": True}}, None, True),
    (None, None, False),
])
def test_installed_sidecar_reads_commit(monkeypatch, direct, commit, editable):
    monkeypatch.setattr(metadata, "distribution", lambda name: FakeDist(direct))
    assert updater.installed_sidecar() == {"version": "0.2.0", "commit": commit, "editable": editable}


def test_installed_sidecar_missing(monkeypatch):
    def missing(name):
        raise metadata.PackageNotFoundError(name)
    monkeypatch.setattr(metadata, "distribution", missing)
    assert updater.installed_sidecar()["version"] is None


class FakeResponse:
    def __init__(self, body: bytes):
        self.body, self.headers = body, {"Content-Length": str(len(body))}

    def read(self, n=-1):
        out, self.body = (self.body, b"") if n < 0 else (self.body[:n], self.body[n:])
        return out

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def serve(monkeypatch, routes):
    """Fake urlopen: the first route whose key is in the URL answers; an exception is raised."""
    seen = []

    def urlopen(req, timeout=None):
        url = req.full_url
        seen.append((url, req.get_header("Cache-control")))
        for key, answer in routes.items():
            if key in url:
                if isinstance(answer, Exception):
                    raise answer
                return FakeResponse(answer() if callable(answer) else answer)
        raise OSError(f"no route for {url}")
    monkeypatch.setattr(updater.urllib.request, "urlopen", urlopen)
    return seen


def test_latest_release_offline(monkeypatch):
    serve(monkeypatch, {"github": OSError("no network")})
    result = updater.latest_release()
    assert result["commit"] is None and "no network" in result["error"]


def test_latest_release_asks_the_api_without_caches(monkeypatch):
    seen = serve(monkeypatch, {"api.github.com": json.dumps({"target_commitish": SHA}).encode()})
    assert updater.latest_release() == {"commit": SHA, "error": None}
    assert seen == [(updater.API, "no-cache")]


def test_latest_release_falls_back_to_version_json(monkeypatch):
    other = "f" * 40
    seen = serve(monkeypatch, {"api.github.com": OSError("rate limited"),
                               "version.json": json.dumps({"commit": other}).encode()})
    assert updater.latest_release() == {"commit": other, "error": None}
    assert "nocache=" in seen[1][0]


def test_download_app_checks_the_build(monkeypatch, tmp_path):
    serve(monkeypatch, {"fabric-desktop.exe": b"MZ..." + SHA.encode() + b"..."})
    assert updater.download_app(str(tmp_path / "new.exe"), SHA) == 0


def test_download_app_refuses_a_stale_copy(monkeypatch, tmp_path, capsys):
    seen = serve(monkeypatch, {"fabric-desktop.exe": b"MZ...old build..."})
    assert updater.download_app(str(tmp_path / "new.exe"), SHA) == 1
    assert len(seen) == 2 and "nocache=" in seen[1][0]
    assert "not build 0123456 yet" in capsys.readouterr().out


def test_install_sidecar_reinstalls_from_commit(monkeypatch):
    calls = []
    monkeypatch.setattr(updater, "load_error", lambda: None)
    monkeypatch.setattr(updater.subprocess, "call", lambda cmd, **kwargs: calls.append(cmd) or 0)
    assert updater.install_sidecar(SHA) == 0
    url = f"https://github.com/KratosDataGod/fabric-desktop/archive/{SHA}.zip#subdirectory=sidecar"
    assert calls[0][-1] == url and "--force-reinstall" not in calls[0]
    assert calls[1][-3:] == ["--force-reinstall", "--no-deps", url]


def test_install_sidecar_stops_on_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(updater.subprocess, "call", lambda cmd, **kwargs: calls.append(cmd) or 1)
    assert updater.install_sidecar(SHA) == 1
    assert len(calls) == 1


def test_install_sidecar_fails_when_it_does_not_start(monkeypatch, capsys):
    monkeypatch.setattr(updater.subprocess, "call", lambda cmd, **kwargs: 0)
    monkeypatch.setattr(updater, "load_error", lambda: "ModuleNotFoundError: No module named 'fabric_desktop.meta'")
    assert updater.install_sidecar(SHA) == 1
    assert "fabric_desktop.meta" in capsys.readouterr().out


def test_load_error_reports_last_line(monkeypatch):
    class Done:
        returncode = 1
        stderr = "Traceback (most recent call last):\n  ...\nModuleNotFoundError: No module named 'x'\n"
    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **k: Done())
    assert updater.load_error() == "ModuleNotFoundError: No module named 'x'"


def test_load_error_none_when_it_starts():
    assert updater.load_error() is None  # the sidecar under test is installed


def test_check_reports_python(monkeypatch, capsys):
    monkeypatch.setattr(updater, "latest_release", lambda: {"commit": SHA, "error": None})
    assert updater.main(["check"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["python_ok"] is True
    assert report["latest"]["commit"] == SHA
    assert set(report["sidecar"]) == {"version", "commit", "editable", "error"}
    assert report["sidecar"]["error"] is None
