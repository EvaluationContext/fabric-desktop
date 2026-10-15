"""Run the project's tests: the gate that custom code passes before it is promoted.

Every hook, custom check and custom step already names a test (``tests/test_x.py::test_y``).
This runs every ``test_*`` function in ``tests/test_*.py`` on this PC, the way pytest would for
plain functions with ``assert``, without needing pytest in the app. A test may take ``tmp_path``
(a new empty folder); other pytest fixtures aren't available and are reported as errors.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import inspect
import io
import sys
import tempfile
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .project import Project

_SUPPORTED = {"tmp_path"}


def discover(project: Project) -> list[Path]:
    return sorted((project.root / "tests").glob("test_*.py"))


def referenced(project: Project) -> dict[str, list[str]]:
    """"file::function" (or "file" for the whole file) -> what custom code points at it."""
    refs: dict[str, list[str]] = {}
    for e in project.entities.values():
        for kind, ref, settings in e.python_refs():
            test = settings.get("test")
            if test:
                refs.setdefault(test, []).append(f"{e.name} {kind} {ref}")
    return refs


def _load(project: Project, path: Path):
    """Import a test file fresh, as module tests.<name>, so edits since the last run count."""
    project.purge_code()
    for mod in [m for m in sys.modules if m == "tests" or m.startswith("tests.")]:
        del sys.modules[mod]
    name = f"tests.{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _failure(exc: BaseException) -> str:
    """The assertion or error, and the line of the test it came from."""
    tb = traceback.extract_tb(exc.__traceback__)
    where = next((f for f in reversed(tb) if "/tests/" in f.filename.replace("\\", "/")), tb[-1] if tb else None)
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    if where is not None:
        text += f" (line {where.lineno}: {where.line})" if where.line else f" (line {where.lineno})"
    return text


def run(project: Project) -> dict[str, Any]:
    """Run every test. Returns {"tests": [...], "passed", "failed", "at", "seconds", "missing"}."""
    started = time.monotonic()
    refs = referenced(project)
    tests: list[dict[str, Any]] = []
    for path in discover(project):
        rel = path.relative_to(project.root).as_posix()
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                module = _load(project, path)
        except BaseException as exc:  # noqa: BLE001 - a broken file is a failed test, not a crash
            tests.append({"id": rel, "file": rel, "name": "(import)", "status": "error",
                          "message": _failure(exc), "seconds": 0.0, "used_by": refs.get(rel, [])})
            continue
        functions = [(n, f) for n, f in vars(module).items()
                     if n.startswith("test") and inspect.isfunction(f) and f.__module__ == module.__name__]
        for name, fn in sorted(functions, key=lambda x: x[1].__code__.co_firstlineno):
            test_id = f"{rel}::{name}"
            used_by = refs.get(test_id, []) + refs.get(rel, [])
            params = [p for p, v in inspect.signature(fn).parameters.items() if v.default is inspect.Parameter.empty]
            unknown = [p for p in params if p not in _SUPPORTED]
            if unknown:
                tests.append({"id": test_id, "file": rel, "name": name, "status": "error", "seconds": 0.0,
                              "message": f"needs pytest fixture {', '.join(unknown)}, which this app's test run "
                                         f"doesn't provide; only tmp_path is available", "used_by": used_by})
                continue
            t0 = time.monotonic()
            status, message = "passed", ""
            with tempfile.TemporaryDirectory(prefix="fd-test-") as tmp:
                kwargs = {"tmp_path": Path(tmp)} if "tmp_path" in params else {}
                try:
                    with redirect_stdout(out), redirect_stderr(err):
                        fn(**kwargs)
                except AssertionError as exc:
                    status, message = "failed", _failure(exc)
                except BaseException as exc:  # noqa: BLE001
                    status, message = "error", _failure(exc)
            tests.append({"id": test_id, "file": rel, "name": name, "status": status, "message": message,
                          "seconds": round(time.monotonic() - t0, 3), "used_by": used_by})
    found = {t["id"] for t in tests} | {t["file"] for t in tests}
    missing = sorted(r for r in refs if r not in found and r.split("::")[0] not in found)
    return {"tests": tests, "passed": sum(t["status"] == "passed" for t in tests),
            "failed": sum(t["status"] != "passed" for t in tests), "missing": missing,
            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "seconds": round(time.monotonic() - started, 2), "version": project.version}


def gate_errors(result: dict[str, Any]) -> list[str]:
    """Reasons the promote gate gives for failing tests."""
    errors = [f"test {t['id']} {'failed' if t['status'] == 'failed' else 'errored'}: {t['message']}"
              for t in result["tests"] if t["status"] != "passed"]
    errors += [f"test {m} doesn't exist" for m in result["missing"]]
    return errors
