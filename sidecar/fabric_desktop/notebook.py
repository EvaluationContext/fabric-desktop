"""Notebooks: a local Python kernel and .ipynb storage.

The kernel keeps one namespace across cells, like Jupyter and Fabric. A cell runs on its own
thread, so the app can show printed output while it runs (``start`` then ``poll``) and Cancel
can stop it without losing the variables earlier cells made (``interrupt``). When a cell's last
statement is an expression its value is shown: data frames as a table, charts as images, objects
with an HTML view as HTML, anything else as its repr. ``display(...)`` shows values mid-cell.

Every cell has what a Fabric notebook has: ``spark`` (see spark_local), ``notebookutils`` and
``mssparkutils`` (see notebookutils_local) and ``display``. ``%%sql`` cells run Spark SQL,
``%%html`` shows HTML, and ``%run`` runs another notebook in this one's namespace.
"""

from __future__ import annotations

import ast
import base64
import contextlib
import ctypes
import inspect
import io
import json
import keyword
import os
import re
import sys
import threading
import time
import traceback
import warnings
from pathlib import Path
from typing import Any, Callable

import pyarrow as pa

# Charts are drawn off screen and shown as images in the cell.
os.environ.setdefault("MPLBACKEND", "Agg")

PREVIEW_ROWS = 1000
_CELL_FILE = "<cell>"
_SECRET_NAME = re.compile(r"(pass(word)?|pwd|secret|token|key|credential)", re.I)
_SPARK_ONLY = re.compile(r"^\s*%%(spark|sparkr|csharp)\b")


def _no_input(*_args, **_kwargs):
    raise RuntimeError("input() is not supported in notebook cells.")


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def as_table(value: Any) -> pa.Table | None:
    """Arrow view of a data frame-like value, or None if it is not one."""
    module = type(value).__module__
    if isinstance(value, pa.Table):
        return value
    if module.startswith("duckdb.experimental.spark"):
        if not hasattr(value, "relation"):
            return None
        rel = value.relation.limit(PREVIEW_ROWS + 1)
        return (getattr(rel, "to_arrow_table", None) or rel.arrow)()
    if module.startswith("polars"):
        if hasattr(value, "collect"):
            value = value.head(PREVIEW_ROWS + 1).collect()
        return value.to_arrow() if hasattr(value, "to_arrow") else None
    if module.startswith("duckdb") and hasattr(value, "limit"):
        rel = value.limit(PREVIEW_ROWS + 1)
        return (getattr(rel, "to_arrow_table", None) or rel.arrow)()
    if module.startswith("pandas") and hasattr(value, "columns"):
        return pa.Table.from_pandas(value.head(PREVIEW_ROWS + 1), preserve_index=False)
    return None


def _total_rows(value: Any, table: pa.Table) -> int | None:
    """All rows, when that is cheap to know; None means "more than the preview"."""
    if type(value).__module__.startswith("duckdb.experimental.spark"):
        return table.num_rows if table.num_rows <= PREVIEW_ROWS else None
    total = getattr(value, "height", None)
    if total is None and not hasattr(value, "collect"):
        try:
            total = len(value)
        except TypeError:
            total = None
    if total is None:
        return table.num_rows if table.num_rows <= PREVIEW_ROWS else None
    return total


def _png(fig: Any) -> dict[str, Any]:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=110)
    return {"type": "image", "mime": "image/png", "data": base64.b64encode(buf.getvalue()).decode("ascii")}


