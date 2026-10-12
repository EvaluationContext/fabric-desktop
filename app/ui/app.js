// Fabric Desktop UI. Every data action is one call to the Python sidecar
// through the shell; all compute runs on this PC.
const invoke = (cmd, args) => window.__TAURI__.core.invoke(cmd, args);
const rpc = (method, params = {}) => invoke("rpc", { method, params });

const state = {
  view: "home",
  user: null,
  python: "",
  query: "",
  workspaces: null,          // [{id, displayName}] once signed in
  openWorkspace: null,       // {id, displayName} shown in the rail
  lakehouseView: null,       // {workspace_id, workspace_name, lakehouse_id, name}
  notebook: null,            // {name, cells, lakehouse, dirty}
  selected: null,            // selected cell id
  kernel: "idle",            // idle | busy | stopped
  ribbonTab: "home",
  explorerTab: "data",
  showExplorer: true,
  tree: {},                  // explorer cache: key -> {open, items, loading}
};

let nextCellId = 1;
let saveTimer = null;
let stopRequested = false;

// ---------- small DOM helpers ----------

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "style") node.style.cssText = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function btn(label, iconName, onClick, cls = "tool", attrs = {}) {
  return h("button", { class: cls, onclick: onClick, ...attrs }, iconName ? icon(iconName, 16) : null, label);
}

function hydrateIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((el) => {
    el.replaceWith(icon(el.dataset.icon, Number(el.dataset.size || 20)));
  });
}

function toast(text, isError = false) {
  const t = document.getElementById("toast");
  t.textContent = text;
  t.className = isError ? "toast error" : "toast";
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, isError ? 8000 : 4000);
}

async function attempt(fn, label) {
  try {
    return await fn();
  } catch (err) {
    toast(label ? `${label}: ${err}` : String(err), true);
    return undefined;
  }
}

function matches(text) {
  return !state.query || String(text).toLowerCase().includes(state.query.toLowerCase());
}

// ---------- dialogs and menus ----------

function closeDialog() {
  document.getElementById("dialog-root").replaceChildren();
}

function showDialog(title, body, actions = []) {
  const overlay = h("div", { class: "overlay", onmousedown: (e) => { if (e.target === overlay) closeDialog(); } },
    h("div", { class: "dialog", role: "dialog", "aria-label": title },
      h("div", { class: "dialog-head" }, title,
        h("button", { class: "tool small", "aria-label": "Close", onclick: closeDialog }, icon("dismiss", 16))),
      body,
      actions.length ? h("div", { class: "dialog-actions" }, actions) : null));
  document.getElementById("dialog-root").replaceChildren(overlay);
  overlay.querySelector("input, select")?.focus();
  return overlay;
}

function askText(title, label, initial = "", okLabel = "OK") {
  return new Promise((resolve) => {
    const input = h("input", { value: initial });
    const done = (value) => { closeDialog(); resolve(value); };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && input.value.trim()) done(input.value.trim());
      if (e.key === "Escape") done(null);
    });
    showDialog(title, h("label", { class: "field" }, label, input), [
      btn("Cancel", null, () => done(null), "tool outline"),
      btn(okLabel, null, () => input.value.trim() && done(input.value.trim()), "tool primary"),
    ]);
    input.select();
  });
}

function showMenu(anchor, items) {
  closeMenu();
  const rect = anchor.getBoundingClientRect();
  const menu = h("div", { class: "menu", id: "menu", style: `left:${rect.left}px;top:${rect.bottom + 4}px` },
    items.map((it) => it.info ? h("div", { class: "info" }, it.info)
      : btn(it.label, it.icon, () => { closeMenu(); it.action(); })));
  document.body.append(menu);
  setTimeout(() => document.addEventListener("mousedown", closeMenuOutside), 0);
}

function closeMenuOutside(e) {
  if (!document.getElementById("menu")?.contains(e.target)) closeMenu();
}

function closeMenu() {
  document.getElementById("menu")?.remove();
  document.removeEventListener("mousedown", closeMenuOutside);
}

// ---------- account and data ----------

async function signIn() {
  const result = await attempt(() => rpc("sign_in"), "Sign-in failed");
  if (!result) return false;
  state.user = result.user;
  renderChrome();
  await loadWorkspaces();
  return true;
}

async function ensureSignedIn() {
  return state.user ? true : signIn();
}

async function loadWorkspaces() {
  if (!(await ensureSignedIn())) return [];
  if (!state.workspaces) {
    state.workspaces = (await attempt(() => rpc("list_workspaces"), "Could not list workspaces")) || null;
  }
  return state.workspaces || [];
}

function lakehouseRef(ws, lh) {
  return { workspace_id: ws.id, workspace_name: ws.displayName, lakehouse_id: lh.id, name: lh.displayName };
}

// ---------- navigation and chrome ----------

function go(view) {
  closeMenu();
  state.view = view;
  render();
}

function renderChrome() {
  const account = document.getElementById("account");
  account.textContent = state.user ? state.user[0].toUpperCase() : "?";
  account.classList.toggle("signed-out", !state.user);
  account.title = state.user ? `Signed in as ${state.user}` : "Sign in";

  document.querySelectorAll("#rail .rail-item[data-view]").forEach((el) => {
    el.classList.toggle("active", el.dataset.view === (state.view === "entity" ? "metadata" : state.view));
  });
  const open = [];
  if (state.openWorkspace) {
    open.push(h("button", {
      class: `rail-item${state.view === "workspace" ? " active" : ""}`,
      onclick: () => openWorkspace(state.openWorkspace),
    }, icon("workspace", 22), state.openWorkspace.displayName));
  }
  if (state.notebook) {
    open.push(h("button", {
      class: `rail-item${state.view === "notebook" ? " active" : ""}`,
      onclick: () => go("notebook"),
    }, icon("notebook", 22), state.notebook.name));
  }
  document.getElementById("rail-open").replaceChildren(...open);

  const crumbs = [];
  if (state.view === "notebook" && state.notebook) {
    const lh = state.notebook.lakehouse;
    crumbs.push(h("span", {}, lh ? lh.workspace_name || "Workspace" : "My notebooks"), h("span", { "aria-hidden": "true" }, "/"));
    crumbs.push(h("button", { class: "current", title: "Rename notebook", onclick: renameNotebook },
      state.notebook.name + (state.notebook.dirty ? " •" : "")));
  } else if (state.view === "workspace" && state.openWorkspace) {
    crumbs.push(h("button", { class: "crumb", onclick: () => go("workspaces") }, "Workspaces"), h("span", {}, "/"),
      h("span", { class: "current" }, state.openWorkspace.displayName));
  } else if (state.view === "lakehouse" && state.lakehouseView) {
    const lh = state.lakehouseView;
    crumbs.push(h("button", { class: "crumb", onclick: () => openWorkspace({ id: lh.workspace_id, displayName: lh.workspace_name }) },
      lh.workspace_name), h("span", {}, "/"), h("span", { class: "current" }, lh.name));
  }
  document.getElementById("breadcrumb").replaceChildren(...crumbs);
}

