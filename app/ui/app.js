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

// A short message at the bottom of the window. Errors, and messages marked log, also go to the
// activity tray, which keeps them; quiet ones (the tray already has them) don't.
function toast(text, isError = false, opts = {}) {
  const t = document.getElementById("toast");
  t.textContent = opts.details ? `${text}\nDetails are in Activity (the bell).` : text;
  if ((isError || opts.log) && !opts.quiet) {
    const title = opts.title || (isError ? "Something didn't work" : text);
    const body = title === text ? "" : opts.title && text.startsWith(`${opts.title}: `) ? text.slice(opts.title.length + 2) : text;
    logActivity({ status: isError ? "error" : "ok", title, text: body, details: opts.details || "" });
  }
  // On Monitor the bottom of the window is Fabric's native view, which would cover the toast.
  t.className = `toast${isError ? " error" : ""}${state.view === "monitor" ? " top" : ""}`;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, isError ? 8000 : 4000);
}

async function attempt(fn, label) {
  try {
    return await fn();
  } catch (err) {
    const f = friendlyError(err);
    toast(label ? `${label}: ${f.text}` : f.text, true, { title: label, details: f.details });
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
  state.tenant = result.tenant;
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
  renderEnvBar();
  renderBell();
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
  if (sqlPage.ref) {
    open.push(h("button", {
      class: `rail-item${state.view === "query" ? " active" : ""}`,
      onclick: () => go("query"),
    }, icon("code", 22), "SQL query"));
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
  } else if (state.view === "query" && sqlPage.ref) {
    const lh = sqlPage.ref;
    crumbs.push(h("span", {}, lh.workspace_name || "Workspace"), h("span", {}, "/"),
      h("button", { class: "crumb", onclick: () => openLakehouse(lh) }, lh.name), h("span", {}, "/"),
      h("span", { class: "current" }, "SQL query"));
  } else if (state.view === "lakehouse" && state.lakehouseView) {
    const lh = state.lakehouseView;
    crumbs.push(h("button", { class: "crumb", onclick: () => openWorkspace({ id: lh.workspace_id, displayName: lh.workspace_name }) },
      lh.workspace_name), h("span", {}, "/"), h("span", { class: "current" }, lh.name));
  }
  document.getElementById("breadcrumb").replaceChildren(...crumbs);
}

function render() {
  renderChrome();
  if (state.view !== fabricView.owner) placeFabricView();
  const views = { home: renderHome, create: renderCreate, workspaces: renderWorkspaces, workspace: renderWorkspace,
    onelake: renderOneLake, fabric: renderFabric, monitor: renderMonitor, lakehouse: renderLakehouse, query: renderQuery, notebook: renderNotebook, ...window.META_VIEWS };
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

// Home answers "what happened since I was last here": problems first, then what is running, what
// runs next and each environment's state, then ways to start something.
async function renderHome() {
  const name = state.user ? state.user.split("@")[0] : "";
  const summary = h("p", { class: "sub" }, "Checking what ran...");
  const attention = h("div", { class: "home-attention" });
  const running = h("div", { class: "muted-text" }, "Loading...");
  const upcoming = h("div", { class: "muted-text" }, "Loading...");
  const envCards = h("div", { class: "home-envs" });
  const list = h("div", { class: "muted-text" }, "Loading notebooks...");
  setView(h("div", { class: "page home" },
    h("h1", {}, name ? `Welcome back, ${name}` : "Welcome to Fabric Desktop"),
    summary,
    signInCallout(),
    attention,
    h("div", { class: "home-attention", id: "home-fresh" }),
    h("div", { class: "home-two" },
      h("section", { class: "card pad" }, h("h2", {}, "Running now"), running),
      h("section", { class: "card pad" }, h("h2", {}, "Coming up"), upcoming)),
    h("section", {}, h("h2", {}, "Environments"), envCards),
    h("section", { class: "row wrap home-start" }, h("h2", {}, "Start something"),
      btn("New notebook", "notebook", newNotebook, "tool outline"),
      btn("New lakehouse", "lakehouse", () => newLakehouse(), "tool outline"),
      btn("OneLake catalog", "onelake", () => go("onelake"), "tool outline")),
    h("section", {}, h("h2", {}, "Recent notebooks"), list)));

  const [meta, cloud, project, notebooks] = await Promise.all([
    rpc("meta_runs_all", { limit: 300 }).catch(() => ({ runs: [], unavailable: {} })),
    rpc("fabric_runs", { limit: 50, refresh: false }).catch(() => []),
    rpc("meta_project").catch(() => null),
    attempt(() => rpc("list_notebooks")),
  ]);
  if (state.view !== "home") return;
  if (project) state.meta.project = project;
  const hasProject = !!project?.path;

  // The latest run of each table in each environment says how that table stands now.
  const latest = new Map();
  for (const r of meta.runs || []) {
    const key = `${r.env}:${r.entity}`;
    if (!latest.has(key)) latest.set(key, r);
  }
  const failing = [...latest.values()].filter((r) => r.status === "failed" || r.status === "needs_decision");
  const cloudFailed = (cloud || []).filter((r) => r.status === "Failed" && Date.now() - new Date(r.started_at) < 86400000);
  const live = activity.items.filter((i) => i.status === "running" && !i.stale);

  const problems = failing.length + cloudFailed.length;
  // Late tables (dataops.js) count too; they may arrive after the runs, so the line is redrawn then.
  state.homeSummary = () => {
    const late = (fresh.items || []).filter((x) => x.status === "late").length;
    const total = problems + late;
    summary.textContent = [
      total ? `${total} ${total === 1 ? "thing needs" : "things need"} your attention` : "Nothing needs your attention",
      live.length ? `${live.length} running` : "",
    ].filter(Boolean).join(" · ") + ".";
  };
  state.homeSummary();
  renderFreshHome();

  attention.replaceChildren(problems ? [h("h2", {}, "Needs your attention"),
    ...failing.slice(0, 5).map((r) => h("div", { class: "card attention" },
      statusPill(r.status),
      h("div", { class: "grow" },
        h("div", {}, h("b", { class: "mono" }, r.entity), ` in ${ENV_LABEL[r.env]} · ${r.compute === "fabric" ? "in Fabric" : "on this PC"}`),
        h("div", { class: "reason" }, runReason(r) || "Did not finish"),
        h("div", { class: "muted-text small" }, when(r.started_at))),
      btn("Open entity", null, () => { setGlobalEnv(r.env); openEntity(r.entity); }, "tool outline"),
      btn("Open run", null, () => showRun(r.run_id, r.env), "tool primary"))),
    ...cloudFailed.slice(0, 3).map((r) => h("div", { class: "card attention" },
      statusPill("failed"),
      h("div", { class: "grow" }, h("div", {}, h("b", {}, r.name), ` · ${r.kind} in Fabric`),
        h("div", { class: "reason" }, r.message || "Failed in Fabric"), h("div", { class: "muted-text small" }, when(r.started_at))),
      r.url ? btn("Open in Fabric", null, () => openInFabric(r.url), "tool primary") : null))] : []);

  running.replaceWith(live.length ? h("div", { class: "home-list" }, live.map((i) => h("div", { class: "home-item" },
    h("div", { class: "row" }, h("b", { class: "grow" }, i.title), h("span", { class: "muted-text small" }, `started ${relTime(i.at)}`)),
    h("div", { class: "progress" }, h("div", { class: "bar indeterminate" })),
    i.text ? h("div", { class: "muted-text small" }, i.text) : null)))
    : h("p", { class: "muted-text" }, "Nothing is running."));

  envCards.replaceChildren(...Object.entries(ENV_LABEL).map(([env, label]) => {
    const where = envWhere(project, env);
    const runs = [...latest.values()].filter((r) => r.env === env);
    const failed = runs.filter((r) => r.status === "failed" || r.status === "needs_decision").length;
    const last = (meta.runs || []).find((r) => r.env === env);
    const busy = live.some((i) => i.env === env);
    const [cls, text] = busy ? ["info", "Running"] : meta.unavailable?.[env] ? ["neu", "Not checked"]
      : !runs.length ? ["neu", "Never run"] : failed ? ["fail", `${failed} failed`] : ["pass", "All green"];
    return h("button", { class: "card env-tile", onclick: () => { setGlobalEnv(env); goMeta("runs"); } },
      h("div", { class: "row" }, h("b", { class: "grow" }, label), h("span", { class: `pill-s ${cls}` }, text)),
      h("div", { class: "muted-text small" }, hasProject ? where.short : "No metadata project yet"),
      h("div", { class: "muted-text small" }, meta.unavailable?.[env] || (last ? ["Last run ", when(last.started_at)] : "")));
  }));

  // Schedules live in Fabric, so they are read only when signed in, after the rest is on screen.
  const fabricEnvs = hasProject && state.user ? Object.keys(ENV_LABEL).filter((env) => envWhere(project, env).fabric) : [];
  const lists = await Promise.all(fabricEnvs.map((env) => rpc("meta_schedules", { env }).catch(() => null)));
  if (state.view !== "home") return;
  const items = lists.flatMap((res, i) => (res?.schedules || []).filter((sc) => sc.enabled)
    .map((sc) => ({ what: `Run all · ${ENV_LABEL[fabricEnvs[i]]}`, text: sc.text })));
  upcoming.replaceWith(items.length ? h("div", { class: "home-list" }, items.map((x) => h("div", { class: "home-item row" },
    h("span", { class: "grow" }, x.what), h("span", { class: "muted-text small" }, x.text))))
    : h("p", { class: "muted-text" }, !state.user ? "Sign in to see schedules in Fabric."
      : fabricEnvs.length ? "No schedules yet. Add one with Schedule on the Metadata page."
        : "Schedules run in Fabric. Set an environment to run in Fabric (Metadata > Environments) to add one."));

  const shown = (notebooks || []).filter((n) => matches(n.name)).slice(0, 8);
  list.replaceWith(shown.length ? h("table", { class: "list" },
    h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Modified"))),
    h("tbody", {}, shown.map((n) => h("tr", { class: "click", onclick: () => openNotebook(n.name) },
      h("td", {}, h("span", { class: "item" }, icon("notebook", 18), n.name)),
      h("td", {}, when(new Date(n.modified * 1000).toISOString())))))) : h("p", { class: "muted-text" },
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

// Filters on Monitor, kept for the session; showFabric is remembered on this PC.
const monitorPage = { env: "all", where: "any", problemsFirst: true, showFabric: true };
try { monitorPage.showFabric = localStorage.getItem("fd.monitor.fabric") !== "hidden"; } catch { /* default: shown */ }

async function renderMonitor() {
  const local = h("div", { class: "muted-text monitor-empty" }, "Loading runs...");
  const slot = h("div", { class: "fabric-slot" },
    h("p", { class: "muted-text" }, "Opening the Fabric Monitoring hub. Sign in once and the app remembers it."));
  const action = (name) => attempt(() => invoke("fabric_view_action", { action: name, page: "monitor" }), "Fabric view");
  const chip = (key, value, label) => h("button", { class: `chip${monitorPage[key] === value ? " on" : ""}`,
    onclick: () => { monitorPage[key] = value; render(); } }, label);
  const toggleFabric = () => {
    monitorPage.showFabric = !monitorPage.showFabric;
    try { localStorage.setItem("fd.monitor.fabric", monitorPage.showFabric ? "shown" : "hidden"); } catch { /* session only */ }
    render();
  };
  const note = h("div", { class: "muted-text small" });
  setView(h("div", { class: `page monitor${monitorPage.showFabric ? "" : " no-fabric"}` },
    h("section", { class: "card monitor-local" },
      h("div", { class: "monitor-band" },
        h("h2", {}, "From Fabric Desktop"),
        h("span", { class: "muted-text" }, "Everything this app ran or started, in every environment, on this PC and in Fabric"),
        h("span", { class: "spacer" }),
        btn("Refresh", "sync", () => render(), "tool outline")),
      h("div", { class: "monitor-filters" },
        h("div", { class: "row", role: "group", "aria-label": "Environment" },
          chip("env", "all", "All"), Object.entries(ENV_LABEL).map(([id, label]) => chip("env", id, label))),
        h("div", { class: "row", role: "group", "aria-label": "Where it ran" },
          chip("where", "any", "Anywhere"), chip("where", "local", "This PC"), chip("where", "fabric", "Fabric")),
        h("label", { class: "check" }, h("input", { type: "checkbox", checked: monitorPage.problemsFirst ? true : null,
          onchange: (e) => { monitorPage.problemsFirst = e.target.checked; render(); } }), "Problems first"),
        note),
      local),
    h("section", { class: "card monitor-fabric" },
      h("div", { class: "monitor-band" },
        h("h2", {}, "In Fabric"),
        h("span", { class: "muted-text" }, "Fabric's own Monitoring hub, for everything else in your workspaces"),
        h("span", { class: "spacer" }),
        monitorPage.showFabric ? [
          btn("Back", null, () => action("back"), "tool outline"),
          btn("Home", "home", () => action("home"), "tool outline"),
          btn("Reload", "restart", () => action("reload"), "tool outline"),
          btn("Open in browser", "publish", () => action("browser"), "tool outline")] : null,
        btn(monitorPage.showFabric ? "Hide" : "Show", null, toggleFabric, "tool outline")),
      monitorPage.showFabric ? slot : null)));
  if (monitorPage.showFabric) dockFabricView(slot, "monitor", "monitor");
  else invoke("fabric_view_hide").catch(() => {});

  const [writes, meta, cloud] = await Promise.all([
    attempt(() => rpc("history")),
    rpc("meta_runs_all", { limit: 200 }).catch(() => ({ runs: [], unavailable: {} })),
    rpc("fabric_runs", { limit: 50 }).catch(() => []),  // not signed in yet
  ]);
  if (state.view !== "monitor") return;
  const unavailable = Object.entries(meta.unavailable || {});
  note.textContent = unavailable.map(([env, why]) => `${ENV_LABEL[env]}: ${why}`).join(" · ");
  const rows = [
    ...(meta.runs || []).map((r) => ({
      at: r.started_at, status: r.status, activity: r.entity, kind: `Metadata run${r.backfill ? ` · backfill ${r.backfill}` : ""}`,
      env: r.env, where: r.compute || "local", seconds: r.seconds, rows: r.rows_written,
      note: r.quarantined ? `${r.quarantined} quarantined` : "", reason: runReason(r),
      open: () => showRun(r.run_id, r.env),
    })),
    ...(cloud || []).map((r) => ({
      at: r.started_at, status: fabricStatus(r.status), activity: r.name, kind: r.kind, env: r.env || "", where: "fabric",
      seconds: r.finished_at ? (new Date(r.finished_at) - new Date(r.started_at)) / 1000 : "", rows: "",
      note: r.workspace_name || "", reason: r.status === "Failed" ? r.message || "" : "",
      open: r.url ? () => openInFabric(r.url) : null,
    })),
    ...(writes || []).map((r) => ({
      at: r.finished_at, status: "ok", activity: r.job, kind: "Notebook write", env: "", where: "local",
      seconds: r.seconds, rows: r.rows, note: `${r.table_uri.split("/Tables/")[1] || r.table_uri} (${r.mode}, v${r.delta_version})`, reason: "",
    })),
  ].filter((r) => (monitorPage.env === "all" || r.env === monitorPage.env)
      && (monitorPage.where === "any" || r.where === monitorPage.where)
      && matches(`${r.activity} ${r.kind} ${r.note} ${r.reason}`))
    .sort((a, b) => String(b.at).localeCompare(String(a.at)));
  const bad = (r) => (r.status === "failed" || r.status === "needs_decision" ? 0 : r.status === "running" ? 1 : 2);
  if (monitorPage.problemsFirst) rows.sort((a, b) => bad(a) - bad(b));  // stable: newest first within each group
  local.replaceWith(rows.length ? h("div", { class: "monitor-scroll" }, h("table", { class: "list" },
    h("thead", {}, h("tr", {}, ["Status", "Activity", "Env", "Ran on", "When", "Took", "Rows", ""].map((c) => h("th", {}, c)))),
    h("tbody", {}, rows.map((r) => h("tr", r.open ? { class: "click", onclick: r.open } : {},
      h("td", {}, statusPill(r.status)),
      h("td", {}, h("div", {}, h("b", {}, r.activity)), h("div", { class: "muted-text small" }, [r.kind, r.note].filter(Boolean).join(" · ")),
        r.reason ? h("div", { class: "reason" }, r.reason) : null),
      h("td", {}, ENV_LABEL[r.env] || r.env), h("td", {}, r.where === "fabric" ? "Fabric" : "This PC"),
      h("td", {}, when(r.at)), h("td", { class: "n" }, took(r.seconds)), h("td", { class: "n" }, r.rows ?? ""),
      h("td", { class: "n" }, r.open ? h("span", { class: "link-text" }, "Details") : null)))))) :
    h("p", { class: "muted-text monitor-empty" }, monitorPage.env !== "all" || monitorPage.where !== "any"
      ? "Nothing matches these filters." : "Nothing has run yet. Run a metadata entity or write a table from a notebook."));
}

// Fabric job states, shown with the same pills as local runs.
function fabricStatus(status) {
  return { Completed: "ok", Failed: "failed", Cancelled: "skipped", Deduped: "skipped" }[status] || "running";
}

// Fabric's own pages (Monitoring hub, OneLake catalog) are one second webview the shell lays
// over the slot's box. It is a native view, so it is hidden whenever the slot isn't showing or a
// dialog is open over it. owner is the app view showing it; page is "monitor", "catalog" or a
// Fabric item link.
const fabricView = { slot: null, observer: null, frame: 0, owner: null, page: "monitor" };

function dockFabricView(slot, owner, page) {
  fabricView.observer?.disconnect();
  fabricView.slot = slot;
  fabricView.owner = owner;
  fabricView.page = page;
  fabricView.observer = new ResizeObserver(placeFabricView);
  fabricView.observer.observe(slot);
}

function placeFabricView() {
  cancelAnimationFrame(fabricView.frame);
  fabricView.frame = requestAnimationFrame(() => {
    const slot = fabricView.slot;
    const covered = document.getElementById("dialog-root").childElementCount > 0 || activity.open;
    if (state.view !== fabricView.owner || !slot?.isConnected || covered) {
      invoke("fabric_view_hide").catch(() => {});
      return;
    }
    const r = slot.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return;
    invoke("fabric_view_show", { x: r.left, y: r.top, width: r.width, height: r.height, tenant: state.tenant || null, page: fabricView.page }).catch((err) => {
      slot.replaceChildren(h("p", { class: "muted-text" }, `The Fabric view could not open: ${err}`));
    });
  });
}

window.addEventListener("resize", placeFabricView);
new MutationObserver(placeFabricView).observe(document.getElementById("dialog-root"), { childList: true });

// ---------- Workspaces, OneLake, lakehouse ----------

// Favorites and pins are kept on this PC; pins sit at the top in the order they were pinned.
const workspacesPage = { marks: null, filter: "all", open: new Set(), items: {} };

async function renderWorkspaces() {
  const wp = workspacesPage;
  const body = h("div", {}, h("p", { class: "muted-text" }, state.user ? "Loading workspaces..." : ""));
  const chips = h("div", { class: "ws-filters" });
  setView(h("div", { class: "page" }, h("h1", {}, "Workspaces"),
    h("p", { class: "sub" }, "Your Fabric workspaces. Expand one to see its items, star your favorites and pin the ones you use most."),
    signInCallout(), chips, body));
  if (!state.user) return;
  const [workspaces, marks] = await Promise.all([loadWorkspaces(),
    wp.marks ? wp.marks : rpc("workspace_marks").catch(() => ({ favorites: [], pins: [] }))]);
  if (state.view !== "workspaces") return;
  wp.marks = marks;
  const here = () => state.view === "workspaces";

  async function mark(ws, which, e) {
    e.stopPropagation();
    const on = !wp.marks[which].includes(ws.id);
    const result = await attempt(() => rpc("set_workspace_mark", { workspace_id: ws.id, mark: which, on }), "Could not save the workspace");
    if (result) { wp.marks = result; draw(); }
  }

  async function toggle(ws, e) {
    e.stopPropagation();
    if (wp.open.has(ws.id)) { wp.open.delete(ws.id); draw(); return; }
    wp.open.add(ws.id);
    draw();
    if (!wp.items[ws.id]) {
      const items = await attempt(() => rpc("list_items", { workspace_id: ws.id }), `Could not list the items in ${ws.displayName}`);
      wp.items[ws.id] = items || [];
      if (here()) draw();
    }
  }

  function itemRows(ws) {
    const items = wp.items[ws.id];
    if (!items) return h("p", { class: "muted-text ws-items-note" }, "Loading items...");
    if (!items.length) return h("p", { class: "muted-text ws-items-note" }, "This workspace has no items yet.");
    const sorted = [...items].sort((a, b) => typeLabel(a.type).localeCompare(typeLabel(b.type)) || a.displayName.localeCompare(b.displayName));
    return h("table", { class: "list ws-items" }, h("tbody", {}, sorted.map((it) => {
      const r = { workspace_id: ws.id, item_id: it.id, type: it.type, name: it.displayName };
      const lh = it.type === "Lakehouse" ? lakehouseRef(ws, it) : null;
      return h("tr", {},
        h("td", {}, h("span", { class: "item" }, itemIcon(it.type),
          h("button", { class: "name-link", title: "Open in Fabric", onclick: () => openInFabric(fabricItemLink(r)) }, it.displayName))),
        h("td", { class: "ws-type" }, typeLabel(it.type)),
        h("td", { class: "actions" },
          lh ? btn("Open", null, () => openLakehouse(lh), "tool small link") : null,
          lh ? btn("SQL query", null, () => openSqlQuery(lh), "tool small link") : null,
          btn("Open in Fabric", "open", () => openInFabric(fabricItemLink(r)), "tool small link")));
    })));
  }

  function draw() {
    if (!here()) return;
    const { favorites, pins } = wp.marks;
    const chip = (key, label, n) => h("button", { class: `chip${wp.filter === key ? " on" : ""}`,
      onclick: () => { wp.filter = key; draw(); } }, `${label} ${n}`);
    const byId = new Map(workspaces.map((w) => [w.id, w]));
    const count = (ids) => ids.filter((id) => byId.has(id)).length;
    chips.replaceChildren(chip("all", "All", workspaces.length), chip("favorites", "Favorites", count(favorites)),
      chip("pins", "Pinned", count(pins)));
    const pinned = pins.map((id) => byId.get(id)).filter(Boolean);
    const ordered = [...pinned, ...workspaces.filter((w) => !pins.includes(w.id))];
    const shown = ordered.filter((w) => matches(w.displayName) && (wp.filter === "all" || wp.marks[wp.filter].includes(w.id)));
    const rows = [];
    shown.forEach((w, i) => {
      const open = wp.open.has(w.id);
      const pin = pins.includes(w.id);
      const fav = favorites.includes(w.id);
      const lastPin = pin && !pins.includes(shown[i + 1]?.id);
      rows.push(h("tr", { class: `click${lastPin && i < shown.length - 1 ? " last-pin" : ""}`, onclick: () => openWorkspace(w) },
        h("td", {}, h("button", { class: "star", title: open ? "Hide items" : "Show items", "aria-label": open ? "Hide items" : "Show items",
          "aria-expanded": String(open), onclick: (e) => toggle(w, e) }, open ? "−" : "+")),
        h("td", {}, h("button", { class: `star pin${pin ? " on" : ""}`, title: pin ? "Unpin" : "Pin to the top",
          "aria-label": pin ? "Unpin" : "Pin to the top", onclick: (e) => mark(w, "pins", e) }, icon(pin ? "pinFilled" : "pin", 16))),
        h("td", {}, h("button", { class: `star${fav ? " on" : ""}`, title: fav ? "Remove from favorites" : "Add to favorites",
          "aria-label": fav ? "Remove from favorites" : "Add to favorites", onclick: (e) => mark(w, "favorites", e) }, fav ? "★" : "☆")),
        h("td", {}, h("span", { class: "item" }, icon("workspace", 18), w.displayName)),
        h("td", {}, w.type || "Workspace"),
        h("td", { class: "actions" }, btn("Open in Fabric", "open", (e) => { e.stopPropagation(); openInFabric(fabricItemLink({ workspace_id: w.id })); }, "tool small link"))));
      if (open) rows.push(h("tr", { class: "sub-row" }, h("td", {}), h("td", { colspan: 5 }, itemRows(w))));
    });
    const table = shown.length ? h("table", { class: "list ws-list" },
      h("thead", {}, h("tr", {}, h("th", { style: "width:32px" }), h("th", { style: "width:32px" }), h("th", { style: "width:32px" }),
        h("th", {}, "Name"), h("th", {}, "Type"), h("th", {}))),
      h("tbody", {}, rows)) : h("p", { class: "muted-text", style: "padding:16px 0" },
      wp.filter === "favorites" ? "No favorite workspaces yet. Click the star next to one." :
      wp.filter === "pins" ? "No pinned workspaces yet. Click the pin next to one." : "Nothing matches.");
    body.replaceChildren(table);
  }
  draw();
}

// The Fabric page: Fabric's own portal on the whole page. Links across the app open their
// item here; the page keeps whatever was shown last.
const FABRIC_HOME = "https://app.fabric.microsoft.com/home?experience=fabric-developer";
const fabricPage = { page: FABRIC_HOME };

function openInFabric(page) {
  fabricPage.page = page;
  if (state.view === "fabric") {
    invoke("fabric_view_action", { action: "home", page }).catch(() => {});
  } else {
    go("fabric");
  }
}

function renderFabric() {
  const slot = h("div", { class: "fabric-slot" },
    h("p", { class: "muted-text" }, "Opening Fabric. Sign in once and the app keeps it between launches."));
  const action = (name) => attempt(() => invoke("fabric_view_action", { action: name, page: fabricPage.page }), "Fabric view");
  setView(h("div", { class: "page monitor fabric-page" },
    h("section", { class: "card monitor-fabric" },
      h("div", { class: "monitor-band" },
        h("h2", {}, "Fabric"),
        h("span", { class: "spacer" }),
        btn("Back", null, () => action("back"), "tool outline"),
        btn("Home", "home", () => openInFabric(FABRIC_HOME), "tool outline"),
        btn("OneLake catalog", "onelake", () => openInFabric("catalog"), "tool outline"),
        btn("Monitoring hub", "monitor", () => openInFabric("monitor"), "tool outline"),
        btn("Reload", "restart", () => action("reload"), "tool outline"),
        btn("Open in browser", "publish", () => action("browser"), "tool outline")),
      slot)));
  dockFabricView(slot, "fabric", fabricPage.page);
  placeFabricView();
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
      h("td", {}, h("span", { class: "item" }, itemIcon("Lakehouse"), lh.displayName)),
      h("td", {}, "Lakehouse"),
      h("td", {}, btn("Open in notebook", "notebook", (e) => { e.stopPropagation(); newNotebook(lakehouseRef(ws, lh)); }, "tool small link")))))) :
    h("p", { class: "muted-text" }, "No lakehouses in this workspace yet."));
}

