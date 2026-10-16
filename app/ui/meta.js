// Metadata framework screens: entities, layers, surrogate keys, source systems, runs, environments and promote.
// Every table is a file in the metadata project; these screens read and edit those files
// through the sidecar, which also runs the gates and the engine.

state.meta = {
  project: null,       // summary from meta_project
  tab: "entities",     // entities | layers | keys | sources | runs | health | model | tests | environments
  env: (() => { try { return localStorage.getItem("fd.env") || "dev"; } catch { return "dev"; } })(),  // set from the top bar
  entity: null,        // open entity detail
  layersDraft: null,   // unsaved layer edits
  runs: null,
  keyTable: null,      // selected key table on the Surrogate keys tab
  source: null,        // selected source system id, or "new"
  sourceEnv: "dev",    // which environment's connection the form shows
  sourceDraft: null,   // unsaved edits to the selected source system
};

const ENV_LABEL = { dev: "Dev", test: "Test", prod: "Prod" };
const LAYER_COLORS = ["#8a5a2b", "#5f6b78", "#b08900", "#5c2e91", "#0e6f8a", "#a3420f", "#3b7d23", "#2b3a8a", "#8a5300", "#6b3fa0"];
const DRIFT_LABEL = { evolve: "Keep new columns", ignore_new: "Ignore new columns", strict: "Stop on new columns" };
const MODE_LABEL = { full: "Full reload", incremental: "Incremental", scd1: "Type 1 dimension", scd2: "Type 2 dimension" };

function layerColor(name) {
  const layers = state.meta.project?.layers || [];
  const i = layers.findIndex((l) => l.name === name);
  return LAYER_COLORS[(i < 0 ? 0 : i) % LAYER_COLORS.length];
}

function layerChip(name) {
  return h("span", { class: "layer-chip", style: `background:${layerColor(name)}` }, name);
}

function statusPill(status) {
  const cls = { ok: "pass", failed: "fail", needs_decision: "wrn", skipped: "neu", unavailable: "neu", running: "info" }[status] || "neu";
  const label = { ok: "Succeeded", failed: "Failed", needs_decision: "Needs a decision", skipped: "Skipped", running: "Running" }[status] || status;
  return h("span", { class: `pill-s ${cls}` }, label);
}

function gateList(errors) {
  return h("ul", { class: "gate-list" }, errors.map((e) => h("li", {}, e)));
}

// Gate errors arrive as "Blocked by the save gate:\n- a\n- b".
function showGateError(err, title = "That was blocked") {
  const text = String(err);
  const lines = text.split("\n");
  if (lines.length > 1 && lines[0].startsWith("Blocked by")) {
    showDialog(title, h("div", { class: "scroll" }, h("p", { class: "text" }, lines[0]),
      gateList(lines.slice(1).map((l) => l.replace(/^- /, "")))), [btn("Close", null, closeDialog, "tool primary")]);
  } else {
    const f = friendlyError(text);
    toast(f.text, true, { title, details: f.details });
  }
}

async function metaCall(method, params, title) {
  try {
    return await rpc(method, params);
  } catch (err) {
    showGateError(err, title);
    return undefined;
  }
}

async function loadMetaProject() {
  const p = await metaCall("meta_project", {});
  if (p) state.meta.project = p;
  return p;
}

// Metadata's tabs, in four groups so new metadata features join a group instead of a long row.
// [group id, label, [[tab id, label, preview feature id]]]
const META_TAB_GROUPS = [
  ["design", "Design", [["entities", "Entities"], ["layers", "Layers"], ["keys", "Surrogate keys"], ["sources", "Source systems"]]],
  ["runs", "Runs", [["runs", "Runs"]]],
  ["quality", "Quality", [["tests", "Tests", "tests"], ["health", "Table health", "health"]]],
  ["deploy", "Deploy", [["environments", "Environments and promote"], ["model", "Semantic model", "model"]]],
];

function metaGroupOf(tab) {
  return META_TAB_GROUPS.find(([, , tabs]) => tabs.some(([id]) => id === tab)) || META_TAB_GROUPS[0];
}

function goMeta(tab) {
  state.meta.tab = tab || state.meta.tab;
  go("metadata");
}

// ---------- main page ----------

async function renderMetadata() {
  setView(h("div", { class: "page" }, h("h1", {}, "Metadata"), h("p", { class: "muted-text" }, "Loading the metadata project...")));
  const p = await loadMetaProject();
  if (state.view !== "metadata" || !p) return;
  if (!p.path) return renderMetaSetup(p);
  renderEnvBar();  // the top bar's environment picker waits for the project
  const m = state.meta;
  // A hidden preview tab falls back to its group's first tab.
  const visible = (tabs) => tabs.filter(([, , previewId]) => !previewId || previewShown());
  if (!visible(metaGroupOf(m.tab)[2]).some(([id]) => id === m.tab)) m.tab = visible(metaGroupOf(m.tab)[2])[0]?.[0] || "entities";
  const group = metaGroupOf(m.tab);
  const tabLabel = (label, previewId) => {
    const node = h("span", {}, label);
    return previewId ? preview(previewId, node) : node;
  };
  const body = h("div", { class: "meta-body" });
  setView(h("div", { class: "page" },
    h("div", { class: "page-head" },
      h("div", {},
        h("h1", {}, "Metadata"),
        h("p", { class: "sub" }, "Every table is a file in ", h("span", { class: "mono" }, p.path),
          ". Metadata version ", h("span", { class: "mono" }, p.version), ". ", learnMore("metadata"))),
      h("div", { class: "head-actions" },
        btn("New entity", "add", newEntityDialog, "tool outline"),
        preview("schedules", btn("Schedule", "history", () => scheduleMetaRuns(m.env), "tool outline",
          { title: `Run every entity in ${ENV_LABEL[m.env]} on a schedule, on this PC or in Fabric` })),
        btn(inFabric(p, m.env) ? "Run all in Fabric" : "Run all", "play", () => runEntities(null), "tool primary",
          { disabled: p.errors.length ? true : null }))),
    problemsBanner(p.errors),
    h("div", { class: "subtabs meta-groups" }, META_TAB_GROUPS.filter(([, , tabs]) => visible(tabs).length).map(([id, label, tabs]) =>
      h("button", { class: `tab${group[0] === id ? " active" : ""}`, onclick: () => goMeta(visible(tabs)[0][0]) }, label))),
    visible(group[2]).length > 1 ? h("div", { class: "seg meta-tabs", role: "group", "aria-label": group[1] }, visible(group[2]).map(([id, label, previewId]) =>
      h("button", { class: m.tab === id ? "on" : null, onclick: () => goMeta(id) }, tabLabel(label, previewId)))) : null,
    body));
  const render = { entities: renderEntitiesTab, layers: renderLayersTab, keys: renderKeysTab, sources: renderSourcesTab,
    runs: renderRunsTab, health: renderHealthTab, model: renderModelTab, tests: renderTestsTab, environments: renderEnvTab }[m.tab];
  await render(body, p);
}

// Where an environment's runs execute: "local" (this PC) or "fabric" (the workspace's capacity).
function inFabric(p, env) {
  return (p.environments?.[env] || {}).compute === "fabric";
}

function problemsBanner(errors) {
  if (!errors.length) return null;
  return h("div", { class: "problems" },
    h("div", { class: "row" }, icon("warning", 18),
      h("b", {}, `${errors.length} problem${errors.length === 1 ? "" : "s"} to fix`),
      h("span", { class: "muted-text" }, "Runs and promote stay blocked until the project passes the save gate.")),
    gateList(errors));
}

function renderMetaSetup(p) {
  let preset = "3";
  const path = h("input", { id: "meta-path", value: p.default_path });
  const sample = h("input", { type: "checkbox", id: "meta-sample", checked: true });
  const presetButtons = Object.entries(p.presets).map(([id, layers]) => h("button", {
    class: `choice${id === preset ? " selected" : ""}`, "data-preset": id,
    onclick: (e) => {
      preset = id;
      e.currentTarget.parentElement.querySelectorAll(".choice").forEach((c) => c.classList.toggle("selected", c.dataset.preset === id));
      sample.disabled = id !== "3";
    } },
  h("span", { class: "main" }, h("span", { class: "n" }, `${id} layers`),
    h("span", { class: "w" }, layers.map((l) => l.name).join(" → ")))));
  const openPath = h("input", { id: "meta-open-path", placeholder: "C:\\Users\\you\\Documents\\metadata" });
  setView(h("div", { class: "page narrow" },
    h("h1", {}, "Set up a metadata project"),
    h("p", { class: "sub" }, "A metadata project is a folder you keep in Git. Each table is one file that says where its data comes from, how to clean and check it, and how to load it. Each layer gets its own lakehouse, so layers never mix."),
    h("div", { class: "card pad" },
      h("h2", { class: "flush" }, "Create a new project"),
      h("label", { class: "field" }, "Folder", path),
      h("div", { class: "field" }, "Layers (you can add, remove and reorder them later)", h("div", { class: "preset-row" }, presetButtons)),
      h("label", { class: "check" }, sample, "Add the sample tables (customers and orders, Bronze to Gold) so you can try it"),
      h("div", { class: "row end" }, btn("Create project", "add", async () => {
        const res = await metaCall("meta_init", { path: path.value.trim(), preset, sample: sample.checked && preset === "3" });
        if (res) { state.meta.project = res; toast("Project created"); render(); }
      }, "tool primary"))),
    h("div", { class: "card pad" },
      h("h2", { class: "flush" }, "Or open an existing project"),
      h("label", { class: "field" }, "Folder with a layers.toml", openPath),
      h("div", { class: "row end" }, btn("Open", "folderOpen", async () => {
        const res = await metaCall("meta_open", { path: openPath.value.trim() });
        if (res) { state.meta.project = res; render(); }
      }, "tool outline")))));
}

// ---------- entities ----------

function lastRuns(runs) {
  const last = {};
  for (const r of runs || []) if (!last[r.entity]) last[r.entity] = r;
  return last;
}

