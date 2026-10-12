// Metadata framework screens: entities, layers, runs, environments and promote.
// Every table is a file in the metadata project; these screens read and edit those files
// through the sidecar, which also runs the gates and the engine.

state.meta = {
  project: null,       // summary from meta_project
  tab: "entities",     // entities | layers | runs | environments
  env: "dev",
  entity: null,        // open entity detail
  layersDraft: null,   // unsaved layer edits
  runs: null,
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
  const cls = { ok: "pass", failed: "fail", needs_decision: "wrn", skipped: "neu", unavailable: "neu" }[status] || "neu";
  const label = { ok: "Succeeded", failed: "Failed", needs_decision: "Needs a decision", skipped: "Skipped" }[status] || status;
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
    toast(text, true);
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
  const m = state.meta;
  const tabs = [["entities", "Entities"], ["layers", "Layers"], ["runs", "Runs"], ["environments", "Environments and promote"]];
  const body = h("div", { class: "meta-body" });
  setView(h("div", { class: "page" },
    h("div", { class: "page-head" },
      h("div", {},
        h("h1", {}, "Metadata"),
        h("p", { class: "sub" }, "Every table is a file in ", h("span", { class: "mono" }, p.path),
          ". Metadata version ", h("span", { class: "mono" }, p.version), ".")),
      h("div", { class: "head-actions" },
        envPicker(),
        btn("New entity", "add", newEntityDialog, "tool outline"),
        btn("Run all", "play", () => runEntities(null), "tool primary", { disabled: p.errors.length ? true : null }))),
    problemsBanner(p.errors),
    h("div", { class: "subtabs" }, tabs.map(([id, label]) =>
      h("button", { class: `tab${m.tab === id ? " active" : ""}`, onclick: () => goMeta(id) }, label))),
    body));
  const render = { entities: renderEntitiesTab, layers: renderLayersTab, runs: renderRunsTab, environments: renderEnvTab }[m.tab];
  await render(body, p);
}