// ---------- OneLake catalog page ----------

// Fabric item types as people say them: SemanticModel -> Semantic model, KQLDatabase -> KQL database.
function typeLabel(type) {
  const words = String(type).replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2").replace(/([a-z])([A-Z])/g, "$1 $2").split(" ");
  return words.map((w, i) => (i && w !== w.toUpperCase() ? w.toLowerCase() : w)).join(" ");
}

// An icon for each Fabric item type, tinted by workload the way the Fabric portal groups them.
// Fluent UI icons, not Fabric's own artwork; other types get a plain document.
const ITEM_ICONS = {
  Lakehouse: ["lakehouse", "eng"], Notebook: ["notebook", "eng"], SparkJobDefinition: ["code", "eng"], Environment: ["boxToolbox", "eng"],
  Warehouse: ["building", "wh"], WarehouseSnapshot: ["camera", "wh"], SQLEndpoint: ["databaseSearch", "wh"], SQLDatabase: ["database", "wh"],
  Datamart: ["windowDatabase", "wh"], MirroredDatabase: ["databaseArrowRight", "wh"], MirroredWarehouse: ["databaseArrowRight", "wh"],
  MirroredAzureDatabricksCatalog: ["databaseArrowRight", "wh"],
  DataPipeline: ["flow", "fac"], Dataflow: ["dataFunnel", "fac"], CopyJob: ["copyArrowRight", "fac"], DataBuildToolJob: ["buildingFactory", "fac"],
  Report: ["dataBarVertical", "bi"], PaginatedReport: ["documentTable", "bi"], SemanticModel: ["cube", "bi"], Dashboard: ["gauge", "bi"],
  OrgApp: ["apps", "bi"], App: ["apps", "bi"],
  Eventhouse: ["tableLightning", "rti"], KQLDatabase: ["database", "rti"], KQLQueryset: ["databaseSearch", "rti"], KQLDashboard: ["gauge", "rti"],
  Eventstream: ["flashFlow", "rti"], Reflex: ["flash", "rti"],
  MLModel: ["brainCircuit", "ds"], MLExperiment: ["beaker", "ds"], DataAgent: ["bot", "ds"],
  UserDataFunction: ["braces", "eng"], GraphQLApi: ["plugConnected", "eng"],
};