def display(value: Any) -> dict[str, Any]:
    """The output a value shows as: a table, an image, HTML or text."""
    table = as_table(value)
    if table is not None:
        rows = [[_json_value(v) for v in row.values()] for row in table.slice(0, PREVIEW_ROWS).to_pylist()]
        total = _total_rows(value, table)
        return {"type": "table", "columns": table.schema.names, "rows": rows,
                "total_rows": total if total is not None else f"more than {PREVIEW_ROWS}"}
    module = type(value).__module__
    if module.startswith("matplotlib"):
        fig = value if hasattr(value, "savefig") else getattr(value, "figure", None) or getattr(value, "get_figure", lambda: None)()
        if fig is not None and hasattr(fig, "savefig"):
            out = _png(fig)
            with contextlib.suppress(Exception):
                import matplotlib.pyplot as plt
                plt.close(fig)
            return out
    if module.startswith("plotly") and hasattr(value, "to_html"):
        return {"type": "html", "html": value.to_html(include_plotlyjs=True, full_html=True)}
    for method, make in (("_repr_html_", lambda r: {"type": "html", "html": r}),
                         ("_repr_svg_", lambda r: {"type": "image", "mime": "image/svg+xml",
                                                   "data": base64.b64encode(r.encode()).decode("ascii")}),
                         ("_repr_png_", lambda r: {"type": "image", "mime": "image/png",
                                                   "data": base64.b64encode(r).decode("ascii")})):
        fn = getattr(value, method, None)
        if callable(fn) and not isinstance(value, type):
            with contextlib.suppress(Exception):
                rendered = fn()
                if rendered:
                    return make(rendered)
    return {"type": "text", "text": repr(value)}


class _Run:
    """One cell run: its outputs so far, shared between the cell's thread and ``poll``."""

    def __init__(self, count: int):
        self.count = count
        self.lock = threading.Lock()
        self.outputs: list[dict[str, Any]] = []
        self.version = 0
        self.status = "running"
        self.started = time.monotonic()
        self.seconds: float | None = None
        self.thread: threading.Thread | None = None
        self.done = threading.Event()

    def write(self, text: str) -> None:
        if not text:
            return
        with self.lock:
            if self.outputs and self.outputs[-1]["type"] == "stream":
                self.outputs[-1] = {"type": "stream", "text": self.outputs[-1]["text"] + text}
            else:
                self.outputs.append({"type": "stream", "text": text})
            self.version += 1

    def add(self, output: dict[str, Any]) -> None:
        with self.lock:
            self.outputs.append(output)
            self.version += 1

    def finish(self, status: str) -> None:
        with self.lock:
            self.status = status
            self.seconds = round(time.monotonic() - self.started, 2)
            self.version += 1
        self.done.set()

    def snapshot(self, known_version: int = -1) -> dict[str, Any]:
        with self.lock:
            changed = self.version != known_version
            return {"status": self.status, "execution_count": self.count, "version": self.version,
                    "seconds": self.seconds if self.seconds is not None else round(time.monotonic() - self.started, 2),
                    "outputs": list(self.outputs) if changed else None}


class _Router(io.TextIOBase):
    """sys.stdout / sys.stderr: what a running cell's thread writes goes to that cell's output."""

    def __init__(self, fallback: Any):
        self.fallback = fallback
        self.targets: dict[int, _Run] = {}

    def writable(self) -> bool:
        return True

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"

    def write(self, text: str) -> int:
        run = self.targets.get(threading.get_ident())
        if run is not None:
            run.write(text)
        elif self.fallback is not None:
            self.fallback.write(text)
        return len(text)

    def flush(self) -> None:
        if self.fallback is not None and self.targets.get(threading.get_ident()) is None:
            with contextlib.suppress(Exception):
                self.fallback.flush()


def _routers() -> tuple[_Router, _Router]:
    if not isinstance(sys.stdout, _Router):
        sys.stdout = _Router(sys.stdout)
    if not isinstance(sys.stderr, _Router):
        sys.stderr = _Router(sys.stderr)
    return sys.stdout, sys.stderr  # type: ignore[return-value]


