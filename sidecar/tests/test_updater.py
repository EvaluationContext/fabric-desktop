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


@pytest.fixture(autouse=True)
def verified_in_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "verified_path", lambda: tmp_path / "engine-verified.json")


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
    assert set(report["sidecar"]) == {"version", "commit", "editable", "error", "blocked", "cached"}
    assert report["sidecar"]["error"] is None


class FakeFile(str):
    @property
    def suffix(self):
        return Path(self).suffix


class FakeBinaryDist:
    def __init__(self, name, files):
        self.metadata = {"Name": name}
        self.files = [FakeFile(f) for f in files]

    def locate_file(self, file):
        return Path("C:/Users/chris/site-packages") / file


def test_blocked_files_names_every_blocked_package(monkeypatch):
    monkeypatch.setattr(updater.sys, "platform", "win32")
    monkeypatch.setattr(updater, "smart_app_control", lambda: "on")
    monkeypatch.setattr(updater, "_sidecar_packages", lambda: [
        FakeBinaryDist("fabric-desktop", ["fabric_desktop/server.py"]),
        FakeBinaryDist("pyarrow", ["pyarrow/lib.cp314-win_amd64.pyd", "pyarrow/arrow.dll", "pyarrow/__init__.py"]),
        FakeBinaryDist("duckdb", ["_duckdb.cp314-win_amd64.pyd"]),
    ])
    blocked = updater.blocked_files(probe=lambda path: "pyarrow" in path)
    assert blocked["packages"] == {"pyarrow": ["pyarrow/lib.cp314-win_amd64.pyd", "pyarrow/arrow.dll"]}
    assert blocked["by"] == "smart_app_control"


def test_blocked_files_blames_a_policy_when_smart_app_control_is_off(monkeypatch):
    monkeypatch.setattr(updater.sys, "platform", "win32")
    monkeypatch.setattr(updater, "smart_app_control", lambda: "evaluation")
    monkeypatch.setattr(updater, "_sidecar_packages", lambda: [FakeBinaryDist("cffi", ["_cffi_backend.pyd"])])
    assert updater.blocked_files(probe=lambda path: True)["by"] == "policy"
    assert updater.blocked_files(probe=lambda path: False) is None


def test_blocked_files_only_on_windows(monkeypatch):
    monkeypatch.setattr(updater.sys, "platform", "linux")
    assert updater.blocked_files(probe=lambda path: True) is None


def test_sidecar_packages_follows_dependencies():
    names = {d.metadata["Name"].lower() for d in updater._sidecar_packages()}
    assert {"fabric-desktop", "pyarrow", "polars", "cryptography"} <= names


def test_install_sidecar_reports_blocked_files(monkeypatch, capsys):
    blocked = {"packages": {"pyarrow": ["pyarrow/lib.pyd"]}, "by": "policy", "smart_app_control": None, "location": "x"}
    monkeypatch.setattr(updater.subprocess, "call", lambda cmd, **kwargs: 0)
    monkeypatch.setattr(updater, "load_error", lambda: "ImportError: DLL load failed while importing lib")
    monkeypatch.setattr(updater, "blocked_files", lambda: blocked)
    assert updater.install_sidecar(SHA) == 1
    line = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("blocked "))
    assert json.loads(line[8:]) == blocked


def test_installed_sidecar_reads_the_bundled_engines_build(monkeypatch, tmp_path):
    (tmp_path / "engine.json").write_text(json.dumps({"commit": SHA}))
    monkeypatch.setattr(updater.sys, "executable", str(tmp_path / "python.exe"))
    monkeypatch.setattr(metadata, "distribution", lambda name: FakeDist({"url": "file:///D:/a/wheels/x.whl"}))
    assert updater.installed_sidecar()["commit"] == SHA


def test_check_skips_starting_a_build_that_started_before(monkeypatch):
    monkeypatch.setattr(updater, "latest_release", lambda: {"commit": SHA, "error": None})
    monkeypatch.setattr(updater, "installed_sidecar", lambda: {"version": "0.9.0", "commit": SHA, "editable": False})
    starts = []
    monkeypatch.setattr(updater, "load_error", lambda: starts.append(1))
    first, second = updater.check(), updater.check()
    assert len(starts) == 1
    assert first["sidecar"]["cached"] is False and second["sidecar"]["cached"] is True
    assert second["sidecar"]["error"] is None


def test_check_starts_again_after_a_new_build_or_a_failure(monkeypatch):
    monkeypatch.setattr(updater, "latest_release", lambda: {"commit": SHA, "error": None})
    build = {"version": "0.9.0", "commit": SHA, "editable": False}
    monkeypatch.setattr(updater, "installed_sidecar", lambda: dict(build))
    errors = ["ImportError: DLL load failed"]
    monkeypatch.setattr(updater, "load_error", lambda: errors.pop() if errors else None)
    assert updater.check()["sidecar"]["error"]  # a failure is never remembered
    assert updater.check()["sidecar"]["cached"] is False
    build["commit"] = "f" * 40
    assert updater.check()["sidecar"]["cached"] is False


def test_check_always_starts_a_developer_install(monkeypatch):
    monkeypatch.setattr(updater, "latest_release", lambda: {"commit": SHA, "error": None})
    monkeypatch.setattr(updater, "installed_sidecar", lambda: {"version": "0.9.0", "commit": None, "editable": True})
    starts = []
    monkeypatch.setattr(updater, "load_error", lambda: starts.append(1))
    updater.check(), updater.check()
    assert len(starts) == 2