function itemIcon(type, size = 18) {
  const [name, family] = ITEM_ICONS[type] || ["document", "other"];
  const svg = icon(name, size);
  svg.classList.add("type-icon", `wl-${family}`);
  return svg;
}

// Paths of item pages in the Fabric portal, by item type; other types open their workspace.
const FABRIC_ITEM_PATHS = {
  Lakehouse: "lakehouses", Warehouse: "datawarehouses", Notebook: "synapsenotebooks", SemanticModel: "datasets",
  Report: "reports", PaginatedReport: "rdlreports", DataPipeline: "pipelines", Dashboard: "dashboards", Eventhouse: "eventhouses", KQLDatabase: "databases",
};

function fabricItemLink(r) {
  const path = FABRIC_ITEM_PATHS[r.type];
  const tail = path ? `${path}/${r.item_id}` : "list";
  return `https://app.fabric.microsoft.com/groups/${r.workspace_id}/${tail}?experience=fabric-developer`;
}

// Endorsement and table names come from slow background reads in the sidecar; ask until done.
async function pollSidecar(method, params, done, keepGoing) {
  for (let i = 0; i < 90 && keepGoing(); i++) {
    if (i) await new Promise((r) => setTimeout(r, 2000));
    const result = await rpc(method, params).catch((err) => ({ pending: false, available: false, items: {}, reason: String(err) }));
    if (!result.pending) { done(result); return; }
  }
}

