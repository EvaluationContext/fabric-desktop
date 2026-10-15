// Looking after the data estate: a table's history with restore, what runs cost in Fabric
// capacity, OneLake shortcuts and access (tabs of the OneLake page), the tests that gate a
// promote, and the VL_Sources variable library.
// Loaded after meta.js and dataops.js. The entity page, Monitor, the OneLake page and the
// Metadata tabs call into this file; everything else here is self-contained.

// replaceChildren, minus the nulls that conditional parts leave (the DOM would print "null").
function fill(el, ...kids) {
  if (el) el.replaceChildren(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));
}

// ---------- history and restore ----------

async function historyDialog(name) {
  const env = state.meta.env;
  const body = h("div", { class: "scroll estate" }, h("p", { class: "muted-text" }, "Reading the table's history..."));
  const overlay = showDialog(`History of ${name} in ${ENV_LABEL[env]}`, body, [btn("Close", null, closeDialog, "tool outline")]);
  overlay.querySelector(".dialog").classList.add("wide");
  if (env !== "dev" && !state.user && !(await signIn())) return;
  const hist = await attempt(() => rpc("meta_table_history", { name, env }), "Could not read the history");
  if (!hist || !body.isConnected) return;
  if (!hist.versions.length) {
    fill(body, h("p", { class: "muted-text" }, `${name} hasn't been written in ${ENV_LABEL[env]} yet.`));
    return;
  }
  const what = (v) => {
    if (v.restored_to !== null && v.restored_to !== undefined) return `Restored to version ${v.restored_to}`;
    const op = { WRITE: "Write", MERGE: "Merge", DELETE: "Delete", RESTORE: "Restore", OPTIMIZE: "Optimize",
      "VACUUM START": "Vacuum started", "VACUUM END": "Vacuum finished", "SET TBLPROPERTIES": "Settings changed",
      "CREATE TABLE": "Created" }[v.operation] || v.operation || "";
    return v.run_id ? `${op} · run ${v.run_id}` : op;
  };
  fill(body,
    h("p", { class: "muted-text" }, `Every run writes a new version. Versions from the last ${hist.retention_days} days can be restored `,
      `(the ${hist.layer} layer's Keep history). Restoring adds a new version, so it can be undone the same way.`),
    h("table", { class: "list" },
      h("thead", {}, h("tr", {}, ["Version", "When", "What happened", "Rows", ""].map((c) => h("th", {}, c)))),
      h("tbody", {}, hist.versions.map((v) => h("tr", { class: v.maintenance ? "quiet" : null },
        h("td", { class: "mono" }, v.version), h("td", {}, when(v.at)), h("td", {}, what(v)),
        h("td", { class: "n" }, v.rows ?? ""),
        h("td", { class: "n" }, v.current ? h("span", { class: "pill-s pass" }, "Current")
          : v.restorable ? btn("Restore", "history", () => restoreDialog(name, env, v, hist), "tool outline small")
            : h("span", { class: "muted-text small", title: v.why_not || "" }, "Files removed")))))),
    hist.restores.length ? [h("h3", {}, "Earlier restores"),
      hist.restores.map((r) => h("div", { class: "muted-text small" },
        `${relTime(r.at)}: back to version ${r.to_version} by ${r.by || "someone"}${r.reason ? ` (${r.reason})` : ""}`))] : null);
}

function restoreDialog(name, env, v, hist) {
  const reason = h("input", { placeholder: "Bad export from CRM on Monday" });
  const restore = async () => {
    closeDialog();
    toast(`Restoring ${name} to version ${v.version}...`);
    const res = await metaCall("meta_restore", { name, env, version: v.version, reason: reason.value.trim() }, "Not restored");
    if (!res) return;
    toast(`${name} in ${ENV_LABEL[env]} is back to version ${v.version}`, false, { log: true });
    const downstream = res.downstream || [];
    showDialog("Restored", h("div", { class: "scroll" },
      h("div", { class: "pass-box" }, icon("ok", 16), `Version ${res.new_version} now holds what version ${v.version} had.`),
      res.incremental ? h("p", { class: "text" }, `Its watermark moved back to ${res.watermark || "the start"}, so the next run loads again what came after.`) : null,
      downstream.length ? [h("p", { class: "text" }, `These tables read it and still hold what they took from the bad load. Run them to pick up the restored data; an incremental one needs Rebuild with downstream on its own page.`),
        h("div", { class: "row wrap" }, downstream.map((d) => h("button", { class: "node", onclick: () => { closeDialog(); openEntity(d); } }, entityName(d))))]
        : h("p", { class: "muted-text" }, "Nothing reads this table.")),
    [btn("Close", null, () => { closeDialog(); render(); }, "tool outline"),
      downstream.length ? btn("Run downstream", "play", () => { closeDialog(); runEntities(downstream); }, "tool primary") : null].filter(Boolean));
  };
  showDialog(`Restore ${name} to version ${v.version}?`, h("div", { class: "scroll" },
    h("p", { class: "text" }, `${name} in ${ENV_LABEL[env]} goes back to how it was ${v.at ? new Date(v.at).toLocaleString() : `at version ${v.version}`}. `,
      "Later versions stay in its history, so you can restore one of them again."),
    hist.incremental ? h("p", { class: "text" }, "It loads incrementally, so its watermark moves back too and the next run picks up what came after.") : null,
    hist.downstream.length ? h("p", { class: "muted-text" }, `Tables that read it aren't changed: ${hist.downstream.join(", ")}.`) : null,
    h("label", { class: "field" }, "Why (kept with the restore)", reason)),
  [btn("Cancel", null, () => historyDialog(name), "tool outline"), btn("Restore", "history", restore, "tool primary")]);
}

