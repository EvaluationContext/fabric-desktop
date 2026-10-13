"""Startup component check, run by the Fabric Desktop shell with the user's Python.

Uses only the standard library, because it must work before the sidecar is installed.

    check            print one JSON object describing Python, the sidecar (and whether it starts) and the latest release
    install-sidecar  pip install the sidecar from the given commit, streaming pip's output
    download-app     download the latest release exe to a path, printing "progress <percent>" lines;
                     with a commit after the path, refuse a download that is another build
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.request

REPO = "KratosDataGod/fabric-desktop"
RELEASE = f"https://github.com/{REPO}/releases/download/latest"
MIN_PYTHON = (3, 11)
API = f"https://api.github.com/repos/{REPO}/releases/tags/latest"
COMMIT = re.compile(r"/archive/([0-9a-f]{40})\.zip")
SHA = re.compile(r"[0-9a-f]{40}")


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


def load_error() -> str | None:
    """Start the sidecar's modules in a fresh Python, the way the app will. None when they load."""
    proc = subprocess.run([sys.executable, "-c", "import fabric_desktop.server"], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode == 0:
        return None
    lines = [line for line in proc.stderr.splitlines() if line.strip()]
    return lines[-1] if lines else f"exited with code {proc.returncode}"


def _get(url: str, timeout: int = 10):
    # Ask every cache on the way for a fresh copy: "latest" is replaced in place on each build.
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "Pragma": "no-cache",
                                               "User-Agent": "fabric-desktop-updater"})
    return urllib.request.urlopen(req, timeout=timeout)


def latest_release() -> dict:
    """Commit of the newest published exe.

    The GitHub API answers first (the release targets the commit it was built from);
    version.json in the release is the fallback when the API is rate limited.
    """
    errors = []
    try:
        with _get(API) as resp:
            commit = json.load(resp).get("target_commitish", "")
        if SHA.fullmatch(commit):
            return {"commit": commit, "error": None}
        errors.append(f"release targets {commit!r}, not a commit")
    except Exception as exc:  # offline or rate limited
        errors.append(f"{type(exc).__name__}: {exc}")
    try:
        with _get(f"{RELEASE}/version.json?nocache={int(time.time())}") as resp:
            return {"commit": json.load(resp).get("commit"), "error": None}
    except Exception as exc:  # offline, or a release from before version.json existed
        errors.append(f"{type(exc).__name__}: {exc}")
    return {"commit": None, "error": "; ".join(errors)}


def check() -> dict:
    sidecar = installed_sidecar()
    if sidecar["version"]:
        sidecar["error"] = load_error()
    return {
        "python": ".".join(map(str, sys.version_info[:3])),
        "python_ok": sys.version_info >= MIN_PYTHON,
        "sidecar": sidecar,
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
    error = load_error()
    if error:
        print(f"The local engine installed but does not start: {error}", flush=True)
        return 1
    return 0


def download_app(dest: str, commit: str = "") -> int:
    """Download the latest exe. With a commit, check the file is that build, since each
    exe carries its commit; a stale cached copy is fetched once more, then refused."""
    for attempt in range(2):
        url = f"{RELEASE}/fabric-desktop.exe" + (f"?nocache={int(time.time())}" if attempt else "")
        with _get(url, timeout=30) as resp, open(dest, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done, last = 0, -1
            while chunk := resp.read(1 << 16):
                out.write(chunk)
                done += len(chunk)
                percent = done * 100 // total if total else 0
                if percent != last:
                    print(f"progress {percent}", flush=True)
                    last = percent
        if not commit:
            return 0
        with open(dest, "rb") as f:
            if commit.encode() in f.read():
                return 0
        print(f"The download was not build {commit[:7]}; trying again", flush=True)
    print(f"The release download is not build {commit[:7]} yet. Try again in a few minutes.", flush=True)
    return 1


def main(argv: list[str]) -> int:
    command, args = argv[0], argv[1:]
    if command == "check":
        print(json.dumps(check()))
        return 0
    if command == "install-sidecar":
        return install_sidecar(args[0])
    if command == "download-app":
        return download_app(*args[:2])
    print(f"unknown command {command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