async function renderEntitiesTab(body, p) {
  const runs = await attempt(() => rpc("meta_runs", { env: state.meta.env, limit: 300 })) || [];
  const last = lastRuns(runs);
  const flow = h("div", { class: "flow" }, p.layers.map((l, i) => [i ? h("span", { class: "arrow" }, "→") : null,
    h("span", { class: "layer-chip big", style: `background:${layerColor(l.name)}` }, l.name,
      h("span", { class: "count" }, l.entities))]));
  const shown = p.entities.filter((e) => matches(`${e.name} ${e.owner} ${e.source}`));
  body.replaceChildren(flow, shown.length ? h("table", { class: "list" },
    h("thead", {}, h("tr", {}, ["Entity", "Layer", "Source", "Load", "Rules", "Custom code", `Last run (${ENV_LABEL[state.meta.env]})`, "Owner"]
      .map((c) => h("th", {}, c)))),
    h("tbody", {}, shown.map((e) => {
      const r = last[e.name];
      const rules = [e.clean && `${e.clean} clean`, e.derive && `${e.derive} derive`, `${e.checks} check${e.checks === 1 ? "" : "s"}`].filter(Boolean).join(" · ");
      return h("tr", { class: "click", onclick: () => openEntity(e.name) },
        h("td", {}, h("span", { class: "item" }, icon("table", 18), h("span", { class: "mono" }, e.name))),
        h("td", {}, layerChip(e.layer)),
        h("td", { class: "mono small" }, e.source),
        h("td", {}, MODE_LABEL[e.mode] || e.mode, e.watermark ? h("span", { class: "muted-text" }, ` · ${e.watermark}`) : null),
        h("td", {}, rules),
        h("td", {}, e.custom.length ? e.custom.map((c) => h("span", { class: "pill-s cus" }, c)) : h("span", { class: "muted-text" }, "none")),
        h("td", {}, r ? [statusPill(r.status), h("span", { class: "muted-text" }, " ", when(r.started_at))]
          : h("span", { class: "muted-text" }, "never")),
        h("td", {}, e.owner));
    }))) : h("div", { class: "empty" }, h("span", { class: "title" }, "No entities yet"),
    h("span", { class: "text" }, "Add one from a template. It starts out meeting your layer's standards."),
    btn("New entity", "add", newEntityDialog, "tool primary")));
}

function newEntityDialog() {
  const p = state.meta.project;
  const template = h("select", { id: "ne-template" }, Object.entries(p.templates).map(([id, d]) => h("option", { value: id }, d)));
  const layer = h("select", { id: "ne-layer" }, p.layers.map((l) => h("option", { value: l.name }, `${l.name} (${l.lakehouse})`)));
  const table = h("input", { id: "ne-table", placeholder: "orders" });
  const source = h("input", { id: "ne-source", list: "ne-entities", placeholder: "landing/orders/*.csv" });
  const keys = h("input", { id: "ne-keys", placeholder: "order_id" });
  const list = h("datalist", { id: "ne-entities" }, p.entities.map((e) => h("option", { value: e.name })));
  const hint = h("p", { class: "text" });
  const sync = () => {
    const t = template.value;
    source.placeholder = t === "files" ? "landing/orders/*.csv (under this layer's lakehouse Files)" : "an entity, like silver.orders";
    hint.textContent = t === "files" ? "Columns are read from the first matching file in Dev, if there is one."
      : t === "custom" ? "Creates transforms/<table>.py and a test for it. The source is optional."
        : "Columns are copied from the source entity's contract.";
    if (["dim1", "dim2", "fact"].includes(t) && !table.value) table.placeholder = t === "fact" ? "fact_sales" : "dim_customer";
  };
  template.addEventListener("change", sync);
  sync();
  showDialog("New entity", h("div", { class: "scroll" },
    h("label", { class: "field" }, "Template", template),
    h("div", { class: "row" }, h("label", { class: "field grow" }, "Layer", layer), h("label", { class: "field grow" }, "Table name", table)),
    h("label", { class: "field" }, "Source", source, list),
    h("label", { class: "field" }, "Key columns (comma separated)", keys),
    hint), [
    btn("Cancel", null, closeDialog, "tool outline"),
    btn("Create", null, async () => {
      if (!table.value.trim()) return toast("Give the table a name", true);
      const res = await metaCall("meta_new_entity", { template: template.value, name: `${layer.value}.${table.value.trim()}`,
        source: source.value.trim(), keys: keys.value.split(",").map((k) => k.trim()).filter(Boolean) }, "Couldn't create the entity");
      if (res) { closeDialog(); toast(`Created ${res.name}`); openEntity(res.name); }
    }, "tool primary"),
  ]);
}

// ---------- entity detail ----------

async function openEntity(name) {
  state.meta.entity = { name, loading: true };
  go("entity");
}

function stepsFor(e) {
  const steps = [["Read", "framework"], ["Contract", "framework"]];
  if (e.surrogate_keys?.length) steps.push([`Surrogate keys · ${e.surrogate_keys.length}`, "framework"]);
  if (e.clean) steps.push([`Clean · ${e.clean}`, "framework"]);
  if (e.derive) steps.push([`Derive · ${e.derive}`, "expression"]);
  for (const c of e.custom) steps.push([c, "your code"]);
  steps.push([`Checks · ${e.checks}`, "framework"], [`Write · ${MODE_LABEL[e.mode] || e.mode}`, "framework"], ["Log", "framework"]);
  return steps;
}

async function renderEntity() {
  const name = state.meta.entity?.name;
  setView(h("div", { class: "page" }, h("p", { class: "muted-text" }, `Loading ${name}...`)));
  const e = await metaCall("meta_entity", { name, env: state.meta.env });
  if (state.view !== "entity" || !e) return;
  state.meta.entity = e;
  if (!state.meta.project) { await loadMetaProject(); renderEnvBar(); }
  const editor = h("textarea", { class: "toml", id: "entity-toml", spellcheck: "false" });
  editor.value = e.text;
  const dirty = () => editor.value !== e.text;
  const errBox = h("div", {}, e.errors.length ? h("div", { class: "problems" }, h("b", {}, "This entity doesn't pass the save gate yet"), gateList(e.errors)) : null);

  const save = async () => {
    const res = await metaCall("meta_save_entity", { name: e.name, text: editor.value }, "Not saved");
    if (!res) return;
    toast(res.errors.length ? `Saved with ${res.errors.length} problem${res.errors.length === 1 ? "" : "s"} to fix` : "Saved. It passes the save gate.");
    if (res.name !== e.name) state.meta.entity = { name: res.name };
    render();
  };
  editor.addEventListener("keydown", (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key === "s") { ev.preventDefault(); save(); }
    if (ev.key === "Tab") { ev.preventDefault(); editor.setRangeText("    ", editor.selectionStart, editor.selectionEnd, "end"); }
  });

  const cols = Object.entries(e.columns).map(([c, t]) => h("div", { class: "col-row" },
    h("span", { class: "mono" }, c), h("span", { class: "muted-text mono" }, t),
    e.keys.includes(c) ? h("span", { class: "pill-s neu" }, "key") : null,
    (e.surrogate_keys || []).some((k) => k.column === c) ? h("span", { class: "pill-s info" }, "LH_Keys") : null,
    e.derived.includes(c) ? h("span", { class: "pill-s pass" }, "derived") : null,
    e.hook_adds.includes(c) ? h("span", { class: "pill-s cus" }, "hook") : null));

  // Runs of this table in every environment; the current one is the fallback while they load.
  const runLines = (list) => list.length ? list.slice(0, 6).map((r) => runLine(r, () => showRun(r.run_id, r.env)))
    : [h("p", { class: "muted-text" }, e.runs?.[0]?.message || "Not run yet.")];
  const recentRuns = h("div", { class: "card pad" },
    h("div", { class: "row" }, h("b", { class: "grow" }, "Recent runs"), btn("All in Monitor", null, () => go("monitor"), "tool link small")),
    ...runLines((e.runs || []).filter((r) => r.run_id).map((r) => ({ ...r, env: r.env || state.meta.env }))));
  rpc("meta_runs_all", { limit: 300 }).then((all) => {
    const mine = (all.runs || []).filter((r) => r.entity === e.name);
    if (mine.length && recentRuns.isConnected) recentRuns.replaceChildren(recentRuns.firstChild, ...runLines(mine));
  }).catch(() => {});
  setView(h("div", { class: "page" },
    h("div", { class: "page-head" },
      h("div", {},
        h("div", { class: "row" }, h("button", { class: "tool link", onclick: () => goMeta("entities") }, "Metadata"), h("span", { class: "muted-text" }, "/")),
        h("h1", { class: "row" }, h("span", { class: "mono" }, e.name), layerChip(e.layer)),
        h("p", { class: "sub" }, `${MODE_LABEL[e.mode] || e.mode}${e.watermark ? ` on ${e.watermark}` : ""} · owner ${e.owner || "not set"} · `,
          h("span", { class: "mono" }, e.path))),
      h("div", { class: "head-actions" },
        btn("Delete", "delete", async () => {
          if (!(await confirmDialog(`Delete ${e.name}?`, "The entity file is removed. Its Delta table stays in the lakehouse until you delete it there.", "Delete"))) return;
          const res = await metaCall("meta_delete_entity", { name: e.name }, "Not deleted");
          if (res) { toast(`Deleted ${e.name}`); goMeta("entities"); }
        }, "tool outline"),
        btn("Profile", "dataBarVertical", () => profileEntity(e.name), "tool outline"),
        btn("Compare", "flow", () => compareDialog(e.name), "tool outline"),
        preview("history", btn("History", "history", () => historyDialog(e.name), "tool outline", { title: "Every version of the table; restore an earlier one" })),
        btn("Rebuild with downstream", "sync", () => runEntities([e.name], { cascade: true }), "tool outline"),
        btn(inFabric(state.meta.project, state.meta.env) ? `Run in Fabric (${ENV_LABEL[state.meta.env]})` : `Run in ${ENV_LABEL[state.meta.env]}`, "play",
          () => (dirty() ? save() : Promise.resolve()).then(() => runEntities([e.name])), "tool primary"))),
    h("div", { class: "card pad" },
      h("div", { class: "row" }, h("b", {}, "What happens on every run"), h("span", { class: "muted-text" }, "Grey steps are the framework. Purple is your code, at a fixed point.")),
      h("div", { class: "steps" }, stepsFor(e).map(([label, kind], i) => [i ? h("span", { class: "arrow" }, "→") : null,
        h("span", { class: `step ${kind === "your code" ? "custom" : kind === "expression" ? "expr" : ""}` },
          h("span", { class: "n" }, label), h("span", { class: "k" }, kind))]))),
    h("div", { class: "entity-grid" },
      h("div", { class: "card pad grow" },
        h("div", { class: "row" }, h("b", {}, "Config"), h("span", { class: "muted-text" }, `metadata/entities/${e.name}.toml`),
          h("span", { class: "spacer" }), btn("Save", "save", save, "tool primary small")),
        errBox, editor),
      h("div", { class: "side" },
        h("div", { class: "card pad" }, h("b", {}, "Contract"), h("div", { class: "cols" }, cols)),
        h("div", { class: "card pad" }, h("b", {}, "Lineage"),
          h("div", { class: "lineage3" },
            h("div", {}, h("span", { class: "muted-text small" }, "Reads"),
              e.reads.length ? e.reads.map((r) => h("button", { class: "node", onclick: () => openEntity(r) }, entityName(r)))
                : h("span", { class: "node plain" }, e.source || "no source"),
              shortcutLineage(e)),
            h("div", {}, h("span", { class: "muted-text small" }, "This table"), h("span", { class: "node self" }, entityName(e.name))),
            h("div", {}, h("span", { class: "muted-text small" }, "Feeds"),
              e.impact.length ? e.impact.map((r) => h("button", { class: "node", onclick: () => openEntity(r) }, entityName(r)))
                : h("span", { class: "muted-text small" }, "Nothing reads it yet")))),
        driftCard(e),
        freshnessCard(e),
        recentRuns)),
    quarantineCard(e),
    keysCard(e),
    e.preview ? h("div", { class: "card pad" }, h("b", {}, `Data in ${ENV_LABEL[state.meta.env]} · ${e.preview.total} rows`), dataTable(e.preview)) : null));
}