// The entity lineage's file source, when the files come through a OneLake shortcut.
function shortcutLineage(e) {
  if (e.reads.length) return null;
  const slot = h("span", {});
  rpc("meta_entity_shortcuts", { name: e.name, env: state.meta.env }).then((list) => {
    if (!slot.isConnected || !list?.length) return;
    slot.replaceWith(...list.map((s) => h("span", { class: "node plain", title: `${s.full_path} points at ${s.target_type}: ${s.target}` },
      icon("databaseLink", 14), ` via shortcut ${s.name}`, h("span", { class: "muted-text small" }, ` → ${s.target_type}`))));
  }).catch(() => {});
  return slot;
}

// ---------- tests that gate promote ----------

async function renderTestsTab(body) {
  const m = state.meta;
  fill(body, h("p", { class: "muted-text" }, "Reading the tests..."));
  let t = await metaCall("meta_tests", { run: false });
  if (!t || state.view !== "metadata" || m.tab !== "tests") return;
  if (!t.at && t.files.length) t = (await metaCall("meta_tests", { run: true })) || t;
  if (state.view !== "metadata" || m.tab !== "tests") return;
  const run = async () => {
    body.querySelector(".tests-summary")?.replaceChildren(h("span", { class: "muted-text" }, "Running the tests..."));
    const res = await metaCall("meta_tests", { run: true }, "Tests didn't run");
    if (res) draw(res);
  };
  const draw = (r) => {
    const failed = r.tests.filter((x) => x.status !== "passed");
    fill(body,
      h("div", { class: "row tests-summary" },
        r.at ? (failed.length ? h("span", { class: "pill-s fail" }, `${failed.length} failing`) : h("span", { class: "pill-s pass" }, "All passing")) : null,
        h("span", { class: "muted-text" }, r.at ? `${r.passed} passed, ${r.failed} failed · run ${relTime(r.at)} in ${took(r.seconds)}` : "Not run yet"),
        r.stale ? h("span", { class: "pill-s wrn", title: "The project changed since this run" }, "Out of date") : null,
        h("span", { class: "spacer" }), btn("Run tests", "beaker", run, "tool primary")),
      r.missing.length ? h("div", { class: "problems" }, h("b", {}, "Custom code points at tests that don't exist"), gateList(r.missing)) : null,
      r.tests.length ? h("table", { class: "list" },
        h("thead", {}, h("tr", {}, ["", "Test", "Checks the code of", "Took", ""].map((c) => h("th", {}, c)))),
        h("tbody", {}, r.tests.map((x) => h("tr", {},
          h("td", {}, h("span", { class: `pill-s ${x.status === "passed" ? "pass" : "fail"}` }, x.status === "passed" ? "Passed" : x.status === "failed" ? "Failed" : "Error")),
          h("td", {}, h("div", { class: "mono" }, x.name), h("div", { class: "muted-text small mono" }, x.file),
            x.message ? h("div", { class: "reason" }, x.message) : null),
          h("td", {}, x.used_by.length ? x.used_by.map((u) => h("div", { class: "small" }, u)) : h("span", { class: "muted-text small" }, "Shared helpers")),
          h("td", { class: "n" }, took(x.seconds)), h("td", {})))))
        : h("p", { class: "muted-text" }, "No tests yet. Put test_*.py files in the project's tests folder, with test_ functions that assert what your code does."),
      h("div", { class: "info-cards" },
        infoCard("Promote runs them first", "Dev can't be promoted to Test, or Test to Prod, while a test fails. Every hook, custom check and custom step names its test, so code without one is caught at save."),
        infoCard("Plain Python", "A test is a function named test_something that uses assert, as in pytest. It can take tmp_path for a scratch folder; other pytest fixtures aren't available here."),
        infoCard("Your transforms, fresh", "Each run imports transforms/ and tests/ again, so it always tests what you just saved.")));
  };
  draw(t);
}

// ---------- VL_Sources variable library ----------

function varlibCard() {
  const card = h("div", { class: "card pad estate" }, h("b", {}, "Variable library VL_Sources"),
    h("p", { class: "muted-text small" }, "Reading the source systems..."));
  const load = () => rpc("meta_varlib").then((v) => { if (card.isConnected) fill(card, ...varlibBody(v, load)); })
    .catch((err) => fill(card, h("b", {}, "Variable library VL_Sources"), h("p", { class: "muted-text" }, friendlyError(err).text)));
  load();
  return card;
}

function varlibBody(v, reload) {
  const envs = ["dev", "test", "prod"];
  const first = v.variables.find((x) => x.name.includes("_") && x.name !== "ENVIRONMENT" && !x.name.startsWith("KEYS_"));
  const snippet = `lib = notebookutils.variableLibrary.getLibrary("${v.library}")\nlib.${first ? first.name : "CRM_server_host"}`;
  const publish = async (env) => {
    if (!state.user && !(await signIn())) return;
    toast(`Publishing ${v.library} to ${ENV_LABEL[env]}...`);
    const res = await metaCall("meta_publish_varlib", { env }, "Not published");
    if (!res) return;
    toast(`${v.library} ${res.created ? "created" : "updated"} in ${res.workspace_name || ENV_LABEL[env]}, with ${res.value_set} active`, false, { log: true });
    reload();
  };
  const cell = (x, env) => env !== "dev" && x[env] === x.dev ? h("td", { class: "muted-text small" }, "same as Dev")
    : h("td", { class: "mono small" }, x[env] || h("span", { class: "muted-text" }, "empty"));
  return [
    h("div", { class: "row wrap" }, h("b", { class: "grow" }, `Variable library ${v.library}`),
      envs.map((env) => {
        const ws = v.environments[env];
        return btn(`Publish to ${ENV_LABEL[env]}`, "publish", () => publish(env), "tool outline small",
          { disabled: ws.workspace_id ? null : true,
            title: ws.workspace_id ? `${ws.workspace_name || ws.workspace_id}, with ${ws.value_set} active${v.published[env] ? ` · last published ${new Date(v.published[env]).toLocaleString()}` : ""}`
              : `Assign a workspace to ${ENV_LABEL[env]} first` });
      })),
    h("p", { class: "muted-text small" }, "One Fabric variable library per environment workspace, with that environment's connection values active. ",
      "Promote updates it in the target. Notebooks read the same names in Fabric and on this PC:"),
    h("pre", { class: "sql-block" }, snippet),
    h("table", { class: "list compact" },
      h("thead", {}, h("tr", {}, h("th", {}, "Variable"), envs.map((env) => h("th", {}, ENV_LABEL[env])))),
      h("tbody", {}, v.variables.map((x) => h("tr", {},
        h("td", {}, h("div", { class: "mono" }, x.name), h("div", { class: "muted-text small" }, x.note)),
        envs.map((env) => cell(x, env)))))),
  ];
}