class Kernel:
    def __init__(self, token: Callable[[str], str] | None = None,
                 load_notebook: Callable[[str], list[dict[str, Any]]] | None = None):
        self.token = token
        self.load_notebook = load_notebook
        self.notebook_name = ""
        self._current: _Run | None = None
        self._local = threading.local()
        self._secrets: set[str] = set()
        self.restart()

    # ---- namespace

    def _base_namespace(self) -> dict[str, Any]:
        from . import notebookutils_local
        ns: dict[str, Any] = {"__name__": "__main__", "input": _no_input, "display": self._display}
        utils = notebookutils_local.build(notebookutils_local.Host(
            token=self.token, run_notebook=self.run_notebook, notebook_name=lambda: self.notebook_name))
        get_secret = utils.credentials.getSecret

        def remembered_secret(*args: Any, **kwargs: Any) -> str:
            value = get_secret(*args, **kwargs)
            self._secrets.add(value)
            return value
        utils.credentials.getSecret = remembered_secret
        sys.modules["notebookutils"] = utils
        sys.modules["mssparkutils"] = utils
        ns.update(notebookutils=utils, mssparkutils=utils)
        try:
            from . import spark_local
            spark_local.install(ns)
        except Exception as exc:  # noqa: BLE001 - plain Python still works without it
            print(f"spark is not available on this PC: {exc}", file=sys.__stderr__)
        return ns

    def restart(self) -> None:
        if self._current is not None and not self._current.done.is_set():
            self.interrupt()
        with contextlib.suppress(Exception):
            from . import spark_local
            spark_local.reset()
        self.namespace: dict[str, Any] = self._base_namespace()
        self.count = 0

    def _display(self, *values: Any, summary: bool = False, **_kw: Any) -> None:
        run = getattr(self._local, "run", None)
        for value in values:
            out = display(value)
            if summary and out["type"] == "table":
                out["summary"] = True
            if run is not None:
                run.add(out)
            else:
                print(out.get("text", out))

    # ---- running a cell

    def start(self, code: str) -> dict[str, Any]:
        """Start ``code`` on its own thread; ``poll`` reports its output as it comes."""
        if self._current is not None and not self._current.done.is_set():
            raise RuntimeError("A cell is already running. Wait for it, or Cancel it.")
        self.count += 1
        run = _Run(self.count)
        self._current = run
        run.thread = threading.Thread(target=self._execute, args=(code, run), daemon=True, name=f"cell-{self.count}")
        run.thread.start()
        return run.snapshot()

    def poll(self, known_version: int = -1, wait: float = 0.0) -> dict[str, Any]:
        """The running (or last) cell: status and, when they changed since ``known_version``, its outputs.

        ``wait`` seconds is how long to wait for news before answering, so the app can poll slowly."""
        run = self._current
        if run is None:
            return {"status": "idle", "execution_count": self.count, "version": 0, "seconds": 0, "outputs": []}
        deadline = time.monotonic() + min(wait, 2.0)
        while run.version == known_version and not run.done.is_set() and time.monotonic() < deadline:
            run.done.wait(0.05)
        return run.snapshot(known_version)

    def interrupt(self) -> dict[str, Any]:
        """Stop the running cell, keeping the namespace. Native code (a DuckDB query) is asked to stop too."""
        run = self._current
        if run is None or run.done.is_set() or run.thread is None:
            return {"interrupted": False}
        with contextlib.suppress(Exception):
            from . import spark_local
            spark_local.interrupt()
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(run.thread.ident), ctypes.py_object(KeyboardInterrupt))
        run.done.wait(2.0)
        return {"interrupted": True, "stopped": run.done.is_set()}

    def run(self, code: str) -> dict[str, Any]:
        """Run ``code`` to the end and return its result (used by tests and scripts)."""
        self.start(code)
        run = self._current
        assert run is not None
        run.done.wait()
        snap = run.snapshot()
        return {"status": snap["status"], "execution_count": snap["execution_count"], "outputs": snap["outputs"]}

    def _execute(self, code: str, run: _Run) -> None:
        from .notebookutils_local import NotebookExit
        out, err = _routers()
        ident = threading.get_ident()
        out.targets[ident] = err.targets[ident] = run
        self._local.run = run
        status = "error"
        try:
            try:
                status = self._exec(code, self.namespace, run)
                self._flush_figures(run)
            except NotebookExit:
                status = "ok"
            except KeyboardInterrupt:
                status = "cancelled"
                run.add({"type": "text", "text": "Cancelled. Variables are kept."})
        except KeyboardInterrupt:  # an interrupt that landed while the first one was being handled
            status = "cancelled"
        finally:
            out.targets.pop(ident, None)
            err.targets.pop(ident, None)
            self._local.run = None
            run.finish(status)

    def _exec(self, code: str, namespace: dict[str, Any], run: _Run | None) -> str:
        """Run one cell's code in ``namespace``; returns "ok" or "error"."""
        from .notebookutils_local import NotebookExit
        head = code.lstrip()
        if head.startswith("%%sql"):
            return self._run_sql(head[len("%%sql"):], run)
        if head.startswith("%%html"):
            self._emit(run, {"type": "html", "html": head[len("%%html"):]})
            return "ok"
        if head.startswith("%%configure"):
            print("Note: %%configure sets Spark session options in Fabric. On this PC it has nothing to set.")
            return "ok"
        if _SPARK_ONLY.match(head):
            self._emit(run, {"type": "error", "ename": "RunsInFabric", "evalue":
                             "Scala, R and .NET cells need Fabric's Spark. Switch Runs on to Fabric to run them.",
                             "traceback": ""})
            return "error"
        if re.match(r"%%(pyspark|python)\b", head):
            code = head.split("\n", 1)[1] if "\n" in head else ""
        code = _line_magics(code)
        namespace.setdefault("__fd_magic__", self._magic)
        self._patch_pyplot(run)
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message=".*non-interactive.*")
                tree = ast.parse(code, _CELL_FILE)
                last = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
                exec(compile(tree, _CELL_FILE, "exec"), namespace)
                value = None
                if last is not None:
                    value = eval(compile(ast.Expression(last.value), _CELL_FILE, "eval"), namespace)
                if value is not None:
                    namespace["_"] = value
                    self._emit(run, display(value))
        except NotebookExit as done:
            print(f"Notebook exited with value: {done.value}")
            namespace["__fd_exit__"] = done.value
            raise
        except (Exception, SystemExit) as exc:  # a cell must never take the kernel down
            self._emit(run, self._error(exc))
            return "error"
        return "ok"

    def _emit(self, run: _Run | None, output: dict[str, Any]) -> None:
        if run is not None:
            run.add(output)

    def _patch_pyplot(self, run: _Run | None) -> None:
        plt = sys.modules.get("matplotlib.pyplot")
        if plt is not None and run is not None and not getattr(plt.show, "_fabric_desktop", False):
            def show(*_a: Any, **_kw: Any) -> None:
                current = getattr(self._local, "run", None)
                if current is not None:
                    self._flush_figures(current)
            show._fabric_desktop = True  # type: ignore[attr-defined]
            plt.show = show

    @staticmethod
    def _flush_figures(run: _Run) -> None:
        plt = sys.modules.get("matplotlib.pyplot")
        if plt is None:
            return
        for num in plt.get_fignums():
            with contextlib.suppress(Exception):
                run.add(_png(plt.figure(num)))
        plt.close("all")

    def _run_sql(self, query: str, run: _Run | None) -> str:
        """A %%sql cell: Spark SQL on this PC, through the notebook's spark session.

        It reads lakehouse tables and the session's temp views; CREATE TABLE AS and INSERT INTO
        write lakehouse tables. MERGE, UPDATE and DELETE run in Fabric."""
        if re.search(r"\b(MERGE\s+INTO|UPDATE\s+\S+\s+SET|DELETE\s+FROM)\b", query, re.IGNORECASE):
            text = ("This cell changes tables, so it runs in Fabric once the notebook is published. "
                    "On this PC, Metadata > Run does the same surrogate key MERGE.\n")
            self._emit(run, {"type": "stream", "text": text})
            return "ok"
        try:
            from . import spark_local
            result = spark_local.session().sql(query)
            if result is not None and result.columns:
                self._emit(run, display(result))
            return "ok"
        except Exception as exc:  # noqa: BLE001
            self._emit(run, self._error(exc))
            return "error"

    # ---- magics and other notebooks

    def _magic(self, name: str, line: str) -> Any:
        if name == "run":
            target, _, rest = line.strip().partition(" ")
            args = ast.literal_eval(rest.strip()) if rest.strip() else {}
            return self._run_notebook_in(target.strip("\"'"), self.namespace, args)
        if name in ("pip", "conda"):
            print(f"Note: %{name} installs packages in Fabric's session. On this PC the engine's packages "
                  f"are fixed, so this line was skipped.")
            return None
        if name == "time":
            started = time.monotonic()
            value = eval(compile(line, _CELL_FILE, "eval"), self.namespace) if line.strip() else None
            print(f"Wall time: {time.monotonic() - started:.3f} s")
            return value
        raise ValueError(f"%{name} isn't supported on this PC. Supported: %run, %pip (skipped), %time.")

    def run_notebook(self, name: str, timeout_seconds: int = 90, arguments: dict[str, Any] | None = None) -> Any:
        """notebookutils.notebook.run: the other notebook runs in a fresh namespace; returns its exit value."""
        ns = self._base_namespace()
        ns["__fd_magic__"] = self._magic
        return self._run_notebook_in(name, ns, arguments or {})

    def _run_notebook_in(self, name: str, namespace: dict[str, Any], arguments: dict[str, Any]) -> Any:
        from .notebookutils_local import NotebookExit
        if self.load_notebook is None:
            raise RuntimeError("Other notebooks can't be opened here.")
        cells = self.load_notebook(name.rsplit("/", 1)[-1])
        has_parameters = any("parameters" in (c.get("meta") or {}).get("tags", []) for c in cells)
        if arguments and not has_parameters:
            namespace.update(arguments)
        run = getattr(self._local, "run", None)
        for cell in cells:
            if cell.get("type") == "markdown" or not cell.get("source", "").strip():
                continue
            try:
                status = self._exec(cell["source"], namespace, run)
            except NotebookExit as done:
                return done.value
            if status != "ok":
                raise RuntimeError(f"Notebook {name!r} failed; its error is shown above.")
            if arguments and "parameters" in (cell.get("meta") or {}).get("tags", []):
                namespace.update(arguments)
        return None

    # ---- what the Variables pane and autocomplete show

    def variables(self) -> list[dict[str, Any]]:
        skip = set(self._base_namespace_keys())
        rows = []
        for name, value in list(self.namespace.items()):
            if name.startswith("_") or name in skip or inspect.ismodule(value) or inspect.isclass(value) \
                    or inspect.isroutine(value):
                continue
            rows.append({"name": name, "type": type(value).__name__, "size": _size(value),
                         "value": "hidden" if self._hidden(name, value) else _preview(value)})
        return sorted(rows, key=lambda r: r["name"].lower())

    @staticmethod
    def _base_namespace_keys() -> tuple[str, ...]:
        return ("input", "display", "notebookutils", "mssparkutils", "spark", "In", "Out")

    def _hidden(self, name: str, value: Any) -> bool:
        return bool(_SECRET_NAME.search(name)) or (isinstance(value, str) and value in self._secrets)

    def complete(self, code: str, cursor: int | None = None) -> dict[str, Any]:
        """Names that can follow the text before ``cursor``: variables, attributes, keywords, builtins."""
        text = code[:len(code) if cursor is None else cursor]
        m = re.search(r"([A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)*)\.?(\w*)$", text)
        if not m:
            return {"from": len(text), "items": []}
        token = m.group(0)
        if "." in token:
            base, _, prefix = token.rpartition(".")
            obj = self._lookup(base)
            if obj is _MISSING:
                return {"from": len(text) - len(prefix), "items": []}
            items = []
            for attr in dir(obj):
                if attr.startswith("_") and not prefix.startswith("_"):
                    continue
                if attr.startswith(prefix):
                    try:
                        static = inspect.getattr_static(obj, attr)
                    except AttributeError:
                        static = None
                    kind = "property" if isinstance(static, property) else \
                        "method" if callable(static) or isinstance(static, (staticmethod, classmethod)) else "variable"
                    items.append({"label": attr, "type": kind})
            return {"from": len(text) - len(prefix), "items": items[:300]}
        prefix = token
        names = {n: "variable" for n in self.namespace if not n.startswith("__")}
        for n, v in self.namespace.items():
            if inspect.ismodule(v):
                names[n] = "namespace"
            elif inspect.isroutine(v) or inspect.isclass(v):
                names[n] = "function" if inspect.isroutine(v) else "class"
        import builtins
        for n in dir(builtins):
            if not n.startswith("_"):
                names.setdefault(n, "function")
        for n in keyword.kwlist:
            names.setdefault(n, "keyword")
        items = [{"label": n, "type": t} for n, t in sorted(names.items()) if n.startswith(prefix) and n != prefix]
        return {"from": len(text) - len(prefix), "items": items[:300]}

    def _lookup(self, dotted: str) -> Any:
        """The object a plain dotted name refers to, without calling anything (properties aren't run)."""
        first, *rest = dotted.split(".")
        if first in self.namespace:
            obj = self.namespace[first]
        else:
            import builtins
            obj = getattr(builtins, first, _MISSING)
        for part in rest:
            if obj is _MISSING:
                break
            try:
                static = inspect.getattr_static(obj, part)
            except AttributeError:
                return _MISSING
            obj = getattr(obj, part) if inspect.ismodule(obj) or not isinstance(static, property) else _MISSING
        return obj

    @staticmethod
    def _error(exc: BaseException) -> dict[str, Any]:
        frames = [f for f in traceback.extract_tb(exc.__traceback__)
                  if f.filename != __file__ and not f.filename.endswith(("notebookutils_local.py",))]
        lines = traceback.format_list(frames) + traceback.format_exception_only(type(exc), exc)
        return {"type": "error", "ename": type(exc).__name__, "evalue": str(exc),
                "traceback": "Traceback (most recent call last):\n" + "".join(lines)}