// The MERGE that starts this entity's template notebook.
function keysCard(e) {
  if (!e.surrogate_keys?.length) return null;
  return h("div", { class: "card pad" },
    h("div", { class: "row wrap" }, h("b", {}, "Cell 1: surrogate keys"),
      h("span", { class: "muted-text" }, e.surrogate_keys.map((k) => `${k.column} from sk.${k.table} on ${k.business_key.join(" + ")}`).join(" · ")),
      h("span", { class: "spacer" }),
      e.keys_sql ? btn("Copy", null, () => attempt(() => navigator.clipboard.writeText(e.keys_sql), "Could not copy").then(() => toast("Copied")), "tool outline small") : null,
      e.keys_sql ? btn("Create template notebook", "notebook", async () => {
        const res = await metaCall("meta_key_notebook", { name: e.name, env: state.meta.env }, "No notebook created");
        if (res) { toast(`Created ${res.name}`); openNotebook(res.name); }
      }, "tool primary small") : null),
    e.keys_sql ? h("pre", { class: "sql-block" }, e.keys_sql)
      : h("p", { class: "muted-text" }, "Fix the problems above to see the MERGE."));
}

function confirmDialog(title, text, okLabel = "OK") {
  return new Promise((resolve) => {
    const done = (v) => { closeDialog(); resolve(v); };
    showDialog(title, h("p", { class: "text" }, text), [
      btn("Cancel", null, () => done(false), "tool outline"), btn(okLabel, null, () => done(true), "tool primary")]);
  });
}

function dataTable(preview) {
  return h("div", { class: "output" }, h("table", {},
    h("thead", {}, h("tr", {}, preview.columns.map((c) => h("th", {}, c)))),
    h("tbody", {}, preview.rows.slice(0, 50).map((r) => h("tr", {}, preview.columns.map((c) => h("td", {}, r[c] === null ? "null" : String(r[c]))))))));
}

// ---------- running ----------

async function runEntities(names, opts = {}) {
  const p = state.meta.project;
  const label = names ? names.join(", ") : `all ${p.entities.length} entities`;
  const env = state.meta.env;
  if (inFabric(p, env)) {
    if (!state.user && !(await signIn())) return;
    toast(`Sending ${label} to Fabric for ${ENV_LABEL[env]}...`);
  } else {
    toast(`Running ${label} in ${ENV_LABEL[env]}...`);
  }
  const results = await metaCall("meta_run", { names, env, backfill: opts.backfill || null, cascade: !!opts.cascade }, "Run blocked");
  if (!results) return;
  if (results[0]?.status === "submitted") return watchMetaFabricRun(results[0], names ? label : "Run all");
  const decision = results.find((r) => r.status === "needs_decision");
  logRunActivity(`${names ? label : "Run all"} · ${ENV_LABEL[env]}`, results, "on this PC", env);
  showRunResults(results, decision);
  render();
}

// One tray entry for a finished batch: how many succeeded, and the results to reopen.
function logRunActivity(title, results, where, env, id) {
  const ok = results.filter((r) => r.status === "ok").length;
  const bad = results.find((r) => r.status === "failed" || r.status === "needs_decision");
  return logActivity({ id, env, title, status: bad ? "failed" : "ok", results, monitor: true,
    text: `${ok} of ${results.length} succeeded ${where}.${bad ? ` ${bad.entity}: ${runReason(bad)}` : ""}`, at: new Date().toISOString() });
}

// A run on Fabric capacity: say it started, then show the results when the runner has saved them.
async function watchMetaFabricRun(run, label) {
  const id = `fabric:${run.job_id}`;
  const title = `${label} · ${ENV_LABEL[run.env]}`;
  logActivity({ id, env: run.env, request: run.request_id, status: "running", title, text: "In Fabric. You can leave this page; the bell follows it.", monitor: true });
  toast(`${ENV_LABEL[run.env]} is running in Fabric. The bell follows it until it finishes.`);
  render();
  for (let wait = 10; ; wait = Math.min(wait * 1.5, 60)) {
    await new Promise((r) => setTimeout(r, wait * 1000));
    const res = await attempt(() => rpc("meta_fabric_result", { request_id: run.request_id }));
    if (!res) return;
    if (!["Completed", "Failed", "Cancelled", "Deduped"].includes(res.status)) continue;
    const here = state.view === "metadata" || state.view === "entity";
    if (res.results) {
      logRunActivity(title, res.results, "in Fabric", run.env, id);
      if (here) showRunResults(res.results, res.results.find((r) => r.status === "needs_decision"));
      else toast(`${title} finished in Fabric. The bell has the results.`, false);
    } else {
      logActivity({ id, status: "failed", text: res.message || `The run ${res.status.toLowerCase()} before it saved any results.` });
      if (here) showDialog(`The Fabric run ${res.status.toLowerCase()}`, h("div", { class: "scroll" },
        h("p", { class: "text" }, res.message || "The runner stopped before it saved any results."),
        h("p", { class: "muted-text" }, "Open it from Monitor to see the notebook's output in Fabric.")),
      [btn("Close", null, closeDialog, "tool primary")]);
    }
    if (here) render();
    return;
  }
}

function showRunResults(results, decision) {
  const rows = results.map((r) => h("div", { class: "result-row" },
    statusPill(r.status), h("span", { class: "mono" }, r.entity),
    h("span", { class: "muted-text" }, r.status === "ok" ? `${r.rows_written} rows written${r.quarantined ? `, ${r.quarantined} quarantined` : ""}`
      : r.status === "needs_decision" ? "Waiting for your choice below" : r.message),
    r.maintenance === "queued" ? h("span", { class: "pill-s info", title: "Fabric's table maintenance job: OPTIMIZE with V-Order, then VACUUM" }, "Optimize queued") : null,
    r.maintenance === "Failed" ? h("span", { class: "pill-s wrn", title: r.maintenance_message }, "Optimize not started") : null,
    r.run_id && r.status !== "needs_decision" ? btn("Details", null, () => showRun(r.run_id), "tool link small") : null));
  const actions = [btn("Close", null, closeDialog, decision ? "tool outline" : "tool primary")];
  let body = h("div", { class: "scroll" }, rows);
  if (decision) {
    const opts = decision.options;
    body = h("div", { class: "scroll" }, rows, h("div", { class: "decision" },
      h("b", {}, `${decision.entity} needs a decision`), h("p", { class: "text" }, decision.message),
      opts.includes("none") ? h("button", { class: "choice", onclick: () => { closeDialog(); runEntities([decision.entity], { backfill: "none" }); } },
        h("span", { class: "main" }, h("span", { class: "n" }, "Apply to new rows only"),
          h("span", { class: "w" }, "Rows already loaded keep the old logic. _run_id shows which rows used which version."))) : null,
      opts.includes("full") ? h("button", { class: "choice", onclick: () => { closeDialog(); runEntities([decision.entity], { cascade: true }); } },
        h("span", { class: "main" }, h("span", { class: "n" }, "Rebuild it and everything downstream"),
          h("span", { class: "w" }, "Reprocesses the full source, then every table that reads it, in order.")), h("span", { class: "tag" }, "Recommended")) : null,
      opts.includes("migrate") ? h("button", { class: "choice", onclick: () => { closeDialog(); runEntities([decision.entity], { backfill: "migrate" }); } },
        h("span", { class: "main" }, h("span", { class: "n" }, "Migrate to Type 2"),
          h("span", { class: "w" }, "Keeps every surrogate key and starts each row's history at 1900-01-01."))) : null));
  }
  const ok = results.filter((r) => r.status === "ok").length;
  showDialog(decision ? "One table needs a decision" : `${ok} of ${results.length} succeeded`, body, actions);
}