// ---------- cost (a panel of the Monitor page) ----------

const costPage = { days: 30 };

function money(value, currency) {
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency: currency || "USD", maximumFractionDigits: value < 10 ? 2 : 0 }).format(value);
  } catch {
    return `${Number(value).toFixed(2)} ${currency || ""}`;
  }
}

function cuHours(n) {
  return `${n < 10 ? Number(n).toFixed(2) : Math.round(n).toLocaleString()} CU h`;
}

function costPanel() {
  const panel = h("div", { class: "monitor-scroll estate cost" }, h("p", { class: "muted-text monitor-empty" }, "Adding up the runs..."));
  rpc("cost_report", { days: costPage.days }).then((r) => { if (panel.isConnected) fill(panel, ...costBody(r)); })
    .catch((err) => fill(panel, h("p", { class: "muted-text monitor-empty" }, friendlyError(err).text)));
  return panel;
}

function costBody(r) {
  const cur = r.settings.currency;
  const stat = (value, label, sub) => h("div", { class: "cmp-stat" }, h("span", { class: "v" }, value), h("span", { class: "k" }, label),
    sub ? h("span", { class: "k" }, sub) : null);
  const t = r.total;
  const daysChip = (d) => h("button", { class: `chip${costPage.days === d ? " on" : ""}`, onclick: () => { costPage.days = d; render(); } }, `${d} days`);
  const unavailable = Object.entries(r.unavailable || {});
  return [
    h("div", { class: "row wrap cost-head" }, daysChip(7), daysChip(30), daysChip(90), h("span", { class: "spacer" }),
      h("span", { class: "muted-text small" }, `${money(r.settings.price_per_cu_hour, cur)} per CU hour · Python ${r.settings.python_cu} CU · Spark ${r.settings.spark_cu} CU`),
      btn("Prices", "gauge", () => costSettingsDialog(r.settings), "tool outline small")),
    h("div", { class: "cmp-stats" },
      stat(money(t.cost, cur), "Fabric capacity used", `${cuHours(t.cu_hours)} · ${t.fabric_runs} run${t.fabric_runs === 1 ? "" : "s"}`),
      stat(t.saved || !t.saved_cu_hours ? money(t.saved, cur) : `under ${money(0.01, cur)}`, "Saved by running on this PC",
        `${cuHours(t.saved_cu_hours)} · ${t.local_runs} run${t.local_runs === 1 ? "" : "s"}`),
      r.by_env.filter((e) => e.capacity_share !== null && e.capacity_share !== undefined && e.fabric_runs).map((e) =>
        stat(`${e.capacity_share < 0.1 ? "<0.1" : e.capacity_share.toFixed(1)}%`, `of ${e.capacity || "the"} capacity (${e.sku})`, `${ENV_LABEL[e.env] || e.env}'s Fabric runs`))),
    r.by_item.length ? h("table", { class: "list" },
      h("thead", {}, h("tr", {}, ["What ran", "Env", "Runs", "Time", "Fabric", "Saved on this PC"].map((c) => h("th", {}, c)))),
      h("tbody", {}, r.by_item.slice(0, 60).map((x) => h("tr", {},
        h("td", {}, h("b", {}, x.name || x.kind), h("div", { class: "muted-text small" }, x.kind)),
        h("td", {}, ENV_LABEL[x.env] || x.env || ""),
        h("td", { class: "n" }, x.runs), h("td", { class: "n" }, took(x.seconds)),
        h("td", { class: "n" }, x.fabric_runs ? `${money(x.cost, cur)} · ${cuHours(x.cu_hours)}` : ""),
        h("td", { class: "n" }, x.local_runs ? (x.saved ? money(x.saved, cur) : x.saved_cu_hours ? `under ${money(0.01, cur)}` : "") : "")))))
      : h("p", { class: "muted-text" }, `Nothing ran in the last ${r.days} days.`),
    h("p", { class: "muted-text small" }, "Estimates: run time times the capacity units of what it ran on. A Python notebook (Fabric Desktop's runner) uses 1 CU per 2 vCores; Spark jobs such as table maintenance use the Spark figure. ",
      "Session start-up in Fabric isn't counted. Fabric's Capacity Metrics app has the billed numbers.",
      unavailable.length ? ` Left out: ${unavailable.map(([env, why]) => `${ENV_LABEL[env]} (${why})`).join(", ")}.` : ""),
  ];
}

function costSettingsDialog(cfg) {
  const field = (label, key, attrs) => {
    const input = h("input", { value: cfg[key], ...attrs });
    return [h("label", { class: "field" }, label, input), () => input.value];
  };
  const [price, getPrice] = field("Price of one CU hour", "price_per_cu_hour", { type: "number", min: "0.001", step: "0.001" });
  const [currency, getCurrency] = field("Currency", "currency", { maxlength: "5" });
  const [py, getPy] = field("CUs of a Python notebook (2 vCores = 1 CU)", "python_cu", { type: "number", min: "0.5", step: "0.5" });
  const [spark, getSpark] = field("CUs of a Spark job (starter pool, 2 medium nodes = 8 CU)", "spark_cu", { type: "number", min: "0.5", step: "0.5" });
  showDialog("Cost estimate", h("div", { class: "scroll" },
    h("p", { class: "text" }, "Pay-as-you-go is about $0.18 per CU hour in US regions; a reserved capacity costs less. Use your own rate."),
    h("div", { class: "form-grid" }, price, currency, py, spark)),
  [btn("Cancel", null, closeDialog, "tool outline"), btn("Save", "save", async () => {
    const res = await attempt(() => rpc("set_cost_settings", { price_per_cu_hour: getPrice(), currency: getCurrency(), python_cu: getPy(), spark_cu: getSpark() }), "Not saved");
    if (res) { closeDialog(); render(); }
  }, "tool primary")]);
}

