// Fabric Desktop notebook screen: the cells, the editor, results and panes, and running cells.
// It works like a Fabric notebook: a code editor with autocomplete, a language per cell,
// command mode keys (A, B, DD, M, Y, Z...), collapse/freeze/parameter cells, live output with a
// Cancel that keeps variables, tables with Table/Chart/Inspect views, charts and HTML, and
// Variables and Outline panes. Opening, saving, publishing and Fabric runs stay in app.js.

state.showVariables = false;
state.lineNumbers = true;
state.mode = "command";        // command | edit, as in Fabric and Jupyter
state.nbTables = {};           // lakehouse_id -> ["dbo.sales", ...], for autocomplete

const editors = new Map();     // cell id -> CodeMirror view
const deletedCells = [];       // for Z (undo delete)
let cellClipboard = null;
let lastDKey = 0;
let cancelRequested = false;

const langComp = new CM.Compartment();
const gutterComp = new CM.Compartment();
const readOnlyComp = new CM.Compartment();

const LANGS = [["python", "Python (PySpark)"], ["sql", "Spark SQL"], ["html", "HTML"], ["markdown", "Markdown"]];
const MAGIC = { sql: "%%sql", html: "%%html" };

function selectedCell() {
  return state.notebook.cells.find((c) => c.id === state.selected) || state.notebook.cells[0];
}

function cellLang(cell) {
  if (cell.type === "markdown") return "markdown";
  const first = cell.source.trimStart().split("\n", 1)[0].trim();
  if (first.startsWith("%%sql")) return "sql";
  if (first.startsWith("%%html")) return "html";
  return "python";
}

const isParams = (cell) => (cell.meta?.tags || []).includes("parameters");

function disposeEditors() {
  editors.forEach((v) => v.destroy());
  editors.clear();
}

function dropEditor(cell) {
  editors.get(cell.id)?.destroy();
  editors.delete(cell.id);
}

// ---------- the screen ----------

function renderNotebook() {
  const nb = state.notebook;
  if (!nb) return go("home");
  const scroller = document.querySelector(".cells");
  const scrollTop = scroller ? scroller.scrollTop : 0;
  const busy = state.kernel === "busy";
  if (nb.lakehouse) loadTableNames(nb.lakehouse);

  const tabs = ["home", "edit", "run", "view"].map((t) => h("button", {
    class: `tab${state.ribbonTab === t ? " active" : ""}`, role: "tab", "aria-selected": String(state.ribbonTab === t),
    onclick: () => { state.ribbonTab = t; renderNotebook(); },
  }, t[0].toUpperCase() + t.slice(1)));

  const runAllBtn = btn("Run all", "play", runAll, "tool", { disabled: busy, "data-run": true });
  runAllBtn.querySelector("svg").classList.add("play");
  const cancelBtn = btn("Cancel", "stop", cancelRun, "tool", { disabled: !busy, "data-stop": true,
    title: "Stop the running cell. Variables from earlier cells are kept." });
  const where = nbWhere(nb);
  const fabricMode = where.fabric;
  const localOnlyAttrs = (attrs = {}) => fabricMode
    ? { ...attrs, disabled: true, "data-local-only": true, title: "Fabric runs the whole notebook. Use Run all, or switch Runs on to This PC to run single cells." }
    : attrs;
  const computeBtn = h("button", { class: `tool outline compute-btn${fabricMode ? " fabric" : ""}`,
    title: "Where Run all runs this notebook", onclick: (e) => showMenu(e.currentTarget, computeMenu(nb)) },
    icon(fabricMode ? "cloud" : "desktop", 16), `Runs on: ${where.short}`, icon("chevronDown", 14));
  const sel = selectedCell();
  const toggleBtn = (label, iconName, on, onClick, attrs = {}) =>
    btn(label, iconName, onClick, `tool${on ? " toggled" : ""}`, { "aria-pressed": String(!!on), ...attrs });

  const toolbars = {
    home: [runAllBtn, cancelBtn, computeBtn, h("div", { class: "sep" }), btn("Add data items", "lakehouse", openDataItems),
      h("div", { class: "spacer" }), btn("Save", "save", saveNotebook),
      btn("Schedule", "history", scheduleNotebook, "tool"),
      btn("Publish to OneLake", "publish", publishNotebook, "tool primary")],
    edit: [btn("Code", "add", () => addCell("code")), btn("Markdown", "add", () => addCell("markdown")),
      h("div", { class: "sep" }), btn("Move up", "up", () => moveCell(-1)), btn("Move down", "down", () => moveCell(1)),
      btn("Split cell", "code", () => splitCell(sel)), btn("Merge with below", "down", () => mergeBelow(sel)),
      btn("Delete cell", "delete", () => deleteCell(sel)), btn("Undo delete", "restart", undoDelete, "tool", { disabled: !deletedCells.length }),
      h("div", { class: "sep" }),
      toggleBtn("Parameter cell", "braces", isParams(sel), () => toggleParams(sel), { disabled: sel.type !== "code" }),
      toggleBtn("Freeze", "shield", sel.meta?.frozen, () => toggleFreeze(sel)),
      btn("Find and replace", "search", () => findInCell(sel)),
      h("div", { class: "sep" }), btn("Clear all outputs", "clear", clearOutputs)],
    run: [runAllBtn, btn("Run cell", "play", () => runCell(selectedCell()), "tool", localOnlyAttrs({ disabled: busy, "data-run": true })),
      btn("Run all above", "up", runAbove, "tool", localOnlyAttrs({ disabled: busy, "data-run": true })),
      btn("Run cell and below", "down", runBelow, "tool", localOnlyAttrs({ disabled: busy, "data-run": true })), cancelBtn, computeBtn, h("div", { class: "sep" }),
      btn("Restart kernel", "restart", restartKernel), btn("Restart and run all", "restart", async () => { await restartKernel(); runAll(); }, "tool", localOnlyAttrs({ disabled: busy, "data-run": true })),
      h("div", { class: "sep" }), btn("Schedule", "history", scheduleNotebook)],
    view: [toggleBtn("Explorer", "panel", state.showExplorer, () => { state.showExplorer = !state.showExplorer; renderNotebook(); }),
      toggleBtn("Outline", "documentTable", state.showExplorer && state.explorerTab === "outline", () => {
        const on = state.showExplorer && state.explorerTab === "outline";
        state.showExplorer = !on;
        if (!on) state.explorerTab = "outline";
        renderNotebook();
      }),
      toggleBtn("Variables", "braces", state.showVariables, toggleVariables),
      h("div", { class: "sep" }),
      btn("Collapse all inputs", "up", () => setAll("source_hidden", true)), btn("Expand all inputs", "down", () => setAll("source_hidden", false)),
      btn("Collapse all outputs", "up", () => setAll("outputs_hidden", true)), btn("Expand all outputs", "down", () => setAll("outputs_hidden", false)),
      h("div", { class: "sep" }), toggleBtn("Line numbers", "code", state.lineNumbers, toggleLineNumbers)],
  };

  const cells = h("section", { class: "cells", "aria-label": "Notebook cells", onmousedown: (e) => {
    if (e.target === e.currentTarget || e.target.classList.contains("cells-inner")) enterCommandMode();
  } },
  h("div", { class: "cells-inner" }, computeNote(nb), nb.cells.map(renderCell),
    h("div", { class: "add-cell" }, btn("Code", "add", () => addCell("code", true), "tool outline"),
      btn("Markdown", "add", () => addCell("markdown", true), "tool outline"))));

  const lh = nb.lakehouse;
  setView(
    h("div", { class: "ribbon" }, h("div", { class: "ribbon-tabs", role: "tablist", "aria-label": "Ribbon" }, tabs),
      h("div", { class: "toolbar" }, toolbars[state.ribbonTab])),
    h("div", { class: "nb-body" }, state.showExplorer ? renderExplorer() : null, cells,
      state.showVariables ? renderVariables() : null),
    h("footer", { class: "statusbar" },
      h("span", {}, h("span", { class: `dot ${state.kernel}` }), kernelLabel()),
      h("span", { class: "mode-hint" }, modeHint()),
      h("span", {}, `Attached: ${lh ? lh.name : "none"}`),
      h("span", {}, fabricMode ? `Compute: Fabric capacity in ${nb.lakehouse?.workspace_name || "the attached lakehouse's workspace"}`
        : `Compute: Python ${state.python || "3"} on this PC, no Fabric capacity used`),
      nb.fabricRun ? h("span", {}, `Last Fabric run: ${nb.fabricRun.status === "NotStarted" ? "starting" : nb.fabricRun.status}`) : null));
  document.querySelector(".cells").scrollTop = scrollTop;
  editors.forEach((v) => v.requestMeasure());
}