const catalogPage = { data: null, tables: null, layers: {}, scope: "all", type: "all", query: "",
                      wsQuery: "", sort: "name", dir: 1, open: new Set() };

async function renderOneLake() {
  const cp = catalogPage;
  const nav = h("nav", { class: "catalog-nav" });
  const head = h("div", { class: "catalog-head" });
  const list = h("div", { class: "catalog-list" }, h("p", { class: "muted-text", style: "padding:16px" },
    state.user ? "Loading items from all workspaces..." : ""));
  const note = h("span", { class: "muted-text" });
  const search = h("input", { class: "catalog-search", placeholder: "Search by name, workspace or table", value: cp.query, "aria-label": "Search the catalog" });
  search.addEventListener("input", () => { cp.query = search.value; draw(); });

  setView(h("div", { class: "page monitor onelake" }, signInCallout(),
    h("section", { class: "card onelake-catalog" },
      h("div", { class: "monitor-band" }, h("h2", {}, "OneLake catalog"),
        h("span", { class: "muted-text" }, "Every item in every workspace you can open"), h("span", { class: "spacer" }), note, search,
        btn("Refresh", "sync", () => { cp.data = null; render(); }, "tool outline"),
        btn("Open Fabric's catalog", "open", () => openInFabric("catalog"), "tool outline")),
      h("div", { class: "catalog-body" }, nav, h("section", { class: "catalog-main" }, head, list)))));
  if (!state.user) return;

  const here = () => state.view === "onelake" && list.isConnected;
  const key = (r) => r.item_id;
  const favorites = () => new Set(cp.data?.favorites || []);
  const endorsed = () => state.endorsements?.items || {};
  const tablesOf = (r) => cp.tables?.[r.item_id] || [];

  function inScope(r) {
    if (cp.scope === "fav") return favorites().has(key(r));
    if (cp.scope === "endorsed") return !!endorsed()[key(r)];
    if (cp.scope.startsWith("ws:")) return r.workspace_id === cp.scope.slice(3);
    return true;
  }
  const sortValue = { name: (r) => r.name, type: (r) => typeLabel(r.type), ws: (r) => r.workspace_name,
    endorsement: (r) => endorsed()[key(r)] || "~", layer: (r) => (r.type === "Lakehouse" && cp.layers[r.name]) || "~" };

  function shown() {
    const q = cp.query.toLowerCase();
    return cp.data.items.filter((r) => inScope(r) && (cp.type === "all" || r.type === cp.type)
      && (!q || `${r.name} ${r.workspace_name} ${typeLabel(r.type)} ${r.description} ${tablesOf(r).join(" ")}`.toLowerCase().includes(q)))
      .sort((a, b) => cp.dir * String(sortValue[cp.sort](a)).localeCompare(String(sortValue[cp.sort](b)), undefined, { sensitivity: "base" }));
  }

  async function toggleFavorite(r, e) {
    e.stopPropagation();
    const result = await attempt(() => rpc("set_favorite", { lakehouse_id: key(r), favorite: !favorites().has(key(r)) }), "Could not save the favorite");
    if (result) { cp.data.favorites = result; draw(); }
  }

  function scopeButton(scope, label, glyph, count, disabled, title) {
    return h("button", { class: `catalog-scope${cp.scope === scope ? " on" : ""}`, disabled, title,
      onclick: () => { cp.scope = scope; draw(); } },
    h("span", { class: "glyph" }, glyph), h("span", { class: "label" }, label), h("span", { class: "count" }, count));
  }

  function draw() {
    if (!cp.data || !here()) return;
    const items = cp.data.items;
    const end = state.endorsements;
    const wsFilter = h("input", { class: "catalog-search small", placeholder: "Find a workspace", value: cp.wsQuery, "aria-label": "Find a workspace" });
    wsFilter.addEventListener("input", () => { cp.wsQuery = wsFilter.value; drawWorkspaces(); wsFilter.focus(); });
    const wsList = h("div", { class: "catalog-ws" });
    const drawWorkspaces = () => wsList.replaceChildren(...cp.data.workspaces
      .filter((w) => w.displayName.toLowerCase().includes(cp.wsQuery.toLowerCase()))
      .map((w) => scopeButton(`ws:${w.id}`, w.displayName, "▢", items.filter((r) => r.workspace_id === w.id).length)));
    drawWorkspaces();
    const keepFocus = document.activeElement === nav.querySelector(".catalog-search");
    nav.replaceChildren(
      scopeButton("all", "All items", "▦", items.length),
      scopeButton("fav", "Favorites", "★", items.filter((r) => favorites().has(key(r))).length),
      scopeButton("endorsed", "Endorsed", "✓", end?.available ? items.filter((r) => endorsed()[key(r)]).length : "",
        !end?.available, !end ? "Checking endorsement..." : end.reason || ""),
      h("div", { class: "sec" }, "Workspaces"), wsFilter, wsList);
    if (keepFocus) wsFilter.focus();

    const counts = {};
    items.forEach((r) => { counts[r.type] = (counts[r.type] || 0) + 1; });
    const types = Object.keys(counts).sort((a, b) => counts[b] - counts[a]);
    const rows = shown();
    const chip = (type, label) => h("button", { class: `chip${cp.type === type ? " on" : ""}`, onclick: () => { cp.type = type; draw(); } }, label);
    head.replaceChildren(h("span", { class: "muted-text", style: "font-weight:600" }, "Type"), chip("all", "All"),
      ...types.map((t) => chip(t, `${typeLabel(t)} ${counts[t]}`)), h("span", { class: "spacer" }),
      h("span", { class: "muted-text" }, `${rows.length} item${rows.length === 1 ? "" : "s"}`));

    const sortHead = (col, label) => h("th", {}, h("button", { class: `sort${cp.sort === col ? " on" : ""}`,
      onclick: () => { cp.dir = cp.sort === col ? -cp.dir : 1; cp.sort = col; draw(); } },
    label + (cp.sort === col ? (cp.dir > 0 ? " ▲" : " ▼") : "")));
    const body = [];
    for (const r of rows) {
      const fav = favorites().has(key(r));
      const isLh = r.type === "Lakehouse";
      const open = isLh && cp.open.has(key(r));
      const e = endorsed()[key(r)];
      body.push(h("tr", {},
        h("td", {}, isLh ? h("button", { class: "star", title: open ? "Hide tables" : "Show tables", "aria-label": open ? "Hide tables" : "Show tables",
          onclick: () => { open ? cp.open.delete(key(r)) : cp.open.add(key(r)); draw(); } }, open ? "▾" : "▸") : ""),
        h("td", {}, h("button", { class: `star${fav ? " on" : ""}`, title: fav ? "Remove from favorites" : "Add to favorites",
          "aria-label": fav ? "Remove from favorites" : "Add to favorites", onclick: (ev) => toggleFavorite(r, ev) }, fav ? "★" : "☆")),
        h("td", {}, h("span", { class: "item" }, itemIcon(r.type),
          h("button", { class: "name-link", title: "Open in Fabric", onclick: () => openInFabric(fabricItemLink(r)) }, r.name))),
        h("td", {}, typeLabel(r.type)),
        h("td", {}, h("button", { class: "name-link", title: "Open the workspace in Fabric",
          onclick: () => openInFabric(fabricItemLink({ workspace_id: r.workspace_id })) }, r.workspace_name)),
        h("td", {}, e ? h("span", { class: `pill-s ${e === "Certified" ? "pass" : "info"}` }, e) : ""),
        h("td", {}, (isLh && cp.layers[r.name]) || ""),
        h("td", { class: "actions" },
          btn("Show in Fabric", null, () => openInFabric(fabricItemLink(r)), "tool small link"),
          isLh ? btn("SQL query", null, () => openSqlQuery(r), "tool small link") : null,
          isLh ? btn("Open in notebook", null, () => newNotebook(r), "tool small link") : null)));
      if (open) {
        const tree = h("div", { class: "explorer-content", style: "padding:0" });
        body.push(h("tr", { class: "sub-row" }, h("td", {}), h("td", {}), h("td", { colspan: 6 }, tree)));
        renderTree(tree, r, null);
      }
    }
    list.replaceChildren(rows.length ? h("table", { class: "list" },
      h("thead", {}, h("tr", {}, h("th", { style: "width:28px" }), h("th", { style: "width:32px" }),
        sortHead("name", "Name"), sortHead("type", "Type"), sortHead("ws", "Location"),
        sortHead("endorsement", "Endorsement"), sortHead("layer", "Layer"), h("th", {}))),
      h("tbody", {}, body)) : h("p", { class: "muted-text", style: "padding:16px" },
      cp.scope === "fav" && !cp.query ? "No favorites yet. Click the star next to an item." : "Nothing matches."));

    const parts = [];
    if (cp.data.skipped.length) parts.push(`Skipped ${cp.data.skipped.length} workspace${cp.data.skipped.length === 1 ? "" : "s"}: ${cp.data.skipped.join(", ")}.`);
    if (!cp.tables) parts.push("Reading table names for search...");
    note.textContent = parts.join(" ");
  }

  if (cp.data) { draw(); return; }
  rpc("meta_project").then((p) => { (p.layers || []).forEach((l) => { cp.layers[l.lakehouse] = l.name; }); draw(); }).catch(() => {});
  const data = await attempt(() => rpc("catalog", { all_types: true }), "Could not list the catalog");
  if (!data || !here()) return;
  cp.data = data;
  cp.tables = null;
  state.workspaces = data.workspaces;
  draw();
  const lakehouses = data.items.filter((r) => r.type === "Lakehouse")
    .map((r) => ({ workspace_id: r.workspace_id, lakehouse_id: r.item_id }));
  pollSidecar("catalog_tables", { lakehouses }, (result) => { cp.tables = result.items || {}; draw(); }, () => cp.data === data);
  if (!state.endorsements) {
    pollSidecar("endorsements", { workspace_ids: data.workspaces.map((w) => w.id) },
      (result) => { state.endorsements = result; draw(); }, () => cp.data === data);
  }
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
      btn("Open in new notebook", "notebook", () => newNotebook(ref), "tool primary"),
      btn("New SQL query", "code", () => openSqlQuery(ref), "tool outline")),
    h("div", { style: "max-width:520px;background:#fff;border:1px solid var(--border);border-radius:8px;padding:8px" }, tree)));
  renderTree(tree, ref, null);
}