// ---------- OneLake page: Shortcuts tab ----------

const shortcutPage = { lakehouse: "all", query: "", all: null, rows: null };

async function onelakeCatalog() {
  if (catalogPage.data) return catalogPage.data;
  const data = await attempt(() => rpc("catalog", { all_types: true }), "Could not list the catalog");
  if (data) { catalogPage.data = data; state.workspaces = data.workspaces; }
  return data;
}

function lakehouseRows(data) {
  return data.items.filter((r) => r.type === "Lakehouse")
    .sort((a, b) => `${a.workspace_name} ${a.name}`.localeCompare(`${b.workspace_name} ${b.name}`, undefined, { sensitivity: "base" }));
}

async function renderShortcuts() {
  const sp = shortcutPage;
  const nav = h("nav", { class: "catalog-nav" });
  const main = h("section", { class: "catalog-main estate" }, h("p", { class: "muted-text", style: "padding:16px" }, state.user ? "Loading lakehouses..." : ""));
  const search = h("input", { class: "catalog-search", placeholder: "Search shortcuts", value: sp.query, "aria-label": "Search shortcuts" });
  search.addEventListener("input", () => { sp.query = search.value; drawMain(); });
  const newBtn = btn("New shortcut", "add", () => newShortcutDialog(current()), "tool primary", { disabled: true });
  setView(h("div", { class: "page monitor onelake" }, signInCallout(), onelakeTabs(),
    h("section", { class: "card onelake-catalog" },
      h("div", { class: "monitor-band" }, h("h2", {}, "Shortcuts"),
        h("span", { class: "muted-text" }, "Tables and folders that point at data somewhere else, without copying it"),
        h("span", { class: "spacer" }), search,
        btn("Refresh", "sync", () => { sp.all = null; sp.rows = null; render(); }, "tool outline"), newBtn),
      h("div", { class: "catalog-body" }, nav, main))));
  if (!state.user) return;
  const data = await onelakeCatalog();
  if (!data || state.view !== "onelake" || catalogPage.tab !== "shortcuts") return;
  const lakehouses = lakehouseRows(data);
  const byId = Object.fromEntries(data.items.map((r) => [r.item_id, r]));
  const current = () => lakehouses.find((l) => l.item_id === sp.lakehouse) || null;
  const here = () => state.view === "onelake" && catalogPage.tab === "shortcuts" && main.isConnected;

  function drawNav() {
    const count = (id) => sp.all?.items?.[id]?.length;
    const item = (id, label, sub, n) => h("button", { class: `catalog-scope${sp.lakehouse === id ? " on" : ""}`,
      onclick: () => { sp.lakehouse = id; sp.rows = null; drawNav(); drawMain(); if (id !== "all") loadOne(); } },
    h("span", { class: "label" }, label, sub ? h("span", { class: "muted-text small" }, ` ${sub}`) : null), h("span", { class: "count" }, n ?? ""));
    const total = sp.all && !sp.all.pending ? Object.values(sp.all.items).reduce((a, l) => a + l.length, 0) : "";
    fill(nav, item("all", "Every lakehouse", "", total), h("div", { class: "sec" }, "Lakehouses"),
      lakehouses.map((l) => item(l.item_id, l.name, l.workspace_name, count(l.item_id))));
    newBtn.disabled = !current();
    newBtn.title = current() ? `New shortcut in ${current().name}` : "Pick a lakehouse first";
  }

  function targetCell(s) {
    if (s.target_type !== "OneLake") return h("div", {}, h("b", {}, s.target_type), h("div", { class: "muted-text small mono" }, s.target));
    const item = byId[s.item_id];
    return h("div", {}, h("b", {}, item ? `${item.name}` : "OneLake"),
      h("div", { class: "muted-text small" }, item ? `${item.workspace_name} · ${s.target_path}` : s.target_path),
      s.item_id && !item ? h("span", { class: "pill-s wrn", title: "Not in any workspace you can open. It may have been deleted, or you can't read it." }, "Target not found") : null);
  }

  function drawMain() {
    if (!here()) return;
    let rows;
    if (sp.lakehouse === "all") {
      if (!sp.all || sp.all.pending) { fill(main, h("p", { class: "muted-text", style: "padding:16px" }, "Reading shortcuts in every lakehouse...")); return; }
      rows = lakehouses.flatMap((l) => (sp.all.items[l.item_id] || []).map((s) => ({ ...s, lakehouse: l })));
    } else {
      if (!sp.rows) { fill(main, h("p", { class: "muted-text", style: "padding:16px" }, "Reading shortcuts...")); return; }
      rows = sp.rows.map((s) => ({ ...s, lakehouse: current() }));
    }
    const q = sp.query.toLowerCase();
    rows = rows.filter((s) => !q || `${s.name} ${s.full_path} ${s.target} ${s.target_type} ${s.lakehouse?.name}`.toLowerCase().includes(q));
    const errors = sp.lakehouse === "all" ? Object.keys(sp.all.errors || {}).length : 0;
    fill(main,
      rows.length ? h("table", { class: "list" },
        h("thead", {}, h("tr", {}, ["Shortcut", sp.lakehouse === "all" ? "In lakehouse" : null, "Points at", "Used by", ""].filter(Boolean).map((c) => h("th", {}, c)))),
        h("tbody", {}, rows.map((s) => h("tr", {},
          h("td", {}, h("span", { class: "item" }, icon(s.kind === "table" ? "table" : "folder", 16), h("b", {}, s.name)),
            h("div", { class: "muted-text small mono" }, s.path)),
          sp.lakehouse === "all" ? h("td", {}, h("div", {}, s.lakehouse.name), h("div", { class: "muted-text small" }, s.lakehouse.workspace_name)) : null,
          h("td", {}, targetCell(s)),
          h("td", {}, s.used_by.length ? s.used_by.map((u) => h("div", { class: "small" },
            h("button", { class: "name-link", onclick: () => openEntity(u.entity) }, u.entity), ` ${u.how}`)) : h("span", { class: "muted-text small" }, "")),
          h("td", { class: "actions" }, btn("Remove", "delete", () => removeShortcut(s), "tool small link"))))))
        : h("p", { class: "muted-text", style: "padding:16px" }, sp.query ? "Nothing matches."
          : sp.lakehouse === "all" ? "No shortcuts in any lakehouse yet. Pick a lakehouse, then New shortcut." : "No shortcuts in this lakehouse yet."),
      errors ? h("p", { class: "muted-text small", style: "padding:0 16px" }, `${errors} lakehouse${errors === 1 ? "" : "s"} couldn't be read (a paused capacity, or no access).`) : null);
  }

  async function removeShortcut(s) {
    if (!(await confirmDialog(`Remove shortcut ${s.name}?`, `Only the shortcut in ${s.lakehouse.name} goes. The data it points at (${s.target}) isn't touched.`, "Remove"))) return;
    const rows = await attempt(() => rpc("delete_shortcut", { workspace_id: s.lakehouse.workspace_id, item_id: s.lakehouse.item_id,
      path: s.path, name: s.name, lakehouse_name: s.lakehouse.name }), "Not removed");
    if (!rows) return;
    toast(`Removed shortcut ${s.name}`);
    sp.all = null;
    if (sp.lakehouse !== "all") sp.rows = rows;
    loadAll();
    drawNav(); drawMain();
  }

  async function loadOne() {
    const l = current();
    if (!l) return;
    const rows = await attempt(() => rpc("list_shortcuts", { workspace_id: l.workspace_id, item_id: l.item_id, lakehouse_name: l.name }), "Could not list shortcuts");
    if (sp.lakehouse === l.item_id) { sp.rows = rows || []; drawMain(); }
  }

  function loadAll() {
    if (sp.all && !sp.all.pending) return;
    const refs = lakehouses.map((l) => ({ workspace_id: l.workspace_id, lakehouse_id: l.item_id, name: l.name }));
    pollSidecar("all_shortcuts", { lakehouses: refs, refresh: !sp.all }, (result) => { sp.all = result; drawNav(); drawMain(); }, here);
  }

  drawNav(); drawMain();
  loadAll();
  if (sp.lakehouse !== "all" && !sp.rows) loadOne();
}