async function showRun(runId, env = state.meta.env) {
  const r = await metaCall("meta_run_detail", { run_id: runId, env });
  if (!r) return;
  showDialog(`${r.entity} · ${new Date(r.started_at).toLocaleString()}`, h("div", { class: "scroll" },
    h("div", { class: "row wrap" }, statusPill(r.status), h("span", { class: "muted-text" }, `Run ${r.run_id} · metadata ${r.metadata} · ${r.seconds}s`)),
    r.message ? h("p", { class: "text" }, r.message) : null,
    h("table", { class: "mini" }, h("thead", {}, h("tr", {}, ["Step", "Rows", "Time", "Result"].map((c) => h("th", {}, c)))),
      h("tbody", {}, r.steps.map((s) => h("tr", {}, h("td", {}, s.name), h("td", { class: "n" }, s.rows), h("td", { class: "n" }, `${s.seconds}s`), h("td", {}, s.detail))))),
    r.dq.length ? [h("b", {}, "Checks"), h("table", { class: "mini" }, h("tbody", {}, r.dq.map((d) => h("tr", {},
      h("td", {}, d.check), h("td", {}, d.failed ? h("span", { class: "pill-s " + (d.action === "stop" ? "fail" : "wrn") }, `${d.failed} failed`) : h("span", { class: "pill-s pass" }, "passed")),
      h("td", { class: "muted-text" }, d.action)))))] : null), [btn("Close", null, closeDialog, "tool primary")]);
}

async function renderRunsTab(body) {
  const runs = await attempt(() => rpc("meta_runs", { env: state.meta.env, limit: 200 })) || [];
  const shown = runs.filter((r) => matches(`${r.entity} ${r.status} ${r.run_id}`));
  body.replaceChildren(shown.length ? h("table", { class: "list" },
    h("thead", {}, h("tr", {}, ["Status", "Entity", "Ran on", "When", "Rows read", "Written", "Quarantined", "Took", "Metadata"].map((c) => h("th", {}, c)))),
    h("tbody", {}, shown.map((r) => h("tr", { class: "click", onclick: () => showRun(r.run_id) },
      h("td", {}, statusPill(r.status)),
      h("td", {}, h("div", { class: "mono" }, r.entity), r.backfill ? h("div", { class: "muted-text small" }, `backfill: ${r.backfill}`) : null,
        runReason(r) ? h("div", { class: "reason" }, runReason(r)) : null),
      h("td", {}, String(r.batch_id || "").startsWith("fabric-") ? "Fabric" : "This PC"), h("td", {}, when(r.started_at)),
      h("td", { class: "n" }, r.rows_read), h("td", { class: "n" }, r.rows_written), h("td", { class: "n" }, r.quarantined),
      h("td", { class: "n" }, took(r.seconds)), h("td", { class: "mono" }, r.metadata))))) :
    h("p", { class: "muted-text" }, `No runs in ${ENV_LABEL[state.meta.env]} yet.`));
}

// ---------- layers ----------

function renderLayersTab(body, p) {
  const m = state.meta;
  if (!m.layersDraft) m.layersDraft = p.layers.map((l) => ({ ...l }));
  const L = m.layersDraft;
  const counts = Object.fromEntries(p.layers.map((l) => [l.name, l.entities]));
  const changed = JSON.stringify(L.map(stripLayer)) !== JSON.stringify(p.layers.map(stripLayer));
  const rerender = () => renderLayersTab(body, p);
  const move = (i, d) => { const j = i + d; if (j < 0 || j >= L.length) return; [L[i], L[j]] = [L[j], L[i]]; rerender(); };
  body.replaceChildren(
    h("div", { class: "flow" }, L.map((l, i) => [i ? h("span", { class: "arrow" }, "→") : null,
      h("span", { class: "layer-chip big", style: `background:${LAYER_COLORS[i % LAYER_COLORS.length]}` }, l.name)])),
    h("table", { class: "list" },
      h("thead", {}, h("tr", {}, ["#", "Layer", "Lakehouse in each environment", "Can read from", "Schema drift", "Required checks", "Surrogate keys", "Keep history", "Entities", ""].map((c) => h("th", {}, c)))),
      h("tbody", {}, L.map((l, i) => h("tr", {},
        h("td", {}, i + 1),
        h("td", {}, h("b", {}, l.name), l.description ? h("div", { class: "muted-text" }, l.description) : null),
        h("td", { class: "mono small" }, `LH_${l.name[0].toUpperCase()}${l.name.slice(1)}`),
        h("td", {}, i === 0 ? h("span", { class: "muted-text" }, "Files and sources") :
          h("select", { "aria-label": "Can read from", onchange: (ev) => { l.reads_from = ev.target.value ? [ev.target.value] : []; rerender(); } },
            h("option", { value: "" }, "Any earlier layer"),
            L.slice(0, i).map((o) => h("option", { value: o.name, selected: l.reads_from?.[0] === o.name ? true : null }, `Only ${o.name}`)))),
        h("td", {}, h("select", { "aria-label": "Schema drift", onchange: (ev) => { l.drift = ev.target.value; rerender(); } },
          Object.entries(DRIFT_LABEL).map(([id, label]) => h("option", { value: id, selected: l.drift === id ? true : null }, label)))),
        h("td", {}, ["key_not_null", "key_unique"].map((c) => h("label", { class: "check inline" },
          h("input", { type: "checkbox", checked: l.required_checks.includes(c) ? true : null,
            onchange: (ev) => { l.required_checks = ev.target.checked ? [...new Set([...l.required_checks, c])] : l.required_checks.filter((x) => x !== c); rerender(); } }),
          c === "key_not_null" ? "Key not null" : "Key unique"))),
        h("td", {}, h("label", { class: "check inline" },
          h("input", { type: "checkbox", checked: l.require_keys ? true : null, onchange: (ev) => { l.require_keys = ev.target.checked; rerender(); } }),
          "Required")),
        h("td", {}, h("label", { class: "row tight", title: "How long earlier versions can be restored. VACUUM keeps their files this long; Fabric needs at least 7 days." },
          h("input", { type: "number", min: "7", max: "3650", step: "1", class: "days", value: l.retention_days ?? 7, "aria-label": "Days of history",
            onchange: (ev) => { l.retention_days = Number(ev.target.value); rerender(); } }), "days")),
        h("td", {}, counts[l.name] || h("span", { class: "muted-text" }, "none")),
        h("td", {}, h("span", { class: "row tight" },
          h("button", { class: "tool small", title: "Move up", onclick: () => move(i, -1) }, icon("up", 14)),
          h("button", { class: "tool small", title: "Move down", onclick: () => move(i, 1) }, icon("down", 14)),
          h("button", { class: "tool small danger", title: counts[l.name] ? "Has entities" : "Remove layer", disabled: counts[l.name] ? true : null,
            onclick: () => { if (L.length <= 1) return toast("A project needs at least one layer", true); L.splice(i, 1); rerender(); } }, "Remove"))))))),
    h("div", { class: "row" },
      btn("Add layer", "add", async () => {
        if (L.length >= 10) return toast("A project can have at most 10 layers", true);
        const name = await askText("Add a layer", "Name (lowercase, like platinum)", "", "Add");
        if (!name) return;
        L.push({ name: name.toLowerCase(), drift: "strict", required_checks: ["key_unique"], reads_from: [], description: "", retention_days: 7 });
        rerender();
      }, "tool outline"),
      h("span", { class: "spacer" }),
      changed ? btn("Discard changes", null, () => { m.layersDraft = null; rerender(); }, "tool outline") : null,
      btn("Save layers", "save", async () => {
        const res = await metaCall("meta_set_layers", { layers: L.map(stripLayer) }, "Layers not saved");
        if (res) { state.meta.project = res; m.layersDraft = null; toast("Layers saved"); render(); }
      }, "tool primary", { disabled: changed ? null : true })),
    h("div", { class: "info-cards" },
      infoCard("No mixing", "A table writes only to its own layer's lakehouse. It reads from earlier layers, or from its own layer when that makes no loop, like a fact reading its dimensions. Reading a later layer is always rejected."),
      infoCard("Lakehouses are created for you", "Each layer is a schema-enabled lakehouse named LH_<Layer> in every environment. Dev keeps them on this PC; Test and Prod get them on the first promote."),
      infoCard("Each layer has its own policy", "Schema drift and required checks are set per layer, so a lenient Bronze and a strict Gold can sit in one flow. Reordering is refused if a table would end up reading a later layer."),
      infoCard("Surrogate keys can be required", "Tick Required on a layer, like silver, and every table in it must take its keys from LH_Keys with a [[keys]] block."),
      infoCard("Undo a bad load", "Every run is a new Delta version. Keep history sets how many days back a table can be restored from its History; Optimize keeps that much when it vacuums.")));
}

function stripLayer(l) {
  return { name: l.name, drift: l.drift, required_checks: [...l.required_checks].sort(), reads_from: l.reads_from || [],
    description: l.description || "", require_keys: !!l.require_keys, retention_days: Number(l.retention_days ?? 7) };
}

function infoCard(title, text) {
  return h("div", { class: "card pad info" }, h("b", {}, title), h("p", { class: "muted-text" }, text));
}

// ---------- environments and promote ----------