_MISSING = object()


def _line_magics(code: str) -> str:
    """``%run nb`` and other line magics become calls the kernel answers."""
    out = []
    for line in code.split("\n"):
        m = re.match(r"^(\s*)%(?!%)(\w+)\s?(.*)$", line)
        if m:
            indent, name, rest = m.groups()
            out.append(f"{indent}__fd_magic__({name!r}, {rest!r})")
        else:
            out.append(line)
    return "\n".join(out)


def _size(value: Any) -> str:
    module = type(value).__module__
    if module.startswith("duckdb.experimental.spark") and hasattr(value, "columns"):
        return f"? × {len(value.columns)}"
    shape = getattr(value, "shape", None)
    if isinstance(shape, tuple) and shape:
        return " × ".join(_count(n) for n in shape)
    if isinstance(value, pa.Table):
        return f"{_count(value.num_rows)} × {value.num_columns}"
    if isinstance(value, (str, bytes, list, tuple, dict, set)):
        return str(len(value))
    return ""


def _count(n: Any) -> str:
    if not isinstance(n, int):
        return str(n)
    for unit, size in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 10_000)):
        if n >= size:
            return f"{n / size:.1f}{unit}".replace(".0", "") if unit != "K" else f"{n // 1000}K"
    return str(n)


def _preview(value: Any) -> str:
    module = type(value).__module__
    if module.startswith(("polars", "pandas", "pyarrow", "duckdb")):
        cols = getattr(value, "columns", None)
        if cols is not None:
            names = list(cols)
            return ", ".join(map(str, names[:6])) + (", ..." if len(names) > 6 else "")
    try:
        text = repr(value)
    except Exception:  # noqa: BLE001
        return "?"
    return text if len(text) <= 80 else text[:77] + "..."


