"""Run the metadata framework on Fabric capacity instead of this PC.

An environment whose compute is Fabric runs through one notebook in its workspace,
``nb_fabric_desktop_runner``, a Fabric Python notebook whose default lakehouse is the
environment's LH_Meta. Before a run the app uploads a bundle to
``LH_Meta/Files/_fabric_desktop/current``: this package's Python files, the metadata
project (minus .local and .git) and ``config.json`` with the environment's workspace
and lakehouse ids. The notebook runs :func:`run_bundle`, the same engine the PC runs,
reading and writing the same OneLake tables, so a run's rows, run log and checks are
identical wherever it ran. Results go to ``Files/_fabric_desktop/results/<request>.json``.

Schedules on the runner notebook run every entity from the last uploaded bundle.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

RUNNER = "nb_fabric_desktop_runner"
BUNDLE_DIR = "Files/_fabric_desktop/current"
RESULTS_DIR = "Files/_fabric_desktop/results"
MOUNT = "/lakehouse/default"
_SKIP = {".git", ".local", "__pycache__", ".pytest_cache"}


def bundle_files(project_root: Path, config: dict[str, Any]) -> dict[str, bytes]:
    """Every file the runner needs, keyed by its path inside the bundle."""
    files: dict[str, bytes] = {}
    package = Path(__file__).parent
    for p in sorted(package.rglob("*.py")):
        rel = p.relative_to(package.parent)
        if not set(rel.parts) & _SKIP:
            files[f"package/{rel.as_posix()}"] = p.read_bytes()
    for p in sorted(Path(project_root).rglob("*")):
        rel = p.relative_to(project_root)
        if p.is_file() and not set(rel.parts) & _SKIP and p.suffix != ".pyc":
            files[f"project/{rel.as_posix()}"] = p.read_bytes()
    files["config.json"] = json.dumps(config, indent=2, sort_keys=True).encode()
    return files


def bundle_hash(files: dict[str, bytes]) -> str:
    h = hashlib.sha1()
    for path in sorted(files):
        h.update(path.encode() + b"\0" + files[path] + b"\0")
    return h.hexdigest()[:12]


# ---- the runner notebook

_SETUP = '''# Fabric Desktop runner: runs the metadata framework on this workspace's capacity.
# Published by Fabric Desktop; changes here are replaced on the next run from the app.
import subprocess, sys
from importlib.metadata import version as _version

def _older(pkg, want):
    try:
        return tuple(int(x) for x in _version(pkg).split(".")[:2]) < want
    except Exception:
        return True

_need = [spec for pkg, want, spec in (("deltalake", (0, 25), "deltalake>=0.25"), ("polars", (1, 20), "polars>=1.20"))
         if _older(pkg, want)]
if _need:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", *_need])
BUNDLE = "%(mount)s/%(bundle)s"
sys.path.insert(0, BUNDLE + "/package")
'''

_RUN = '''import notebookutils
from fabric_desktop.fabric_compute import run_bundle

result = run_bundle(BUNDLE, entities=entities, backfill=backfill, cascade=cascade, request_id=request_id,
                    storage_token=lambda: notebookutils.credentials.getToken("storage"),
                    fabric_token=lambda: notebookutils.credentials.getToken("pbi"))
for r in result["results"]:
    print(f"{r['status']:>15}  {r['entity']}  {r.get('rows_written', 0)} rows  {r.get('message', '')}")
result["raise_if_failed"]()
'''


def runner_ipynb(workspace_id: str, meta_lakehouse_id: str, meta_lakehouse: str = "LH_Meta") -> dict[str, Any]:
    from .fabric_run import PYTHON_NOTEBOOK_METADATA

    def code(src: str, tags: list[str] | None = None) -> dict[str, Any]:
        return {"cell_type": "code", "metadata": {"tags": tags} if tags else {}, "execution_count": None,
                "outputs": [], "source": src.splitlines(keepends=True)}

    params = ('entities = ""   # comma-separated entity names; empty runs every entity\n'
              'backfill = ""   # "", "full", "none" or "migrate"\n'
              'cascade = False  # rebuild one entity and everything downstream\n'
              'request_id = ""  # set by Fabric Desktop; scheduled runs leave it empty\n')
    return {"nbformat": 4, "nbformat_minor": 5,
            "metadata": PYTHON_NOTEBOOK_METADATA | {"dependencies": {"lakehouse": {
                "default_lakehouse": meta_lakehouse_id, "default_lakehouse_name": meta_lakehouse,
                "default_lakehouse_workspace_id": workspace_id}}},
            "cells": [{"cell_type": "markdown", "metadata": {}, "source": [
                "# Fabric Desktop runner\n",
                "Runs the metadata project uploaded by Fabric Desktop to `Files/_fabric_desktop/current` "
                "of this notebook's lakehouse. Start runs and schedules from **Metadata** in the app.\n"]},
                      code(params, ["parameters"]),
                      code(_SETUP % {"mount": MOUNT, "bundle": BUNDLE_DIR}),
                      code(_RUN)]}


def run_parameters(entities: list[str] | None, backfill: str | None, cascade: bool, request_id: str) -> dict[str, Any]:
    return {"parameters": {
        "entities": {"value": ",".join(entities or []), "type": "string"},
        "backfill": {"value": backfill or "", "type": "string"},
        "cascade": {"value": bool(cascade), "type": "bool"},
        "request_id": {"value": request_id, "type": "string"},
    }}


# ---- what runs inside Fabric

def run_bundle(bundle: str | Path, entities: str = "", backfill: str = "", cascade: bool = False,
               request_id: str = "", storage_token: Callable[[], str] | None = None,
               fabric_token: Callable[[], str] | None = None, stores: tuple[Any, Any] | None = None,
               results_dir: str | Path | None = None) -> dict[str, Any]:
    """Run the bundled project against the environment's OneLake tables and save the results.

    ``stores`` (main store, keys store) replaces the OneLake stores in tests."""
    from .meta import Engine, OneLakeStore, Project
    from .meta.engine import RunResult

    bundle = Path(bundle)
    config = json.loads((bundle / "config.json").read_text(encoding="utf-8"))
    work = Path(tempfile.mkdtemp(prefix="fabric_desktop_"))
    # A local copy: imports and file reads are much faster than over the lakehouse mount.
    shutil.copytree(bundle / "project", work / "project")
    project = Project(work / "project")
    env = config["env"]
    if stores is None:
        store = OneLakeStore(env, config["workspace_id"], config["lakehouse_ids"], storage_token, work / "cache")
        keys_store = (OneLakeStore(env, config["keys_workspace_id"], config["keys_lakehouse_ids"], storage_token,
                                   work / "cache") if config.get("keys_workspace_id") else None)
    else:
        store, keys_store = stores
    names = [n.strip() for n in entities.split(",") if n.strip()] or None
    started = dt.datetime.now(dt.timezone.utc)
    engine = Engine(project, store, keys_store=keys_store)
    if cascade and names and len(names) == 1:
        results: list[RunResult] = engine.backfill(names[0])
    else:
        results = engine.run_many(names, backfill=backfill or None)
    out = [r.to_dict() for r in results]
    if fabric_token and config.get("optimize_after_run", True):
        _optimize(project, config, out, fabric_token)
    request_id = request_id or f"scheduled-{started:%Y%m%d-%H%M%S}"
    record = {"request_id": request_id, "env": env, "version": project.version, "compute": "fabric",
              "started_at": started.isoformat(timespec="seconds"),
              "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "results": out}
    folder = Path(results_dir) if results_dir else Path(MOUNT) / RESULTS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{request_id}.json").write_text(json.dumps(record, default=str, indent=2), encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)

    def raise_if_failed() -> None:
        # So Fabric's Monitoring hub shows the job as failed too.
        bad = [r for r in out if r["status"] in ("failed", "needs_decision")]
        if bad:
            raise RuntimeError("; ".join(f"{r['entity']}: {r['message']}" for r in bad))

    return record | {"raise_if_failed": raise_if_failed}


def _optimize(project: Any, config: dict[str, Any], results: list[dict[str, Any]],
              fabric_token: Callable[[], str]) -> None:
    from .fabric_api import FabricClient
    fabric = FabricClient(fabric_token)
    for r in results:
        e = project.entities.get(r["entity"])
        if not e or r.get("status") != "ok" or not r.get("rows_written"):
            continue
        lakehouse_id = config["lakehouse_ids"].get(project.layer(e.layer).lakehouse)
        try:
            fabric.run_table_maintenance(config["workspace_id"], lakehouse_id, e.table, e.schema)
            r["maintenance"] = "queued"
        except Exception as exc:  # the run itself succeeded; say why the optimize didn't start
            r["maintenance"], r["maintenance_message"] = "Failed", str(exc)