function kernelLabel() {
  return { idle: "Kernel idle", busy: "Kernel busy", stopped: "Kernel stopped, variables cleared" }[state.kernel];
}

function modeHint() {
  return state.mode === "edit" ? "Edit mode: Esc for command mode, Shift+Enter to run"
    : "Command mode: Enter to edit, A above, B below, DD delete, M markdown, Y code, Z undo";
}

function setMode(mode) {
  if (state.mode === mode) return;
  state.mode = mode;
  const hint = document.querySelector(".mode-hint");
  if (hint) hint.textContent = modeHint();
  document.querySelector(".cells")?.classList.toggle("editing", mode === "edit");
}

function enterCommandMode(cell = selectedCell()) {
  setMode("command");
  const el = document.querySelector(`[data-cell="${cell.id}"]`);
  if (el && !el.contains(document.activeElement) || document.activeElement?.closest?.(".cm-editor")) el?.focus({ preventScroll: true });
}

function enterEditMode(cell = selectedCell()) {
  if (cell.meta?.frozen) { toast("This cell is frozen. Unfreeze it on the Edit tab to change it."); return; }
  if (cell.meta?.source_hidden) setMeta(cell, "source_hidden", false);
  if (cell.type === "markdown" && !cell.editing) { cell.editing = true; updateCell(cell); }
  editors.get(cell.id)?.focus();
}

// ---------- Explorer: data items, outline, resources ----------

function renderExplorer() {
  const lh = state.notebook.lakehouse;
  const tab = (id, label) => h("button", { class: `tab${state.explorerTab === id ? " active" : ""}`,
    onclick: () => { state.explorerTab = id; renderNotebook(); } }, label);
  const tabs = h("div", { class: "explorer-tabs" }, tab("data", "Data items"), tab("outline", "Outline"), tab("resources", "Resources"));
  const content = h("div", { class: "explorer-content" });

  if (state.explorerTab === "outline") {
    content.append(renderOutline());
  } else if (state.explorerTab === "resources") {
    content.append(h("div", { class: "muted-text", style: "padding:4px 4px 10px" }, "Click a snippet to add it as a new cell."),
      ...SNIPPETS.map(([title, code]) => h("button", { class: "snippet", onclick: () => insertCell(code) },
        h("div", { class: "t" }, title), h("code", {}, code))));
  } else if (lh) {
    const tree = h("div");
    content.append(
      h("div", { class: "lh-head" }, icon("lakehouse", 18), h("span", { class: "name", title: `${lh.name} in ${lh.workspace_name}` }, lh.name),
        h("span", { class: "badge" }, "Default"),
        h("button", { class: "tool small", title: "Refresh", "aria-label": "Refresh", onclick: () => {
          Object.keys(state.tree).filter((k) => k.startsWith(lh.lakehouse_id)).forEach((k) => delete state.tree[k]);
          delete state.nbTables[lh.lakehouse_id];
          renderNotebook();
        } }, icon("restart", 14)),
        h("button", { class: "tool small", title: "Remove lakehouse", "aria-label": "Remove lakehouse", onclick: detachLakehouse }, icon("dismiss", 14))),
      tree,
      btn("Add data items", "add", openDataItems, "tool link", { style: "margin-top:12px" }));
    renderTree(tree, lh, insertCell);
  } else {
    content.append(h("div", { class: "empty" },
      h("span", { style: "color:var(--accent)" }, icon("lakehouse", 40)),
      h("span", { class: "title" }, "No data items yet"),
      h("span", { class: "text" }, "Attach a lakehouse to read its files and write Delta tables back to OneLake."),
      btn("Add data items", null, openDataItems, "tool primary")));
  }
  return h("aside", { class: "explorer", "aria-label": "Explorer" }, h("div", { class: "explorer-head" }, "Explorer"), tabs, content);
}