async function newShortcutDialog(lh) {
  if (!lh) return;
  const data = await onelakeCatalog();
  const lakehouses = lakehouseRows(data);
  const own = await attempt(() => rpc("list_tables", { workspace_id: lh.workspace_id, lakehouse_id: lh.item_id }), "Could not read the lakehouse");
  const schemas = Object.keys(own || { dbo: [] });
  const where = h("select", { "aria-label": "Put it in" },
    schemas.map((s) => h("option", { value: `Tables/${s}` }, `Tables / ${s} (a table)`)), h("option", { value: "Files" }, "Files (a folder)"));
  const folder = h("input", { placeholder: "landing/crm (optional)" });
  const folderField = h("label", { class: "field" }, "Folder under Files", folder);
  const name = h("input", { placeholder: "orders" });
  let kind = "oneLake";
  const seg = h("div", { class: "seg", role: "group", "aria-label": "Points at" });
  const source = h("div", { class: "form-grid" });
  const targetLh = h("select", { "aria-label": "Lakehouse" }, lakehouses.map((l) => h("option", { value: l.item_id }, `${l.name} · ${l.workspace_name}`)));
  const targetTable = h("select", { "aria-label": "Table" });
  const targetPath = h("input", { placeholder: "Files/exports" });
  const loadTables = async () => {
    const l = lakehouses.find((x) => x.item_id === targetLh.value);
    fill(targetTable, h("option", { value: "" }, "Loading tables..."));
    const tables = l ? await rpc("list_tables", { workspace_id: l.workspace_id, lakehouse_id: l.item_id }).catch(() => ({})) : {};
    fill(targetTable, h("option", { value: "" }, "A folder in Files instead"),
      ...Object.entries(tables).flatMap(([s, ts]) => ts.map((t) => h("option", { value: `Tables/${s}/${t}` }, `${s}.${t}`))));
    if (targetTable.options.length > 1) targetTable.selectedIndex = 1;
    sync();
  };
  targetLh.addEventListener("change", loadTables);
  const conn = h("select", { "aria-label": "Connection" }, h("option", { value: "" }, "Loading connections..."));
  const location = h("input", { placeholder: "https://account.dfs.core.windows.net" });
  const subpath = h("input", { placeholder: "container/folder" });
  let connections = null;
  const loadConnections = async () => {
    if (!connections) connections = (await attempt(() => rpc("shortcut_connections"), "Could not list connections")) || [];
    const fitting = connections.filter((c) => c.type === kind);
    fill(conn, fitting.length ? fitting.map((c) => h("option", { value: c.id }, c.name))
      : [h("option", { value: "" }, `No ${kind === "adlsGen2" ? "ADLS Gen2" : "Amazon S3"} connection yet. Create one in Fabric (Settings > Manage connections).`)]);
    const c = fitting[0];
    if (c?.path && !location.value) location.value = c.path;
  };
  const sync = () => {
    folderField.hidden = where.value !== "Files";
    targetPath.disabled = !!targetTable.value;
    if (!name.value || name.dataset.auto === "1") {
      const guess = kind === "oneLake" ? (targetTable.value || targetPath.value).split("/").pop() : subpath.value.split("/").pop();
      name.value = guess || "";
      name.dataset.auto = "1";
    }
  };
  name.addEventListener("input", () => { name.dataset.auto = "0"; });
  [where, targetTable].forEach((x) => x.addEventListener("change", sync));
  [targetPath, subpath].forEach((x) => x.addEventListener("input", sync));
  const drawSource = () => {
    fill(seg, ...[["oneLake", "OneLake"], ["adlsGen2", "ADLS Gen2"], ["amazonS3", "Amazon S3"]].map(([id, label]) =>
      h("button", { class: kind === id ? "on" : null, onclick: () => { kind = id; drawSource(); } }, label)));
    if (kind === "oneLake") {
      fill(source, h("label", { class: "field" }, "Lakehouse", targetLh), h("label", { class: "field" }, "Table", targetTable),
        h("label", { class: "field" }, "Folder (when not a table)", targetPath));
      if (!targetTable.options.length) loadTables();
    } else {
      fill(source, h("label", { class: "field" }, "Connection", conn), h("label", { class: "field" }, "Location (URL)", location),
        h("label", { class: "field" }, "Subpath", subpath));
      loadConnections();
    }
    sync();
  };
  drawSource();
  const create = async () => {
    const l = lakehouses.find((x) => x.item_id === targetLh.value);
    const src = kind === "oneLake"
      ? { type: "oneLake", workspace_id: l?.workspace_id, item_id: l?.item_id, path: targetTable.value || targetPath.value }
      : { type: kind, connection_id: conn.value, location: location.value, subpath: subpath.value };
    const whereTo = where.value === "Files" ? `Files/${folder.value.trim().replace(/^\/+|\/+$/g, "")}`.replace(/\/$/, "") : where.value;
    const rows = await attempt(() => rpc("create_shortcut", { workspace_id: lh.workspace_id, item_id: lh.item_id, where: whereTo,
      name: name.value.trim(), source: src, lakehouse_name: lh.name }), "Shortcut not created");
    if (!rows) return;
    closeDialog();
    toast(`Created shortcut ${name.value.trim()} in ${lh.name}`, false, { log: true });
    shortcutPage.rows = rows;
    shortcutPage.all = null;
    render();
  };
  const overlay = showDialog(`New shortcut in ${lh.name}`, h("div", { class: "scroll estate" },
    h("p", { class: "text" }, "A shortcut shows data from somewhere else as a table or folder in this lakehouse. Nothing is copied; Fabric reads it where it is."),
    h("div", { class: "form-grid" }, h("label", { class: "field" }, "Put it in", where), folderField, h("label", { class: "field" }, "Name", name)),
    h("div", { class: "row" }, h("b", {}, "Points at"), seg), source,
    h("p", { class: "muted-text small" }, "A table shortcut must point at a Delta table. ADLS Gen2 and S3 use a connection you set up once in Fabric.")),
  [btn("Cancel", null, closeDialog, "tool outline"), btn("Create shortcut", "databaseLink", create, "tool primary")]);
  overlay.querySelector(".dialog").classList.add("roomy");
  sync();
}

