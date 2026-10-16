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
  // A button near the right edge (Publish options): keep the menu on screen.
  const overflow = menu.getBoundingClientRect().right - (window.innerWidth - 8);
  if (overflow > 0) menu.style.left = `${Math.max(8, rect.left - overflow)}px`;
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
  // Items opened recently, each with a close button.
  const open = [];
  const recent = (view, iconName, label, onOpen, onClose) => h("div", { class: "rail-open-item" },
    h("button", { class: `rail-item${state.view === view ? " active" : ""}`, onclick: onOpen }, icon(iconName, 22), label),
    h("button", { class: "rail-close", title: `Close ${label}`, "aria-label": `Close ${label}`,
      onclick: async (e) => {
        e.stopPropagation();
        await onClose();
        if (state.view === view) go("home"); else renderChrome();
      } }, icon("dismiss", 12)));
  if (state.openWorkspace) {
    open.push(recent("workspace", "workspace", state.openWorkspace.displayName, () => openWorkspace(state.openWorkspace),
      () => { state.openWorkspace = null; }));
  }
  if (sqlPage.ref) {
    open.push(recent("query", "code", "SQL query", () => go("query"), () => { sqlPage.ref = null; }));
  }
  if (state.notebook) {
    open.push(recent("notebook", "notebook", state.notebook.name, () => go("notebook"), async () => {
      if (state.notebook?.dirty) await saveNotebook();  // closing never loses edits
      state.notebook = null;
    }));
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

// A section that folds to its header, remembered on this PC. head is what stays visible.
function foldable(id, head, body, { cls = "card pad", always = null } = {}) {
  let folded = false;
  try { folded = localStorage.getItem(`fd.fold.${id}`) === "1"; } catch { /* unfolded */ }
  const box = h("section", { class: `${cls} foldable${folded ? " folded" : ""}` });
  const toggle = h("button", { class: "fold-toggle", "aria-expanded": String(!folded), title: folded ? "Show" : "Hide",
    onclick: () => {
      folded = !folded;
      try { localStorage.setItem(`fd.fold.${id}`, folded ? "1" : "0"); } catch { /* this session only */ }
      box.classList.toggle("folded", folded);
      toggle.setAttribute("aria-expanded", String(!folded));
      toggle.title = folded ? "Show" : "Hide";
    } }, h("span", { class: "chev", "aria-hidden": "true" }, "▾"), head);
  box.append(h("div", { class: "fold-head" }, toggle), ...(always ? [always] : []), h("div", { class: "fold-body" }, body));
  return box;
}

// Home answers "what happened since I was last here": problems first, then what is running, what
// runs next and each environment's state, then ways to start something. Each part draws as soon as
// its own data is in, quickest first; the Estate X-ray reads only after all of them.
async function renderHome() {
  const name = state.user ? state.user.split("@")[0] : "";
  const summary = h("p", { class: "sub" }, "Checking what ran...");
  const attention = h("div", { class: "home-attention" });
  const running = h("div", { class: "muted-text" }, "Loading...");
  const upcoming = h("div", { class: "muted-text" }, "Loading...");
  const envCards = h("div", { class: "home-envs" });
  const list = h("div", { class: "muted-text" }, "Loading notebooks...");
  const xray = xrayCard();
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
    foldable("home.notebooks", h("h2", {}, "Recent notebooks"), list),
    xray));
  const onHome = () => state.view === "home";

  // What is running is already known in the app: no call needed.
  const live = activity.items.filter((i) => i.status === "running" && !i.stale);
  running.replaceWith(live.length ? h("div", { class: "home-list" }, live.map((i) => h("div", { class: "home-item" },
    h("div", { class: "row" }, h("b", { class: "grow" }, i.title), h("span", { class: "muted-text small" }, `started ${relTime(i.at)}`)),
    h("div", { class: "progress" }, h("div", { class: "bar indeterminate" })),
    i.text ? h("div", { class: "muted-text small" }, i.text) : null)))
    : h("p", { class: "muted-text" }, "Nothing is running."));

  // The sidecar answers one call at a time, so the quick local ones go first.
  const notebooksDone = rpc("list_notebooks").catch(() => null).then((notebooks) => {
    if (!onHome()) return;
    const shown = (notebooks || []).filter((n) => matches(n.name)).slice(0, 8);
    list.replaceWith(shown.length ? h("table", { class: "list" },
      h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Modified"))),
      h("tbody", {}, shown.map((n) => h("tr", { class: "click", onclick: () => openNotebook(n.name) },
        h("td", {}, h("span", { class: "item" }, icon("notebook", 18), n.name)),
        h("td", {}, when(new Date(n.modified * 1000).toISOString())))))) : h("p", { class: "muted-text" },
      notebooks === null ? "Couldn't list notebooks." : state.query ? "No notebooks match your search." : "No notebooks yet. Create one to get started."));
  });
  const projectCall = rpc("meta_project").catch(() => null);
  const cloudCall = rpc("fabric_runs", { limit: 50, refresh: false }).catch(() => []);
  const pcCall = rpc("pc_schedules_for", { kind: "all" }).catch(() => []);
  const metaCall = rpc("meta_runs_all", { limit: 300 }).catch(() => ({ runs: [], unavailable: {} }));

  // Coming up: this PC's schedules straight away, Fabric's added once read (only when signed in).
  const upcomingDone = (async () => {
    const [project, pcList] = await Promise.all([projectCall, pcCall]);
    if (!onHome()) return;
    const draw = (lists, fabricEnvs) => {
      const items = [
        ...(pcList || []).filter((sc) => sc.enabled && sc.next_run).sort((a, b) => a.next_run.localeCompare(b.next_run))
          .map((sc) => ({ what: sc.label, where: "This PC", text: nextRunText(sc.next_run) })),
        ...lists.flatMap((res, i) => (res?.schedules || []).filter((sc) => sc.enabled)
          .map((sc) => ({ what: `Run all · ${ENV_LABEL[fabricEnvs[i]]}`, where: "Fabric", text: sc.text }))),
      ].slice(0, 6);
      const next = h("div", {}, items.length ? h("div", { class: "home-list" }, items.map((x) => h("div", { class: "home-item row" },
        h("span", { class: "grow" }, x.what, h("span", { class: "muted-text small" }, ` · ${x.where}`)), h("span", { class: "muted-text small" }, x.text))))
        : h("p", { class: "muted-text" }, "Nothing scheduled. Use Schedule on a notebook or on Metadata, on this PC or in Fabric."),
        btn("All schedules", "history", () => { monitorPage.panel = "schedules"; go("monitor"); }, "tool link small"));
      upcoming.replaceWith(next);
      return next;
    };
    let shown = draw([], []);
    const fabricEnvs = project?.path && state.user ? Object.keys(ENV_LABEL).filter((env) => envWhere(project, env).fabric) : [];
    if (!fabricEnvs.length) return;
    await metaCall;  // Fabric's schedules come after the run history, which fills more of the page
    const lists = await Promise.all(fabricEnvs.map((env) => rpc("meta_schedules", { env }).catch(() => null)));
    if (!onHome()) return;
    const next = draw(lists, fabricEnvs);
    shown.replaceWith(next);
  })();

  const runsDone = (async () => {
    const [meta, cloud, project] = await Promise.all([metaCall, cloudCall, projectCall]);
    if (!onHome()) return;
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

    attention.replaceChildren(...(problems ? [h("h2", {}, "Needs your attention"),
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
        r.url ? btn("Open in Fabric", null, () => openInFabric(r.url), "tool primary") : null))] : []));

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
  })();

  // The X-ray's saved scan is read last, so it never holds up the rest of Home.
  await Promise.allSettled([notebooksDone, upcomingDone, runsDone]);
  if (onHome() && xray.isConnected) loadXray(xray, false);
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
const monitorPage = { env: "all", where: "any", problemsFirst: true, showFabric: true, panel: "runs" };
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
        h("div", { class: "seg", role: "group", "aria-label": "Show" },
          h("button", { class: monitorPage.panel === "runs" ? "on" : null, onclick: () => { monitorPage.panel = "runs"; render(); } }, "Runs"),
          h("button", { class: monitorPage.panel === "schedules" ? "on" : null, onclick: () => { monitorPage.panel = "schedules"; render(); } }, "Schedules"),
          h("button", { class: monitorPage.panel === "cost" ? "on" : null, onclick: () => { monitorPage.panel = "cost"; render(); } }, "Cost")),
        h("span", { class: "muted-text" }, monitorPage.panel === "cost" ? "Capacity each run used in Fabric, and what running on this PC saved"
          : monitorPage.panel === "schedules" ? "Every schedule, on this PC and in Fabric, with how its last run went"
          : "Everything this app ran or started, in every environment, on this PC and in Fabric"),
        h("span", { class: "spacer" }),
        btn("Refresh", "sync", () => render(), "tool outline")),
      monitorPage.panel !== "runs" ? null : h("div", { class: "monitor-filters" },
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
  if (monitorPage.panel === "cost") { local.replaceWith(costPanel()); return; }
  if (monitorPage.panel === "schedules") { local.replaceWith(schedulesPanel()); return; }

  const [writes, meta, cloud, scheduled] = await Promise.all([
    attempt(() => rpc("history")),
    rpc("meta_runs_all", { limit: 200 }).catch(() => ({ runs: [], unavailable: {} })),
    rpc("fabric_runs", { limit: 50 }).catch(() => []),  // not signed in yet
    rpc("pc_schedule_runs", { limit: 100 }).catch(() => []),
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
    ...(scheduled || []).map((r) => ({
      at: r.started, status: r.status === "ok" ? "ok" : r.status === "skipped" ? "skipped" : "failed", activity: r.label,
      kind: r.trigger === "manual" ? "Schedule, run now" : "Scheduled run", env: "", where: "local", seconds: r.seconds,
      rows: r.rows ?? "", note: r.status === "ok" ? r.message : "", reason: r.status === "ok" ? "" : r.message,
      open: () => { monitorPage.panel = "schedules"; render(); },
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
const fabricView = { slot: null, observer: null, frame: 0, owner: null, page: "monitor", force: false };

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
    const force = fabricView.force;
    fabricView.force = false;
    invoke("fabric_view_show", { x: r.left, y: r.top, width: r.width, height: r.height, tenant: state.tenant || null,
      page: fabricView.page, force }).catch((err) => {
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
  fabricView.force = true;  // a click always goes to its page, even the one shown last
  if (state.view === "fabric") {
    fabricView.force = false;
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

// Paths of item pages in the Fabric portal, by item type; other types open their workspace,
// since a wrong guess shows Fabric's "not found" page instead.
const FABRIC_ITEM_PATHS = {
  Lakehouse: "lakehouses", Warehouse: "datawarehouses", SQLEndpoint: "lakewarehouses", Notebook: "synapsenotebooks",
  SparkJobDefinition: "sparkjobdefinitions", SemanticModel: "datasets", Report: "reports", PaginatedReport: "rdlreports",
  DataPipeline: "pipelines", Dashboard: "dashboards", Datamart: "datamarts", Eventhouse: "eventhouses", KQLDatabase: "databases",
  KQLQueryset: "queryworkbenches", KQLDashboard: "kustodashboards", Eventstream: "eventstreams", MirroredDatabase: "mirroreddatabases",
  SQLDatabase: "sqldatabases", MLModel: "mlmodels", MLExperiment: "mlexperiments",
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
                      wsQuery: "", wsAll: false, sort: "name", dir: 1, open: new Set(), tab: "catalog", typeOpen: new Set() };

// Item types grouped the way Fabric's OneLake catalog groups its Type filter. A type not listed
// falls under Other types.
const TYPE_GROUPS = [
  ["data", "Data", "database", ["Lakehouse", "Warehouse", "WarehouseSnapshot", "SQLEndpoint", "SQLDatabase", "Datamart", "MirroredDatabase",
    "MirroredWarehouse", "MirroredAzureDatabricksCatalog", "Eventhouse", "KQLDatabase", "SemanticModel"]],
  ["insights", "Insights", "dataBarVertical", ["Report", "PaginatedReport", "Dashboard", "KQLDashboard", "KQLQueryset", "Scorecard"]],
  ["processes", "Processes", "flow", ["Notebook", "SparkJobDefinition", "DataPipeline", "Dataflow", "CopyJob", "Eventstream", "Reflex",
    "DataBuildToolJob", "MLExperiment", "UserDataFunction"]],
  ["solutions", "Solutions", "apps", ["OrgApp", "App", "DataAgent", "MLModel", "GraphQLApi"]],
  ["configurations", "Configurations", "boxToolbox", ["Environment", "VariableLibrary", "MountedDataFactory", "ApacheAirflowJob"]],
];
const TYPE_GROUP_OF = Object.fromEntries(TYPE_GROUPS.flatMap(([id, , , types]) => types.map((t) => [t, id])));
const typeGroup = (type) => TYPE_GROUP_OF[type] || "other";
// A click anywhere outside the open Type panel closes it.
document.addEventListener("mousedown", (e) => {
  if (catalogPage.typePanel && !e.target.closest?.(".type-filter")) {
    catalogPage.typePanel = false;
    document.querySelector(".type-panel")?.remove();
  }
});
const TYPE_GROUP_LABEL = { ...Object.fromEntries(TYPE_GROUPS.map(([id, label]) => [id, label])), other: "Other types" };

// The OneLake page's tabs: the catalog here; Shortcuts and Access are in estate.js.
function onelakeTabs() {
  const tab = (id, label) => h("button", { class: `tab${catalogPage.tab === id ? " active" : ""}`,
    onclick: () => { catalogPage.tab = id; render(); } }, label);
  return h("div", { class: "subtabs onelake-tabs" }, tab("catalog", "Catalog"), tab("shortcuts", "Shortcuts"), tab("access", "Access"));
}

async function renderOneLake() {
  if (catalogPage.tab === "shortcuts") return renderShortcuts();
  if (catalogPage.tab === "access") return renderAccess();
  const cp = catalogPage;
  const nav = h("nav", { class: "catalog-nav" });
  const head = h("div", { class: "catalog-head" });
  const list = h("div", { class: "catalog-list" }, h("p", { class: "muted-text", style: "padding:16px" },
    state.user ? "Loading items from all workspaces..." : ""));
  const note = h("span", { class: "muted-text" });
  const search = h("input", { class: "catalog-search", placeholder: "Search by name, workspace or table", value: cp.query, "aria-label": "Search the catalog" });
  search.addEventListener("input", () => { cp.query = search.value; draw(); });

  setView(h("div", { class: "page monitor onelake" }, signInCallout(), onelakeTabs(),
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

  // cp.type is "all", a group ("g:data") or one item type ("Lakehouse").
  const typeMatches = (r) => cp.type === "all" || (cp.type.startsWith("g:") ? typeGroup(r.type) === cp.type.slice(2) : r.type === cp.type);
  const personal = () => new Set((cp.data?.workspaces || []).filter((w) => w.type === "Personal").map((w) => w.id));

  function inScope(r) {
    if (cp.scope === "fav") return favorites().has(key(r));
    if (cp.scope === "mine") return personal().has(r.workspace_id);
    if (cp.scope === "endorsed") return !!endorsed()[key(r)];
    if (cp.scope.startsWith("ws:")) return r.workspace_id === cp.scope.slice(3);
    return true;
  }
  const sortValue = { name: (r) => r.name, type: (r) => typeLabel(r.type), ws: (r) => r.workspace_name,
    endorsement: (r) => endorsed()[key(r)] || "~", layer: (r) => (r.type === "Lakehouse" && cp.layers[r.name]) || "~" };

  function shown() {
    const q = cp.query.toLowerCase();
    return cp.data.items.filter((r) => inScope(r) && typeMatches(r)
      && (!q || `${r.name} ${r.workspace_name} ${typeLabel(r.type)} ${r.description} ${tablesOf(r).join(" ")}`.toLowerCase().includes(q)))
      .sort((a, b) => cp.dir * String(sortValue[cp.sort](a)).localeCompare(String(sortValue[cp.sort](b)), undefined, { sensitivity: "base" }));
  }

  async function toggleFavorite(r, e) {
    e.stopPropagation();
    const result = await attempt(() => rpc("set_favorite", { lakehouse_id: key(r), favorite: !favorites().has(key(r)) }), "Could not save the favorite");
    if (result) { cp.data.favorites = result; draw(); }
  }

  function scopeButton(scope, label, iconName, count, disabled, title) {
    return h("button", { class: `catalog-scope${cp.scope === scope ? " on" : ""}`, disabled, title,
      onclick: () => { cp.scope = scope; draw(); } },
    h("span", { class: "glyph" }, icon(iconName, 16)), h("span", { class: "label" }, label), h("span", { class: "count" }, count));
  }

  // Fabric's Type filter: a button naming the choice, and a panel of groups that open to their types.
  function typeFilter(items) {
    const counts = {};
    items.forEach((r) => { counts[r.type] = (counts[r.type] || 0) + 1; });
    const groups = [...TYPE_GROUPS.map(([id, label, ic]) => [id, label, ic]), ["other", "Other types", "document"]]
      .map(([id, label, ic]) => [id, label, ic, Object.keys(counts).filter((t) => typeGroup(t) === id).sort((a, b) => counts[b] - counts[a])])
      .filter(([, , , types]) => types.length);
    const total = (types) => types.reduce((n, t) => n + counts[t], 0);
    const chosen = cp.type === "all" ? `All (${items.length})`
      : cp.type.startsWith("g:") ? `${TYPE_GROUP_LABEL[cp.type.slice(2)]} (${total(Object.keys(counts).filter((t) => typeGroup(t) === cp.type.slice(2)))})`
      : `${typeLabel(cp.type)} (${counts[cp.type] || 0})`;
    const pick = (type) => { cp.type = type; closeTypePanel(); draw(); };
    const check = (on) => h("span", { class: "tick", "aria-hidden": "true" }, on ? "✓" : "");
    const panel = h("div", { class: "type-panel", role: "menu" },
      h("button", { class: "type-row", role: "menuitemradio", "aria-checked": String(cp.type === "all"), onclick: () => pick("all") },
        check(cp.type === "all"), h("span", { class: "label" }, "All types"), h("span", { class: "count" }, items.length)),
      ...groups.map(([id, label, ic, types]) => {
        const open = cp.typeOpen.has(id) || types.includes(cp.type);
        return h("div", { class: "type-group" },
          h("div", { class: "type-row-wrap" },
            h("button", { class: "type-row", role: "menuitemradio", "aria-checked": String(cp.type === `g:${id}`), onclick: () => pick(`g:${id}`) },
              check(cp.type === `g:${id}`), icon(ic, 16), h("span", { class: "label" }, label), h("span", { class: "count" }, total(types))),
            h("button", { class: "type-expand", title: open ? `Hide ${label} types` : `Show ${label} types`, "aria-expanded": String(open),
              onclick: (e) => { e.stopPropagation(); open ? cp.typeOpen.delete(id) : cp.typeOpen.add(id); drawTypePanel(); } },
            icon(open ? "chevronDown" : "chevronRight", 14))),
          open ? h("div", { class: "type-sub" }, types.map((t) => h("button", { class: "type-row", role: "menuitemradio",
            "aria-checked": String(cp.type === t), onclick: () => pick(t) },
          check(cp.type === t), itemIcon(t, 16), h("span", { class: "label" }, typeLabel(t)), h("span", { class: "count" }, counts[t])))) : null);
      }));
    const wrap = h("div", { class: "type-filter" },
      h("button", { class: `tool outline type-button${cp.type !== "all" ? " on" : ""}`, "aria-haspopup": "menu",
        onclick: (e) => { e.stopPropagation(); cp.typePanel = !cp.typePanel; drawTypePanel(); } },
      icon("database", 16), `Type: ${chosen}`, icon("chevronDown", 14)),
      cp.typePanel ? panel : null);
    return wrap;
  }
  function closeTypePanel() { cp.typePanel = false; }
  function drawTypePanel() { draw(); }

  function draw() {
    if (!cp.data || !here()) return;
    const items = cp.data.items;
    const end = state.endorsements;
    const wsFilter = h("input", { class: "catalog-search small", placeholder: "Find a workspace", value: cp.wsQuery, "aria-label": "Find a workspace" });
    wsFilter.addEventListener("input", () => { cp.wsQuery = wsFilter.value; drawWorkspaces(); wsFilter.focus(); });
    const wsList = h("div", { class: "catalog-ws" });
    // Workspaces as Fabric lists them: My workspace first, then the first few, then "More workspaces...".
    const WS_SHOWN = 6;
    const drawWorkspaces = () => {
      const q = cp.wsQuery.toLowerCase();
      const all = cp.data.workspaces.filter((w) => w.type !== "Personal" && w.displayName.toLowerCase().includes(q));
      const list = q || cp.wsAll ? all : all.slice(0, WS_SHOWN);
      wsList.replaceChildren(...[
        ...list.map((w) => scopeButton(`ws:${w.id}`, w.displayName, "workspace", items.filter((r) => r.workspace_id === w.id).length)),
        !q && all.length > WS_SHOWN ? h("button", { class: "catalog-more", onclick: () => { cp.wsAll = !cp.wsAll; drawWorkspaces(); } },
          cp.wsAll ? "Fewer workspaces" : `More workspaces (${all.length - WS_SHOWN})...`) : null].filter(Boolean));
    };
    drawWorkspaces();
    const keepFocus = document.activeElement === nav.querySelector(".catalog-search");
    const mine = personal();
    nav.replaceChildren(...[
      scopeButton("all", "All items", "apps", items.length),
      mine.size ? scopeButton("mine", "My workspace", "person", items.filter((r) => mine.has(r.workspace_id)).length) : null,
      scopeButton("endorsed", "Endorsed items", "shield", end?.available ? items.filter((r) => endorsed()[key(r)]).length : "",
        !end?.available, !end ? "Checking endorsement..." : end.reason || ""),
      scopeButton("fav", "Favorites", "star", items.filter((r) => favorites().has(key(r))).length),
      h("div", { class: "sec" }, "Workspaces"), wsFilter, wsList].filter(Boolean));
    if (keepFocus) wsFilter.focus();

    const rows = shown();
    // The Type filter counts what the left panel and search leave, as Fabric's does.
    const q = cp.query.toLowerCase();
    const scoped = items.filter((r) => inScope(r)
      && (!q || `${r.name} ${r.workspace_name} ${typeLabel(r.type)} ${r.description} ${tablesOf(r).join(" ")}`.toLowerCase().includes(q)));
    head.replaceChildren(...[typeFilter(scoped),
      cp.type !== "all" ? btn("Clear", "dismiss", () => { cp.type = "all"; draw(); }, "tool link small") : null,
      h("span", { class: "spacer" }),
      h("span", { class: "muted-text" }, `${rows.length} item${rows.length === 1 ? "" : "s"}`)].filter(Boolean));

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
    setSource(cell, cell.source + `\ndf = lakehouse.sql("""\n${query.replace(/"""/g, '\\"\\"\\"')}\n""")\ndf\n`);
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

function pickWorkspace(workspaces, { title = "Choose a workspace", selected = null, okLabel = "Next", note = null } = {}) {
  return new Promise((resolve) => {
    const select = h("select", {}, workspaces.map((w) => h("option", { value: w.id, selected: w.id === selected }, w.displayName)));
    showDialog(title, h("div", {}, note ? h("p", { class: "text" }, note) : null, h("label", { class: "field" }, "Workspace", select)), [
      btn("Cancel", null, () => { closeDialog(); resolve(null); }, "tool outline"),
      btn(okLabel, null, () => { closeDialog(); resolve(workspaces.find((w) => w.id === select.value)); }, "tool primary"),
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
  return { id: nextCellId++, type: c.type, source: c.source || "", meta: c.meta || {}, outputs: [], status: null, count: null, editing: !c.source };
}

function loadIntoState(nb) {
  disposeEditors();
  state.variables = null;
  state.notebook = { name: nb.name, cells: nb.cells.map(cellFrom), lakehouse: nb.lakehouse || null, dirty: false,
    compute: nb.compute || "default", workspace: nb.workspace || null, check: nb.check || null };
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
  return { name: state.notebook.name, cells: state.notebook.cells.map((c) => ({ type: c.type, source: c.source, meta: c.meta })),
    lakehouse_ref: state.notebook.lakehouse, compute: state.notebook.compute, workspace: state.notebook.workspace };
}

async function saveNotebook() {
  if (!state.notebook) return;
  clearTimeout(saveTimer);
  const ok = await attempt(() => rpc("save_notebook", notebookPayload()), "Could not save");
  if (ok && state.notebook) {
    state.notebook.dirty = false;
    if (ok.check) setComputeCheck(ok.check);
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

// ---------- Publish to OneLake ----------

// Each notebook remembers the workspace it is published to (saved in the .ipynb). The first
// Publish asks for it, starting from the attached lakehouse's workspace; later ones reuse it.
// Run in Fabric and schedules use the same workspace.
function publishWorkspace(nb) {
  if (nb?.workspace?.workspace_id) return nb.workspace;
  if (nb?.lakehouse) return { workspace_id: nb.lakehouse.workspace_id, workspace_name: nb.lakehouse.workspace_name };
  return null;
}

function publishWorkspaceName(nb) {
  return publishWorkspace(nb)?.workspace_name || "the workspace";
}

async function chooseNotebookWorkspace(nb, okLabel = "Save") {
  const workspaces = await loadWorkspaces();
  if (!workspaces.length) return null;
  const current = publishWorkspace(nb);
  const lh = nb.lakehouse ? ` Its lakehouse, ${nb.lakehouse.name}, stays attached wherever it is published.` : "";
  const ws = await pickWorkspace(workspaces, { title: `Publish ${nb.name} to`, selected: current?.workspace_id, okLabel,
    note: `The notebook is created or updated as a Fabric notebook item in this workspace. Publish, Run in Fabric and schedules keep using it until you change it here.${lh}` });
  if (!ws || state.notebook !== nb) return null;
  nb.workspace = { workspace_id: ws.id, workspace_name: ws.displayName };
  await saveNotebook();
  if (state.view === "notebook") renderNotebook();
  return nb.workspace;
}

function publishMenu(nb) {
  const ws = nb.workspace?.workspace_id ? nb.workspace.workspace_name : null;
  return [
    { info: ws ? `Publishes to ${ws}.` : "Publish asks which workspace to use the first time." },
    { label: ws ? `Publish to ${ws}` : "Publish...", icon: "publish", action: publishNotebook },
    { label: "Change workspace...", icon: "folder", action: () => ensureSignedIn().then((ok) => ok && chooseNotebookWorkspace(nb)) },
  ];
}

async function publishNotebook() {
  const nb = state.notebook;
  if (!(await ensureSignedIn())) return;
  if (!nb.workspace?.workspace_id && !(await chooseNotebookWorkspace(nb, "Publish"))) return;
  const ws = nb.workspace.workspace_name;
  toast(`Publishing ${nb.name} to ${ws}...`);
  const result = await attempt(() => rpc("publish_notebook", notebookPayload()), "Publish failed");
  if (result) {
    nb.dirty = false;
    renderChrome();
    toast(`Published ${nb.name} to ${ws} as a Fabric notebook item.`);
  }
}

// ---------- Where a notebook runs: this PC or Fabric ----------

// Each notebook has a "Runs on" choice: This PC, Fabric, or the default, which follows where the
// environment last picked on the Metadata pages runs (Metadata > Environments). Run all goes there. Fabric
// runs the whole published notebook, so single cells only run on this PC.
function nbWhere(nb) {
  const choice = nb?.compute || "default";
  const ws = nb?.lakehouse ? publishWorkspace(nb).workspace_name : null;
  const envFabric = envWhere(state.meta?.project, state.meta?.env).fabric;
  const fabric = choice === "fabric" || (choice === "default" && envFabric);
  const short = fabric ? (ws ? `Fabric · ${ws}` : "Fabric") : "This PC";
  return { fabric, choice, envFabric, short };
}

function computeMenu(nb) {
  const where = nbWhere(nb);
  const env = ENV_LABEL[state.meta?.env] || "Dev";
  const option = (value, label, iconName) => ({ label, icon: where.choice === value ? "ok" : iconName,
    action: () => setNotebookCompute(value) });
  return [
    { info: "Where Run all runs this notebook." },
    option("local", "This PC (no Fabric capacity used)", "desktop"),
    option("fabric", `Fabric${nb.lakehouse ? ` (${publishWorkspaceName(nb)})` : " (attach a lakehouse first)"}`, "cloud"),
    option("default", `Same as ${env} (${where.envFabric ? "Fabric" : "this PC"})`, "sync"),
    { info: `Python ${state.python || "3"} on this PC. Fabric runs publish the notebook first.` },
    { label: "Restart kernel", icon: "restart", action: restartKernel },
  ];
}

async function setNotebookCompute(value) {
  const nb = state.notebook;
  const before = nbWhere(nb).fabric;
  nb.compute = value;
  await saveNotebook();
  if (state.notebook !== nb) return;
  const now = nbWhere(nb);
  if (now.fabric !== before) toast(now.fabric ? `${nb.name} now runs in Fabric. Run all publishes it and starts it there.`
    : `${nb.name} now runs on this PC.`);
  renderNotebook();
}

// After a save: whether the notebook can still run where it is set to. Only the note is redrawn,
// so typing in a cell isn't interrupted.
function setComputeCheck(check) {
  const nb = state.notebook;
  const changed = JSON.stringify(nb.check) !== JSON.stringify(check);
  nb.check = check;
  if (!changed || state.view !== "notebook") return;
  const old = document.querySelector(".compute-note-slot");
  if (old) old.replaceWith(computeNote(nb));
}

function computeReasons(reasons) {
  return h("ul", { class: "gate-list" }, reasons.map((r) => h("li", {},
    r.cell ? [`Cell ${r.cell} ${r.text}`, r.code ? [": ", h("code", {}, r.code)] : null] : `The notebook ${r.text}`)));
}

// A note at the top of the notebook when it can't run where it is set to, or how a Fabric run works.
function computeNote(nb) {
  const slot = h("div", { class: "compute-note-slot" });
  const where = nbWhere(nb);
  const check = nb.check;
  const here = check?.[where.fabric ? "fabric" : "local"];
  const other = check?.[where.fabric ? "local" : "fabric"];
  if (here && !here.ok) {
    const switchTo = where.fabric ? "local" : "fabric";
    slot.append(h("div", { class: "problems" },
      h("div", { class: "row" }, icon("warning", 16),
        h("b", { class: "grow" }, where.fabric ? "This notebook can't run in Fabric yet" : "This notebook only runs in Fabric"),
        other?.ok ? btn(where.fabric ? "Run on this PC" : "Run in Fabric", where.fabric ? "desktop" : "cloud",
          () => setNotebookCompute(switchTo), "tool small") : null),
      computeReasons(here.reasons)));
  } else if (where.fabric) {
    const run = nb.fabricRun;
    slot.append(h("div", { class: "callout compute-callout" }, icon("cloud", 18),
      h("span", { class: "grow" }, `Run all publishes this notebook and runs it on ${publishWorkspaceName(nb)}'s Fabric capacity. `,
        "Cell output shows in Fabric, and the bell tells you when it finishes."),
      run ? h("a", { href: "#", onclick: (e) => { e.preventDefault(); openInFabric(run.url); } },
        `Last run: ${run.status === "NotStarted" ? "starting" : run.status}`) : null));
  }
  return slot;
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
  toast(`Publishing ${nb.name} and starting it in ${publishWorkspaceName(nb)}...`);
  const run = await attempt(() => rpc("run_notebook_in_fabric", notebookPayload()), "Could not start the run in Fabric");
  if (!run) return;
  nb.dirty = false;
  nb.fabricRun = run;
  renderChrome();
  if (state.view === "notebook") renderNotebook();
  toast(`${nb.name} is running in Fabric. The bell follows it until it finishes.`);
  logActivity({ id: `fabric:${run.job_id}`, status: "running", title: `${nb.name} · Notebook run`, text: `In Fabric on ${run.workspace_name || publishWorkspaceName(nb)}`, url: run.url, monitor: true });
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
  // Where it runs by default: the notebook's own choice, else the environment's compute.
  const fabricDefault = nb.compute === "fabric" || (nb.compute !== "local" && state.meta?.project?.environments?.[state.env]?.compute === "fabric");
  openScheduleDialog({
    title: `Schedule ${nb.name}`,
    mode: fabricDefault && nb.lakehouse ? "fabric" : "pc",
    pc: { target: { kind: "notebook", name: nb.name }, query: { kind: "notebook", name: nb.name },
          intro: "This PC runs the saved notebook, every cell in order, with its attached lakehouse. Save your changes first." },
    fabric: {
      unavailable: nb.lakehouse ? "" : "Attach a lakehouse first. Fabric runs the notebook in that lakehouse's workspace.",
      intro: `Fabric runs the published notebook in ${publishWorkspaceName(nb)} on its own capacity, even when this PC is off. Adding a schedule publishes what is on screen first.`,
      adding: `Publishing ${nb.name} and adding the schedule...`,
      added: "Scheduled. Fabric runs the published notebook on its own capacity.",
      list: () => rpc("notebook_schedules", { name: nb.name, lakehouse_ref: nb.lakehouse, workspace: nb.workspace }),
      add: async (schedule) => {
        const res = await rpc("add_notebook_schedule", { ...notebookPayload(), schedule });
        nb.dirty = false; renderChrome();
        return res;
      },
      remove: (id) => rpc("delete_notebook_schedule", { name: nb.name, lakehouse_ref: nb.lakehouse, workspace: nb.workspace, schedule_id: id }),
    },
  });
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

// ---------- Markdown ----------

function esc(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
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
// current and match each other, updating whatever is behind. Release builds run a
// bundled engine; after installing one, the check runs once more (afterInstall) with it.
async function checkComponents(afterInstall = false) {
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

  if (report.bundled) {
    set("python", report.python ? "ok" : "skip", report.python ? `${report.python}, part of the engine` : "Comes with the engine");
  } else {
    set("python", report.python_ok ? "ok" : "error",
      report.python_ok ? report.python : `${report.python} is too old. Fabric Desktop needs Python 3.11 or later.`);
  }

  state.build = app.commit ? short(app.commit) : `${app.version} (local build)`;
  if (!app.commit) set("app", "ok", `${app.version}, local build (update check off)`);
  else if (report.bundled && !report.python) set("app", "ok", `Build ${short(app.commit)}; checked for updates once the engine is installed`);
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
    if (afterInstall && report.bundled) {
      // The engine just installed is still not this build's: the exe update above failed.
      set("sidecar", "error", `The latest engine is build ${sidecar.commit ? short(sidecar.commit) : "unknown"}, ` +
        `but this app is ${short(app.commit)}. Restart Fabric Desktop to update it.`);
      if (await choose("Retry", "Continue anyway") === 0) return checkComponents();
      return closeDialog();
    }
    set("sidecar", "busy", sidecar.commit === app.commit ? `Repairing (it would not start: ${sidecar.error})…`
      : report.bundled ? (sidecar.version ? "Updating the engine…" : "Installing the engine…")
      : sidecar.version ? `Updating to build ${short(app.commit)}…` : `Installing build ${short(app.commit)}…`);
    log.hidden = false;
    let blocked = null;
    const unlisten = await listen((line) => {
      if (line.startsWith("blocked {")) { blocked = JSON.parse(line.slice(8)); return; }
      log.textContent += `${line}\n`;
      log.scrollTop = log.scrollHeight;
    });
    try {
      const result = await invoke("update_sidecar");
      if (result?.engine) {
        // Check again with the new engine: it may be a newer build than this exe, and
        // it may be blocked from loading.
        return checkComponents(true);
      }
      set("sidecar", "ok", `Updated to build ${short(app.commit)}`);
      log.hidden = true;
    } catch (err) {
      if (blocked) showBlocked(blocked);
      else set("sidecar", "error", report.bundled ? "The engine could not be installed. Details are below."
        : "Update failed. The pip output is below.");
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
    // After an update the engine lives in a new folder: point this PC's scheduled runs at it.
    rpc("sync_pc_schedules").then((r) => r.error && toast(`Scheduled runs on this PC: ${friendlyError(r.error).text}`, true)).catch(() => {});
  }, "Could not start the Python sidecar");
});
