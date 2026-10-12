"""Startup component check, run by the Fabric Desktop shell with the user's Python.

Uses only the standard library, because it must work before the sidecar is installed.

    check            print one JSON object describing Python, the sidecar and the latest release
    install-sidecar  pip install the sidecar from the given commit, streaming pip's output
    download-app     download the latest release exe to a path, printing "progress <percent>" lines
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.request

REPO = "KratosDataGod/fabric-desktop"
RELEASE = f"https://github.com/{REPO}/releases/download/latest"
MIN_PYTHON = (3, 11)
COMMIT = re.compile(r"/archive/([0-9a-f]{40})\.zip")


def sidecar_url(commit: str) -> str:
    return f"https://github.com/{REPO}/archive/{commit}.zip#subdirectory=sidecar"


def installed_sidecar() -> dict:
    """Version of the installed sidecar and the commit it was installed from, when known."""
    from importlib import metadata

    try:
        dist = metadata.distribution("fabric-desktop")
    except metadata.PackageNotFoundError:
        return {"version": None, "commit": None, "editable": False}
    direct = json.loads(dist.read_text("direct_url.json") or "null") or {}
    commit = (direct.get("vcs_info") or {}).get("commit_id")
    if not commit:
        match = COMMIT.search(direct.get("url", ""))
        commit = match.group(1) if match else None
    editable = bool((direct.get("dir_info") or {}).get("editable"))
    return {"version": dist.version, "commit": commit, "editable": editable}


def latest_release() -> dict:
    """Commit of the newest published exe, from version.json in the "latest" release."""
    try:
        with urllib.request.urlopen(f"{RELEASE}/version.json", timeout=10) as resp:
            return {"commit": json.load(resp).get("commit"), "error": None}
    except Exception as exc:  # offline, or a release from before version.json existed
        return {"commit": None, "error": f"{type(exc).__name__}: {exc}"}


def check() -> dict:
    return {
        "python": ".".join(map(str, sys.version_info[:3])),
        "python_ok": sys.version_info >= MIN_PYTHON,
        "sidecar": installed_sidecar(),
        "latest": latest_release(),
    }


def install_sidecar(commit: str) -> int:
    url = sidecar_url(commit)
    pip = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--progress-bar", "off"]
    # The first install brings in any new dependencies. The second replaces the
    # package itself even when its version string has not changed.
    for args in ([url], ["--force-reinstall", "--no-deps", url]):
        code = subprocess.call(pip + args, stdout=sys.stdout, stderr=subprocess.STDOUT,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if code:
            return code
    return 0


def download_app(dest: str) -> int:
    with urllib.request.urlopen(f"{RELEASE}/fabric-desktop.exe", timeout=30) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done, last = 0, -1
        while chunk := resp.read(1 << 16):
            out.write(chunk)
            done += len(chunk)
            percent = done * 100 // total if total else 0
            if percent != last:
                print(f"progress {percent}", flush=True)
                last = percent
    return 0


def main(argv: list[str]) -> int:
    command, args = argv[0], argv[1:]
    if command == "check":
        print(json.dumps(check()))
        return 0
    if command == "install-sidecar":
        return install_sidecar(args[0])
    if command == "download-app":
        return download_app(args[0])
    print(f"unknown command {command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