function envPicker() {
  const select = h("select", { class: "env-select", "aria-label": "Environment",
    onchange: (e) => { state.meta.env = e.target.value; state.meta.runs = null; render(); } },
  Object.entries(ENV_LABEL).map(([id, label]) => h("option", { value: id, selected: state.meta.env === id ? true : null },
    `Environment: ${label}`)));
  return select;
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
        h("td", {}, r ? [statusPill(r.status), h("span", { class: "muted-text" }, ` ${new Date(r.started_at).toLocaleString()}`)]
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
  if (!state.meta.project) await loadMetaProject();
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
    e.derived.includes(c) ? h("span", { class: "pill-s pass" }, "derived") : null,
    e.hook_adds.includes(c) ? h("span", { class: "pill-s cus" }, "hook") : null));

  const runs = (e.runs || []).filter((r) => r.run_id);
  setView(h("div", { class: "page" },
    h("div", { class: "page-head" },
      h("div", {},
        h("div", { class: "row" }, h("button", { class: "tool link", onclick: () => goMeta("entities") }, "Metadata"), h("span", { class: "muted-text" }, "/")),
        h("h1", { class: "row" }, h("span", { class: "mono" }, e.name), layerChip(e.layer)),
        h("p", { class: "sub" }, `${MODE_LABEL[e.mode] || e.mode}${e.watermark ? ` on ${e.watermark}` : ""} · owner ${e.owner || "not set"} · `,
          h("span", { class: "mono" }, e.path))),
      h("div", { class: "head-actions" }, envPicker(),
        btn("Delete", "delete", async () => {
          if (!(await confirmDialog(`Delete ${e.name}?`, "The entity file is removed. Its Delta table stays in the lakehouse until you delete it there.", "Delete"))) return;
          const res = await metaCall("meta_delete_entity", { name: e.name }, "Not deleted");
          if (res) { toast(`Deleted ${e.name}`); goMeta("entities"); }
        }, "tool outline"),
        btn("Rebuild with downstream", "sync", () => runEntities([e.name], { cascade: true }), "tool outline"),
        btn("Run", "play", () => (dirty() ? save() : Promise.resolve()).then(() => runEntities([e.name])), "tool primary"))),
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
          h("div", { class: "lineage" },
            e.reads.length ? e.reads.map((r) => h("button", { class: "tool link mono", onclick: () => openEntity(r) }, r)) : h("span", { class: "muted-text" }, e.source || "no source"),
            h("span", { class: "arrow" }, "→"), h("b", { class: "mono" }, e.name),
            e.impact.length ? [h("span", { class: "arrow" }, "→"), e.impact.map((r) => h("button", { class: "tool link mono", onclick: () => openEntity(r) }, r))] : null)),
        h("div", { class: "card pad" }, h("b", {}, `Recent runs in ${ENV_LABEL[state.meta.env]}`),
          runs.length ? h("table", { class: "mini" }, h("tbody", {}, runs.slice(0, 6).map((r) => h("tr", { class: "click", onclick: () => showRun(r.run_id) },
            h("td", {}, statusPill(r.status)), h("td", {}, new Date(r.started_at).toLocaleString()),
            h("td", { class: "n" }, `${r.rows_written} rows`), h("td", { class: "n" }, `${r.seconds}s`)))))
            : h("p", { class: "muted-text" }, e.runs?.[0]?.message || "Not run yet.")))),
    e.preview ? h("div", { class: "card pad" }, h("b", {}, `Data in ${ENV_LABEL[state.meta.env]} · ${e.preview.total} rows`), dataTable(e.preview)) : null));
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
  toast(`Running ${label} in ${ENV_LABEL[state.meta.env]}...`);
  const results = await metaCall("meta_run", { names, env: state.meta.env, backfill: opts.backfill || null, cascade: !!opts.cascade }, "Run blocked");
  if (!results) return;
  const decision = results.find((r) => r.status === "needs_decision");
  showRunResults(results, decision);
  render();
}

function showRunResults(results, decision) {
  const rows = results.map((r) => h("div", { class: "result-row" },
    statusPill(r.status), h("span", { class: "mono" }, r.entity),
    h("span", { class: "muted-text" }, r.status === "ok" ? `${r.rows_written} rows written${r.quarantined ? `, ${r.quarantined} quarantined` : ""}`
      : r.status === "needs_decision" ? "Waiting for your choice below" : r.message),
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

async function showRun(runId) {
  const r = await metaCall("meta_run_detail", { run_id: runId, env: state.meta.env });
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
    h("thead", {}, h("tr", {}, ["Started", "Entity", "Status", "Rows read", "Written", "Quarantined", "Seconds", "Metadata", ""].map((c) => h("th", {}, c)))),
    h("tbody", {}, shown.map((r) => h("tr", { class: "click", onclick: () => showRun(r.run_id) },
      h("td", {}, new Date(r.started_at).toLocaleString()), h("td", { class: "mono" }, r.entity), h("td", {}, statusPill(r.status)),
      h("td", { class: "n" }, r.rows_read), h("td", { class: "n" }, r.rows_written), h("td", { class: "n" }, r.quarantined),
      h("td", { class: "n" }, r.seconds), h("td", { class: "mono" }, r.metadata),
      h("td", { class: "muted-text" }, r.backfill ? `backfill: ${r.backfill}` : ""))))) :
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
      h("thead", {}, h("tr", {}, ["#", "Layer", "Lakehouse in each environment", "Can read from", "Schema drift", "Required checks", "Entities", ""].map((c) => h("th", {}, c)))),
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
        L.push({ name: name.toLowerCase(), drift: "strict", required_checks: ["key_unique"], reads_from: [], description: "" });
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
      infoCard("Each layer has its own policy", "Schema drift and required checks are set per layer, so a lenient Bronze and a strict Gold can sit in one flow. Reordering is refused if a table would end up reading a later layer.")));
}