# Storage: notebooks are standard .ipynb files with the attached lakehouse in metadata.

_SAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def check_notebook_name(name: str) -> str:
    name = name.strip()
    if not name or _SAFE_NAME.search(name) or name.endswith("."):
        raise ValueError(f"Invalid notebook name {name!r}: avoid < > : \" / \\ | ? * and a trailing dot.")
    return name


COMPUTE = ("default", "local", "fabric")


def _cell_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    """A cell's settings as Jupyter and Fabric store them: the parameters tag, hidden input or
    output, and frozen (can't be edited or run)."""
    md: dict[str, Any] = {}
    tags = [t for t in meta.get("tags", []) if isinstance(t, str)]
    if tags:
        md["tags"] = tags
    hidden = {k: True for k in ("source_hidden", "outputs_hidden") if meta.get(k)}
    if hidden:
        md["jupyter"] = hidden
    if meta.get("frozen"):
        md.update(editable=False, deletable=False, run_control={"frozen": True})
    return md


def _cell_meta(md: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    if md.get("tags"):
        meta["tags"] = [t for t in md["tags"] if isinstance(t, str)]
    for k in ("source_hidden", "outputs_hidden"):
        if (md.get("jupyter") or {}).get(k):
            meta[k] = True
    if (md.get("run_control") or {}).get("frozen"):
        meta["frozen"] = True
    return meta


def to_ipynb(cells: list[dict[str, Any]], lakehouse: dict[str, Any] | None,
             compute: str = "default") -> dict[str, Any]:
    nb_cells = []
    for c in cells:
        cell = {"cell_type": "markdown" if c.get("type") == "markdown" else "code",
                "metadata": _cell_metadata(c.get("meta") or {}), "source": c.get("source", "").splitlines(keepends=True)}
        if cell["cell_type"] == "code":
            cell.update(execution_count=None, outputs=[])
        nb_cells.append(cell)
    metadata: dict[str, Any] = {
        "kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"},
        "language_info": {"name": "python"},
    }
    if compute != "default":
        metadata["fabric_desktop"] = {"compute": compute}
    if lakehouse:
        metadata.setdefault("fabric_desktop", {})["lakehouse"] = lakehouse
        # Fabric reads this to show the default lakehouse when the notebook is published.
        metadata["dependencies"] = {"lakehouse": {
            "default_lakehouse": lakehouse["lakehouse_id"],
            "default_lakehouse_name": lakehouse["name"],
            "default_lakehouse_workspace_id": lakehouse["workspace_id"],
        }}
    return {"nbformat": 4, "nbformat_minor": 5, "metadata": metadata, "cells": nb_cells}