// ---------- SQL query page ----------

// Spark SQL over one lakehouse's tables, run on this PC by the sidecar (DuckDB).
const QUERY_HINT = "-- Spark SQL runs on this PC. Press Ctrl+Enter to run.\n";
const sqlPage = { ref: null, tabs: [], active: 0, tables: null, cols: {}, open: {} };

function openSqlQuery(ref) {
  if (sqlPage.ref?.lakehouse_id !== ref.lakehouse_id) {
    Object.assign(sqlPage, { ref, tabs: loadQueryTabs(ref), active: 0, tables: null, cols: {}, open: {} });
  }
  go("query");
}

function queryTabsKey(ref) {
  return `fabric-desktop.sql.${ref.lakehouse_id}`;
}

function loadQueryTabs(ref) {
  try {
    const saved = JSON.parse(localStorage.getItem(queryTabsKey(ref)));
    if (Array.isArray(saved) && saved.length) return saved.map((t) => ({ name: String(t.name), sql: String(t.sql) }));
  } catch { /* no saved tabs */ }
  return [{ name: "Query 1", sql: QUERY_HINT }];
}

function saveQueryTabs() {
  try {
    localStorage.setItem(queryTabsKey(sqlPage.ref), JSON.stringify(sqlPage.tabs.map((t) => ({ name: t.name, sql: t.sql }))));
  } catch { /* storage unavailable: tabs last for this session */ }
}

// "dbo.sales" reads as "sales", the way Fabric's query tab names it.
function sqlTableName(schema, table) {
  const quote = (s) => (/^[A-Za-z_][A-Za-z0-9_]*$/.test(s) ? s : `\`${s.replace(/`/g, "``")}\``);
  return schema === "dbo" ? quote(table) : `${quote(schema)}.${quote(table)}`;
}

function cellText(v) {
  if (v === null || v === undefined) return "NULL";
  return typeof v === "number" ? v.toLocaleString(undefined, { maximumFractionDigits: 6 }) : String(v);
}