// ---------- OneLake page: Access tab ----------

const accessPage = { extra: [], data: null, lakehouse: null, lh: null };

async function renderAccess() {
  const ap = accessPage;
  const people = h("div", { class: "estate" }, h("p", { class: "muted-text" }, state.user ? "Reading workspace roles..." : ""));
  const lhSlot = h("div", { class: "estate" });
  setView(h("div", { class: "page" }, signInCallout(), onelakeTabs(),
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "Access"),
        h("p", { class: "sub" }, "Who can reach each environment's workspace, and who can read which tables. Changes apply in Fabric right away.")),
      h("div", { class: "head-actions" },
        btn("Add a workspace", "add", addReviewWorkspace, "tool outline"),
        btn("Give access", "person", () => grantDialog(), "tool outline"),
        btn("Refresh", "sync", () => { ap.data = null; ap.lh = null; render(); }, "tool outline"))),
    h("div", { class: "card pad" }, h("h2", { class: "flush" }, "Workspace roles"), people),
    h("div", { class: "card pad" }, h("h2", { class: "flush" }, "Table access in a lakehouse"), lhSlot)));
  if (!state.user) return;
  if (!ap.data) ap.data = await attempt(() => rpc("access_review", { extra: ap.extra }), "Could not read workspace roles");
  if (state.view !== "onelake" || catalogPage.tab !== "access") return;
  if (!ap.data) { fill(people, h("p", { class: "muted-text" }, "Couldn't read the workspace roles. Details are in Activity (the bell).")); return; }
  fill(people, ...accessTable(ap.data));
  drawLakehouseAccess(lhSlot);
}

async function addReviewWorkspace() {
  const workspaces = await loadWorkspaces();
  const ws = await pickWorkspace(workspaces, { title: "Add a workspace to the review", okLabel: "Add" });
  if (!ws) return;
  accessPage.extra.push({ workspace_id: ws.id, workspace_name: ws.displayName });
  accessPage.data = null;
  render();
}