function stripLayer(l) {
  return { name: l.name, drift: l.drift, required_checks: [...l.required_checks].sort(), reads_from: l.reads_from || [], description: l.description || "" };
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
    const where = local ? "This PC (.local folder in the project)" : cfg.workspace_name || cfg.workspace_id || "No workspace yet";
    return h("div", { class: "card pad env-card" },
      h("div", { class: "row" }, h("b", {}, ENV_LABEL[env]), h("span", { class: "spacer" }),
        btn("Change", null, () => pickEnvTarget(env), "tool link small")),
      h("div", { class: "muted-text" }, where),
      h("div", { class: "muted-text small" }, p.layers.map((l) => l.lakehouse).concat("LH_Meta").join(" · ")));
  };
  const promoteRow = (source, target) => {
    const box = h("div", { class: "muted-text" }, "Checking the promote gate...");
    attempt(() => rpc("meta_promote_check", { source, target })).then((res) => {
      if (!res) return;
      checks[target] = res;
      box.replaceWith(res.errors.length ? h("div", { class: "problems" }, h("b", {}, `${res.errors.length} thing${res.errors.length === 1 ? "" : "s"} to do first`), gateList(res.errors))
        : h("div", { class: "pass-box" }, icon("ok", 16), `Ready: metadata ${res.version} passed every check in ${ENV_LABEL[source]}.`));
    });
    return h("div", { class: "card pad" },
      h("div", { class: "row" }, h("b", {}, `Promote ${ENV_LABEL[source]} to ${ENV_LABEL[target]}`), h("span", { class: "spacer" }),
        btn(`Promote to ${ENV_LABEL[target]}`, "publish", async () => {
          if (!state.user && !(await signIn())) return;
          toast(`Promoting to ${ENV_LABEL[target]}...`);
          const res = await metaCall("meta_promote", { source, target }, "Promote blocked");
          if (res) { toast(`${ENV_LABEL[target]} is now on metadata ${res.version}`); render(); }
        }, "tool primary small")),
      h("p", { class: "muted-text" }, "Needs the project to pass the save gate, and every table's last run here to be green on this exact metadata version. Lakehouses that don't exist yet are created."),
      box);
  };
  body.replaceChildren(h("div", { class: "env-cards" }, ["dev", "test", "prod"].map(card)),
    promoteRow("dev", "test"), promoteRow("test", "prod"));
}

async function pickEnvTarget(env) {
  const options = [];
  if (env === "dev") options.push(h("button", { class: "choice", onclick: () => setEnv(env, { target: "local" }) },
    icon("desktop", 20), h("span", { class: "main" }, h("span", { class: "n" }, "This PC"), h("span", { class: "w" }, "Delta tables in the project's .local folder"))));
  const list = h("div", { class: "scroll" }, options, h("p", { class: "muted-text" }, state.user ? "Loading workspaces..." : "Sign in to pick a Fabric workspace."));
  showDialog(`Where ${ENV_LABEL[env]} writes`, list, [btn("Cancel", null, closeDialog, "tool outline")]);
  if (!state.user && !(await signIn())) return;
  const workspaces = await loadWorkspaces();
  list.replaceChildren(...options, ...workspaces.map((w) => h("button", { class: "choice", onclick: () => setEnv(env, { target: "onelake", workspace_id: w.id, workspace_name: w.displayName }) },
    icon("workspace", 20), h("span", { class: "main" }, h("span", { class: "n" }, w.displayName), h("span", { class: "w" }, "One lakehouse per layer in this workspace")))));
}

async function setEnv(env, cfg) {
  closeDialog();
  const res = await metaCall("meta_set_environment", { env, ...cfg });
  if (res) { state.meta.project = res; render(); }
}

window.META_VIEWS = { metadata: renderMetadata, entity: renderEntity };