function render() {
  renderChrome();
  const views = { home: renderHome, create: renderCreate, workspaces: renderWorkspaces, workspace: renderWorkspace,
    onelake: renderOneLake, monitor: renderMonitor, lakehouse: renderLakehouse, notebook: renderNotebook, ...window.META_VIEWS };
  (views[state.view] || renderHome)();
}

function setView(...children) {
  document.getElementById("view").replaceChildren(...children);
}

function signInCallout() {
  if (state.user) return null;
  return h("div", { class: "callout" }, icon("person", 20),
    h("span", {}, "Sign in to see your Fabric workspaces and lakehouses. Notebooks run on this PC either way."),
    h("span", { class: "spacer" }), btn("Sign in", null, async () => { if (await signIn()) render(); }, "tool primary"));
}

// ---------- Home, Create, Monitor ----------

async function renderHome() {
  const list = h("div", { class: "muted-text" }, "Loading notebooks...");
  setView(h("div", { class: "page" },
    h("h1", {}, state.user ? `Welcome, ${state.user.split("@")[0]}` : "Welcome to Fabric Desktop"),
    h("p", { class: "sub" }, "Work like you do in Fabric, with the compute on this PC. Results go straight to OneLake."),
    signInCallout(),
    h("div", { class: "tiles" },
      tile("notebook", "New notebook", "Python on this PC, attached to a lakehouse", newNotebook),
      tile("lakehouse", "New lakehouse", "A schema-enabled lakehouse in a workspace", () => newLakehouse()),
      tile("onelake", "OneLake catalog", "Browse lakehouses across your workspaces", () => go("onelake"))),
    h("h2", {}, "Recent notebooks"), list));
  const notebooks = (await attempt(() => rpc("list_notebooks"))) || [];
  if (state.view !== "home") return;
  const shown = notebooks.filter((n) => matches(n.name));
  list.replaceWith(shown.length ? h("table", { class: "list" },
    h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Type"), h("th", {}, "Modified"))),
    h("tbody", {}, shown.map((n) => h("tr", { class: "click", onclick: () => openNotebook(n.name) },
      h("td", {}, h("span", { class: "item" }, icon("notebook", 18), n.name)),
      h("td", {}, "Notebook"),
      h("td", {}, new Date(n.modified * 1000).toLocaleString()))))) : h("p", { class: "muted-text" },
    state.query ? "No notebooks match your search." : "No notebooks yet. Create one to get started."));
}

function tile(iconName, title, desc, onClick) {
  return h("button", { class: "tile", onclick: onClick },
    h("span", { class: "ti" }, icon(iconName, 28)), h("span", { class: "tt" }, title), h("span", { class: "td" }, desc));
}

function renderCreate() {
  setView(h("div", { class: "page" },
    h("h1", {}, "Create"), h("p", { class: "sub" }, "Items you create here run on this PC and store their data in OneLake."),
    h("h2", {}, "Data engineering"),
    h("div", { class: "tiles" },
      tile("notebook", "Notebook", "Explore, transform and write Delta tables with Python", newNotebook),
      tile("lakehouse", "Lakehouse", "Store tables and files in OneLake, organized by schema", () => newLakehouse()))));
}

async function renderMonitor() {
  const body = h("div", { class: "muted-text" }, "Loading runs...");
  setView(h("div", { class: "page" }, h("h1", {}, "Monitor"),
    h("p", { class: "sub" }, "Every table this PC has written to OneLake."), body));
  const runs = (await attempt(() => rpc("history"))) || [];
  if (state.view !== "monitor") return;
  const shown = runs.filter((r) => matches(`${r.job} ${r.table_uri}`));
  body.replaceWith(shown.length ? h("table", { class: "list" },
    h("thead", {}, h("tr", {}, ["Finished", "Activity", "Table", "Mode", "Rows", "Delta version", "Seconds"].map((c) => h("th", {}, c)))),
    h("tbody", {}, shown.map((r) => h("tr", {},
      h("td", {}, new Date(r.finished_at).toLocaleString()), h("td", {}, r.job),
      h("td", { title: r.table_uri }, r.table_uri.split("/Tables/")[1] || r.table_uri),
      h("td", {}, r.mode), h("td", {}, r.rows), h("td", {}, r.delta_version), h("td", {}, r.seconds))))) :
    h("p", { class: "muted-text" }, "No runs yet. Write a table from a notebook and it shows up here."));
}

// ---------- Workspaces, OneLake, lakehouse ----------

async function renderWorkspaces() {
  const body = h("div", { class: "muted-text" }, state.user ? "Loading workspaces..." : "");
  setView(h("div", { class: "page" }, h("h1", {}, "Workspaces"),
    h("p", { class: "sub" }, "Your Fabric workspaces."), signInCallout(), body));
  if (!state.user) return;
  const workspaces = await loadWorkspaces();
  if (state.view !== "workspaces") return;
  const shown = workspaces.filter((w) => matches(w.displayName));
  body.replaceWith(h("table", { class: "list" },
    h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Type"))),
    h("tbody", {}, shown.map((w) => h("tr", { class: "click", onclick: () => openWorkspace(w) },
      h("td", {}, h("span", { class: "item" }, icon("workspace", 18), w.displayName)),
      h("td", {}, w.type || "Workspace"))))));
}

function openWorkspace(ws) {
  state.openWorkspace = ws;
  go("workspace");
}

async function renderWorkspace() {
  const ws = state.openWorkspace;
  const body = h("div", { class: "muted-text" }, "Loading items...");
  setView(h("div", { class: "page" }, h("h1", {}, ws.displayName),
    h("div", { class: "toolbar", style: "padding:0;border:0;margin-bottom:16px" },
      btn("New lakehouse", "add", () => newLakehouse(ws), "tool outline"),
      btn("New notebook", "add", newNotebook, "tool outline")),
    body));
  const lakehouses = (await attempt(() => rpc("list_lakehouses", { workspace_id: ws.id }), "Could not list lakehouses")) || [];
  if (state.view !== "workspace" || state.openWorkspace !== ws) return;
  const shown = lakehouses.filter((l) => matches(l.displayName));
  body.replaceWith(shown.length ? h("table", { class: "list" },
    h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Type"), h("th", {}, ""))),
    h("tbody", {}, shown.map((lh) => h("tr", { class: "click", onclick: () => openLakehouse(lakehouseRef(ws, lh)) },
      h("td", {}, h("span", { class: "item" }, icon("lakehouse", 18), lh.displayName)),
      h("td", {}, "Lakehouse"),
      h("td", {}, btn("Open in notebook", "notebook", (e) => { e.stopPropagation(); newNotebook(lakehouseRef(ws, lh)); }, "tool small link")))))) :
    h("p", { class: "muted-text" }, "No lakehouses in this workspace yet."));
}