function renderQuery() {
  const p = sqlPage;
  const ref = p.ref;
  if (!ref) return go("onelake");
  if (!p.tabs[p.active]) p.active = 0;
  const tab = p.tabs[p.active];
  const here = () => state.view === "query" && p.ref === ref && p.tabs[p.active] === tab;

  const editor = h("textarea", { class: "sql-editor", spellcheck: "false", "aria-label": "SQL query" });
  editor.value = tab.sql;
  const gutter = h("div", { class: "sql-gutter", "aria-hidden": "true" });
  const syncGutter = () => {
    const n = Math.max(editor.value.split("\n").length, 10);
    gutter.textContent = Array.from({ length: n }, (_, i) => i + 1).join("\n");
    gutter.scrollTop = editor.scrollTop;
  };
  const setSql = (text) => { tab.sql = text; saveQueryTabs(); };
  editor.addEventListener("input", () => { setSql(editor.value); syncGutter(); });
  editor.addEventListener("scroll", () => { gutter.scrollTop = editor.scrollTop; });
  editor.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); run(); }
    else if (e.key === "Tab" && !e.shiftKey) {
      e.preventDefault();
      editor.setRangeText("  ", editor.selectionStart, editor.selectionEnd, "end");
      setSql(editor.value);
    }
  });
  const chosenSql = () => {
    const { selectionStart: a, selectionEnd: b } = editor;
    return b > a ? editor.value.slice(a, b) : editor.value;
  };
  const insertAtCursor = (text) => {
    editor.focus();
    editor.setRangeText(text, editor.selectionStart, editor.selectionEnd, "end");
    setSql(editor.value);
    syncGutter();
  };

  async function run(query = chosenSql()) {
    if (!query.trim() || tab.running) return;
    tab.running = true;
    tab.error = null;
    drawResults();
    try {
      tab.result = await rpc("run_sql", { query, lakehouse_ref: ref });
      tab.ranSql = query;
    } catch (err) {
      tab.result = null;
      tab.error = String(err);
    }
    tab.running = false;
    if (here()) drawResults();
  }

  async function saveAsTable() {
    const query = chosenSql();
    if (!query.trim()) return;
    const name = await askText("Save as table", `Table name in ${ref.name}, written with the full query result`, "query_result", "Save");
    if (!name) return;
    toast(`Writing ${name}...`);
    const out = await attempt(() => rpc("save_sql", { query, lakehouse_ref: ref, table: name }), "Could not save the table");
    if (out) {
      toast(out.message);
      delete state.tree[treeKey(ref, "tables")];
      p.tables = null;
      if (here()) drawTables();
    }
  }

  async function openInNotebook() {
    const query = chosenSql().trim();
    await newNotebook(ref);
    if (!state.notebook) return;
    const cell = state.notebook.cells[0];
    cell.source += `\ndf = lakehouse.sql("""\n${query.replace(/"""/g, '\\"\\"\\"')}\n""")\ndf\n`;
    markDirty();
    renderNotebook();
  }

  async function downloadCsv() {
    if (!tab.ranSql) return;
    const out = await attempt(() => rpc("export_sql_csv", { query: tab.ranSql, lakehouse_ref: ref }), "Could not save the CSV");
    if (out) toast(`Saved ${out.rows.toLocaleString()} rows to ${out.path}`);
  }

  async function copyRows() {
    const r = tab.result;
    if (!r) return;
    const lines = [r.columns.map((c) => c.name), ...r.rows.map((row) => row.map((v) => (v === null ? "" : String(v))))];
    await attempt(() => navigator.clipboard.writeText(lines.map((l) => l.join("\t")).join("\n")), "Could not copy");
    toast(`Copied ${r.rows.length.toLocaleString()} rows`);
  }

  // Explorer: click a table to query it, a column to type its name.
  const tablesBox = h("div", { class: "explorer-content" });
  function queryTable(name) {
    const text = `SELECT *\nFROM ${name}\nLIMIT 100;`;
    if (!tab.sql.trim() || tab.sql === QUERY_HINT) {
      setSql(text);
      editor.value = text;
      syncGutter();
      run(text);
    } else {
      p.tabs.push({ name: nextTabName(), sql: text, autorun: true });
      p.active = p.tabs.length - 1;
      saveQueryTabs();
      renderQuery();
    }
  }
  async function toggleColumns(name) {
    p.open[name] = !p.open[name];
    if (p.open[name] && !p.cols[name]) {
      drawTables();
      p.cols[name] = (await attempt(() => rpc("table_columns", { lakehouse_ref: ref, table: name }), "Could not read the columns")) || null;
      if (!p.cols[name]) p.open[name] = false;
    }
    if (state.view === "query" && p.ref === ref) drawTables();
  }
  function drawTables() {
    if (!p.tables) {
      tablesBox.replaceChildren(h("div", { class: "tree-row muted" }, "Loading tables..."));
      return;
    }
    const rows = [];
    for (const [schema, names] of Object.entries(p.tables)) {
      const qualify = Object.keys(p.tables).length > 1 || schema !== "dbo";
      if (qualify) rows.push(h("div", { class: "tree-row muted", style: "padding-left:6px" }, schema));
      if (!names.length) rows.push(h("div", { class: "tree-row muted", style: "padding-left:28px" }, "No tables"));
      for (const t of names) {
        const name = sqlTableName(schema, t);
        const open = !!p.open[name];
        rows.push(h("div", { class: "sql-table-row" },
          h("button", { class: "tree-row chev", "aria-label": open ? `Hide columns of ${t}` : `Show columns of ${t}`,
            style: `padding-left:${qualify ? 22 : 6}px`, onclick: () => toggleColumns(name) }, chevron(open)),
          h("button", { class: "tree-row", title: `Query ${name}`, onclick: () => queryTable(name) },
            icon("table", 15), h("span", { class: "label" }, t))));
        if (open) {
          if (!p.cols[name]) rows.push(h("div", { class: "tree-row muted", style: "padding-left:52px" }, "Loading..."));
          for (const c of p.cols[name] || []) {
            rows.push(h("button", { class: "tree-row sql-col", title: `Insert ${c.name}`,
              onclick: () => insertAtCursor(/^[A-Za-z_][A-Za-z0-9_]*$/.test(c.name) ? c.name : `\`${c.name}\``) },
            h("span", { class: "label" }, c.name), h("span", { class: "hint" }, c.type)));
          }
        }
      }
    }
    tablesBox.replaceChildren(...rows);
  }

  const band = h("div", { class: "sql-band" });
  const grid = h("div", { class: "sql-grid" });
  function drawResults() {
    const r = tab.result;
    const pill = tab.running ? ["busy", "Running"] : tab.error ? ["bad", "Failed"] : r ? ["ok", "Succeeded"] : ["", "Not run"];
    let msg = "Press Run to see results";
    if (tab.running) msg = "Running on this PC...";
    else if (r) {
      msg = `${r.row_count.toLocaleString()} row${r.row_count === 1 ? "" : "s"} · ${r.seconds} s on this PC`;
      if (r.rows.length < r.row_count) msg += ` · showing the first ${r.rows.length.toLocaleString()}`;
    }
    band.replaceChildren(h("span", { style: "font-weight:600" }, "Results"),
      h("span", { class: `sql-pill ${pill[0]}` }, pill[1]), h("span", { class: "muted-text" }, tab.error ? "" : msg),
      h("span", { class: "spacer" }),
      btn("Copy", null, copyRows, "tool small", { disabled: !r }),
      btn("Download CSV", null, downloadCsv, "tool small", { disabled: !r }));
    if (tab.error) {
      grid.replaceChildren(h("div", { class: "output error" }, h("pre", {}, tab.error.replace(/^Error: /, ""))));
    } else if (r && r.columns.length) {
      grid.replaceChildren(h("table", {},
        h("thead", {}, h("tr", {}, h("th", { class: "i" }), r.columns.map((c) => h("th", { title: c.type }, c.name)))),
        h("tbody", {}, r.rows.map((row, i) => h("tr", {}, h("td", { class: "i" }, String(i + 1)),
          row.map((v) => h("td", { class: `${typeof v === "number" ? "n" : ""}${v === null ? " null" : ""}` }, cellText(v))))))));
    } else {
      grid.replaceChildren(h("p", { class: "muted-text", style: "padding:12px 16px" },
        r ? "The query ran and returned no columns." : "Tables are named as in Fabric, so SELECT * FROM sales works here too."));
    }
  }

  const nextTabName = () => {
    let n = p.tabs.length + 1;
    while (p.tabs.some((t) => t.name === `Query ${n}`)) n++;
    return `Query ${n}`;
  };
  const tabs = h("div", { class: "sql-tabs", role: "tablist", "aria-label": "Queries" },
    p.tabs.map((t, i) => h("div", { class: `sql-tab${i === p.active ? " on" : ""}` },
      h("button", { class: "name", role: "tab", "aria-selected": String(i === p.active),
        onclick: () => { p.active = i; renderQuery(); },
        ondblclick: async () => {
          const name = await askText("Rename query", "Name", t.name, "Rename");
          if (name) { t.name = name; saveQueryTabs(); renderQuery(); }
        } }, icon("code", 14), t.name),
      p.tabs.length > 1 ? h("button", { class: "close", "aria-label": `Close ${t.name}`,
        onclick: () => { p.tabs.splice(i, 1); p.active = Math.min(p.active, p.tabs.length - 1); saveQueryTabs(); renderQuery(); } }, "×") : null)),
    h("button", { class: "sql-tab add", "aria-label": "New query",
      onclick: () => { p.tabs.push({ name: nextTabName(), sql: QUERY_HINT }); p.active = p.tabs.length - 1; saveQueryTabs(); renderQuery(); } }, "+"));

  const runBtn = btn("Run", "play", () => run(), "tool primary");
  setView(h("div", { class: "sql-page" },
    h("div", { class: "ribbon" }, h("div", { class: "toolbar" },
      btn("New SQL query", "add", () => { p.tabs.push({ name: nextTabName(), sql: QUERY_HINT }); p.active = p.tabs.length - 1; saveQueryTabs(); renderQuery(); }),
      btn("New notebook", "notebook", () => newNotebook(ref)),
      btn("Refresh", "sync", () => { p.tables = null; p.cols = {}; renderQuery(); }),
      h("span", { class: "spacer" }),
      h("span", { class: "muted-text" }, "Queries run on this PC with DuckDB. No Fabric capacity is used."))),
    h("div", { class: "nb-body" },
      h("aside", { class: "explorer" }, h("div", { class: "explorer-head" }, "Explorer"),
        h("div", { class: "tree-row", style: "margin:4px 8px 0;font-weight:600;cursor:default" }, icon("lakehouse", 16),
          h("span", { class: "label" }, ref.name)),
        tablesBox),
      h("section", { class: "sql-main" }, tabs,
        h("div", { class: "sql-card" },
          h("div", { class: "sql-actions" }, runBtn,
            h("span", { class: "muted-text" }, "Ctrl+Enter, or select text to run just that"), h("span", { class: "spacer" }),
            btn("Save as table", "save", saveAsTable, "tool outline"),
            btn("Open in notebook", "notebook", openInNotebook, "tool outline")),
          h("div", { class: "sql-edit" }, gutter, editor),
          band, grid)))));
  syncGutter();
  drawResults();
  drawTables();
  if (tab.autorun) { delete tab.autorun; run(tab.sql); }
  if (!p.tables) {
    rpc("list_tables", { workspace_id: ref.workspace_id, lakehouse_id: ref.lakehouse_id })
      .then((t) => { p.tables = t; })
      .catch((err) => { p.tables = {}; toast(`Could not list tables: ${err}`, true); })
      .then(() => { if (state.view === "query" && p.ref === ref) drawTables(); });
  }
  editor.focus();
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

// ---------- Run in Fabric and schedules ----------

// The same notebook, run on the workspace's Fabric capacity: published first, then started
// with Fabric's job scheduler. It shows in Monitor (both lists) while it runs.
const FABRIC_DONE = new Set(["Completed", "Failed", "Cancelled", "Deduped"]);

async function runInFabric() {
  const nb = state.notebook;
  if (!nb.lakehouse) {
    toast("Attach a lakehouse first. The notebook runs in that lakehouse's workspace.", true);
    return;
  }
  if (!(await ensureSignedIn())) return;
  toast(`Publishing ${nb.name} and starting it in ${nb.lakehouse.workspace_name}...`);
  const run = await attempt(() => rpc("run_notebook_in_fabric", notebookPayload()), "Could not start the run in Fabric");
  if (!run) return;
  nb.dirty = false;
  nb.fabricRun = run;
  renderChrome();
  if (state.view === "notebook") renderNotebook();
  toast(`${nb.name} is running in Fabric. The bell follows it until it finishes.`);
  logActivity({ id: `fabric:${run.job_id}`, status: "running", title: `${nb.name} · Notebook run`, text: `In Fabric on ${nb.lakehouse.workspace_name}`, url: run.url, monitor: true });
  trackFabricJobs();
  watchFabricRun(nb, run);
}

async function watchFabricRun(nb, run) {
  for (let i = 0; i < 360; i++) {  // up to an hour while the app is open
    await new Promise((r) => setTimeout(r, 10000));
    const runs = await rpc("fabric_runs", { limit: 50 }).catch(() => null);
    const now = runs?.find((r) => r.job_id === run.job_id);
    if (!now) continue;
    nb.fabricRun = now;
    if (state.view === "notebook" && state.notebook === nb) renderNotebook();
    if (FABRIC_DONE.has(now.status)) return;  // the activity tray reports how it ended
  }
}

// Windows time zone ids, which Fabric schedules use, for the zones people pick most.
const TIME_ZONES = {
  "UTC": "UTC", "America/New_York": "Eastern Standard Time", "America/Chicago": "Central Standard Time",
  "America/Denver": "Mountain Standard Time", "America/Phoenix": "US Mountain Standard Time",
  "America/Los_Angeles": "Pacific Standard Time", "Europe/London": "GMT Standard Time",
  "Europe/Paris": "Romance Standard Time", "Europe/Berlin": "W. Europe Standard Time",
  "Asia/Kolkata": "India Standard Time", "Asia/Singapore": "Singapore Standard Time",
  "Australia/Sydney": "AUS Eastern Standard Time",
};
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

// The repeat, times, days and time zone fields of a Fabric schedule, shared by notebooks and metadata runs.
function scheduleForm() {
  const kind = h("select", {}, h("option", { value: "daily" }, "Every day"), h("option", { value: "weekly" }, "Some days of the week"),
    h("option", { value: "cron" }, "Every few minutes"));
  const times = h("input", { value: "06:00", placeholder: "06:00, 18:00" });
  const minutes = h("input", { type: "number", min: "1", value: "60" });
  const days = WEEKDAYS.map((d, i) => h("label", { class: "check" }, h("input", { type: "checkbox", value: d, checked: i < 5 ? true : null }), d.slice(0, 3)));
  const here = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const zone = h("select", {}, Object.entries(TIME_ZONES).map(([iana, win]) =>
    h("option", { value: win, selected: iana === here ? true : null }, `${win}${iana === "UTC" ? "" : ` (${iana})`}`)));
  const timesField = h("label", { class: "field" }, "Times of day (24-hour, comma between)", times);
  const minutesField = h("label", { class: "field", hidden: true }, "Minutes between runs", minutes);
  const daysField = h("div", { class: "field", hidden: true }, "Days", h("div", { class: "row wrap" }, days));
  const zoneField = h("label", { class: "field" }, "Time zone", zone);
  const sync = () => {
    minutesField.hidden = kind.value !== "cron";
    timesField.hidden = zoneField.hidden = kind.value === "cron";
    daysField.hidden = kind.value !== "weekly";
  };
  kind.addEventListener("change", sync);
  return {
    fields: [h("label", { class: "field" }, "Repeat", kind), timesField, daysField, minutesField, zoneField],
    sync,
    value: () => ({ kind: kind.value, times: times.value.split(","), every_minutes: Number(minutes.value),
      weekdays: days.map((l) => l.firstChild).filter((c) => c.checked).map((c) => c.value), time_zone: zone.value }),
  };
}