async function renderEnvTab(body, p) {
  const envs = p.environments;
  const checks = {};
  const card = (env) => {
    const cfg = envs[env] || {};
    const local = (cfg.target || (env === "dev" ? "local" : "onelake")) === "local";
    const wsName = cfg.workspace_name || cfg.workspace_id;
    const lakehouses = p.layers.map((l) => l.lakehouse).concat("LH_Meta").join(" · ");
    const onWorkspace = !local && !!cfg.workspace_id;
    const where = local
      ? (wsName ? `Offline: tables stay in the project's .local folder. Switch Tables in back to Workspace to use ${wsName}.` : "Tables stay in the project's .local folder on this PC until you assign a workspace.")
      : !wsName ? `Assign a workspace before ${ENV_LABEL[env]} can run. You can still run it on this PC.`
        : cfg.compute === "fabric" ? `Fabric runs it on ${wsName}'s capacity and writes its lakehouses.`
          : `This PC runs it, reading and writing ${wsName}'s lakehouses in OneLake.`;
    return h("div", { class: "card pad env-card" },
      h("div", { class: "row" }, h("b", {}, ENV_LABEL[env]), h("span", { class: "spacer" }),
        btn(wsName ? "Change" : "Assign workspace", null, () => pickEnvWorkspace(env, p), "tool link small")),
      h("div", { class: "env-ws" }, icon("workspace", 16),
        wsName ? h("b", { title: cfg.workspace_id || null }, wsName) : h("span", { class: "muted-text" }, "No workspace yet")),
      h("div", { class: "muted-text small" }, lakehouses),
      h("div", { class: "compute-row" }, h("span", { class: "muted-text small" }, "Tables in"),
        h("div", { class: "seg" },
          h("button", { class: onWorkspace ? "on" : null, disabled: !cfg.workspace_id ? true : null,
            title: !cfg.workspace_id ? `Assign a workspace to ${ENV_LABEL[env]} first.` : null,
            onclick: !onWorkspace && cfg.workspace_id ? () => setEnv(env, { target: "onelake" }) : null }, "Workspace"),
          h("button", { class: local ? "on" : null, onclick: local ? null : () => setEnv(env, { target: "local" }) }, "This PC only"))),
      computeChoice(env, local, cfg.compute === "fabric"),
      h("p", { class: "muted-text small" }, where));
  };
  const promoteRow = (source, target) => {
    const box = h("div", { class: "muted-text" }, "Checking the promote gate...");
    attempt(() => rpc("meta_promote_check", { source, target })).then((res) => {
      if (!res) return;
      checks[target] = res;
      box.replaceWith(res.errors.length ? h("div", { class: "problems" }, h("b", {}, `${res.errors.length} thing${res.errors.length === 1 ? "" : "s"} to do first`), gateList(res.errors))
        : h("div", { class: "pass-box" }, icon("ok", 16), `Ready: metadata ${res.version} passed every check in ${ENV_LABEL[source]}${res.tests?.passed ? `, and all ${res.tests.passed} test${res.tests.passed === 1 ? "" : "s"} passed` : ""}.`));
    });
    return h("div", { class: "card pad" },
      h("div", { class: "row" }, h("b", {}, `Promote ${ENV_LABEL[source]} to ${ENV_LABEL[target]}`), h("span", { class: "spacer" }),
        btn(`Promote to ${ENV_LABEL[target]}`, "publish", async () => {
          if (!state.user && !(await signIn())) return;
          toast(`Promoting to ${ENV_LABEL[target]}...`);
          const res = await metaCall("meta_promote", { source, target }, "Promote blocked");
          if (res) { toast(`${ENV_LABEL[target]} is now on metadata ${res.version}`, false, { log: true }); render(); }
        }, "tool primary small")),
      h("p", { class: "muted-text" }, "Needs the project to pass the save gate, its tests to pass, and every table's last run here to be green on this exact metadata version. Lakehouses that don't exist yet are created."),
      box, promoteImpact(source, target));
  };
  body.replaceChildren(h("div", { class: "env-cards" }, ["dev", "test", "prod"].map(card)),
    promoteRow("dev", "test"), promoteRow("test", "prod"));
}

// This PC or Fabric runs the environment's entities. Fabric can only reach tables in OneLake.
function computeChoice(env, local, fabric) {
  const option = (value, label, on) => h("button", {
    class: on ? "on" : null, disabled: value === "fabric" && local ? true : null,
    title: value === "fabric" && local ? `${ENV_LABEL[env]} keeps its tables on this PC only. Switch Tables in to Workspace first.` : null,
    onclick: on ? null : () => setCompute(env, value),
  }, label);
  return h("div", { class: "compute-row" }, h("span", { class: "muted-text small" }, "Runs on"),
    h("div", { class: "seg" }, option("local", "This PC", !fabric), preview("fabricRun", option("fabric", "Fabric", fabric)) || (fabric ? option("fabric", "Fabric", fabric) : null)),
    preview("schedules", btn("Schedules", "history", () => scheduleMetaRuns(env), "tool link small")));
}

async function setCompute(env, compute) {
  if (compute === "fabric" && !state.user && !(await signIn())) return;
  const res = await metaCall("meta_set_compute", { env, compute });
  if (!res) return;
  state.meta.project = res;
  toast(compute === "fabric" ? `${ENV_LABEL[env]} now runs in Fabric. The first run uploads the project to LH_Meta and publishes a runner notebook.`
    : `${ENV_LABEL[env]} now runs on this PC.`);
  render();
}

function scheduleMetaRuns(env) {
  const p = state.meta.project;
  const fabric = inFabric(p, env);
  openScheduleDialog({
    title: `Schedule ${ENV_LABEL[env]} runs`,
    mode: fabric ? "fabric" : "pc",
    pc: { target: { kind: "meta", env }, query: { kind: "meta", env },
          intro: `This PC runs every entity in ${ENV_LABEL[env]}, in layer order, from the project files as they are when it runs.` },
    fabric: {
      unavailable: fabric ? "" : `${ENV_LABEL[env]} runs on this PC. Set Runs on to Fabric in Environments to schedule it there.`,
      intro: `Fabric runs every entity in ${ENV_LABEL[env]}, in layer order, on its own capacity, using the project as it was last uploaded (each run or promote from this app uploads it).`,
      adding: `Uploading the project and adding the schedule for ${ENV_LABEL[env]}...`,
      added: "Scheduled. Fabric runs every entity on its own capacity, even when this PC is off.",
      list: () => rpc("meta_schedules", { env }),
      add: (schedule) => rpc("meta_add_schedule", { env, schedule }),
      remove: (id) => rpc("meta_delete_schedule", { env, schedule_id: id }),
    },
  });
}

// Every environment has a workspace, whether this PC or Fabric runs it: pick one, or create one.
async function pickEnvWorkspace(env, p) {
  const current = p.environments?.[env]?.workspace_id;
  const list = h("div", { class: "scroll" }, h("p", { class: "muted-text" }, state.user ? "Loading workspaces..." : "Sign in to pick a Fabric workspace."));
  showDialog(`Workspace for ${ENV_LABEL[env]}`, list, [btn("Cancel", null, closeDialog, "tool outline")]);
  if (!state.user && !(await signIn())) return;
  const workspaces = await loadWorkspaces();
  const taken = Object.fromEntries(["dev", "test", "prod"].filter((e) => e !== env && p.environments?.[e]?.workspace_id)
    .map((e) => [p.environments[e].workspace_id, ENV_LABEL[e]]));
  list.replaceChildren(
    h("p", { class: "muted-text" }, `${ENV_LABEL[env]}'s lakehouses (one per layer, plus LH_Meta) live in this workspace, whether this PC or Fabric runs it.`),
    h("button", { class: "choice", onclick: () => createEnvWorkspace(env, p) }, icon("add", 20),
      h("span", { class: "main" }, h("span", { class: "n" }, "Create a new workspace"), h("span", { class: "w" }, "Made with your Fabric sign-in, on a capacity you pick"))),
    ...workspaces.map((w) => h("button", { class: `choice${w.id === current ? " selected" : ""}`,
      onclick: () => setEnv(env, { workspace_id: w.id, workspace_name: w.displayName }) },
    icon("workspace", 20), h("span", { class: "main" }, h("span", { class: "n" }, w.displayName),
      h("span", { class: "w" }, taken[w.id] ? `Also used by ${taken[w.id]}` : "One lakehouse per layer in this workspace")),
    w.id === current ? h("span", { class: "tag" }, "Current") : null)));
}

function suggestedWorkspaceName(env, p) {
  const parts = (p.path || "").split(/[\\/]/).filter(Boolean);
  let base = parts.pop() || "Fabric Desktop";
  if (base.toLowerCase() === "metadata" && parts.length) base = parts.pop();
  return `${base} - ${ENV_LABEL[env]}`;
}

async function createEnvWorkspace(env, p) {
  const name = h("input", { value: suggestedWorkspaceName(env, p) });
  const capacity = h("select", {}, h("option", { value: "" }, "Loading capacities..."));
  const note = h("p", { class: "muted-text small" }, "Lakehouses need a Fabric capacity, so pick an active one.");
  const create = async () => {
    if (!name.value.trim()) { name.focus(); return; }
    toast(`Creating ${name.value.trim()}...`);
    const res = await metaCall("meta_create_workspace", { env, name: name.value.trim(), capacity_id: capacity.value }, "Workspace not created");
    if (!res) return;
    closeDialog();
    state.workspaces = null;  // the new one shows up in every picker
    state.meta.project = res;
    toast(`Created ${res.created.displayName} and assigned it to ${ENV_LABEL[env]}. Its lakehouses are created on the first ${env === "dev" ? "run" : "promote"}.`, false, { log: true });
    render();
  };
  showDialog(`New workspace for ${ENV_LABEL[env]}`, h("div", {},
    h("label", { class: "field" }, "Name", name),
    h("label", { class: "field" }, "Capacity", capacity), note),
  [btn("Back", null, () => pickEnvWorkspace(env, p), "tool outline"), btn("Create workspace", "add", create, "tool primary")]);
  name.select();
  const caps = (await attempt(() => rpc("list_capacities"), "Could not list capacities")) || [];
  capacity.replaceChildren(...caps.map((c) => h("option", { value: c.id },
    `${c.displayName}${c.sku ? ` (${c.sku})` : ""}${c.state && c.state !== "Active" ? ` - ${c.state.toLowerCase()}` : ""}`)),
  h("option", { value: "" }, "No capacity (assign one in Fabric later)"));
  if (!caps.length) note.textContent = "No Fabric capacity found for your account. The workspace is created without one; assign a capacity in Fabric before its lakehouses can be made.";
}

async function setEnv(env, cfg) {
  closeDialog();
  const res = await metaCall("meta_set_environment", { env, ...cfg });
  if (!res) return;
  state.meta.project = res;
  if (cfg.workspace_name) toast(`${ENV_LABEL[env]} now uses ${cfg.workspace_name}.`);
  else if (cfg.target === "local") toast(`${ENV_LABEL[env]} now keeps its tables on this PC only.`);
  else if (cfg.target === "onelake") toast(`${ENV_LABEL[env]} now reads and writes its workspace's lakehouses.`);
  render();
}