async function renderOneLake() {
  const body = h("div", { class: "muted-text" }, state.user ? "Loading lakehouses..." : "");
  setView(h("div", { class: "page" }, h("h1", {}, "OneLake catalog"),
    h("p", { class: "sub" }, "Lakehouses across all your workspaces."), signInCallout(), body));
  if (!state.user) return;
  const rows = [];
  const skipped = [];
  for (const ws of await loadWorkspaces()) {
    try {
      const lakehouses = await rpc("list_lakehouses", { workspace_id: ws.id });
      lakehouses.forEach((lh) => rows.push(lakehouseRef(ws, lh)));
    } catch (err) {
      // A workspace on a paused capacity can't list items; show the rest.
      skipped.push(String(err).includes("capacity is paused") ? `${ws.displayName} (capacity paused)` : ws.displayName);
    }
    if (state.view !== "onelake") return;
  }
  if (skipped.length) {
    body.before(h("p", { class: "muted-text" }, `Skipped ${skipped.length} workspace${skipped.length === 1 ? "" : "s"}: ${skipped.join(", ")}.`));
  }
  const shown = rows.filter((r) => matches(`${r.name} ${r.workspace_name}`));
  body.replaceWith(shown.length ? h("table", { class: "list" },
    h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Workspace"), h("th", {}, "Type"))),
    h("tbody", {}, shown.map((r) => h("tr", { class: "click", onclick: () => openLakehouse(r) },
      h("td", {}, h("span", { class: "item" }, icon("lakehouse", 18), r.name)),
      h("td", {}, r.workspace_name), h("td", {}, "Lakehouse"))))) :
    h("p", { class: "muted-text" }, "No lakehouses found."));
}

function openLakehouse(ref) {
  state.lakehouseView = ref;
  state.openWorkspace = { id: ref.workspace_id, displayName: ref.workspace_name };
  go("lakehouse");
}

function renderLakehouse() {
  const ref = state.lakehouseView;
  const tree = h("div", { class: "explorer-content", style: "padding:0" });
  setView(h("div", { class: "page" }, h("h1", {}, ref.name),
    h("p", { class: "sub" }, `Lakehouse in ${ref.workspace_name}`),
    h("div", { class: "toolbar", style: "padding:0;border:0;margin-bottom:16px" },
      btn("Open in new notebook", "notebook", () => newNotebook(ref), "tool primary")),
    h("div", { style: "max-width:520px;background:#fff;border:1px solid var(--border);border-radius:8px;padding:8px" }, tree)));
  renderTree(tree, ref, null);
}

async function newLakehouse(ws) {
  if (!(await ensureSignedIn())) return;
  if (!ws) {
    const workspaces = await loadWorkspaces();
    ws = await pickWorkspace(workspaces);
    if (!ws) return;
  }
  const name = await askText("New lakehouse", `Name (created in ${ws.displayName} with schemas enabled)`, "", "Create");
  if (!name) return;
  toast(`Creating ${name}...`);
  const lh = await attempt(() => rpc("create_lakehouse", { workspace_id: ws.id, name }), "Could not create the lakehouse");
  if (lh) {
    toast(`Created lakehouse ${name}`);
    openLakehouse(lakehouseRef(ws, lh));
  }
  return lh ? lakehouseRef(ws, lh) : null;
}

function pickWorkspace(workspaces) {
  return new Promise((resolve) => {
    const select = h("select", {}, workspaces.map((w) => h("option", { value: w.id }, w.displayName)));
    showDialog("Choose a workspace", h("label", { class: "field" }, "Workspace", select), [
      btn("Cancel", null, () => { closeDialog(); resolve(null); }, "tool outline"),
      btn("Next", null, () => { closeDialog(); resolve(workspaces.find((w) => w.id === select.value)); }, "tool primary"),
    ]);
  });
}

// ---------- Explorer tree (tables and files of a lakehouse) ----------

function treeKey(ref, part) {
  return `${ref.lakehouse_id}:${part}`;
}

async function toggleNode(key, loader, rerender) {
  const node = state.tree[key] || (state.tree[key] = { open: false });
  node.open = !node.open;
  if (node.open && !node.items) {
    node.loading = true;
    rerender();
    node.items = (await attempt(loader, "Could not load")) || null;
    node.loading = false;
    if (!node.items) node.open = false;
  }
  rerender();
}

function chevron(open) {
  return icon(open ? "chevronDown" : "chevronRight", 12);
}

function renderTree(container, ref, onInsert) {
  const rerender = () => renderTree(container, ref, onInsert);
  const rows = [];
  const tablesKey = treeKey(ref, "tables");
  const tables = state.tree[tablesKey] || {};
  rows.push(h("button", { class: "tree-row", style: "padding-left:6px",
    onclick: () => toggleNode(tablesKey, () => rpc("list_tables", { workspace_id: ref.workspace_id, lakehouse_id: ref.lakehouse_id }), rerender) },
  chevron(tables.open), h("span", { class: "label" }, "Tables")));
  if (tables.open) {
    if (tables.loading) rows.push(h("div", { class: "tree-row muted", style: "padding-left:28px" }, "Loading..."));
    for (const [schema, names] of Object.entries(tables.items || {})) {
      const sKey = treeKey(ref, `schema:${schema}`);
      const sOpen = state.tree[sKey]?.open ?? true;
      rows.push(h("button", { class: "tree-row", style: "padding-left:24px",
        onclick: () => { state.tree[sKey] = { open: !sOpen }; rerender(); } },
      chevron(sOpen), h("span", { class: "label", style: "color:var(--muted)" }, schema)));
      if (sOpen) {
        if (!names.length) rows.push(h("div", { class: "tree-row muted", style: "padding-left:46px" }, "No tables"));
        names.forEach((t) => rows.push(h("button", {
          class: "tree-row", style: "padding-left:44px", title: onInsert ? "Insert code that loads this table" : `${schema}.${t}`,
          onclick: () => onInsert && onInsert(`df = lakehouse.read_table("${schema}.${t}")\ndf`),
        }, icon("table", 15), h("span", { class: "label" }, t))));
      }
    }
  }
  rows.push(...fileRows(ref, "Files", 6, onInsert, rerender));
  container.replaceChildren(...rows);
}