// Headings from Markdown cells, and the parameter cell, as Fabric's outline shows them.
function renderOutline() {
  const items = [];
  state.notebook.cells.forEach((cell) => {
    if (cell.type === "markdown") {
      for (const line of cell.source.split("\n")) {
        const m = line.match(/^(#{1,4})\s+(.+)$/);
        if (m) items.push({ cell, level: m[1].length, text: m[2].replace(/[*_`]/g, "") });
      }
    } else if (isParams(cell)) {
      items.push({ cell, level: 1, text: "Parameters" });
    }
  });
  if (!items.length) {
    return h("div", { class: "muted-text", style: "padding:8px 4px" }, "Add Markdown headings (# Title) to build an outline.");
  }
  return h("nav", { class: "nb-outline", "aria-label": "Outline" }, items.map((it) => h("button", {
    class: `outline-item l${it.level}${it.cell.id === state.selected ? " active" : ""}`,
    onclick: () => { selectCell(it.cell); document.querySelector(`[data-cell="${it.cell.id}"]`)?.scrollIntoView({ block: "start", behavior: "smooth" }); },
  }, it.text)));
}

function refreshOutline() {
  if (!state.showExplorer || state.explorerTab !== "outline") return;
  const content = document.querySelector(".explorer-content");
  content?.replaceChildren(renderOutline());
}

const SNIPPETS = [
  ["Import the lakehouse", "import polars as pl\nfrom fabric_desktop import lakehouse"],
  ["Read a table with Spark", 'df = spark.read.table("dbo.my_table")\ndisplay(df)'],
  ["Spark SQL cell", "%%sql\nSELECT * FROM dbo.my_table LIMIT 100"],
  ["Write a table with Spark", 'df.write.mode("overwrite").saveAsTable("dbo.my_table")'],
  ["Read a CSV from Files", 'df = lakehouse.read_csv("Files/raw/data.csv")\ndf'],
  ["Read a table", 'df = lakehouse.read_table("dbo.my_table")\ndf'],
  ["Write a table", 'lakehouse.write_table(df, "dbo.my_table", mode="overwrite")'],
  ["List files", 'notebookutils.fs.ls("Files")'],
  ["Get a Key Vault secret", 'secret = notebookutils.credentials.getSecret("https://<vault>.vault.azure.net/", "<secret-name>")'],
  ["Run another notebook", 'result = notebookutils.notebook.run("Other notebook", 600, {"run_date": "2026-01-01"})'],
  ["Plot with matplotlib", 'import matplotlib.pyplot as plt\nplt.bar(["East", "West"], [960, 1350])\nplt.show()'],
];

// ---------- Variables pane ----------

function toggleVariables() {
  state.showVariables = !state.showVariables;
  renderNotebook();
  if (state.showVariables) refreshVariables();
}

function renderVariables() {
  return h("aside", { class: "varpane", "aria-label": "Variables" },
    h("div", { class: "explorer-head" }, "Variables",
      h("button", { class: "tool small", title: "Refresh", "aria-label": "Refresh variables", onclick: refreshVariables }, icon("restart", 14))),
    h("div", { class: "var-list" }, state.variables ? variableTable(state.variables) : h("div", { class: "muted-text" }, "Loading...")));
}

function variableTable(rows) {
  if (!rows.length) return h("div", { class: "muted-text", style: "padding:8px" }, "No variables yet. Run a cell to see them here.");
  return h("table", { class: "vars" },
    h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Type"), h("th", {}, "Size"), h("th", {}, "Value"))),
    h("tbody", {}, rows.map((r) => h("tr", { title: "Click to display it in a new cell", class: "click",
      onclick: () => insertCell(`display(${r.name})`) },
    h("td", { class: "name" }, r.name), h("td", {}, r.type), h("td", {}, r.size), h("td", { class: "val" }, r.value)))));
}

async function refreshVariables() {
  if (!state.showVariables || nbWhere(state.notebook).fabric) return;
  try {
    state.variables = await rpc("kernel_variables");
  } catch {
    state.variables = [];
  }
  document.querySelector(".var-list")?.replaceChildren(variableTable(state.variables));
}

// ---------- cells ----------

function renderCell(cell) {
  const selected = cell.id === state.selected;
  const meta = cell.meta || (cell.meta = {});
  const node = h("div", {
    class: `cell ${cell.type}${selected ? " selected" : ""}${meta.frozen ? " frozen" : ""}`, "data-cell": cell.id, tabindex: "-1",
    onmousedown: (e) => {
      if (state.selected !== cell.id) selectCell(cell);
      if (!e.target.closest(".cm-editor, input, select, textarea, button")) {
        setMode("command");
        setTimeout(() => { if (!document.activeElement?.closest?.(".cm-editor, input, select")) node.focus({ preventScroll: true }); });
      }
    },
  });
  const head = h("div", { class: "cell-head" },
    h("select", { class: "lang", "aria-label": "Cell language", disabled: meta.frozen || null,
      onchange: (e) => setLanguage(cell, e.target.value) },
    LANGS.map(([v, label]) => h("option", { value: v, selected: cellLang(cell) === v || null }, label))),
    isParams(cell) ? h("span", { class: "tag" }, "Parameters") : null,
    meta.frozen ? h("span", { class: "tag frozen-tag" }, icon("shield", 12), "Frozen") : null,
    h("span", { class: "spacer" }),
    cell.type === "code" ? h("button", { class: "tool small", title: meta.source_hidden ? "Show code" : "Hide code",
      "aria-label": meta.source_hidden ? "Show code" : "Hide code", onclick: () => setMeta(cell, "source_hidden", !meta.source_hidden) },
    icon("code", 14)) : null,
    h("button", { class: "tool small", title: "Move up", "aria-label": "Move up", onclick: () => moveCell(-1, cell) }, icon("up", 14)),
    h("button", { class: "tool small", title: "Move down", "aria-label": "Move down", onclick: () => moveCell(1, cell) }, icon("down", 14)),
    h("button", { class: "tool small", title: "More cell actions", "aria-label": "More cell actions",
      onclick: (e) => showMenu(e.currentTarget, cellMenu(cell)) }, "..."),
    h("button", { class: "tool small", title: "Delete cell", "aria-label": "Delete cell", onclick: () => deleteCell(cell) }, icon("delete", 14)));

  if (cell.type === "markdown") {
    let body;
    if (cell.editing && !meta.frozen) {
      body = h("div", { class: "md-edit" }, editorFor(cell).dom);
    } else {
      body = h("div", { class: "md-view", ondblclick: () => enterEditMode(cell) }, markdown(cell.source));
    }
    node.append(h("div", { class: "gutter" }), h("div", { class: "cell-box" }, head, body));
    return node;
  }

  const gutter = renderGutter(cell);

  const box = h("div", { class: "cell-box" }, head);
  if (meta.source_hidden) {
    const first = cell.source.split("\n").find((l) => l.trim()) || "";
    box.append(h("button", { class: "hidden-input", onclick: () => setMeta(cell, "source_hidden", false) },
      `Code hidden (${cell.source.split("\n").length} lines): ${first.slice(0, 80)}`));
  } else {
    box.append(h("div", { class: "editor" }, editorFor(cell).dom));
  }
  box.append(h("div", { class: "cell-results" }, cellResults(cell)));
  node.append(gutter, box);
  return node;
}

function renderGutter(cell) {
  const running = cell.status === "running" || cell.status === "queued";
  const runBtn = cell.status === "running"
    ? h("button", { class: "tool small", "aria-label": "Cancel", title: "Cancel (keeps variables)", onclick: cancelRun }, icon("stop", 14))
    : h("button", { class: "tool small", "aria-label": "Run cell", title: "Run cell (Shift+Enter)",
      disabled: state.kernel === "busy" || cell.meta?.frozen || null, "data-run": true, onclick: () => runCell(cell) }, icon("play", 14));
  runBtn.querySelector("svg")?.classList.add("play");
  return h("div", { class: "gutter" }, runBtn, running ? "[*]" : `[${cell.count ?? " "}]`);
}

function cellResults(cell) {
  const parts = [];
  if (cell.status) parts.push(statusLine(cell));
  if (!cell.outputs.length) return parts;
  if (cell.meta?.outputs_hidden) {
    parts.push(h("button", { class: "hidden-input", onclick: () => setMeta(cell, "outputs_hidden", false) },
      `Output hidden (${cell.outputs.length} item${cell.outputs.length === 1 ? "" : "s"}). Click to show.`));
  } else {
    cell.outputs.forEach((o) => parts.push(renderOutput(o)));
  }
  return parts;
}

// Redraws only a cell's status and output, so the editor keeps focus while output streams in.
function updateResults(cell) {
  const el = document.querySelector(`[data-cell="${cell.id}"]`);
  if (!el) return;
  el.querySelector(".cell-results")?.replaceChildren(...cellResults(cell));
  if (cell.type === "code") el.querySelector(".gutter")?.replaceWith(renderGutter(cell));
}

function cellMenu(cell) {
  const items = [];
  if (cell.type === "code") {
    items.push({ label: isParams(cell) ? "Remove parameter tag" : "Toggle parameter cell", icon: "braces", action: () => toggleParams(cell) });
    items.push({ label: cell.meta.outputs_hidden ? "Show output" : "Hide output", icon: "panel", action: () => setMeta(cell, "outputs_hidden", !cell.meta.outputs_hidden) });
    items.push({ label: "Clear output", icon: "clear", action: () => { cell.outputs = []; cell.status = null; updateCell(cell); } });
  }
  items.push({ label: cell.meta.frozen ? "Unfreeze cell" : "Freeze cell", icon: "shield", action: () => toggleFreeze(cell) });
  items.push({ label: "Split cell at cursor", icon: "code", action: () => splitCell(cell) });
  items.push({ label: "Merge with cell below", icon: "down", action: () => mergeBelow(cell) });
  items.push({ label: "Copy cell", icon: "copyArrowRight", action: () => copyCell(cell) });
  items.push({ label: "Paste cell below", icon: "add", action: () => pasteCell(cell, 1) });
  items.push({ label: "Find and replace", icon: "search", action: () => findInCell(cell) });
  return items;
}

function statusLine(cell) {
  const s = cell.status;
  const label = { queued: ["busy", "Queued"], running: ["busy", "Running"], ok: ["ok", "Succeeded"],
    error: ["bad", "Failed"], stopped: ["bad", "Stopped"], cancelled: ["bad", "Cancelled"] }[s];
  return h("div", { class: "cell-status" }, h("span", { class: label[0] }, label[1]),
    s === "running" && cell.seconds != null ? h("span", {}, `${cell.seconds.toFixed(1)} s`) : null,
    s === "ok" || s === "error" || s === "cancelled" ? h("span", {}, "Ran on this PC") : null,
    cell.seconds != null && s !== "running" && s !== "queued" ? h("span", {}, `${cell.seconds.toFixed(1)} s`) : null,
    cell.finishedAt && s !== "running" ? h("span", {}, cell.finishedAt) : null);
}

function updateCell(cell, focus = false) {
  const old = document.querySelector(`[data-cell="${cell.id}"]`);
  if (!old) return;
  const fresh = renderCell(cell);
  old.replaceWith(fresh);
  editors.get(cell.id)?.requestMeasure();
  if (focus) enterEditMode(cell);
}

function selectCell(cell) {
  const prev = state.selected;
  state.selected = cell.id;
  document.querySelector(`[data-cell="${prev}"]`)?.classList.remove("selected");
  document.querySelector(`[data-cell="${cell.id}"]`)?.classList.add("selected");
  if (state.ribbonTab === "edit" && prev !== cell.id) renderToolbar();
  document.querySelectorAll(".outline-item").forEach((b) => b.classList.remove("active"));
}

// The Edit tab's toggles follow the selected cell.
function renderToolbar() {
  const scroller = document.querySelector(".cells");
  const top = scroller?.scrollTop;
  const focused = document.activeElement?.closest?.(".cm-editor") ? state.selected : null;
  renderNotebook();
  if (scroller && top != null) document.querySelector(".cells").scrollTop = top;
  if (focused) editors.get(focused)?.focus();
}

function selectRelative(delta) {
  const cells = state.notebook.cells;
  const i = cells.indexOf(selectedCell());
  const next = cells[Math.max(0, Math.min(cells.length - 1, i + delta))];
  selectCell(next);
  const el = document.querySelector(`[data-cell="${next.id}"]`);
  el?.focus({ preventScroll: true });
  el?.scrollIntoView({ block: "nearest" });
}

function focusNext(cell, edit = true) {
  const cells = state.notebook.cells;
  const i = cells.indexOf(cell);
  let next = cells[i + 1];
  if (!next) {
    next = cellFrom({ type: "code" });
    cells.push(next);
    state.selected = next.id;
    renderNotebook();
  } else {
    selectCell(next);
  }
  const el = document.querySelector(`[data-cell="${next.id}"]`);
  el?.scrollIntoView({ block: "nearest" });
  if (edit && next.type === "code") enterEditMode(next);
  else el?.focus({ preventScroll: true });
}

function addCell(type, atEnd = false, offset = 1) {
  const cells = state.notebook.cells;
  const cell = cellFrom({ type });
  const i = atEnd ? cells.length - 1 : cells.indexOf(selectedCell());
  cells.splice(offset > 0 ? i + 1 : Math.max(i, 0), 0, cell);
  state.selected = cell.id;
  markDirty();
  renderNotebook();
  document.querySelector(`[data-cell="${cell.id}"]`)?.scrollIntoView({ block: "nearest" });
  enterEditMode(cell);
  return cell;
}

function insertCell(code) {
  const cell = addCell("code");
  setSource(cell, code);
  markDirty();
}

function setSource(cell, source) {
  cell.source = source;
  const view = editors.get(cell.id);
  if (view && view.state.doc.toString() !== source) {
    view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: source } });
  }
}

function moveCell(delta, cell = selectedCell()) {
  const cells = state.notebook.cells;
  const i = cells.indexOf(cell);
  const j = i + delta;
  if (j < 0 || j >= cells.length) return;
  [cells[i], cells[j]] = [cells[j], cells[i]];
  state.selected = cell.id;
  markDirty();
  renderNotebook();
  refreshOutline();
}

function deleteCell(cell) {
  if (cell.meta?.frozen) { toast("This cell is frozen. Unfreeze it to delete it."); return; }
  const cells = state.notebook.cells;
  const i = cells.indexOf(cell);
  cells.splice(i, 1);
  deletedCells.push({ cell: { type: cell.type, source: cell.source, meta: structuredClone(cell.meta || {}) }, index: i });
  dropEditor(cell);
  if (!cells.length) cells.push(cellFrom({ type: "code" }));
  state.selected = (cells[i] || cells[i - 1]).id;
  markDirty();
  renderNotebook();
  enterCommandMode();
}

function undoDelete() {
  const last = deletedCells.pop();
  if (!last) return;
  const cell = cellFrom(last.cell);
  if (cell.type === "markdown") cell.editing = false;
  state.notebook.cells.splice(Math.min(last.index, state.notebook.cells.length), 0, cell);
  state.selected = cell.id;
  markDirty();
  renderNotebook();
  enterCommandMode(cell);
}

function copyCell(cell) {
  cellClipboard = { type: cell.type, source: cell.source, meta: structuredClone(cell.meta || {}) };
  toast("Cell copied");
}

function pasteCell(cell, offset) {
  if (!cellClipboard) return;
  const cells = state.notebook.cells;
  const copy = cellFrom({ ...cellClipboard, meta: structuredClone(cellClipboard.meta) });
  if (copy.type === "markdown") copy.editing = false;
  delete copy.meta.tags;
  cells.splice(cells.indexOf(cell) + (offset > 0 ? 1 : 0), 0, copy);
  state.selected = copy.id;
  markDirty();
  renderNotebook();
  enterCommandMode(copy);
}

function splitCell(cell) {
  if (cell.meta?.frozen) return;
  const view = editors.get(cell.id);
  const pos = view ? view.state.selection.main.head : cell.source.length;
  const before = cell.source.slice(0, pos).replace(/\n$/, "");
  let after = cell.source.slice(pos).replace(/^\n/, "");
  const magic = MAGIC[cellLang(cell)];
  if (magic && !after.trimStart().startsWith(magic)) after = `${magic}\n${after}`;
  setSource(cell, before);
  const cells = state.notebook.cells;
  const fresh = cellFrom({ type: cell.type, source: after });
  if (fresh.type === "markdown") fresh.editing = true;
  cells.splice(cells.indexOf(cell) + 1, 0, fresh);
  state.selected = fresh.id;
  markDirty();
  renderNotebook();
  enterEditMode(fresh);
}

function mergeBelow(cell) {
  const cells = state.notebook.cells;
  const next = cells[cells.indexOf(cell) + 1];
  if (!next || cell.meta?.frozen || next.meta?.frozen) return;
  let tail = next.source;
  const magic = MAGIC[cellLang(cell)];
  if (magic && tail.trimStart().startsWith(magic)) tail = tail.trimStart().slice(magic.length).replace(/^\n/, "");
  setSource(cell, `${cell.source.replace(/\n$/, "")}\n${tail}`);
  cells.splice(cells.indexOf(next), 1);
  dropEditor(next);
  markDirty();
  renderNotebook();
}

function setLanguage(cell, lang) {
  const body = cell.source.replace(/^\s*%%(sql|html|pyspark|python)[^\n]*\n?/, "");
  const wasMarkdown = cell.type === "markdown";
  if (lang === "markdown") {
    cell.type = "markdown";
    cell.source = body;
    cell.editing = true;
    cell.outputs = [];
    cell.status = null;
  } else {
    cell.type = "code";
    cell.source = MAGIC[lang] ? `${MAGIC[lang]}\n${body}` : body;
  }
  if (wasMarkdown !== (cell.type === "markdown")) dropEditor(cell);
  else setSource(cell, cell.source);
  editors.get(cell.id)?.dispatch({ effects: langComp.reconfigure(languageFor(cell)) });
  markDirty();
  updateCell(cell);
  refreshOutline();
}

function setMeta(cell, key, value) {
  cell.meta = cell.meta || {};
  if (value) cell.meta[key] = true;
  else delete cell.meta[key];
  markDirty();
  updateCell(cell);
}

function setAll(key, value) {
  state.notebook.cells.filter((c) => c.type === "code").forEach((c) => {
    c.meta = c.meta || {};
    if (value) c.meta[key] = true;
    else delete c.meta[key];
  });
  markDirty();
  renderNotebook();
}

// One parameter cell per notebook, as in Fabric: pipelines pass values that replace its variables.
function toggleParams(cell) {
  if (cell.type !== "code") return;
  const on = !isParams(cell);
  state.notebook.cells.forEach((c) => { if (c.meta?.tags) c.meta.tags = c.meta.tags.filter((t) => t !== "parameters"); });
  if (on) (cell.meta.tags ||= []).push("parameters");
  state.notebook.cells.forEach((c) => { if (c.meta?.tags && !c.meta.tags.length) delete c.meta.tags; });
  markDirty();
  renderNotebook();
}

function toggleFreeze(cell) {
  setMeta(cell, "frozen", !cell.meta?.frozen);
  editors.get(cell.id)?.dispatch({ effects: readOnlyComp.reconfigure(readOnly(cell)) });
  if (state.ribbonTab === "edit") renderToolbar();
}

function findInCell(cell) {
  const view = editors.get(cell.id);
  if (!view) { enterEditMode(cell); return; }
  view.focus();
  CM.openSearchPanel(view);
}

function toggleLineNumbers() {
  state.lineNumbers = !state.lineNumbers;
  editors.forEach((v, id) => {
    const cell = state.notebook.cells.find((c) => c.id === id);
    if (cell) v.dispatch({ effects: gutterComp.reconfigure(gutters(cell)) });
  });
  renderNotebook();
}

function clearOutputs() {
  state.notebook.cells.forEach((c) => { c.outputs = []; c.status = null; c.count = null; c.seconds = null; });
  renderNotebook();
}

// ---------- the code editor ----------

const editorHighlight = CM.HighlightStyle.define([
  { tag: [CM.tags.keyword, CM.tags.controlKeyword, CM.tags.definitionKeyword, CM.tags.moduleKeyword, CM.tags.operatorKeyword], class: "kw" },
  { tag: [CM.tags.string, CM.tags.special(CM.tags.string)], class: "str" },
  { tag: [CM.tags.number, CM.tags.bool, CM.tags.null], class: "num" },
  { tag: [CM.tags.comment, CM.tags.lineComment, CM.tags.blockComment], class: "cm" },
  { tag: [CM.tags.function(CM.tags.variableName), CM.tags.function(CM.tags.propertyName), CM.tags.definition(CM.tags.function(CM.tags.variableName))], class: "fn" },
  { tag: [CM.tags.standard(CM.tags.variableName), CM.tags.className, CM.tags.typeName], class: "bi" },
  { tag: CM.tags.heading, class: "md-h" },
  { tag: [CM.tags.tagName, CM.tags.angleBracket], class: "kw" },
  { tag: CM.tags.attributeName, class: "fn" },
]);

const editorTheme = CM.EditorView.theme({
  "&": { fontSize: "13px" },
  "&.cm-focused": { outline: "none" },
  ".cm-content": { fontFamily: "var(--mono)", padding: "8px 0", lineHeight: "20px" },
  ".cm-gutters": { background: "transparent", border: "none", color: "#a19f9d", fontFamily: "var(--mono)" },
  ".cm-activeLine": { background: "transparent" },
  "&.cm-focused .cm-activeLine": { background: "rgba(27, 54, 100, 0.04)" },
  ".cm-activeLineGutter": { background: "transparent" },
  ".cm-tooltip-autocomplete": { fontFamily: "var(--mono)", fontSize: "12.5px" },
  ".cm-scroller": { overflowX: "auto" },
});

function gutters(cell) {
  return state.lineNumbers && cell.type === "code" ? [CM.lineNumbers(), CM.foldGutter()] : [];
}

function readOnly(cell) {
  return cell.meta?.frozen ? [CM.EditorState.readOnly.of(true), CM.EditorView.editable.of(false)] : [];
}

function languageFor(cell) {
  const lang = cellLang(cell);
  if (lang === "markdown") return [CM.markdown(), CM.autocompletion()];
  if (lang === "html") return [CM.html(), CM.autocompletion()];
  if (lang === "sql") {
    const schema = Object.fromEntries(currentTables().map((t) => [t, []]));
    return [CM.sql({ dialect: CM.StandardSQL, schema, upperCaseKeywords: true }), CM.autocompletion()];
  }
  return [CM.python(), CM.autocompletion({ override: [pythonCompletions], activateOnTypingDelay: 150 })];
}

function currentTables() {
  const lh = state.notebook?.lakehouse;
  return (lh && state.nbTables[lh.lakehouse_id]) || [];
}

async function loadTableNames(lh) {
  if (state.nbTables[lh.lakehouse_id]) return;
  state.nbTables[lh.lakehouse_id] = [];
  try {
    const bySchema = await rpc("list_tables", { workspace_id: lh.workspace_id, lakehouse_id: lh.lakehouse_id });
    state.nbTables[lh.lakehouse_id] = Object.entries(bySchema || {}).flatMap(([s, names]) => names.map((n) => `${s}.${n}`));
  } catch {
    // Not signed in or offline: autocomplete just has no table names.
  }
}

const CM_TYPES = { method: "method", function: "function", property: "property", variable: "variable",
  keyword: "keyword", namespace: "namespace", class: "class" };

// Python completion: table names inside strings, otherwise names from the kernel (variables,
// attributes of live objects, builtins, keywords) plus names assigned in this cell.
async function pythonCompletions(ctx) {
  const node = CM.syntaxTree(ctx.state).resolveInner(ctx.pos, -1);
  if (node.name === "String" || node.name === "FormatString") {
    const word = ctx.matchBefore(/[\w.]*/);
    const tables = currentTables();
    if (!tables.length) return null;
    return { from: word.from, options: tables.map((t) => ({ label: t, type: "class", detail: "table" })), validFor: /^[\w.]*$/ };
  }
  if (node.name === "Comment") return null;
  const word = ctx.matchBefore(/[A-Za-z_][\w.]*/);
  if (!word && !ctx.explicit) return null;
  const line = ctx.state.doc.lineAt(ctx.pos);
  const before = line.text.slice(0, ctx.pos - line.from);
  let result = { from: ctx.pos, items: [] };
  if (!nbWhere(state.notebook).fabric) {
    try {
      result = await rpc("kernel_complete", { code: before, cursor: before.length });
    } catch {
      // The engine isn't running: fall back to names in this cell.
    }
  }
  if (ctx.aborted) return null;
  const from = line.from + result.from;
  const options = result.items.map((i) => ({ label: i.label, type: CM_TYPES[i.type] || "variable" }));
  if (!(word?.text || "").includes(".")) {
    const seen = new Set(options.map((o) => o.label));
    for (const m of ctx.state.doc.toString().matchAll(/(?:^|\s)(?:def\s+|class\s+)?([A-Za-z_]\w*)\s*(?==[^=]|\()/gm)) {
      if (!seen.has(m[1])) { seen.add(m[1]); options.push({ label: m[1], type: "variable" }); }
    }
  }
  return options.length ? { from, options, validFor: /^\w*$/ } : null;
}

function editorFor(cell) {
  let view = editors.get(cell.id);
  if (view) return view;
  const keys = cellKeymap(cell);
  view = new CM.EditorView({
    state: CM.EditorState.create({
      doc: cell.source,
      extensions: [
        gutterComp.of(gutters(cell)),
        CM.highlightSpecialChars(), CM.history(), CM.drawSelection(), CM.dropCursor(), CM.indentOnInput(),
        CM.bracketMatching(), CM.closeBrackets(), CM.highlightActiveLine(), CM.highlightSelectionMatches(),
        CM.syntaxHighlighting(editorHighlight),
        langComp.of(languageFor(cell)),
        readOnlyComp.of(readOnly(cell)),
        CM.Prec.highest(CM.keymap.of(keys)),
        CM.keymap.of([...CM.closeBracketsKeymap, ...CM.defaultKeymap, ...CM.searchKeymap, ...CM.historyKeymap,
          ...CM.foldKeymap, ...CM.completionKeymap, CM.indentWithTab]),
        CM.placeholder(cell.type === "markdown" ? "Write Markdown. Press Shift+Enter to render."
          : "Write Python or PySpark. Press Shift+Enter to run, Ctrl+Space for suggestions."),
        CM.EditorView.updateListener.of((u) => {
          if (u.docChanged) {
            const before = cellLang(cell);
            cell.source = u.state.doc.toString();
            if (cellLang(cell) !== before && cell.type === "code") {
              u.view.dispatch({ effects: langComp.reconfigure(languageFor(cell)) });
              const select = document.querySelector(`[data-cell="${cell.id}"] select.lang`);
              if (select) select.value = cellLang(cell);
            }
            markDirty();
            if (cell.type === "markdown") refreshOutline();
          }
          if (u.focusChanged && u.view.hasFocus) {
            if (state.selected !== cell.id) selectCell(cell);
            setMode("edit");
          }
        }),
        editorTheme,
      ],
    }),
  });
  editors.set(cell.id, view);
  return view;
}

function cellKeymap(cell) {
  const run = (next, insert) => () => {
    if (cell.type === "markdown") {
      cell.editing = false;
      updateCell(cell);
      refreshOutline();
      if (next) focusNext(cell, false);
      else enterCommandMode(cell);
      return true;
    }
    runCell(cell).then(() => {
      if (insert) addCell("code");
      else if (next) focusNext(cell);
    });
    return true;
  };
  return [
    { key: "Shift-Enter", run: run(true, false) },
    { key: "Mod-Enter", run: run(false, false) },
    { key: "Alt-Enter", run: run(false, true) },
    { key: "Escape", run: () => { editors.get(cell.id)?.contentDOM.blur(); enterCommandMode(cell); return true; } },
    { key: "Mod-Shift-Minus", run: () => { splitCell(cell); return true; } },
    { key: "Mod-/", run: CM.toggleComment },
  ];
}

// ---------- command mode keys ----------

document.addEventListener("keydown", (e) => {
  if (state.view !== "notebook" || !state.notebook || document.getElementById("dialog-root").childElementCount) return;
  const t = e.target;
  if (t.closest?.(".cm-editor") || ["INPUT", "SELECT", "TEXTAREA"].includes(t.tagName) || t.isContentEditable) return;
  if (!t.closest?.(".cells") && t !== document.body) return;
  const cell = selectedCell();
  const key = e.key;
  const plain = !e.ctrlKey && !e.metaKey && !e.altKey;
  let handled = true;
  if (key === "Enter" && e.shiftKey) runCell(cell).then(() => focusNext(cell, false));
  else if (key === "Enter" && (e.ctrlKey || e.metaKey)) runCell(cell);
  else if (key === "Enter" && e.altKey) runCell(cell).then(() => addCell("code"));
  else if (key === "Enter" && plain) enterEditMode(cell);
  else if ((key === "ArrowUp" || key === "k") && plain) selectRelative(-1);
  else if ((key === "ArrowDown" || key === "j") && plain) selectRelative(1);
  else if (key === "a" && plain) addCell("code", false, -1);
  else if (key === "b" && plain) addCell("code");
  else if (key === "d" && plain) {
    if (Date.now() - lastDKey < 600) { deleteCell(cell); lastDKey = 0; } else lastDKey = Date.now();
  } else if (key === "z" && plain) undoDelete();
  else if (key === "m" && plain && cell.type === "code") setLanguage(cell, "markdown");
  else if (key === "y" && plain && cell.type === "markdown") setLanguage(cell, "python");
  else if (key === "c" && plain) copyCell(cell);
  else if (key === "x" && plain) { copyCell(cell); deleteCell(cell); }
  else if (key === "v" && plain) pasteCell(cell, 1);
  else if (key === "V" && e.shiftKey) pasteCell(cell, -1);
  else if (key === "L" && e.shiftKey) toggleLineNumbers();
  else if (key === "o" && plain && cell.type === "code") setMeta(cell, "outputs_hidden", !cell.meta?.outputs_hidden);
  else handled = false;
  if (handled) e.preventDefault();
});

// ---------- outputs ----------

function renderOutput(o) {
  if (o.type === "stream" || o.type === "text") return h("div", { class: "output" }, h("pre", {}, o.text));
  if (o.type === "error") {
    return h("div", { class: "output error" }, h("div", { class: "ename" }, `${o.ename}: ${o.evalue}`),
      o.traceback ? h("details", {}, h("summary", {}, "Traceback"), h("pre", {}, o.traceback)) : null);
  }
  if (o.type === "table") return renderTableOutput(o);
  if (o.type === "image") {
    return h("div", { class: "output" }, h("img", { class: "out-img", alt: "Chart from this cell", src: `data:${o.mime};base64,${o.data}` }));
  }
  if (o.type === "html") return h("div", { class: "output" }, htmlFrame(o.html));
  return h("div", { class: "output" }, h("pre", {}, JSON.stringify(o)));
}

// HTML output runs in a sandboxed frame with no access to the app, sized to its content.
function htmlFrame(html) {
  const sizer = "<script>(function(){function s(){parent.postMessage({fdFrameHeight:document.documentElement.scrollHeight},'*')}" +
    "addEventListener('load',s);new ResizeObserver(s).observe(document.documentElement);})();<\/script>";
  return h("iframe", { class: "out-html", sandbox: "allow-scripts", title: "HTML output", srcdoc: `${html}${sizer}` });
}

window.addEventListener("message", (e) => {
  const height = e.data?.fdFrameHeight;
  if (typeof height !== "number") return;
  document.querySelectorAll("iframe.out-html").forEach((f) => {
    if (f.contentWindow === e.source) f.style.height = `${Math.min(Math.max(height + 4, 40), 1400)}px`;
  });
});

const isNumber = (v) => typeof v === "number";

function numericColumns(o) {
  return o.columns.map((_, i) => o.rows.length > 0 && o.rows.every((r) => r[i] === null || isNumber(r[i])) && o.rows.some((r) => isNumber(r[i])));
}

// A table result with Fabric's views: Table (search, sort), Chart, and Inspect (a summary per column).
function renderTableOutput(o) {
  o.view ||= o.summary ? "inspect" : "table";
  const wrap = h("div", { class: "output table-output" });
  const redraw = () => wrap.replaceWith(renderTableOutput(o));
  const seg = (id, label) => h("button", { class: `seg${o.view === id ? " on" : ""}`, "aria-pressed": String(o.view === id),
    onclick: () => { o.view = id; redraw(); } }, label);
  const bar = h("div", { class: "out-bar" }, h("div", { class: "segs", role: "group", "aria-label": "View" },
    seg("table", "Table"), seg("chart", "Chart"), seg("inspect", "Inspect")));
  wrap.append(bar);
  const numeric = numericColumns(o);
  const count = typeof o.total_rows === "number"
    ? (o.total_rows > o.rows.length ? `Showing the first ${o.rows.length} of ${o.total_rows} rows` : `${o.total_rows} row${o.total_rows === 1 ? "" : "s"}`)
    : `Showing the first ${o.rows.length} rows (${o.total_rows})`;

  if (o.view === "table") {
    const search = h("input", { type: "search", class: "out-search", placeholder: "Search rows", value: o.q || "", "aria-label": "Search rows" });
    bar.append(h("span", { class: "spacer" }), search);
    const body = h("div", { class: "out-table-scroll" });
    const draw = () => {
      let rows = o.rows;
      const q = (o.q || "").toLowerCase();
      if (q) rows = rows.filter((r) => r.some((v) => v !== null && String(v).toLowerCase().includes(q)));
      if (o.sort) {
        const { i, dir } = o.sort;
        rows = [...rows].sort((a, b) => (a[i] === b[i] ? 0 : a[i] === null ? 1 : b[i] === null ? -1 : a[i] < b[i] ? -dir : dir));
      }
      body.replaceChildren(h("table", {},
        h("thead", {}, h("tr", {}, o.columns.map((c, i) => h("th", { class: numeric[i] ? "n" : null },
          h("button", { class: "th-sort", onclick: () => { o.sort = o.sort?.i === i ? { i, dir: -o.sort.dir } : { i, dir: 1 }; draw(); } },
            c, o.sort?.i === i ? (o.sort.dir > 0 ? " ▲" : " ▼") : ""))))),
        h("tbody", {}, rows.map((r) => h("tr", {}, r.map((v, i) => h("td", { class: numeric[i] ? "n" : null }, v === null ? "null" : v)))))));
      wrap.querySelector(".more").textContent = q ? `${rows.length} matching of ${count.toLowerCase()}` : count;
    };
    search.addEventListener("input", () => { o.q = search.value; draw(); });
    wrap.append(body, h("div", { class: "more" }, count));
    draw();
  } else if (o.view === "chart") {
    wrap.append(renderChart(o, numeric), h("div", { class: "more" }, `Chart of ${count.toLowerCase()}`));
  } else {
    wrap.append(renderInspect(o, numeric), h("div", { class: "more" }, `Summary of ${count.toLowerCase()}`));
  }
  return wrap;
}

function renderChart(o, numeric) {
  const nums = o.columns.filter((_, i) => numeric[i]);
  const cats = o.columns.filter((_, i) => !numeric[i]);
  const cfg = o.chart ||= { kind: "bar", x: cats[0] || o.columns[0], y: nums.find((c) => c !== (cats[0] || o.columns[0])) || nums[0] || null, agg: "sum" };
  const box = h("div", { class: "chart-box" });
  const select = (label, key, options) => h("label", { class: "chart-opt" }, label, h("select", {
    onchange: (e) => { cfg[key] = e.target.value; box.replaceWith(renderChart(o, numeric)); } },
  options.map(([v, text]) => h("option", { value: v, selected: cfg[key] === v || null }, text))));
  box.append(h("div", { class: "chart-opts" },
    select("Chart", "kind", [["bar", "Bar"], ["line", "Line"], ["scatter", "Scatter"]]),
    select("X axis", "x", o.columns.map((c) => [c, c])),
    select("Values", "y", (nums.length ? nums : o.columns).map((c) => [c, c])),
    cfg.kind !== "scatter" ? select("Aggregate", "agg", [["sum", "Sum"], ["avg", "Average"], ["count", "Count"], ["max", "Max"], ["min", "Min"]]) : null));
  if (!cfg.y && cfg.agg !== "count") {
    box.append(h("div", { class: "muted-text", style: "padding:12px 0" }, "This result has no number columns to chart. Pick Count to count rows by the X axis."));
    return box;
  }
  const xi = o.columns.indexOf(cfg.x);
  const yi = o.columns.indexOf(cfg.y);
  let points;
  if (cfg.kind === "scatter") {
    points = o.rows.filter((r) => isNumber(r[xi]) && isNumber(r[yi])).map((r) => [r[xi], r[yi]]);
  } else {
    const groups = new Map();
    for (const r of o.rows) {
      const k = r[xi] === null ? "null" : String(r[xi]);
      const g = groups.get(k) || { n: 0, sum: 0, max: -Infinity, min: Infinity };
      const v = r[yi];
      g.n += 1;
      if (isNumber(v)) { g.sum += v; g.max = Math.max(g.max, v); g.min = Math.min(g.min, v); }
      groups.set(k, g);
    }
    const value = (g) => ({ sum: g.sum, avg: g.n ? g.sum / g.n : 0, count: g.n, max: g.max, min: g.min }[cfg.agg]);
    points = [...groups.entries()].slice(0, 60).map(([k, g]) => [k, value(g)]);
    if (cfg.kind === "line" && numeric[xi]) points.sort((a, b) => Number(a[0]) - Number(b[0]));
  }
  box.append(svgChart(points, cfg));
  return box;
}

function svgChart(points, cfg) {
  const W = 720, H = 280, L = 56, R = 16, T = 12, B = 56;
  const ns = "http://www.w3.org/2000/svg";
  const el = (tag, attrs, text) => {
    const n = document.createElementNS(ns, tag);
    Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, v));
    if (text != null) n.textContent = text;
    return n;
  };
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": `${cfg.kind} chart of ${cfg.y || "count"} by ${cfg.x}` });
  if (!points.length) {
    svg.append(el("text", { x: W / 2, y: H / 2, "text-anchor": "middle", class: "chart-label" }, "Nothing to chart"));
    return svg;
  }
  const ys = points.map((p) => p[1]).filter(isFinite);
  const yMax = Math.max(0, ...ys), yMin = Math.min(0, ...ys);
  const span = yMax - yMin || 1;
  const y = (v) => T + (H - T - B) * (1 - (v - yMin) / span);
  const fmt = (v) => (Math.abs(v) >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : Math.abs(v) >= 1e3 ? `${(v / 1e3).toFixed(1)}K` : `${+v.toFixed(2)}`);
  for (let k = 0; k <= 4; k++) {
    const v = yMin + (span * k) / 4;
    svg.append(el("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: "chart-grid" }),
      el("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end", class: "chart-label" }, fmt(v)));
  }
  const plotW = W - L - R;
  if (cfg.kind === "scatter") {
    const xs = points.map((p) => p[0]);
    const xMin = Math.min(...xs), xSpan = Math.max(...xs) - xMin || 1;
    points.forEach(([px, py]) => svg.append(el("circle", { cx: L + (plotW * (px - xMin)) / xSpan, cy: y(py), r: 3.5, class: "chart-dot" })));
    svg.append(el("text", { x: L, y: H - 8, class: "chart-label" }, fmt(xMin)),
      el("text", { x: W - R, y: H - 8, "text-anchor": "end", class: "chart-label" }, fmt(xMin + xSpan)));
    return svg;
  }
  const step = plotW / points.length;
  const label = (i, text) => {
    const every = Math.ceil(points.length / 14);
    if (i % every) return;
    const t = String(text);
    svg.append(el("text", { x: L + step * (i + 0.5), y: H - B + 16, "text-anchor": "middle", class: "chart-label" }, t.length > 12 ? `${t.slice(0, 11)}…` : t));
  };
  if (cfg.kind === "bar") {
    points.forEach(([k, v], i) => {
      const bw = Math.max(2, step * 0.7);
      const top = y(Math.max(v, 0)), bottom = y(Math.min(v, 0));
      const rect = el("rect", { x: L + step * i + (step - bw) / 2, y: top, width: bw, height: Math.max(1, bottom - top), class: "chart-bar", rx: 2 });
      rect.append(el("title", {}, `${k}: ${fmt(v)}`));
      svg.append(rect);
      label(i, k);
    });
  } else {
    const d = points.map(([, v], i) => `${i ? "L" : "M"}${L + step * (i + 0.5)},${y(v)}`).join(" ");
    svg.append(el("path", { d, class: "chart-line" }));
    points.forEach(([k, v], i) => {
      const dot = el("circle", { cx: L + step * (i + 0.5), cy: y(v), r: 3, class: "chart-dot" });
      dot.append(el("title", {}, `${k}: ${fmt(v)}`));
      svg.append(dot);
      label(i, k);
    });
  }
  return svg;
}

function renderInspect(o, numeric) {
  return h("div", { class: "inspect" }, o.columns.map((c, i) => {
    const values = o.rows.map((r) => r[i]);
    const present = values.filter((v) => v !== null && v !== "nan");
    const distinct = new Set(present.map(String)).size;
    const stats = [["Missing", values.length - present.length], ["Distinct", distinct]];
    let picture = null;
    if (numeric[i] && present.length) {
      const nums = present.filter(isNumber);
      const min = Math.min(...nums), max = Math.max(...nums);
      stats.push(["Min", +min.toFixed(4)], ["Max", +max.toFixed(4)], ["Mean", +(nums.reduce((a, b) => a + b, 0) / nums.length).toFixed(4)]);
      const bins = new Array(12).fill(0);
      nums.forEach((v) => { bins[Math.min(11, Math.floor(((v - min) / (max - min || 1)) * 12))] += 1; });
      const top = Math.max(...bins);
      picture = h("div", { class: "hist", "aria-label": `Distribution of ${c}` }, bins.map((b) => h("span", { style: `height:${Math.round((b / top) * 100)}%` })));
    } else if (present.length) {
      const counts = new Map();
      present.forEach((v) => counts.set(String(v), (counts.get(String(v)) || 0) + 1));
      const topValues = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 4);
      picture = h("ul", { class: "top-values" }, topValues.map(([v, n]) => h("li", {}, h("span", {}, v.length > 24 ? `${v.slice(0, 23)}…` : v), h("b", {}, n))));
    }
    return h("div", { class: "inspect-col" }, h("div", { class: "inspect-name" }, c, h("span", {}, numeric[i] ? "number" : "text")),
      picture, h("dl", {}, stats.map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])));
  }));
}

// ---------- running cells ----------

async function runCell(cell) {
  if (cell.type !== "code") return true;
  if (nbWhere(state.notebook).fabric) {
    toast("This notebook runs in Fabric, which runs the whole notebook. Use Run all, or switch Runs on to This PC to run one cell.");
    return false;
  }
  if (cell.meta?.frozen) {
    toast("This cell is frozen, so it doesn't run. Unfreeze it on the Edit tab.");
    return true;
  }
  if (state.kernel === "busy" && cell.status !== "queued") return false;
  stopRequested = false;
  setKernel("busy");
  cell.status = "running";
  cell.outputs = [];
  cell.seconds = 0;
  updateResults(cell);
  const started = performance.now();
  let ok = false;
  try {
    let snap = await rpc("start_cell", { code: cell.source, lakehouse_ref: state.notebook.lakehouse, notebook: state.notebook.name });
    for (;;) {
      if (snap.outputs) cell.outputs = snap.outputs;
      cell.count = snap.execution_count;
      cell.seconds = snap.seconds;
      if (snap.status !== "running") break;
      if (state.notebook?.cells.includes(cell)) updateResults(cell);
      snap = await rpc("poll_cell", { version: snap.version, wait: 0.4 });
    }
    cell.status = snap.status;
    ok = snap.status === "ok";
  } catch (err) {
    if (stopRequested) {
      cell.status = "stopped";
      cell.outputs = [{ type: "text", text: "Stopped. The kernel restarted, so variables from earlier cells are gone." }];
    } else {
      cell.status = "error";
      cell.outputs = [{ type: "error", ename: "Kernel error", evalue: String(err) }];
    }
    cell.seconds = (performance.now() - started) / 1000;
  }
  cell.finishedAt = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  setKernel(stopRequested ? "stopped" : "idle");
  if (state.notebook?.cells.includes(cell)) updateResults(cell);
  if (state.notebook?.dirty) markDirty();
  refreshVariables();
  return ok;
}

async function runCells(cells) {
  const code = cells.filter((c) => c.type === "code" && c.source.trim() && !c.meta?.frozen);
  cancelRequested = false;
  code.forEach((c) => { c.status = "queued"; c.outputs = []; });
  setKernel("busy");
  renderNotebook();
  for (const [i, cell] of code.entries()) {
    const ok = await runCell(cell);
    if (!ok || cancelRequested) {
      code.slice(i + 1).forEach((c) => { c.status = null; updateResults(c); });
      setKernel(stopRequested ? "stopped" : "idle");
      return;
    }
    setKernel("busy");
  }
  setKernel(stopRequested ? "stopped" : "idle");
}

function runAll() { return nbWhere(state.notebook).fabric ? runInFabric() : runCells(state.notebook.cells); }

function runAbove() {
  const cells = state.notebook.cells;
  return runCells(cells.slice(0, cells.indexOf(selectedCell())));
}

function runBelow() {
  const cells = state.notebook.cells;
  return runCells(cells.slice(cells.indexOf(selectedCell())));
}

function setKernel(status) {
  if (state.kernel === status) return;
  state.kernel = status;
  if (state.view !== "notebook") return;
  const bar = document.querySelector(".statusbar span");
  if (bar) bar.replaceChildren(h("span", { class: `dot ${status}` }), kernelLabel());
  document.querySelectorAll("[data-run]").forEach((b) => { b.disabled = status === "busy" || b.hasAttribute("data-local-only"); });
  document.querySelectorAll("[data-stop]").forEach((b) => { b.disabled = status !== "busy"; });
}

// Cancel stops the running cell and keeps variables. Code stuck inside a native library can't be
// stopped that way; then restarting the kernel is the way out, and it clears variables.
async function cancelRun() {
  cancelRequested = true;
  state.notebook?.cells.forEach((c) => { if (c.status === "queued") { c.status = null; updateResults(c); } });
  const res = await attempt(() => rpc("interrupt_cell"), "Could not cancel");
  if (res?.interrupted && !res.stopped) {
    const overlay = showDialog("The cell is still running", h("p", { class: "dialog-text" },
      "It is inside a library call that can't be interrupted. Restarting the kernel stops it, but clears your variables."), [
      btn("Keep waiting", null, closeDialog, "tool outline"),
      btn("Restart kernel", null, () => { closeDialog(); stopKernel(); }, "tool primary"),
    ]);
    overlay.querySelector(".tool.primary")?.focus();
  }
}

async function stopKernel() {
  stopRequested = true;
  cancelRequested = true;
  await attempt(() => invoke("stop_kernel"), "Could not stop the kernel");
  state.notebook?.cells.forEach((c) => { if (c.status === "queued") c.status = null; });
}

async function restartKernel() {
  if (state.kernel === "busy") {
    await stopKernel();
  } else if (await attempt(() => rpc("restart_kernel"), "Could not restart")) {
    state.notebook?.cells.forEach((c) => { c.count = null; });
    state.kernel = "idle";
    state.variables = [];
    toast("Kernel restarted. Variables were cleared.");
    if (state.view === "notebook") renderNotebook();
  }
}