// ---------- surrogate keys ----------

const CONNECTION_LABEL = {
  connection_type: "Connection type", server_host: "Server or host", port: "Port", database_name: "Database or catalog",
  schema_name: "Schema", base_url: "Base URL (APIs)", file_path: "File path or container", auth_method: "Authentication",
  tenant_id: "Tenant id", username: "Username or client id", key_vault_url: "Key Vault URL", secret_name: "Secret name",
  fabric_connection_id: "Fabric connection id", gateway_id: "Gateway", extra_options: "Extra options (JSON)",
};
const AUTH_METHODS = ["", "Entra ID (your sign-in)", "Service principal", "SQL login", "OAuth client", "API key", "None"];

function keysWhere(k) {
  if (k.local) return h("span", {}, "This PC, ", h("span", { class: "mono" }, k.path));
  return k.workspace_name || k.workspace_id ? h("span", {}, "Workspace ", h("b", {}, k.workspace_name || k.workspace_id))
    : h("span", { class: "muted-text" }, "No Keys workspace yet");
}

async function renderKeysTab(body) {
  const m = state.meta;
  const k = await metaCall("meta_keys", { env: m.env });
  if (!k || state.view !== "metadata" || m.tab !== "keys") return;
  const tables = k.key_tables;
  if (!tables.find((t) => t.name === m.keyTable)) m.keyTable = tables[0]?.name || null;
  const sel = tables.find((t) => t.name === m.keyTable);
  const rerender = () => renderKeysTab(body);

  const head = h("div", { class: "card pad" }, h("div", { class: "keys-head" },
    h("span", { class: "item" }, icon("database", 24),
      h("span", {}, h("b", {}, `${k.lakehouse} in ${ENV_LABEL[k.env]}`),
        h("div", { class: "muted-text" }, "One table per business key. Every run MERGEs new business keys here first, then puts the key on every row."),
        h("div", { class: "muted-text small" }, keysWhere(k)))),
    k.local ? null : btn(k.workspace_id ? "Change Keys workspace" : "Pick Keys workspace", "workspace", () => pickKeysWorkspace(k.env), "tool outline small"),
    btn("Sync to LH_Keys", "sync", async () => {
      if (!k.local && !state.user && !(await signIn())) return;
      const res = await metaCall("meta_keys_sync", { env: k.env }, "Not synced");
      if (res) {
        toast(`Synced: ${res.config.source_system} source systems${res.created.length ? `, created ${res.created.map((n) => `sk.${n}`).join(", ")}` : ""}`);
        rerender();
      }
    }, "tool outline small"),
    btn("New key table", "add", () => newKeyTableDialog(rerender), "tool primary small")));

  const problems = [...k.errors, ...(k.problem ? [k.problem] : [])];
  const list = tables.length ? h("table", { class: "list" },
    h("thead", {}, h("tr", {}, ["Key table", "Surrogate key", "Business key", "Created", "Keys", "Next key", "Used by"].map((c) => h("th", {}, c)))),
    h("tbody", {}, tables.map((t) => h("tr", { class: `click${t.name === m.keyTable ? " picked" : ""}`, onclick: () => { m.keyTable = t.name; rerender(); } },
      h("td", {}, h("span", { class: "item" }, icon("table", 18), h("b", { class: "mono" }, t.table))),
      h("td", { class: "mono" }, `${t.sk} INT`),
      h("td", { class: "mono" }, t.bk),
      h("td", { class: "mono" }, "create_date"),
      h("td", { class: "n" }, t.created ? t.keys.toLocaleString() : h("span", { class: "muted-text" }, "not created")),
      h("td", { class: "n mono" }, t.created ? t.next.toLocaleString() : "1"),
      h("td", {}, t.used_by.length ? `${t.used_by.length} table${t.used_by.length === 1 ? "" : "s"}` : h("span", { class: "muted-text" }, "none"))))))
    : h("div", { class: "empty" }, h("span", { class: "title" }, "No key tables yet"),
      h("span", { class: "text" }, "Add one per business key, like customer or product. Each gets its own table in LH_Keys."),
      btn("New key table", "add", () => newKeyTableDialog(rerender), "tool primary"));

  body.replaceChildren(...[head,
    problems.length ? h("div", { class: "problems" }, h("b", {}, "Fix these first"), gateList(problems)) : null,
    h("div", { class: "keys-grid" }, list, sel ? keyTableDetail(sel, k, rerender) : null)].filter(Boolean));
}

function keyTableDetail(t, k, rerender) {
  const input = h("input", { "aria-label": "Business key", placeholder: "Business key, like ABC" });
  const answer = h("p", { class: "muted-text" });
  const find = async () => {
    if (!input.value.trim()) return;
    const res = await metaCall("meta_key_lookup", { table: t.name, value: input.value, env: k.env });
    if (res === undefined) return;
    answer.replaceChildren(res ? h("span", {}, h("span", { class: "mono" }, res.bk), " is ", h("b", { class: "mono" }, String(res.sk)),
      `, created ${new Date(res.create_date).toLocaleString()}.`) : `No ${t.bk} ${input.value.trim()} yet. The first run that sees it gives it a key.`);
  };
  input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") find(); });
  return h("div", { class: "card pad side-panel" },
    h("div", { class: "row" }, h("h2", { class: "flush mono" }, t.table), h("span", { class: "spacer" }),
      btn("Delete", "delete", async () => {
        if (!(await confirmDialog(`Delete key table ${t.name}?`, "The definition file is removed. The table stays in LH_Keys, so no key is lost.", "Delete"))) return;
        const res = await metaCall("meta_delete_key_table", { name: t.name, env: k.env }, "Not deleted");
        if (res) { toast(`Deleted ${t.name}`); state.meta.keyTable = null; rerender(); }
      }, "tool outline small", { disabled: t.used_by.length ? true : null, title: t.used_by.length ? "Tables still use it" : "Delete" })),
    h("p", { class: "muted-text small" }, t.description || "", t.description ? " · " : "", "File ", h("span", { class: "mono" }, `metadata/keys/${t.name}.toml`),
      t.owner ? `, owner ${t.owner}` : ""),
    h("b", {}, "Attributes"),
    h("table", { class: "mini" }, h("thead", {}, h("tr", {}, ["Column", "Type", "Rule"].map((c) => h("th", {}, c)))),
      h("tbody", {}, t.columns.map((c) => h("tr", {}, h("td", { class: "mono" }, c.name), h("td", { class: "mono" }, c.type), h("td", {}, c.rule))))),
    h("p", { class: "muted-text small" }, "-1 is the Unknown member, for rows with no business key."),
    h("b", {}, "Look up a business key"),
    h("div", { class: "row" }, input, btn("Find", "search", find, "tool outline small")),
    answer,
    h("b", {}, "Used by"),
    t.used_by.length ? h("div", { class: "row wrap" }, t.used_by.map((n) => h("button", { class: "tool link mono", onclick: () => openEntity(n) }, n)))
      : h("p", { class: "muted-text" }, "No table uses it yet. Add this to an entity:"),
    h("pre", { class: "sql-block" }, `[[keys]]\ntable = "${t.name}"\nbusiness_key = "${t.name}_id"`));
}

function newKeyTableDialog(done) {
  const name = h("input", { placeholder: "customer" });
  const description = h("input", { placeholder: "A customer, from any source" });
  const preview = h("p", { class: "muted-text" });
  const sync = () => {
    const n = name.value.trim().toLowerCase() || "customer";
    preview.textContent = `Creates sk.${n} in LH_Keys with ${n}_sk (INT), ${n}_bk (unique) and create_date.`;
  };
  name.addEventListener("input", sync);
  sync();
  showDialog("New key table", h("div", { class: "scroll" },
    h("label", { class: "field" }, "Business key name", name),
    h("label", { class: "field" }, "Description", description), preview), [
    btn("Cancel", null, closeDialog, "tool outline"),
    btn("Create", null, async () => {
      if (!name.value.trim()) return toast("Give the key table a name", true);
      const res = await metaCall("meta_new_key_table", { name: name.value.trim(), description: description.value.trim(), env: state.meta.env }, "Not created");
      if (res) { closeDialog(); state.meta.keyTable = name.value.trim().toLowerCase(); toast(`Created sk.${state.meta.keyTable}`); done(); }
    }, "tool primary"),
  ]);
}

async function pickKeysWorkspace(env) {
  const list = h("div", { class: "scroll" }, h("p", { class: "muted-text" }, state.user ? "Loading workspaces..." : "Sign in to pick a Fabric workspace."));
  showDialog(`Keys workspace for ${ENV_LABEL[env]}`, list, [btn("Cancel", null, closeDialog, "tool outline")]);
  if (!state.user && !(await signIn())) return;
  const workspaces = await loadWorkspaces();
  list.replaceChildren(h("p", { class: "muted-text" }, "LH_Keys is created in this workspace on the next sync or promote."),
    ...workspaces.map((w) => h("button", { class: "choice", onclick: async () => {
      closeDialog();
      const res = await metaCall("meta_set_keys_workspace", { env, workspace_id: w.id, workspace_name: w.displayName });
      if (res) render();
    } }, icon("workspace", 20), h("span", { class: "main" }, h("span", { class: "n" }, w.displayName), h("span", { class: "w" }, "Holds LH_Keys")))));
}

// ---------- source systems ----------