function fileRows(ref, path, indent, onInsert, rerender) {
  const key = treeKey(ref, `files:${path}`);
  const node = state.tree[key] || {};
  const isRoot = path === "Files";
  const rows = [h("button", { class: "tree-row", style: `padding-left:${indent}px;${isRoot ? "margin-top:4px" : ""}`,
    onclick: () => toggleNode(key, () => rpc("list_files", { workspace_id: ref.workspace_id, lakehouse_id: ref.lakehouse_id, directory: path }), rerender) },
  chevron(node.open), isRoot ? null : icon("folder", 15), h("span", { class: "label" }, isRoot ? "Files" : path.split("/").pop()))];
  if (!node.open) return rows;
  if (node.loading) rows.push(h("div", { class: "tree-row muted", style: `padding-left:${indent + 22}px` }, "Loading..."));
  if (node.items && !node.items.length) rows.push(h("div", { class: "tree-row muted", style: `padding-left:${indent + 22}px` }, "Empty"));
  for (const item of node.items || []) {
    if (item.is_dir) {
      rows.push(...fileRows(ref, item.path, indent + 20, onInsert, rerender));
    } else {
      rows.push(h("button", { class: "tree-row", style: `padding-left:${indent + 38}px`,
        title: onInsert ? "Insert code that reads this file" : item.path,
        onclick: () => onInsert && onInsert(readSnippet(item.path)) },
      icon("file", 15), h("span", { class: "label" }, item.name), h("span", { class: "hint" }, formatSize(item.size))));
    }
  }
  return rows;
}

function readSnippet(path) {
  const ext = path.split(".").pop().toLowerCase();
  const reader = { parquet: "read_parquet", json: "read_json" }[ext] || "read_csv";
  return `df = lakehouse.${reader}("${path}")\ndf`;
}

function formatSize(bytes) {
  if (!bytes) return "";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
  return `${bytes.toFixed(i ? 1 : 0)} ${units[i]}`;
}

// ---------- Notebook files ----------

function cellFrom(c) {
  return { id: nextCellId++, type: c.type, source: c.source || "", outputs: [], status: null, count: null, editing: !c.source };
}

function loadIntoState(nb) {
  state.notebook = { name: nb.name, cells: nb.cells.map(cellFrom), lakehouse: nb.lakehouse || null, dirty: false };
  if (!state.notebook.cells.length) state.notebook.cells.push(cellFrom({ type: "code" }));
  state.notebook.cells.forEach((c) => { if (c.type === "markdown" && c.source) c.editing = false; });
  state.selected = state.notebook.cells[0].id;
  state.ribbonTab = "home";
}

async function openNotebook(name) {
  if (state.notebook?.dirty) await saveNotebook();
  const nb = await attempt(() => rpc("open_notebook", { name }), "Could not open the notebook");
  if (!nb) return;
  loadIntoState(nb);
  go("notebook");
}

async function newNotebook(lakehouse) {
  if (state.notebook?.dirty) await saveNotebook();
  const nb = await attempt(() => rpc("new_notebook"), "Could not create a notebook");
  if (!nb) return;
  closeDialog();
  loadIntoState(nb);
  const ref = lakehouse && lakehouse.lakehouse_id ? lakehouse : null;
  if (ref) {
    state.notebook.lakehouse = ref;
    state.notebook.cells[0].source = "import polars as pl\nfrom fabric_desktop import lakehouse\n";
    state.notebook.cells[0].editing = true;
    markDirty();
  }
  go("notebook");
}

function notebookPayload() {
  return { name: state.notebook.name, cells: state.notebook.cells.map((c) => ({ type: c.type, source: c.source })),
    lakehouse_ref: state.notebook.lakehouse };
}

async function saveNotebook() {
  if (!state.notebook) return;
  clearTimeout(saveTimer);
  const ok = await attempt(() => rpc("save_notebook", notebookPayload()), "Could not save");
  if (ok && state.notebook) {
    state.notebook.dirty = false;
    renderChrome();
  }
}

function markDirty() {
  if (!state.notebook) return;
  const wasDirty = state.notebook.dirty;
  state.notebook.dirty = true;
  if (!wasDirty) renderChrome();
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => { if (state.kernel !== "busy") saveNotebook(); }, 1500);
}

async function renameNotebook() {
  const nb = state.notebook;
  const name = await askText("Rename notebook", "Name", nb.name, "Rename");
  if (!name || name === nb.name) return;
  await saveNotebook();
  const ok = await attempt(() => rpc("rename_notebook", { name: nb.name, new_name: name }), "Could not rename");
  if (ok) { nb.name = name; renderChrome(); }
}

async function publishNotebook() {
  const nb = state.notebook;
  if (!nb.lakehouse) {
    toast("Attach a lakehouse first. The notebook is published to that lakehouse's workspace.", true);
    return;
  }
  if (!(await ensureSignedIn())) return;
  toast(`Publishing ${nb.name} to ${nb.lakehouse.workspace_name}...`);
  const result = await attempt(() => rpc("publish_notebook", notebookPayload()), "Publish failed");
  if (result) {
    nb.dirty = false;
    renderChrome();
    toast(`Published ${nb.name} to ${nb.lakehouse.workspace_name} as a Fabric notebook item.`);
  }
}

// ---------- Add data items ----------