def from_ipynb(nb: dict[str, Any]) -> dict[str, Any]:
    cells = []
    for c in nb.get("cells", []):
        source = c.get("source", "")
        cell = {"type": "markdown" if c.get("cell_type") == "markdown" else "code",
                "source": "".join(source) if isinstance(source, list) else source}
        meta = _cell_meta(c.get("metadata") or {})
        if meta:
            cell["meta"] = meta
        cells.append(cell)
    ours = nb.get("metadata", {}).get("fabric_desktop", {})
    compute = ours.get("compute") if ours.get("compute") in COMPUTE else "default"
    return {"cells": cells, "lakehouse": ours.get("lakehouse"), "compute": compute}


class NotebookStore:
    def __init__(self, folder: Path):
        self.folder = folder

    def path(self, name: str) -> Path:
        return self.folder / f"{check_notebook_name(name)}.ipynb"

    def list(self) -> list[dict[str, Any]]:
        if not self.folder.exists():
            return []
        files = sorted(self.folder.glob("*.ipynb"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [{"name": p.stem, "modified": p.stat().st_mtime} for p in files]

    def load(self, name: str) -> dict[str, Any]:
        nb = json.loads(self.path(name).read_text(encoding="utf-8"))
        return {"name": name, **from_ipynb(nb)}

    def save(self, name: str, cells: list[dict[str, Any]], lakehouse: dict[str, Any] | None = None,
             compute: str | None = None) -> dict[str, Any]:
        """Save the notebook. ``compute`` is where Run all runs it: "local" (this PC), "fabric",
        or "default" (whatever the environment picked in the top bar runs on). None keeps the saved choice."""
        path = self.path(name)
        if compute is None:
            compute = self.load(name)["compute"] if path.exists() else "default"
        if compute not in COMPUTE:
            raise ValueError(f"compute is one of {', '.join(COMPUTE)}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(to_ipynb(cells, lakehouse, compute), indent=1), encoding="utf-8")
        return {"name": name}

    def new(self, prefix: str = "Notebook") -> dict[str, Any]:
        taken = {n["name"] for n in self.list()}
        i = 1
        while f"{prefix} {i}" in taken:
            i += 1
        name = f"{prefix} {i}"
        self.save(name, [{"type": "code", "source": ""}])
        return self.load(name)

    def rename(self, name: str, new_name: str) -> dict[str, Any]:
        target = self.path(new_name)
        if target.exists():
            raise ValueError(f"A notebook named {new_name!r} already exists.")
        self.path(name).rename(target)
        return {"name": new_name}

    def delete(self, name: str) -> None:
        self.path(name).unlink(missing_ok=True)