async function renderSourcesTab(body) {
  const m = state.meta;
  const k = await metaCall("meta_keys", { env: m.env });
  if (!k || state.view !== "metadata" || m.tab !== "sources") return;
  const systems = k.source_systems;
  if (m.source !== "new" && !systems.find((s) => s.id === m.source)) m.source = systems[0]?.id ?? "new";
  const rerender = () => renderSourcesTab(body);
  const nextId = Math.max(0, ...systems.map((s) => s.id)) + 1;
  const blank = { id: nextId, code: "", name: "", system_type: "", owner: "", description: "", active: true,
    connections: Object.fromEntries(["dev", "test", "prod"].map((env) => [env, Object.fromEntries(k.connection_fields.map((f) => [f, ""]))])) };
  const current = m.source === "new" ? blank : systems.find((s) => s.id === m.source);
  // Unsaved edits survive switching the environment shown, until another source system is picked.
  if (!m.sourceDraft || m.sourceDraft.key !== String(m.source)) m.sourceDraft = { key: String(m.source), draft: JSON.parse(JSON.stringify(current)) };
  const draft = m.sourceDraft.draft;
  const pick = (id) => { m.source = id; m.sourceDraft = null; rerender(); };

  const list = h("div", { class: "card source-list" },
    systems.map((s) => h("button", { class: `source-item${s.id === m.source ? " picked" : ""}`, onclick: () => pick(s.id) },
      h("b", {}, s.code), h("span", { class: "muted-text small" }, `${s.system_type || "Type not set"} · id ${s.id}${s.active ? "" : " · inactive"}`))),
    h("button", { class: `source-item add${m.source === "new" ? " picked" : ""}`, onclick: () => pick("new") },
      icon("add", 16), "New source system"));

  const field = (label, value, onInput, attrs = {}) => {
    const input = h("input", { value: value ?? "", ...attrs });
    input.addEventListener("input", () => onInput(input.value));
    return h("label", { class: "field" }, label, input);
  };
  const select = (label, value, options, onChange) => h("label", { class: "field" }, label,
    h("select", { onchange: (ev) => onChange(ev.target.value) },
      options.map((o) => h("option", { value: o, selected: o === value ? true : null }, o || "Not set"))));

  const conn = draft.connections[m.sourceEnv];
  const connFields = k.connection_fields.map((f) => f === "auth_method"
    ? select(CONNECTION_LABEL[f], conn[f], AUTH_METHODS.includes(conn[f]) ? AUTH_METHODS : [...AUTH_METHODS, conn[f]], (v) => { conn[f] = v; })
    : field(CONNECTION_LABEL[f] || f, conn[f], (v) => { conn[f] = v; }));

  const save = async () => {
    if (!draft.code.trim()) return toast("Give the source system a code, like CRM", true);
    const res = await metaCall("meta_save_source_system", { system: draft, original_id: m.source === "new" ? null : current.id, env: m.env }, "Not saved");
    if (res) { toast(`Saved ${draft.code}`); pick(Number(draft.id)); }
  };

  const form = h("div", { class: "card pad source-form" },
    h("div", { class: "row" }, h("h2", { class: "flush" }, m.source === "new" ? "New source system" : current.code), h("span", { class: "spacer" }),
      m.source === "new" ? null : btn("Delete", "delete", async () => {
        if (!(await confirmDialog(`Delete ${current.code}?`, "It is removed from source_systems.toml. Sync to remove it from LH_Keys too.", "Delete"))) return;
        const res = await metaCall("meta_delete_source_system", { id: current.id, env: m.env }, "Not deleted");
        if (res) { toast(`Deleted ${current.code}`); pick(null); }
      }, "tool outline small"),
      btn("Save", "save", save, "tool primary small")),
    h("b", {}, "General"),
    h("div", { class: "form-grid" },
      field("Source system id (never changes)", draft.id, (v) => { draft.id = v; }, { type: "number", min: "1", readonly: m.source === "new" ? null : true }),
      field("Code", draft.code, (v) => { draft.code = v; }, { placeholder: "CRM" }),
      field("Name", draft.name, (v) => { draft.name = v; }, { placeholder: "Dynamics 365 Sales" }),
      select("System type", draft.system_type, ["", ...k.system_types], (v) => { draft.system_type = v; }),
      field("Owner", draft.owner, (v) => { draft.owner = v; }),
      select("Active", draft.active ? "Yes" : "No", ["Yes", "No"], (v) => { draft.active = v === "Yes"; })),
    field("Description", draft.description, (v) => { draft.description = v; }),
    h("div", { class: "row" }, h("b", {}, "Connection"), h("span", { class: "spacer" }),
      h("div", { class: "seg", role: "group", "aria-label": "Environment" }, ["dev", "test", "prod"].map((env) =>
        h("button", { class: env === m.sourceEnv ? "on" : "", onclick: () => { m.sourceEnv = env; rerender(); } }, ENV_LABEL[env])))),
    h("div", { class: "form-grid" }, connFields),
    h("div", { class: "pass-box" }, icon("shield", 16),
      "Passwords and keys never go here. Give the Key Vault URL and the secret's name; the notebook reads the secret when it runs."),
    h("p", { class: "muted-text small" }, "Sync writes these to LH_Keys as config.source_system and config.source_connection, one row per source per environment, so notebooks and pipelines look up connections by code."));

  body.replaceChildren(...[
    k.errors.length ? h("div", { class: "problems" }, h("b", {}, "Fix these first"), gateList(k.errors)) : null,
    h("div", { class: "sources-grid" }, list, form), varlibCard()].filter(Boolean));
}

registerFeature({
  id: "metadata", rail: { label: "Metadata", icon: "metadata", order: 50, also: ["entity"] },
  views: { metadata: renderMetadata, entity: renderEntity },
  commands: [
    ...META_TAB_GROUPS.flatMap(([, , tabs]) => tabs.map(([id, label, previewId]) => ({
      label: `Metadata: ${label}`, icon: "metadata", keywords: "medallion", preview: !!previewId, run: () => goMeta(id) }))),
    { label: "New entity", icon: "add", keywords: "metadata table create", run: () => { goMeta("entities"); newEntityDialog(); },
      when: () => !!state.meta?.project?.path },
    { label: "Run all entities", icon: "play", keywords: "metadata run", run: () => { goMeta("runs"); runEntities(null); },
      when: () => !!state.meta?.project?.path },
  ],
});

// ---------- table health ----------

function formatBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
}

function shortDate(iso) {
  return iso ? new Date(iso).toLocaleString() : "never";
}

async function renderHealthTab(body) {
  const m = state.meta;
  body.replaceChildren(h("p", { class: "muted-text" }, "Reading every table's Delta log..."));
  if (m.env !== "dev" && !state.user && !(await signIn())) return;
  const hl = await metaCall("meta_health", { env: m.env });
  if (!hl || state.view !== "metadata" || m.tab !== "health") return;
  const optimize = async (entities) => {
    toast(hl.local ? "Compacting on this PC..." : "Starting Fabric's table maintenance...");
    const res = await metaCall("meta_optimize", { env: m.env, entities }, "Optimize failed");
    if (!res) return;
    const failed = res.filter((r) => r.status === "Failed");
    toast(failed.length ? `${failed.length} of ${res.length} didn't start: ${failed[0].message}`
      : hl.local ? `Compacted ${res.length} table${res.length === 1 ? "" : "s"}` : `Queued ${res.length} maintenance job${res.length === 1 ? "" : "s"} in Fabric`, failed.length > 0);
    renderHealthTab(body);
  };
  const needs = hl.tables.filter((t) => t.status === "needs_maintenance");
  const head = h("div", { class: "card pad" },
    h("div", { class: "row wrap" },
      h("b", {}, hl.local ? `${ENV_LABEL[m.env]} tables on this PC` : `${ENV_LABEL[m.env]} tables in OneLake`),
      h("span", { class: "muted-text" }, needs.length ? `${needs.length} of ${hl.tables.length} would benefit from maintenance` : "Every table is in good shape"),
      h("span", { class: "spacer" }),
      btn("Refresh", "sync", () => renderHealthTab(body), "tool outline small"),
      btn(hl.local ? "Compact all" : "Optimize all in Fabric", "flash", () => optimize(null), "tool primary small")),
    h("p", { class: "muted-text" }, hl.local
      ? "Optimize here compacts small files and removes versions older than 7 days with delta-rs. V-Order is added in Fabric, after promote."
      : "Optimize runs Fabric's table maintenance job on the workspace's capacity: OPTIMIZE with V-Order (what Direct Lake and the SQL endpoint read fastest), then VACUUM with 7 days' retention. Tables written from this PC have no V-Order until it runs."),
    hl.local ? null : h("label", { class: "check" },
      h("input", { type: "checkbox", checked: hl.optimize_after_run ? true : null, onchange: async (ev) => {
        const res = await metaCall("meta_set_maintenance", { env: m.env, optimize_after_run: ev.target.checked });
        if (res) toast(ev.target.checked ? "Every table a run writes gets optimized in Fabric afterwards" : "Runs no longer optimize afterwards");
      } }),
      `Optimize every table a ${ENV_LABEL[m.env]} run writes, right after the run`));
  const rows = hl.tables.filter((t) => matches(`${t.entity} ${t.layer}`));
  const list = h("table", { class: "list" },
    h("thead", {}, h("tr", {}, ["Table", "Layer", "Files", "Size", "Avg file", hl.local ? null : "V-Order", "Versions since VACUUM", "Last optimize", "Advice", ""]
      .filter((c) => c !== null).map((c) => h("th", {}, c)))),
    h("tbody", {}, rows.map((t) => h("tr", {},
      h("td", {}, h("span", { class: "mono" }, t.entity)),
      h("td", {}, t.layer ? layerChip(t.layer) : h("span", { class: "muted-text" }, "run metadata")),
      t.exists ? [
        h("td", { class: "n" }, t.files), h("td", { class: "n" }, formatBytes(t.bytes)), h("td", { class: "n" }, formatBytes(t.avg_file_bytes)),
        hl.local ? null : h("td", {}, t.v_ordered ? h("span", { class: "pill-s pass" }, "Yes") : h("span", { class: "pill-s wrn" }, "No")),
        h("td", { class: "n" }, t.versions_since_vacuum), h("td", {}, shortDate(t.last_optimize)),
        h("td", {}, t.advice.length ? h("ul", { class: "gate-list" }, t.advice.map((a) => h("li", {}, a))) : h("span", { class: "pill-s pass" }, "Healthy")),
        h("td", {}, btn(hl.local ? "Compact" : "Optimize", null, () => optimize([t.entity]), "tool link small")),
      ] : h("td", { colspan: hl.local ? 8 : 9, class: "muted-text" }, t.message || "Not written in this environment yet")))));
  const jobs = hl.maintenance || [];
  body.replaceChildren(head, list, jobs.length ? h("div", { class: "card pad" }, h("b", {}, "Recent maintenance jobs in Fabric"),
    h("table", { class: "mini" }, h("tbody", {}, jobs.map((j) => h("tr", {},
      h("td", {}, statusPill(fabricStatus(j.status))), h("td", { class: "mono" }, j.table),
      h("td", {}, shortDate(j.started_at)), h("td", { class: "muted-text" }, j.message || "")))))) : null);
}