function accessTable(d) {
  const ws = d.workspaces;
  if (!ws.length) {
    return [h("p", { class: "muted-text" }, "No environment has a workspace yet. Assign them in Metadata > Environments and promote, or add any workspace to review it.")];
  }
  const roleSelect = (p, w) => {
    const cur = p.roles[w.key];
    const sel = h("select", { "aria-label": `${p.name} in ${w.label}`, disabled: w.error ? true : null },
      h("option", { value: "" }, "No access"), d.roles.map((r) => h("option", { value: r, selected: cur?.role === r ? true : null }, r)));
    sel.addEventListener("change", async () => {
      const role = sel.value;
      const what = role ? `${cur ? "change" : "give"} ${p.name} ${role} on` : `remove ${p.name} from`;
      if (!(await confirmDialog("Change access?", `This will ${what} ${w.workspace_name || w.label} in Fabric now.`, "Change"))) { sel.value = cur?.role || ""; return; }
      const ok = await attempt(() => rpc("access_set_role", { workspace_id: w.workspace_id, role, assignment_id: cur?.assignment_id || "",
        principal_id: p.id, principal_type: p.type || "User" }), "Access not changed");
      if (ok) { toast(`Access changed for ${p.name}`, false, { log: true }); accessPage.data = null; render(); } else sel.value = cur?.role || "";
    });
    return sel;
  };
  return [
    d.flagged ? h("div", { class: "problems" }, h("b", {}, `${d.flagged} to look at`), h("span", { class: "muted-text" }, " Flags are listed next to each person.")) : null,
    h("div", { class: "monitor-scroll" }, h("table", { class: "list" },
      h("thead", {}, h("tr", {}, h("th", {}, "Person, group or app"),
        ws.map((w) => h("th", { title: w.workspace_name || w.workspace_id }, w.label, w.error ? h("div", { class: "reason small" }, "Can't read roles") : h("div", { class: "muted-text small" }, w.workspace_name))),
        h("th", {}, "Worth a look"))),
      h("tbody", {}, d.people.map((p) => h("tr", {},
        h("td", {}, h("span", { class: "item" }, icon(p.type === "Group" ? "workspaces" : p.type === "User" ? "person" : "bot", 16), h("b", {}, p.name)),
          h("div", { class: "muted-text small" }, [p.type, p.email].filter(Boolean).join(" · "))),
        ws.map((w) => h("td", {}, roleSelect(p, w))),
        h("td", {}, p.flags.map((f) => h("div", { class: "reason small" }, f)))))))),
    ws.some((w) => w.error) ? h("p", { class: "muted-text small" }, `Listing roles needs Member or Admin on the workspace: ${ws.filter((w) => w.error).map((w) => w.label).join(", ")}.`) : null,
  ];
}

async function grantDialog() {
  const d = accessPage.data;
  if (!d?.workspaces.length) return toast("Add a workspace to the review first", true);
  const who = h("input", { placeholder: "name@contoso.com, or a group's object ID" });
  const wsSel = h("select", {}, d.workspaces.filter((w) => !w.error).map((w) => h("option", { value: w.workspace_id }, `${w.label} · ${w.workspace_name || ""}`)));
  const roleSel = h("select", {}, d.roles.map((r) => h("option", { value: r, selected: r === "Viewer" ? true : null }, r)));
  const typeSel = h("select", {}, ["User", "Group", "ServicePrincipal"].map((t) => h("option", { value: t }, t === "ServicePrincipal" ? "App (service principal)" : t)));
  showDialog("Give access", h("div", { class: "scroll" },
    h("div", { class: "form-grid" }, h("label", { class: "field" }, "Who", who), h("label", { class: "field" }, "Kind (for an object ID)", typeSel),
      h("label", { class: "field" }, "Workspace", wsSel), h("label", { class: "field" }, "Role", roleSel)),
    h("p", { class: "muted-text small" }, "Viewer reads; Contributor also writes items; Member also shares; Admin manages access. Finding someone by email asks you once to let the app read your directory's names.")),
  [btn("Cancel", null, closeDialog, "tool outline"), btn("Give access", "person", async () => {
    const person = await attempt(() => rpc("access_find_person", { text: who.value }), "Couldn't find them");
    if (!person) return;
    const type = person.type === "User" ? "User" : typeSel.value;
    const ok = await attempt(() => rpc("access_set_role", { workspace_id: wsSel.value, role: roleSel.value, principal_id: person.id, principal_type: type }), "Access not given");
    if (!ok) return;
    closeDialog();
    toast(`${person.name} is now ${roleSel.value}`, false, { log: true });
    accessPage.data = null;
    render();
  }, "tool primary")]);
}

async function drawLakehouseAccess(slot) {
  const ap = accessPage;
  const data = await onelakeCatalog();
  if (!data || !slot.isConnected) return;
  const reviewed = new Set((ap.data?.workspaces || []).map((w) => w.workspace_id));
  const lakehouses = lakehouseRows(data).sort((a, b) => (reviewed.has(b.workspace_id) - reviewed.has(a.workspace_id)));
  if (!ap.lakehouse) ap.lakehouse = lakehouses[0]?.item_id || null;
  const sel = h("select", { "aria-label": "Lakehouse" }, lakehouses.map((l) => h("option", { value: l.item_id, selected: l.item_id === ap.lakehouse ? true : null }, `${l.name} · ${l.workspace_name}`)));
  const out = h("div", { class: "estate" }, h("p", { class: "muted-text" }, "Reading its data access roles..."));
  sel.addEventListener("change", () => { ap.lakehouse = sel.value; ap.lh = null; drawLakehouseAccess(slot); });
  fill(slot, h("div", { class: "row wrap" }, h("label", { class: "field grow" }, "Lakehouse", sel)), out);
  const lh = lakehouses.find((l) => l.item_id === ap.lakehouse);
  if (!lh) { fill(out, h("p", { class: "muted-text" }, "No lakehouses yet.")); return; }
  if (!ap.lh || ap.lh.item_id !== lh.item_id) ap.lh = await attempt(() => rpc("access_lakehouse", { workspace_id: lh.workspace_id, item_id: lh.item_id }), "Could not read table access");
  if (!ap.lh || !out.isConnected) return;
  fill(out, ...lakehouseAccessBody(ap.lh, lh, () => drawLakehouseAccess(slot)));
}