async function scheduleNotebook() {
  const nb = state.notebook;
  if (!nb.lakehouse) {
    toast("Attach a lakehouse first. The schedule runs the notebook in that lakehouse's workspace.", true);
    return;
  }
  if (!(await ensureSignedIn())) return;
  const list = h("div", { class: "scroll" }, h("p", { class: "muted-text" }, "Loading schedules..."));
  const form = scheduleForm();

  const show = (res) => {
    list.replaceChildren(res.schedules.length ? h("table", { class: "mini" }, h("tbody", {}, res.schedules.map((s) => h("tr", {},
      h("td", {}, s.enabled ? h("span", { class: "pill-s pass" }, "On") : h("span", { class: "pill-s neu" }, "Off")),
      h("td", {}, s.text),
      h("td", { class: "n" }, btn("Remove", "delete", async () => {
        const next = await attempt(() => rpc("delete_notebook_schedule", { name: nb.name, lakehouse_ref: nb.lakehouse, schedule_id: s.id }), "Not removed");
        if (next) show(next);
      }, "tool link small"))))))
      : h("p", { class: "muted-text" }, res.published ? "No schedules yet." : "Not published yet. Adding a schedule publishes it."));
  };
  const add = async () => {
    const schedule = form.value();
    toast(`Publishing ${nb.name} and adding the schedule...`);
    const res = await attempt(() => rpc("add_notebook_schedule", { ...notebookPayload(), schedule }), "Schedule not added");
    if (res) { nb.dirty = false; renderChrome(); show(res); toast("Scheduled. Fabric runs the published notebook on its own capacity."); }
  };
  showDialog(`Schedule ${nb.name} in Fabric`, h("div", { class: "scroll" },
    h("p", { class: "text" }, `Fabric runs the published notebook in ${nb.lakehouse.workspace_name} on its own capacity, even when this PC is off. Adding a schedule publishes what is on screen first.`),
    list, h("b", {}, "Add a schedule"), form.fields),
  [btn("Close", null, closeDialog, "tool outline"), btn("Add schedule", "add", add, "tool primary")]);
  form.sync();
  const res = await attempt(() => rpc("notebook_schedules", { name: nb.name, lakehouse_ref: nb.lakehouse }), "Could not read schedules");
  if (res) show(res);
}

// ---------- Add data items ----------