async function openDataItems() {
  if (!(await ensureSignedIn())) return;
  const workspaces = await loadWorkspaces();
  if (!workspaces.length) { toast("No workspaces found for your account.", true); return; }
  const current = state.notebook.lakehouse?.workspace_id || state.openWorkspace?.id || workspaces[0].id;
  const select = h("select", {}, workspaces.map((w) => h("option", { value: w.id, selected: w.id === current }, w.displayName)));
  const list = h("div", { class: "scroll" });
  const load = async () => {
    const ws = workspaces.find((w) => w.id === select.value);
    list.replaceChildren(h("span", { class: "text" }, "Loading lakehouses..."));
    const lakehouses = (await attempt(() => rpc("list_lakehouses", { workspace_id: ws.id }), "Could not list lakehouses")) || [];
    if (select.value !== ws.id) return;
    list.replaceChildren(...lakehouses.map((lh) => {
      const attached = state.notebook.lakehouse?.lakehouse_id === lh.id;
      return h("button", { class: "choice", onclick: () => attachLakehouse(lakehouseRef(ws, lh)) },
        icon("lakehouse", 20),
        h("span", { class: "main" }, h("span", { class: "n" }, lh.displayName), h("span", { class: "w" }, ws.displayName)),
        h("span", { class: "tag" }, attached ? "Attached" : ""));
    }), lakehouses.length ? "" : h("span", { class: "text" }, "No lakehouses in this workspace yet."));
  };
  select.addEventListener("change", load);
  showDialog("Add data items", h("div", { style: "display:flex;flex-direction:column;gap:12px;min-height:0" },
    h("span", { class: "text" }, "Choose a lakehouse. The notebook reads its files and writes tables to it through OneLake."),
    h("label", { class: "field" }, "Workspace", select), list,
    btn("New lakehouse", "add", async () => {
      const ws = workspaces.find((w) => w.id === select.value);
      closeDialog();
      const view = state.view;
      const ref = await newLakehouse(ws);
      if (ref) { state.view = view; attachLakehouse(ref); }
    }, "tool link", { style: "align-self:flex-start" })));
  load();
}

function attachLakehouse(ref) {
  closeDialog();
  state.notebook.lakehouse = ref;
  state.explorerTab = "data";
  state.showExplorer = true;
  markDirty();
  go("notebook");
}

function detachLakehouse() {
  state.notebook.lakehouse = null;
  markDirty();
  renderNotebook();
}

// ---------- Notebook view ----------

function selectedCell() {
  return state.notebook.cells.find((c) => c.id === state.selected) || state.notebook.cells[0];
}

function renderNotebook() {
  const nb = state.notebook;
  if (!nb) return go("home");
  const scroller = document.querySelector(".cells");
  const scrollTop = scroller ? scroller.scrollTop : 0;
  const busy = state.kernel === "busy";

  const tabs = ["home", "edit", "run", "view"].map((t) => h("button", {
    class: `tab${state.ribbonTab === t ? " active" : ""}`, role: "tab", "aria-selected": String(state.ribbonTab === t),
    onclick: () => { state.ribbonTab = t; renderNotebook(); },
  }, t[0].toUpperCase() + t.slice(1)));

  const runAllBtn = btn("Run all", "play", runAll, "tool", { disabled: busy, "data-run": true });
  runAllBtn.querySelector("svg").classList.add("play");
  const stopBtn = btn("Stop", "stop", stopKernel, "tool", { disabled: !busy, "data-stop": true });
  const envBtn = h("button", { class: "tool outline", onclick: (e) => showMenu(e.currentTarget, [
    { info: `Python ${state.python || ""} on this PC. No Fabric capacity is used.` },
    { label: "Restart kernel", icon: "restart", action: restartKernel },
  ]) }, icon("desktop", 16), `Python ${state.python || "3"} · this PC`, icon("chevronDown", 14));

  const toolbars = {
    home: [runAllBtn, stopBtn, h("div", { class: "sep" }), btn("Add data items", "lakehouse", openDataItems), envBtn,
      h("div", { class: "spacer" }), btn("Save", "save", saveNotebook),
      btn("Publish to OneLake", "publish", publishNotebook, "tool primary")],
    edit: [btn("Code", "add", () => addCell("code")), btn("Markdown", "add", () => addCell("markdown")),
      h("div", { class: "sep" }), btn("Move up", "up", () => moveCell(-1)), btn("Move down", "down", () => moveCell(1)),
      btn("Delete cell", "delete", () => deleteCell(selectedCell())), h("div", { class: "sep" }),
      btn("Clear all outputs", "clear", clearOutputs)],
    run: [runAllBtn, btn("Run cell", "play", () => runCell(selectedCell()), "tool", { disabled: busy, "data-run": true }),
      btn("Run all above", "up", runAbove, "tool", { disabled: busy, "data-run": true }), stopBtn, h("div", { class: "sep" }),
      btn("Restart kernel", "restart", restartKernel), btn("Restart and run all", "restart", async () => { await restartKernel(); runAll(); }, "tool", { disabled: busy, "data-run": true })],
    view: [btn(state.showExplorer ? "Hide Explorer" : "Show Explorer", "panel", () => { state.showExplorer = !state.showExplorer; renderNotebook(); })],
  };

  const cells = h("section", { class: "cells", "aria-label": "Notebook cells" },
    h("div", { class: "cells-inner" }, nb.cells.map(renderCell),
      h("div", { class: "add-cell" }, btn("Code", "add", () => addCell("code", true), "tool outline"),
        btn("Markdown", "add", () => addCell("markdown", true), "tool outline"))));

  const lh = nb.lakehouse;
  setView(
    h("div", { class: "ribbon" }, h("div", { class: "ribbon-tabs", role: "tablist", "aria-label": "Ribbon" }, tabs),
      h("div", { class: "toolbar" }, toolbars[state.ribbonTab])),
    h("div", { class: "nb-body" }, state.showExplorer ? renderExplorer() : null, cells),
    h("footer", { class: "statusbar" },
      h("span", {}, h("span", { class: `dot ${state.kernel}` }), kernelLabel()),
      h("span", {}, `Attached: ${lh ? lh.name : "none"}`),
      h("span", {}, "Compute: this PC, no Fabric capacity used")));
  document.querySelector(".cells").scrollTop = scrollTop;
  document.querySelectorAll(".editor textarea, .md-edit textarea").forEach(autoGrow);
}

function kernelLabel() {
  return { idle: "Kernel idle", busy: "Kernel busy", stopped: "Kernel stopped, variables cleared" }[state.kernel];
}