// ---------- Direct Lake semantic model ----------

async function renderModelTab(body) {
  const m = state.meta;
  if (m.env === "dev" && (state.meta.project?.environments?.dev?.target || "local") === "local") {
    body.replaceChildren(infoCard("Pick Test or Prod",
      "A Direct Lake semantic model reads its tables straight from OneLake, and Dev keeps its tables on this PC. Switch the environment above to Test or Prod, where promote has put the tables in Fabric."));
    return;
  }
  body.replaceChildren(h("p", { class: "muted-text" }, "Reading the layer's tables and relationships..."));
  if (!state.user && !(await signIn())) return;
  const plan = await metaCall("meta_model_plan", { env: m.env, layer: m.modelLayer || null });
  if (!plan || state.view !== "metadata" || m.tab !== "model") return;
  const layer = h("select", { onchange: (e) => { m.modelLayer = e.target.value; renderModelTab(body); } },
    plan.layers.map((l) => h("option", { value: l, selected: l === plan.layer ? true : null }, l)));
  const name = h("input", { value: m.modelName || plan.name, oninput: (e) => { m.modelName = e.target.value; } });
  const result = h("div", {});
  const build = async () => {
    toast(`Building ${name.value} in ${plan.workspace_name || "the workspace"}...`);
    const res = await metaCall("meta_build_model", { env: m.env, layer: plan.layer, name: name.value }, "Model not built");
    if (!res) return;
    toast(`${res.created ? "Created" : "Updated"} ${res.name}`);
    result.replaceChildren(h("div", { class: "pass-box" }, icon("ok", 16),
      `${res.created ? "Created" : "Updated"} ${res.name}: ${res.tables.length} tables, ${res.relationships} relationship${res.relationships === 1 ? "" : "s"}.`,
      res.skipped.length ? ` Not written yet, so left out: ${res.skipped.join(", ")}.` : "",
      res.endpoint_refreshed ? "" : " The SQL endpoint refresh didn't run; if a table is missing, refresh the lakehouse in Fabric and build again.",
      res.url ? btn("Open in Fabric", "open", () => openInFabric(res.url), "tool link small") : null));
  };
  body.replaceChildren(
    h("div", { class: "card pad" },
      h("div", { class: "row wrap" }, h("b", {}, "Direct Lake semantic model"), h("span", { class: "spacer" }),
        plan.existing ? btn("Open in Fabric", "open", () => openInFabric(plan.existing.url), "tool outline small") : null,
        btn(plan.existing ? "Update model" : "Create model", "cube", build, "tool primary small")),
      h("p", { class: "muted-text" }, `Reads ${plan.lakehouse} in ${plan.workspace_name || plan.workspace_id} through its SQL analytics endpoint, with no import and no refresh schedule. Relationships come from the metadata. Surrogate keys and the framework's _run_id and _loaded_at columns are hidden; every fact gets a row count measure. Updating keeps every column's lineage tag, so reports built on the model keep working.`),
      h("div", { class: "row wrap" }, h("label", { class: "field" }, "Layer", layer), h("label", { class: "field grow" }, "Model name", name)),
      result),
    h("div", { class: "entity-grid" },
      h("div", { class: "card pad grow" }, h("b", {}, `Tables (${plan.tables.length})`),
        h("table", { class: "mini" }, h("thead", {}, h("tr", {}, ["Table", "Kind", "Delta table", "Entity"].map((c) => h("th", {}, c)))),
          h("tbody", {}, plan.tables.map((t) => h("tr", {}, h("td", { class: "mono" }, t.name),
            h("td", {}, h("span", { class: `pill-s ${t.kind === "dimension" ? "info" : "neu"}` }, t.kind)),
            h("td", { class: "mono" }, t.source), h("td", {}, h("button", { class: "tool link mono", onclick: () => openEntity(t.entity) }, t.entity))))))),
      h("div", { class: "side" }, h("div", { class: "card pad" }, h("b", {}, `Relationships (${plan.relationships.length})`),
        plan.relationships.length ? h("table", { class: "mini" }, h("tbody", {}, plan.relationships.map((r) => h("tr", {},
          h("td", {}, h("div", { class: "rel-cell" }, h("span", { class: "mono" }, `${r.from_table}.${r.from_column}`),
            h("span", { class: "mono" }, `→ ${r.to_table}.${r.to_column}`),
            h("span", { class: "muted-text" }, r.active ? r.why : `inactive: ${r.why}`)))))))
          : h("p", { class: "muted-text" }, "None found. Facts relate to a dimension through a [[lookup]] block, or a column named like the dimension's surrogate key.")))));
}

// ---------- profiling ----------

// Profile what an entity reads, on this PC, and turn what it finds into [[clean]] and [[check]] blocks.
async function profileEntity(name, source = true) {
  const m = state.meta;
  const overlay = showDialog(`Profile ${name}`, h("p", { class: "muted-text" }, `Reading ${source ? "what it reads" : "its table"} in ${ENV_LABEL[m.env]}...`));
  overlay.querySelector(".dialog").classList.add("wide");
  const prof = await metaCall("meta_profile", { name, env: m.env, source }, "Profile failed");
  if (!prof) return;
  const picked = new Set();
  const preview = h("div", {});
  const addBtn = btn("Add to entity", "add", async () => {
    const blocks = prof.suggestions.filter((s) => picked.has(s.id)).map((s) => s.toml);
    const res = await metaCall("meta_add_rules", { name, blocks }, "Not added");
    if (!res) return;
    closeDialog();
    toast(res.errors.length ? `Added, with ${res.errors.length} problem${res.errors.length === 1 ? "" : "s"} to fix` : `Added ${blocks.length} rule${blocks.length === 1 ? "" : "s"} to ${name}. Run it to apply them.`);
    render();
  }, "tool primary", { disabled: true });
  const sync = () => {
    addBtn.disabled = !picked.size;
    addBtn.lastChild.textContent = picked.size ? `Add ${picked.size} to entity` : "Add to entity";
  };
  const showPreview = async () => {
    const rules = prof.suggestions.filter((s) => picked.has(s.id) && s.kind === "clean").map((s) => s.rule);
    if (!rules.length) { preview.replaceChildren(h("p", { class: "muted-text" }, "Tick a cleaning rule to see what it changes.")); return; }
    preview.replaceChildren(h("p", { class: "muted-text" }, "Applying the rules on this PC..."));
    const res = await metaCall("meta_profile_preview", { name, env: m.env, rules });
    if (!res) return;
    const cols = res.columns.flatMap((c) => [c, `${c} (after)`]);
    preview.replaceChildren(h("b", {}, `Before and after: ${res.changed} value${res.changed === 1 ? "" : "s"} change`),
      res.rows.length ? dataTable({ columns: cols, rows: res.rows }) : h("p", { class: "muted-text" }, "Nothing changes."));
  };
  const kindPill = (k) => h("span", { class: `pill-s ${k === "clean" ? "cus" : k === "check" ? "info" : "neu"}` }, k === "info" ? "note" : k);
  const suggestions = prof.suggestions.length ? h("table", { class: "mini" }, h("tbody", {}, prof.suggestions.map((sg) => h("tr", {},
    h("td", {}, sg.toml ? h("input", { type: "checkbox", "aria-label": sg.id, onchange: (e) => {
      if (e.target.checked) picked.add(sg.id); else picked.delete(sg.id);
      sync();
      if (sg.kind === "clean") showPreview();
    } }) : null),
    h("td", {}, kindPill(sg.kind)), h("td", { class: "mono" }, sg.column), h("td", {}, sg.why),
    h("td", {}, sg.toml ? h("pre", { class: "sql-block" }, sg.toml.trim()) : null)))))
    : h("p", { class: "muted-text" }, "No suggestions: the rules in place already cover what the profile found.");
  const columns = h("table", { class: "mini" },
    h("thead", {}, h("tr", {}, ["Column", "Type", "Nulls", "Distinct", "Range or length", "Most common", "Found"].map((c) => h("th", {}, c)))),
    h("tbody", {}, prof.columns.map((c) => h("tr", {},
      h("td", { class: "mono" }, c.name), h("td", { class: "muted-text mono" }, c.type),
      h("td", { class: "n" }, `${c.nulls} (${c.null_pct}%)`), h("td", { class: "n" }, c.distinct),
      h("td", { class: "mono small" }, c.min !== undefined ? `${c.min} to ${c.max}` : c.min_length !== undefined ? `${c.min_length} to ${c.max_length} chars` : ""),
      h("td", { class: "small" }, c.top.slice(0, 3).map((t) => `${t.value} (${t.count})`).join(", ")),
      h("td", {}, c.issues.length ? c.issues.map((i) => h("div", { class: "small" }, i)) : h("span", { class: "pill-s pass" }, "clean"))))));
  showDialog(`Profile ${name}`, h("div", { class: "scroll" },
    h("div", { class: "row wrap" },
      h("span", { class: "muted-text" }, `${prof.rows} rows ${source ? "as read, before cleaning" : "in its table"} · ${ENV_LABEL[m.env]} · ${prof.seconds}s on this PC`),
      h("span", { class: "spacer" }),
      btn(source ? "Profile its table instead" : "Profile what it reads", null, () => profileEntity(name, !source), "tool link small")),
    h("b", {}, "Suggestions"), suggestions, preview, h("b", {}, "Columns"), columns),
  [btn("Close", null, closeDialog, "tool outline"), addBtn]).querySelector(".dialog").classList.add("wide");
}