// Lakehouses across every workspace, like Fabric's OneLake catalog. Favorites are stars kept on
// this PC (Fabric has no public favorites API); endorsement comes from Fabric's admin scan when
// the account may read it.
async function openDataItems() {
  if (!(await ensureSignedIn())) return;
  const pick = { scope: "all", query: "", selected: null, catalog: null, layers: {} };
  const nav = h("nav", { class: "catalog-nav" });
  const list = h("div", { class: "catalog-list" }, h("p", { class: "muted-text", style: "padding:16px" }, "Loading lakehouses from all workspaces..."));
  const head = h("div", { class: "catalog-head" });
  const detail = h("aside", { class: "catalog-detail" });
  const note = h("span", { class: "muted-text" });
  const search = h("input", { class: "catalog-search", placeholder: "Filter by name or workspace", "aria-label": "Filter lakehouses" });
  const attachBtn = btn("Attach", null, () => pick.selected && attachLakehouse(pick.selected), "tool primary", { disabled: true });
  const newBtn = btn("New lakehouse", "add", async () => {
    const ws = pick.scope.startsWith("ws:") ? workspaceById(pick.scope.slice(3))
      : pick.selected ? workspaceById(pick.selected.workspace_id) : null;
    closeDialog();
    const view = state.view;
    const ref = await newLakehouse(ws);
    if (ref) { state.view = view; attachLakehouse(ref); }
  }, "tool link");
  const workspaceById = (id) => pick.catalog?.workspaces.find((w) => w.id === id) || null;
  const key = (r) => r.item_id;
  const favorites = () => new Set(pick.catalog?.favorites || []);
  const endorsed = () => state.endorsements?.items || {};

  const inScope = (r) => pick.scope === "all" ? true
    : pick.scope === "fav" ? favorites().has(key(r))
    : pick.scope === "endorsed" ? !!endorsed()[key(r)]
    : r.workspace_id === pick.scope.slice(3);
  const shown = () => (pick.catalog?.items || []).filter((r) => inScope(r)
    && `${r.name} ${r.workspace_name}`.toLowerCase().includes(pick.query.toLowerCase()));

  const navButton = (scope, label, glyph, count, disabled, title) => h("button", {
    class: `catalog-scope${pick.scope === scope ? " on" : ""}`, disabled, title,
    onclick: () => { pick.scope = scope; draw(); },
  }, h("span", { class: "glyph" }, glyph), h("span", { class: "label" }, label), h("span", { class: "count" }, count));

  const endorsementPill = (r) => {
    const e = endorsed()[key(r)];
    return e ? h("span", { class: `pill-s ${e === "Certified" ? "pass" : "info"}` }, e) : "";
  };

  async function toggleFavorite(r, e) {
    e.stopPropagation();
    const on = !favorites().has(key(r));
    const result = await attempt(() => rpc("set_favorite", { lakehouse_id: key(r), favorite: on }), "Could not save the favorite");
    if (result) { pick.catalog.favorites = result; draw(); }
  }

  function select(r, row) {
    pick.selected = r;
    // Mark the row in place: redrawing the list would swallow the second click of a double-click.
    list.querySelectorAll("tr.selected").forEach((tr) => tr.classList.remove("selected"));
    row?.classList.add("selected");
    attachBtn.disabled = false;
    const tree = h("div", { class: "explorer-content", style: "padding:0" });
    detail.replaceChildren(h("div", { class: "n" }, r.name), h("div", { class: "muted-text" }, `${r.workspace_name} workspace`),
      endorsementPill(r), h("div", { class: "sec" }, "Contents"), tree);
    renderTree(tree, r, null);
  }

  function draw() {
    const c = pick.catalog;
    if (!c) return;
    const items = c.items;
    const end = state.endorsements;
    const endorsedTitle = !end || end.pending ? "Checking endorsement..." : end.available ? "" : end.reason;
    nav.replaceChildren(
      navButton("all", "All items", "▦", items.length),
      navButton("fav", "Favorites", "★", items.filter((r) => favorites().has(key(r))).length),
      navButton("endorsed", "Endorsed", "✓", end?.available ? items.filter((r) => endorsed()[key(r)]).length : "", !end?.available, endorsedTitle),
      h("div", { class: "sec" }, "Workspaces"),
      ...c.workspaces.map((w) => navButton(`ws:${w.id}`, w.displayName, "▢", items.filter((r) => r.workspace_id === w.id).length)));
    const rows = shown();
    const title = { all: "All items", fav: "Favorites", endorsed: "Endorsed" }[pick.scope] || workspaceById(pick.scope.slice(3))?.displayName;
    head.replaceChildren(h("span", { class: "t" }, title), h("span", { class: "muted-text" }, `${rows.length} lakehouse${rows.length === 1 ? "" : "s"}`));
    const attachedId = state.notebook?.lakehouse?.lakehouse_id;
    list.replaceChildren(rows.length ? h("table", { class: "list" },
      h("thead", {}, h("tr", {}, h("th", { style: "width:32px" }, ""), h("th", {}, "Name"), h("th", {}, "Location"),
        h("th", {}, "Endorsement"), h("th", {}, "Layer"), h("th", {}, ""))),
      h("tbody", {}, rows.map((r) => {
        const fav = favorites().has(key(r));
        return h("tr", { class: `click${pick.selected && key(pick.selected) === key(r) ? " selected" : ""}`, onclick: (e) => select(r, e.currentTarget), ondblclick: () => attachLakehouse(r) },
          h("td", {}, h("button", { class: `star${fav ? " on" : ""}`, title: fav ? "Remove from favorites" : "Add to favorites",
            "aria-label": fav ? "Remove from favorites" : "Add to favorites", onclick: (e) => toggleFavorite(r, e) }, fav ? "★" : "☆")),
          h("td", {}, h("span", { class: "item" }, itemIcon("Lakehouse"), r.name)),
          h("td", {}, r.workspace_name),
          h("td", {}, endorsementPill(r)),
          h("td", {}, pick.layers[r.name] || ""),
          h("td", {}, r.lakehouse_id === attachedId ? h("span", { class: "pill-s pass" }, "Attached") : ""));
      }))) : h("p", { class: "muted-text", style: "padding:16px" },
      pick.scope === "fav" && !pick.query ? "No favorites yet. Click the star next to a lakehouse." : "No lakehouses match."));
    const skipped = c.skipped.length ? `Skipped ${c.skipped.length} workspace${c.skipped.length === 1 ? "" : "s"} that could not be read: ${c.skipped.join(", ")}.` : "";
    note.textContent = [skipped, end && !end.pending && !end.available ? end.reason : ""].filter(Boolean).join(" ");
  }

  search.addEventListener("input", () => { pick.query = search.value; draw(); });
  const overlay = showDialog("OneLake catalog", h("div", { class: "catalog" },
    h("div", { class: "catalog-top" }, h("span", { class: "text" }, "Choose a lakehouse. The notebook reads its files and writes tables to it through OneLake."), search),
    h("div", { class: "catalog-body" }, nav, h("section", { class: "catalog-main" }, head, list), detail),
    h("div", { class: "dialog-actions catalog-foot" }, newBtn, h("span", { style: "flex:1" }), note,
      btn("Cancel", null, closeDialog, "tool outline"), attachBtn)));
  overlay.querySelector(".dialog").classList.add("wide");
  search.focus();
  detail.replaceChildren(h("p", { class: "muted-text" }, "Select a lakehouse to see its tables and files."));

  rpc("meta_project").then((p) => (p.layers || []).forEach((l) => { pick.layers[l.lakehouse] = l.name; draw(); })).catch(() => {});
  const catalog = await attempt(() => rpc("catalog"), "Could not list lakehouses");
  if (!catalog || !overlay.isConnected) return;
  pick.catalog = catalog;
  state.workspaces = catalog.workspaces;
  const attached = state.notebook?.lakehouse;
  draw();
  if (attached) {
    const r = catalog.items.find((i) => i.lakehouse_id === attached.lakehouse_id);
    if (r) select(r, null);
    draw();
  }
  if (!state.endorsements) {
    await pollSidecar("endorsements", { workspace_ids: catalog.workspaces.map((w) => w.id) },
      (result) => { state.endorsements = result; }, () => overlay.isConnected);
  }
  if (overlay.isConnected) draw();
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
      btn("Schedule", "history", scheduleNotebook, "tool"),
      btn("Run in Fabric", "cloud", runInFabric, "tool"),
      btn("Publish to OneLake", "publish", publishNotebook, "tool primary")],
    edit: [btn("Code", "add", () => addCell("code")), btn("Markdown", "add", () => addCell("markdown")),
      h("div", { class: "sep" }), btn("Move up", "up", () => moveCell(-1)), btn("Move down", "down", () => moveCell(1)),
      btn("Delete cell", "delete", () => deleteCell(selectedCell())), h("div", { class: "sep" }),
      btn("Clear all outputs", "clear", clearOutputs)],
    run: [runAllBtn, btn("Run cell", "play", () => runCell(selectedCell()), "tool", { disabled: busy, "data-run": true }),
      btn("Run all above", "up", runAbove, "tool", { disabled: busy, "data-run": true }), stopBtn, h("div", { class: "sep" }),
      btn("Restart kernel", "restart", restartKernel), btn("Restart and run all", "restart", async () => { await restartKernel(); runAll(); }, "tool", { disabled: busy, "data-run": true }),
      h("div", { class: "sep" }), btn("Run in Fabric", "cloud", runInFabric), btn("Schedule", "history", scheduleNotebook)],
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
      h("span", {}, "Compute: this PC, no Fabric capacity used"),
      nb.fabricRun ? h("span", {}, `Last Fabric run: ${nb.fabricRun.status === "NotStarted" ? "starting" : nb.fabricRun.status}`) : null));
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
document.getElementById("bell").addEventListener("click", () => toggleTray());
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && activity.open && !document.getElementById("dialog-root").childElementCount) toggleTray(false); });
document.getElementById("account").addEventListener("click", (e) => {
  if (!state.user) { signIn().then((ok) => ok && render()); return; }
  showMenu(e.currentTarget, [{ info: `Signed in as ${state.user}` }, { info: `Fabric Desktop build ${state.build || "unknown"}` }]);
});
document.getElementById("search").addEventListener("input", (e) => {
  state.query = e.target.value;
  if (state.view === "notebook" || state.view === "create" || state.view === "lakehouse" || state.view === "query") state.view = "home";
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

// What to do when Windows' application control refuses the engine's DLLs (from the
// startup check's "blocked" report: files by package, who blocked them, where they are).
function blockedHelp(blocked) {
  const names = Object.entries(blocked.packages)
    .map(([pkg, files]) => `${pkg} (${files.length} file${files.length === 1 ? "" : "s"})`).join(", ");
  const why = "Nothing is wrong with the install, so reinstalling won't fix it.";
  if (blocked.by === "smart_app_control") {
    return [
      h("p", {}, "Smart App Control stops program files that aren't signed or that Microsoft doesn't recognize yet. " +
        `It stopped files the local engine needs: ${names}. ${why}`),
      h("p", {}, "Smart App Control can't allow just one app. To run the engine on this PC, turn it off in " +
        "Windows Security > App & browser control > Smart App Control settings, then click Retry. " +
        "Some versions of Windows can't turn it back on without a reset, so decide that first."),
    ];
  }
  return [
    h("p", {}, "An App Control policy on this PC, usually set by your organization, stops program files it doesn't trust. " +
      `It stopped files the local engine needs: ${names}. ${why}`),
    h("p", {}, `Ask your IT admin to allow the Python packages in ${blocked.location}. ` +
      "They can see each block in Event Viewer under Microsoft > Windows > CodeIntegrity > Operational. Then click Retry."),
  ];
}

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
  const help = h("div", { class: "startup-help", hidden: true });
  const actions = h("div", { class: "dialog-actions" });
  const choose = (...labels) => new Promise((resolve) => actions.replaceChildren(...labels.map((label, i) =>
    btn(label, null, () => { actions.replaceChildren(); resolve(i); }, i === 0 ? "tool primary" : "tool outline"))));
  const listen = (fn) => window.__TAURI__.event.listen("component-log", (e) => fn(e.payload));

  document.getElementById("dialog-root").replaceChildren(h("div", { class: "overlay" },
    h("div", { class: "dialog", role: "dialog", "aria-label": "Checking components" },
      h("div", { class: "dialog-head" }, "Checking components"),
      h("div", { class: "startup-rows" }, row("app", "Fabric Desktop"), row("python", "Python"), row("sidecar", "Local engine")),
      help, log, actions)));

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

  state.build = app.commit ? short(app.commit) : `${app.version} (local build)`;
  if (!app.commit) set("app", "ok", `${app.version}, local build (update check off)`);
  else if (app.updated_to && app.updated_to !== app.commit) {
    set("app", "error", `The update didn't take: this is still build ${short(app.commit)}, not ${short(app.updated_to)}. ` +
      "Download fabric-desktop.exe again from the latest release.");
  } else if (!latest.commit) set("app", "warn", `Build ${short(app.commit)}. Could not check for a newer build (${latest.error}).`);
  else if (latest.commit === app.commit) set("app", "ok", `Up to date (build ${short(app.commit)})`);
  else {
    // A newer build is installed straight away, so nobody keeps running an old one.
    set("app", "busy", `Updating to build ${short(latest.commit)}…`);
    const unlisten = await listen((line) => {
      const m = /^progress (\d+)/.exec(line);
      if (m) set("app", "busy", `Downloading build ${short(latest.commit)}… ${m[1]}%`);
    });
    try {
      await invoke("update_app", { commit: latest.commit });
      set("app", "ok", "Restarting…");
      return new Promise(() => {});  // the new exe takes over
    } catch (err) {
      set("app", "error", `Update to build ${short(latest.commit)} failed: ${err} You are still on ${short(app.commit)}.`);
    } finally {
      unlisten();
    }
  }

  // Windows refusing to load the engine's DLLs is not something pip can repair, so
  // explain it in place of the pip output.
  const showBlocked = (blocked) => {
    set("sidecar", "error", blocked.by === "smart_app_control"
      ? "Windows Smart App Control blocked it." : "An App Control policy on this PC blocked it.");
    log.hidden = true;
    help.replaceChildren(...blockedHelp(blocked));
    help.hidden = false;
  };

  if (!report.python_ok) set("sidecar", "skip", "Needs Python 3.11 or later");
  else if (sidecar.editable || !app.commit) {
    const kind = sidecar.editable ? "developer install (not managed)" : "not checked in a local build";
    if (sidecar.version && sidecar.error) set("sidecar", "error", `${sidecar.version} does not start: ${sidecar.error}`);
    else if (sidecar.version) set("sidecar", "ok", `${sidecar.version}, ${kind}`);
    else set("sidecar", "error", "Not installed. In the sidecar folder, run: python -m pip install -e .");
  } else if (sidecar.commit === app.commit && !sidecar.error) {
    set("sidecar", "ok", `${sidecar.version}, matches this build`);
  } else if (sidecar.commit === app.commit && sidecar.blocked) {
    showBlocked(sidecar.blocked);
  } else {
    set("sidecar", "busy", sidecar.commit === app.commit ? `Repairing (it would not start: ${sidecar.error})…`
      : sidecar.version ? `Updating to build ${short(app.commit)}…` : `Installing build ${short(app.commit)}…`);
    log.hidden = false;
    let blocked = null;
    const unlisten = await listen((line) => {
      if (line.startsWith("blocked {")) { blocked = JSON.parse(line.slice(8)); return; }
      log.textContent += `${line}\n`;
      log.scrollTop = log.scrollHeight;
    });
    try {
      await invoke("update_sidecar");
      set("sidecar", "ok", `Updated to build ${short(app.commit)}`);
      log.hidden = true;
    } catch (err) {
      if (blocked) showBlocked(blocked);
      else set("sidecar", "error", "Update failed. The pip output is below.");
      // The error repeats the last lines of output, which the log already shows.
      const shown = new Set(log.textContent.split("\n"));
      const extra = String(err).split("\n").filter((l) => !shown.has(l) && !l.startsWith("blocked {"));
      if (extra.length) log.textContent += `${extra.join("\n")}\n`;
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
    state.tenant = env.tenant;
    // The environment pill needs the metadata project; there may not be one yet.
    state.meta.project = await rpc("meta_project").catch(() => state.meta.project);
    render();
    trackFabricJobs();
    checkFreshness();
  }, "Could not start the Python sidecar");
});