function renderExplorer() {
  const lh = state.notebook.lakehouse;
  const tabs = h("div", { class: "explorer-tabs" },
    h("button", { class: `tab${state.explorerTab === "data" ? " active" : ""}`, onclick: () => { state.explorerTab = "data"; renderNotebook(); } }, "Data items"),
    h("button", { class: `tab${state.explorerTab === "resources" ? " active" : ""}`, onclick: () => { state.explorerTab = "resources"; renderNotebook(); } }, "Resources"));
  const content = h("div", { class: "explorer-content" });

  if (state.explorerTab === "resources") {
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

const SNIPPETS = [
  ["Import the lakehouse", "import polars as pl\nfrom fabric_desktop import lakehouse"],
  ["Read a CSV from Files", 'df = lakehouse.read_csv("Files/raw/data.csv")\ndf'],
  ["Read a Parquet file", 'df = lakehouse.read_parquet("Files/data.parquet")\ndf'],
  ["Read a table", 'df = lakehouse.read_table("dbo.my_table")\ndf'],
  ["Write a table", 'lakehouse.write_table(df, "dbo.my_table", mode="overwrite")'],
  ["Append to a table", 'lakehouse.write_table(df, "dbo.my_table", mode="append")'],
  ["List files", 'lakehouse.files("Files")'],
  ["Query with DuckDB", 'import duckdb\nduckdb.sql("select * from df limit 10").pl()'],
];

function renderCell(cell) {
  const nb = state.notebook;
  const selected = cell.id === state.selected;
  const node = h("div", {
    class: `cell ${cell.type}${selected ? " selected" : ""}`, "data-cell": cell.id,
    onmousedown: () => { if (state.selected !== cell.id) selectCell(cell); },
  });
  const actions = h("div", { class: "cell-actions" },
    h("button", { class: "tool small", title: "Move up", onclick: () => moveCell(-1, cell) }, icon("up", 14)),
    h("button", { class: "tool small", title: "Move down", onclick: () => moveCell(1, cell) }, icon("down", 14)),
    h("button", { class: "tool small", title: "Delete cell", onclick: () => deleteCell(cell) }, icon("delete", 14)));

  if (cell.type === "markdown") {
    let body;
    if (cell.editing) {
      const ta = h("textarea", { spellcheck: "false", placeholder: "Write Markdown. Press Shift+Enter to render." });
      ta.value = cell.source;
      ta.addEventListener("input", () => { cell.source = ta.value; autoGrow(ta); markDirty(); });
      ta.addEventListener("keydown", (e) => cellKeys(e, cell));
      ta.addEventListener("blur", () => { if (cell.source.trim()) { cell.editing = false; updateCell(cell); } });
      body = h("div", { class: "md-edit" }, ta);
    } else {
      body = h("div", { class: "md-view", ondblclick: () => { cell.editing = true; updateCell(cell, true); } }, markdown(cell.source));
    }
    node.append(h("div", { class: "gutter" }), h("div", { class: "cell-box" }, actions, body));
    return node;
  }

  const running = cell.status === "running" || cell.status === "queued";
  const runBtn = h("button", { class: "tool small", "aria-label": "Run cell", title: "Run cell (Shift+Enter)",
    disabled: state.kernel === "busy", "data-run": true, onclick: () => runCell(cell) }, icon("play", 14));
  runBtn.querySelector("svg").classList.add("play");
  const gutter = h("div", { class: "gutter" }, runBtn, running ? "[*]" : `[${cell.count ?? " "}]`);

  const pre = h("pre", { "aria-hidden": "true" });
  const ta = h("textarea", { spellcheck: "false", wrap: "off", placeholder: "Write Python. Press Shift+Enter to run.", "aria-label": "Code" });
  ta.value = cell.source;
  pre.innerHTML = highlight(cell.source);
  ta.addEventListener("input", () => { cell.source = ta.value; pre.innerHTML = highlight(ta.value); autoGrow(ta); markDirty(); });
  ta.addEventListener("scroll", () => { pre.scrollLeft = ta.scrollLeft; });
  ta.addEventListener("keydown", (e) => cellKeys(e, cell));

  const box = h("div", { class: "cell-box" }, actions, h("div", { class: "editor" }, pre, ta));
  if (cell.status) box.append(statusLine(cell));
  cell.outputs.forEach((o) => box.append(renderOutput(o)));
  node.append(gutter, box);
  return node;
}

function statusLine(cell) {
  const s = cell.status;
  const label = { queued: ["busy", "Queued"], running: ["busy", "Running"], ok: ["ok", "Succeeded"],
    error: ["bad", "Failed"], stopped: ["bad", "Stopped"] }[s];
  return h("div", { class: "cell-status" }, h("span", { class: label[0] }, label[1]),
    s === "ok" || s === "error" ? h("span", {}, "Ran on this PC") : null,
    cell.seconds != null && s !== "running" ? h("span", {}, `${cell.seconds.toFixed(1)} s`) : null);
}

function renderOutput(o) {
  if (o.type === "stream" || o.type === "text") return h("div", { class: "output" }, h("pre", {}, o.text));
  if (o.type === "error") {
    return h("div", { class: "output error" }, h("div", { class: "ename" }, `${o.ename}: ${o.evalue}`),
      o.traceback ? h("details", {}, h("summary", {}, "Traceback"), h("pre", {}, o.traceback)) : null);
  }
  if (o.type === "table") {
    const numeric = o.columns.map((_, i) => o.rows.length > 0 && o.rows.every((r) => r[i] === null || typeof r[i] === "number"));
    return h("div", { class: "output" },
      h("table", {}, h("thead", {}, h("tr", {}, o.columns.map((c, i) => h("th", { class: numeric[i] ? "n" : null }, c)))),
        h("tbody", {}, o.rows.map((r) => h("tr", {}, r.map((v, i) => h("td", { class: numeric[i] ? "n" : null }, v === null ? "null" : v)))))),
      h("div", { class: "more" }, o.total_rows > o.rows.length
        ? `Showing the first ${o.rows.length} of ${o.total_rows} rows`
        : `${o.total_rows} row${o.total_rows === 1 ? "" : "s"}`));
  }
  return h("div", { class: "output" }, h("pre", {}, JSON.stringify(o)));
}

function updateCell(cell, focus = false) {
  const old = document.querySelector(`[data-cell="${cell.id}"]`);
  if (!old) return;
  const fresh = renderCell(cell);
  old.replaceWith(fresh);
  fresh.querySelectorAll("textarea").forEach(autoGrow);
  if (focus) fresh.querySelector("textarea")?.focus();
}

function selectCell(cell) {
  const prev = state.selected;
  state.selected = cell.id;
  document.querySelector(`[data-cell="${prev}"]`)?.classList.remove("selected");
  document.querySelector(`[data-cell="${cell.id}"]`)?.classList.add("selected");
}

function autoGrow(ta) {
  ta.style.height = "auto";
  ta.style.height = `${ta.scrollHeight + (ta.scrollWidth > ta.clientWidth ? 12 : 0)}px`;
}

function cellKeys(e, cell) {
  const ta = e.target;
  if (e.key === "Enter" && (e.shiftKey || e.ctrlKey)) {
    e.preventDefault();
    if (cell.type === "markdown") {
      cell.editing = false;
      updateCell(cell);
      if (e.shiftKey) focusNext(cell);
    } else {
      runCell(cell).then(() => { if (e.shiftKey) focusNext(cell); });
    }
  } else if (e.key === "Tab") {
    e.preventDefault();
    const { selectionStart: s, selectionEnd: end } = ta;
    ta.setRangeText("    ", s, end, "end");
    ta.dispatchEvent(new Event("input"));
  } else if (e.key === "Escape") {
    ta.blur();
  }
}

function focusNext(cell) {
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
  el?.querySelector("textarea")?.focus();
  el?.scrollIntoView({ block: "nearest" });
}

function addCell(type, atEnd = false) {
  const cells = state.notebook.cells;
  const cell = cellFrom({ type });
  const i = atEnd ? cells.length - 1 : cells.indexOf(selectedCell());
  cells.splice(i + 1, 0, cell);
  state.selected = cell.id;
  markDirty();
  renderNotebook();
  const el = document.querySelector(`[data-cell="${cell.id}"]`);
  el?.querySelector("textarea")?.focus();
  el?.scrollIntoView({ block: "nearest" });
  return cell;
}

function insertCell(code) {
  const cell = addCell("code");
  cell.source = code;
  updateCell(cell, true);
  markDirty();
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
}

function deleteCell(cell) {
  const cells = state.notebook.cells;
  const i = cells.indexOf(cell);
  cells.splice(i, 1);
  if (!cells.length) cells.push(cellFrom({ type: "code" }));
  state.selected = (cells[i] || cells[i - 1]).id;
  markDirty();
  renderNotebook();
}

function clearOutputs() {
  state.notebook.cells.forEach((c) => { c.outputs = []; c.status = null; c.count = null; c.seconds = null; });
  renderNotebook();
}

// ---------- Running cells ----------

async function runCell(cell) {
  if (cell.type !== "code") return true;
  if (state.kernel === "busy" && cell.status !== "queued") return false;
  stopRequested = false;
  setKernel("busy");
  cell.status = "running";
  cell.outputs = [];
  updateCell(cell);
  const started = performance.now();
  let ok = false;
  try {
    const result = await rpc("run_cell", { code: cell.source, lakehouse_ref: state.notebook.lakehouse });
    cell.outputs = result.outputs;
    cell.count = result.execution_count;
    cell.status = result.status;
    ok = result.status === "ok";
  } catch (err) {
    if (stopRequested) {
      cell.status = "stopped";
      cell.outputs = [{ type: "text", text: "Stopped. The kernel restarted, so variables from earlier cells are gone." }];
    } else {
      cell.status = "error";
      cell.outputs = [{ type: "error", ename: "Kernel error", evalue: String(err) }];
    }
  }
  cell.seconds = (performance.now() - started) / 1000;
  setKernel(stopRequested ? "stopped" : "idle");
  if (state.notebook?.cells.includes(cell)) updateCell(cell);
  if (state.notebook?.dirty) markDirty();
  return ok;
}

async function runCells(cells) {
  const code = cells.filter((c) => c.type === "code" && c.source.trim());
  code.forEach((c) => { c.status = "queued"; c.outputs = []; });
  setKernel("busy");
  renderNotebook();
  for (const [i, cell] of code.entries()) {
    const ok = await runCell(cell);
    if (!ok) {
      code.slice(i + 1).forEach((c) => { c.status = null; });
      renderNotebook();
      return;
    }
    setKernel("busy");
  }
  setKernel(stopRequested ? "stopped" : "idle");
}

function runAll() { return runCells(state.notebook.cells); }

function runAbove() {
  const cells = state.notebook.cells;
  return runCells(cells.slice(0, cells.indexOf(selectedCell())));
}

function setKernel(status) {
  if (state.kernel === status) return;
  state.kernel = status;
  if (state.view !== "notebook") return;
  const bar = document.querySelector(".statusbar span");
  if (bar) bar.replaceChildren(h("span", { class: `dot ${status}` }), kernelLabel());
  document.querySelectorAll("[data-run]").forEach((b) => { b.disabled = status === "busy"; });
  document.querySelectorAll("[data-stop]").forEach((b) => { b.disabled = status !== "busy"; });
}

async function stopKernel() {
  stopRequested = true;
  await attempt(() => invoke("stop_kernel"), "Could not stop the kernel");
  state.notebook?.cells.forEach((c) => { if (c.status === "queued") c.status = null; });
}

async function restartKernel() {
  if (state.kernel === "busy") {
    await stopKernel();
  } else if (await attempt(() => rpc("restart_kernel"), "Could not restart")) {
    state.notebook?.cells.forEach((c) => { c.count = null; });
    state.kernel = "idle";
    toast("Kernel restarted. Variables were cleared.");
    if (state.view === "notebook") renderNotebook();
  }
}

// ---------- Syntax highlighting and Markdown ----------

const PY_KEYWORDS = new Set(("False None True and as assert async await break class continue def del elif else except finally " +
  "for from global if import in is lambda nonlocal not or pass raise return try while with yield match case").split(" "));
const PY_BUILTINS = new Set("print len range list dict set tuple str int float bool sum min max sorted enumerate zip open type isinstance any all abs round".split(" "));
const PY_TOKEN = /(#[^\n]*)|([rRbBuUfF]{0,2}(?:"""[\s\S]*?(?:"""|$)|'''[\s\S]*?(?:'''|$)|"(?:\\.|[^"\\\n])*"?|'(?:\\.|[^'\\\n])*'?))|\b(\d+(?:\.\d+)?(?:e[+-]?\d+)?)\b|([A-Za-z_]\w*)(?=\s*\()|([A-Za-z_]\w*)/g;

function esc(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function highlight(code) {
  let out = "";
  let last = 0;
  for (const m of code.matchAll(PY_TOKEN)) {
    out += esc(code.slice(last, m.index));
    const [text, comment, string, number, call, word] = m;
    let cls = null;
    if (comment) cls = "cm";
    else if (string) cls = "str";
    else if (number) cls = "num";
    else if (call) cls = PY_KEYWORDS.has(call) ? "kw" : PY_BUILTINS.has(call) ? "bi" : "fn";
    else if (word && PY_KEYWORDS.has(word)) cls = "kw";
    out += cls ? `<span class="${cls}">${esc(text)}</span>` : esc(text);
    last = m.index + text.length;
  }
  // A trailing newline needs a character after it or the overlay is one line short.
  return out + esc(code.slice(last)) + "\n";
}

function inlineMd(text) {
  return esc(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<u title="$2">$1</u>');
}

function markdown(src) {
  const wrap = h("div");
  if (!src.trim()) {
    wrap.innerHTML = '<p class="placeholder">Empty Markdown cell. Double-click to edit.</p>';
    return wrap;
  }
  const html = [];
  let list = null;
  let para = [];
  const flushPara = () => { if (para.length) { html.push(`<p>${inlineMd(para.join(" "))}</p>`); para = []; } };
  const flushList = () => { if (list) { html.push(`<ul>${list.map((li) => `<li>${inlineMd(li)}</li>`).join("")}</ul>`); list = null; } };
  for (const line of src.split("\n")) {
    const heading = line.match(/^(#{1,3})\s+(.*)$/);
    const item = line.match(/^\s*[-*]\s+(.*)$/);
    if (heading) { flushPara(); flushList(); html.push(`<h${heading[1].length}>${inlineMd(heading[2])}</h${heading[1].length}>`); }
    else if (item) { flushPara(); (list ||= []).push(item[1]); }
    else if (!line.trim()) { flushPara(); flushList(); }
    else { flushList(); para.push(line.trim()); }
  }
  flushPara();
  flushList();
  wrap.innerHTML = html.join("");
  return wrap;
}

// ---------- startup ----------

document.querySelectorAll("#rail .rail-item[data-view]").forEach((el) => {
  el.addEventListener("click", () => go(el.dataset.view));
});
document.getElementById("account").addEventListener("click", (e) => {
  if (!state.user) { signIn().then((ok) => ok && render()); return; }
  showMenu(e.currentTarget, [{ info: `Signed in as ${state.user}` }]);
});
document.getElementById("search").addEventListener("input", (e) => {
  state.query = e.target.value;
  if (state.view === "notebook" || state.view === "create" || state.view === "lakehouse") state.view = "home";
  render();
});
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s" && state.view === "notebook") {
    e.preventDefault();
    saveNotebook().then(() => toast("Saved"));
  }
});
window.addEventListener("beforeunload", () => { if (state.notebook?.dirty) saveNotebook(); });

// ---------- startup component check ----------

// Before the sidecar starts, make sure this exe, Python and the sidecar are
// current and match each other, updating whatever is behind.
async function checkComponents() {
  const short = (commit) => commit.slice(0, 7);
  const rows = {};
  const row = (key, label) => (rows[key] = h("div", { class: "startup-row", "data-status": "busy" },
    h("span", { class: "startup-mark" }), h("span", { class: "startup-name" }, label),
    h("span", { class: "startup-detail" }, "Checking…")));
  const set = (key, status, detail) => {
    rows[key].dataset.status = status;
    rows[key].lastChild.textContent = detail;
  };
  const log = h("pre", { class: "startup-log", hidden: true });
  const actions = h("div", { class: "dialog-actions" });
  const choose = (...labels) => new Promise((resolve) => actions.replaceChildren(...labels.map((label, i) =>
    btn(label, null, () => { actions.replaceChildren(); resolve(i); }, i === 0 ? "tool primary" : "tool outline"))));
  const listen = (fn) => window.__TAURI__.event.listen("component-log", (e) => fn(e.payload));

  document.getElementById("dialog-root").replaceChildren(h("div", { class: "overlay" },
    h("div", { class: "dialog", role: "dialog", "aria-label": "Checking components" },
      h("div", { class: "dialog-head" }, "Checking components"),
      h("div", { class: "startup-rows" }, row("app", "Fabric Desktop"), row("python", "Python"), row("sidecar", "Local engine")),
      log, actions)));

  let report;
  try {
    report = await invoke("check_components");
  } catch (err) {
    set("app", "skip", "Needs Python");
    set("python", "error", `${err} Install Python 3.11 or later from python.org, or set FABRIC_DESKTOP_PYTHON to its full path.`);
    set("sidecar", "skip", "Needs Python");
    if (await choose("Retry", "Continue anyway") === 0) return checkComponents();
    return closeDialog();
  }
  const { app, sidecar, latest } = report;

  set("python", report.python_ok ? "ok" : "error",
    report.python_ok ? report.python : `${report.python} is too old. Fabric Desktop needs Python 3.11 or later.`);

  if (!app.commit) set("app", "ok", `${app.version}, local build (update check off)`);
  else if (!latest.commit) set("app", "warn", `Build ${short(app.commit)}. Could not check for a newer build.`);
  else if (latest.commit === app.commit) set("app", "ok", `Up to date (build ${short(app.commit)})`);
  else {
    set("app", "update", `Build ${short(latest.commit)} is available. You have ${short(app.commit)}.`);
    if (await choose("Update now", "Later") === 0) {
      set("app", "busy", "Downloading…");
      const unlisten = await listen((line) => {
        const m = /^progress (\d+)/.exec(line);
        if (m) set("app", "busy", `Downloading… ${m[1]}%`);
      });
      try {
        await invoke("update_app");
        set("app", "ok", "Restarting…");
        return new Promise(() => {});  // the new exe takes over
      } catch (err) {
        set("app", "warn", `Update failed: ${err}`);
      } finally {
        unlisten();
      }
    } else {
      set("app", "warn", `Update skipped. Still on build ${short(app.commit)}.`);
    }
  }

  if (!report.python_ok) set("sidecar", "skip", "Needs Python 3.11 or later");
  else if (sidecar.editable) set("sidecar", "ok", `${sidecar.version}, developer install (not managed)`);
  else if (!app.commit) {
    if (sidecar.version) set("sidecar", "ok", `${sidecar.version} (not checked in a local build)`);
    else set("sidecar", "error", "Not installed. In the sidecar folder, run: python -m pip install -e .");
  } else if (sidecar.commit === app.commit) {
    set("sidecar", "ok", `${sidecar.version}, matches this build`);
  } else {
    set("sidecar", "busy", sidecar.version ? `Updating to build ${short(app.commit)}…` : `Installing build ${short(app.commit)}…`);
    log.hidden = false;
    const unlisten = await listen((line) => {
      log.textContent += `${line}\n`;
      log.scrollTop = log.scrollHeight;
    });
    try {
      await invoke("update_sidecar");
      set("sidecar", "ok", `Updated to build ${short(app.commit)}`);
      log.hidden = true;
    } catch (err) {
      set("sidecar", "error", "Update failed. The pip output is below.");
      log.textContent += `${err}\n`;
    } finally {
      unlisten();
    }
  }

  if (Object.values(rows).some((r) => r.dataset.status === "error")) {
    if (await choose("Retry", "Continue anyway") === 0) return checkComponents();
  }
  closeDialog();
}

hydrateIcons();
renderChrome();
// Nothing may start the sidecar until the check is done: pip cannot replace files it has open.
checkComponents().finally(() => {
  render();
  attempt(async () => {
    const env = await rpc("environment");
    state.python = env.python;
    state.user = env.user;
    render();
  }, "Could not start the Python sidecar");
});