function lakehouseAccessBody(a, lh, redraw) {
  const names = Object.fromEntries(a.people.map((p) => [p.id, p.name]));
  const pathLabel = (p) => (p === "*" ? "Everything" : p.replace(/^\/Tables\//, "").replace(/\//g, ".").replace(/^\/Files/, "Files"));
  const who = h("div", {});
  const tableSel = h("select", { "aria-label": "Table" }, h("option", { value: "" }, "Pick a table"), a.tables.map((t) => h("option", { value: t }, pathLabel(t))));
  tableSel.addEventListener("change", async () => {
    if (!tableSel.value) { fill(who); return; }
    fill(who, h("p", { class: "muted-text small" }, "Working it out..."));
    const list = await attempt(() => rpc("access_who_can_read", { workspace_id: lh.workspace_id, item_id: lh.item_id, table: tableSel.value }), "Could not work it out");
    if (list) fill(who, ...(list.length ? list.map((p) => h("div", { class: "row tight" }, h("b", {}, p.name), h("span", { class: "muted-text small" }, p.how)))
      : [h("p", { class: "muted-text small" }, "Only workspace admins.")]));
  });
  const whoBox = h("div", { class: "card pad who-reads" }, h("b", {}, "Who can read a table"), h("label", { class: "field" }, "Table", tableSel), who);
  if (!a.enabled) {
    return [h("div", { class: "problems" }, h("b", {}, "OneLake security isn't on for this lakehouse, or you can't manage it."),
      h("p", {}, "Until it is, Admins, Members and Contributors read every table and Viewers read through the SQL endpoint's permissions. Turn it on in the lakehouse in Fabric (Manage OneLake security)."),
      h("details", {}, h("summary", { class: "muted-text small" }, "Details"), h("pre", { class: "sql-block" }, friendlyError(a.problem).details || a.problem))),
    btn("Open the lakehouse in Fabric", "open", () => openInFabric(fabricItemLink(lh)), "tool outline"), whoBox];
  }
  return [
    h("div", { class: "row" }, h("span", { class: "muted-text grow" }, "Data access roles decide which tables Viewers (and people outside the workspace) can read. Admins, Members and Contributors read everything."),
      btn("New role", "add", () => roleDialog(a, lh, null, redraw), "tool primary small")),
    a.roles.length ? h("table", { class: "list" },
      h("thead", {}, h("tr", {}, ["Role", "Can read", "Members", ""].map((c) => h("th", {}, c)))),
      h("tbody", {}, a.roles.map((r) => h("tr", {},
        h("td", {}, h("b", {}, r.name)),
        h("td", {}, r.paths.map((p) => h("span", { class: "pill-s info" }, pathLabel(p)))),
        h("td", {}, r.entra.map((o) => h("div", { class: "small" }, names[o] || o)), r.item_members.map((m) => h("div", { class: "muted-text small" }, `Everyone with ${m} permission`))),
        h("td", { class: "actions" }, btn("Edit", null, () => roleDialog(a, lh, r, redraw), "tool small link"),
          btn("Delete", "delete", async () => {
            if (!(await confirmDialog(`Delete role ${r.name}?`, r.name === "DefaultReader" ? "DefaultReader is what lets people with read permission see every table. Without it they only see what other roles give them." : "Its members lose what this role let them read.", "Delete"))) return;
            const res = await attempt(() => rpc("access_delete_role", { workspace_id: lh.workspace_id, item_id: lh.item_id, name: r.name }), "Role not deleted");
            if (res) { accessPage.lh = res; toast(`Deleted role ${r.name}`, false, { log: true }); redraw(); }
          }, "tool small link"))))))
      : h("p", { class: "muted-text" }, "No roles yet."),
    whoBox,
  ];
}

function roleDialog(a, lh, role, redraw) {
  const name = h("input", { value: role?.name || "", placeholder: "SalesReaders" });
  const chosen = new Set(role?.paths || []);
  const members = new Set(role?.entra || []);
  const extra = [];
  const pathLabel = (p) => (p === "*" ? "Everything in the lakehouse" : p.replace(/^\/Tables\//, "").replace(/\//g, "."));
  const box = (value, label, set) => h("label", { class: "check" },
    h("input", { type: "checkbox", checked: set.has(value) ? true : null, onchange: (ev) => { ev.target.checked ? set.add(value) : set.delete(value); } }), label);
  const peopleList = h("div", { class: "check-list" });
  const drawPeople = () => fill(peopleList, ...[...a.people, ...extra].map((p) => box(p.id, `${p.name}${p.role ? ` (${p.role})` : ""}`, members)),
    ...[...members].filter((o) => ![...a.people, ...extra].some((p) => p.id === o)).map((o) => box(o, o, members)));
  drawPeople();
  const add = h("input", { placeholder: "name@contoso.com or an object ID" });
  showDialog(role ? `Edit role ${role.name}` : "New data access role", h("div", { class: "scroll estate" },
    h("label", { class: "field" }, "Name (letters and digits)", name),
    h("b", {}, "Can read"),
    h("div", { class: "check-list" }, box("*", pathLabel("*"), chosen), a.schemas.map((s) => box(s, `Schema ${pathLabel(s)}`, chosen)),
      a.tables.map((t) => box(t, pathLabel(t), chosen))),
    h("b", {}, "Members"), peopleList,
    h("div", { class: "row" }, h("label", { class: "field grow" }, "Someone else", add), btn("Add", "add", async () => {
      const p = await attempt(() => rpc("access_find_person", { text: add.value }), "Couldn't find them");
      if (!p) return;
      extra.push(p); members.add(p.id); add.value = ""; drawPeople();
    }, "tool outline small")),
    role?.item_members?.length ? h("p", { class: "muted-text small" }, `Kept as is: everyone with ${role.item_members.join(", ")} permission.`) : null),
  [btn("Cancel", null, closeDialog, "tool outline"), btn("Save role", "save", async () => {
    const res = await attempt(() => rpc("access_save_role", { workspace_id: lh.workspace_id, item_id: lh.item_id, name: name.value.trim(),
      paths: [...chosen], members: [...members], original_name: role?.name || null }), "Role not saved");
    if (!res) return;
    closeDialog();
    accessPage.lh = res;
    toast(`Saved role ${name.value.trim()}`, false, { log: true });
    redraw();
  }, "tool primary")]);
}
