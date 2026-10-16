// Estate X-ray on Home: every lakehouse you can reach, read once, with what needs attention
// ranked first and a fix on each line. "Adopt" turns a lakehouse's tables into metadata entities.

const xrayPage = { data: null, timer: null, kind: "all", showAll: false, folded: false };

const XRAY_KINDS = {
  small_files: "Small files", unidentified: "Unidentified", access: "Access", cost: "Cost",
  stale: "Not changing", vacuum: "Never vacuumed", empty: "Empty lakehouses",
};

function bytesText(n) {
  if (!n) return "0 bytes";
  const units = ["bytes", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${i ? n.toFixed(1) : n} ${units[i]}`;
}

// Where each kind of finding is explained, with how to fix it by hand (docs/xray-fixes.md).
const XRAY_DOCS = {
  small_files: "small-files", unidentified: "unidentified-tables", access: "access", cost: "cost",
  stale: "not-changing", vacuum: "never-vacuumed", empty: "empty-lakehouses",
};

function openXrayDoc(kind) {
  const anchor = XRAY_DOCS[kind];
  attempt(() => invoke("open_doc", { page: `xray-fixes.md${anchor ? `#${anchor}` : ""}` }), "Could not open the guide");
}

// Home calls loadXray once everything else on Home has loaded. The card starts folded or not as
// last left; folded, it still shows the overall numbers.
function xrayCard() {
  let folded = false;
  try { folded = localStorage.getItem("fd.fold.home.xray") === "1"; } catch { /* unfolded */ }
  xrayPage.folded = folded;
  return h("section", { class: `card pad xray${folded ? " folded" : ""}` },
    h("div", { class: "muted-text" }, "The Estate X-ray loads once the rest of Home is in."));
}

async function loadXray(box, refresh) {
  clearTimeout(xrayPage.timer);
  if (!state.user) { drawXray(box, null); return; }
  // auto: the very first scan on this PC starts by itself; after that only the button scans.
  const res = await attempt(() => rpc("xray", { refresh, auto: !refresh }), refresh ? "Scan not started" : "Could not read the X-ray");
  if (!res || state.view !== "home" || !box.isConnected) return;
  xrayPage.data = res;
  drawXray(box, res);
  if (res.running && !res.paused) xrayPage.timer = setTimeout(() => loadXray(box, false), 2000);
}

async function pauseXray(box, paused) {
  clearTimeout(xrayPage.timer);
  const res = await attempt(() => rpc("xray_pause", { paused }), paused ? "Could not pause the scan" : "Could not resume the scan");
  if (!res || !box.isConnected) return;
  xrayPage.data = res;
  drawXray(box, res);
  if (res.running && !res.paused) xrayPage.timer = setTimeout(() => loadXray(box, false), 2000);
}

function drawXray(box, res) {
  const foldBtn = h("button", { class: "tool outline small", "aria-expanded": String(!xrayPage.folded),
    onclick: () => {
      xrayPage.folded = !xrayPage.folded;
      try { localStorage.setItem("fd.fold.home.xray", xrayPage.folded ? "1" : "0"); } catch { /* this session only */ }
      box.classList.toggle("folded", xrayPage.folded);
      drawXray(box, res);
    } }, xrayPage.folded ? "Show details" : "Hide details");
  const head = (extra) => h("div", { class: "xray-head" },
    h("div", { class: "grow" }, h("h2", {}, "Estate X-ray"),
      h("div", { class: "muted-text small" }, "Every lakehouse you can reach, read once: what slows it down, what is broken, who has access, and what it costs. ",
        h("a", { href: "#", onclick: (e) => { e.preventDefault(); openXrayDoc(null); } }, "How each fix works"))),
    extra);
  if (!state.user) {
    fill(box, head(btn("Sign in and scan", "search", async () => { if (await signIn()) render(); }, "tool primary")),
      h("p", { class: "muted-text" }, "The scan reads table logs and workspace roles only. It changes nothing."));
    return;
  }
  const result = res.result;
  const scanBtn = res.running
    ? btn(res.paused ? "Resume" : "Pause", res.paused ? "play" : "pause", () => pauseXray(box, !res.paused), "tool outline")
    : btn(result ? "Scan again" : "Scan now", "sync", () => loadXray(box, true), "tool outline");
  const progress = res.running ? h("div", { class: "xray-progress" },
    h("div", { class: "row" }, h("b", {}, res.paused ? `Paused: ${(res.progress?.phase || "scanning").toLowerCase()}` : res.progress?.phase || "Scanning"),
      res.progress?.total ? h("span", { class: "muted-text small" }, `${res.progress.done} of ${res.progress.total}`) : null),
    h("div", { class: "progress" }, res.progress?.total
      ? h("div", { class: `bar${res.paused ? " paused" : ""}`, style: `width:${Math.max(4, Math.round(100 * res.progress.done / res.progress.total))}%` })
      : h("div", { class: `bar${res.paused ? " paused" : " indeterminate"}` }))) : null;
  if (!result) {
    fill(box, head(h("div", { class: "row" }, scanBtn)), progress,
      res.error ? h("p", { class: "reason" }, `The scan stopped: ${friendlyError(res.error).text}`) : null,
      !res.running && !res.error ? h("p", { class: "muted-text" }, "No scan yet. It reads table logs and workspace roles only, and changes nothing.") : null);
    return;
  }
  const open = result.findings.filter((f) => !f.fixed);
  const byKind = {};
  for (const f of open) byKind[f.kind] = (byKind[f.kind] || 0) + 1;
  const shown = open.filter((f) => (xrayPage.kind === "all" || f.kind === xrayPage.kind)
    && matches(`${f.title} ${f.detail} ${f.workspace_name || ""} ${f.lakehouse_name || ""}`));
  const chip = (kind, label, n) => h("button", { class: `chip${xrayPage.kind === kind ? " on" : ""}`,
    onclick: () => { xrayPage.kind = kind; xrayPage.showAll = false; drawXray(box, res); } }, label, n !== undefined ? h("span", { class: "count" }, n) : null);
  const limit = xrayPage.showAll ? shown.length : 6;
  const high = open.filter((f) => f.severity === "high").length;

  const stats = h("div", { class: "xray-stats" },
    stat(result.workspaces, "workspaces"), stat(result.lakehouses, "lakehouses"),
    stat(result.tables, result.partial ? "tables (first 500)" : "tables"), stat(bytesText(result.bytes), "in Delta tables"),
    stat(open.length, open.length === 1 ? "finding" : "findings", open.length ? (high ? "bad" : "warn") : "good"));
  const top = head(h("div", { class: "row" }, h("span", { class: "muted-text small" }, "Scanned ", when(result.scanned)), scanBtn, foldBtn));
  if (xrayPage.folded) {
    fill(box, top, progress, stats);
    return;
  }
  fill(box,
    top,
    progress,
    stats,
    result.skipped?.length ? h("p", { class: "muted-text small" }, `Couldn't read: ${result.skipped.join(", ")} (a paused capacity, or no access).`) : null,
    open.length ? h("div", { class: "row wrap xray-chips" }, chip("all", "All", open.length),
      Object.entries(XRAY_KINDS).filter(([k]) => byKind[k]).map(([k, label]) => chip(k, label, byKind[k]))) : null,
    open.length ? h("div", { class: "xray-list" }, shown.slice(0, limit).map((f) => xrayFinding(f, box, res)))
      : h("p", { class: "muted-text" }, "Nothing needs attention. Every table is compact, readable and changing."),
    shown.length > limit ? btn(`Show all ${shown.length}`, null, () => { xrayPage.showAll = true; drawXray(box, res); }, "tool link") : null,
    xrayLakehouses(result.lakehouse_rows || []));
}

function stat(value, label, tone = "") {
  return h("div", { class: `xray-stat ${tone}` }, h("b", {}, typeof value === "number" ? value.toLocaleString() : value), h("span", {}, label));
}

function xrayWhere(f) {
  if (!f.lakehouse_name) return f.kind === "access" ? "Workspace roles" : f.kind === "cost" ? "Runs this app started in Fabric" : "";
  return `${f.workspace_name} / ${f.lakehouse_name}`;
}

function xrayFinding(f, box, res) {
  const sev = { high: ["fail", "Fix first"], medium: ["wrn", "Worth fixing"], low: ["neu", "Good to know"] }[f.severity];
  const lakehouseLink = () => openInFabric(fabricItemLink({ type: "Lakehouse", item_id: f.lakehouse_id, workspace_id: f.workspace_id }));
  const action = {
    optimize: () => btn("Optimize", "sync", async (e) => {
      e.target.disabled = true;
      const r = await attempt(() => rpc("xray_optimize", { workspace_id: f.workspace_id, lakehouse_id: f.lakehouse_id, table: f.table,
        schema: f.schema, lakehouse_name: f.lakehouse_name, workspace_name: f.workspace_name, finding_id: f.id }), "Optimize not started");
      if (r) { f.fixed = true; toast(`Optimizing ${f.table} in Fabric. Follow it in Monitor.`); drawXray(box, res); }
      else e.target.disabled = false;
    }, "tool primary"),
    open: () => btn("Open in Fabric", "publish", lakehouseLink, "tool outline"),
    access: () => btn("Review access", "person", () => { catalogPage.tab = "access"; go("onelake"); }, "tool outline"),
    run_local: () => btn("Open notebook", "notebook", () => {
      openNotebook(f.notebook);
      toast("Set Runs on to This PC in the ribbon, and its runs stop costing capacity.");
    }, "tool primary"),
    cost: () => btn("See cost", null, () => { monitorPage.panel = "cost"; go("monitor"); }, "tool outline"),
  }[f.action];
  return h("div", { class: "xray-item" },
    h("span", { class: `pill-s ${sev[0]}` }, sev[1]),
    h("div", { class: "grow" },
      h("div", {}, h("b", {}, f.title)),
      h("div", { class: "muted-text small" }, f.detail, " ",
        h("a", { href: "#", onclick: (e) => { e.preventDefault(); openXrayDoc(f.kind); } }, "How to fix this")),
      xrayWhere(f) ? h("div", { class: "muted-text small where" }, xrayWhere(f)) : null),
    action ? action() : null);
}

function xrayLakehouses(rows) {
  if (!rows.length) return null;
  const shown = rows.filter((r) => matches(`${r.lakehouse_name} ${r.workspace_name}`));
  return h("details", { class: "xray-lakehouses" },
    h("summary", {}, h("b", {}, "Your lakehouses"), h("span", { class: "muted-text small" },
      ` ${rows.length}, with Adopt to bring a lakehouse's tables under metadata`)),
    h("table", { class: "list" },
      h("thead", {}, h("tr", {}, ["Lakehouse", "Workspace", "Tables", "Size", "Findings", ""].map((c, i) => h("th", { class: i > 1 ? "n" : null }, c)))),
      h("tbody", {}, shown.map((r) => h("tr", {},
        h("td", {}, h("span", { class: "item" }, itemIcon("Lakehouse", 16), r.lakehouse_name)),
        h("td", {}, r.workspace_name),
        h("td", { class: "n" }, r.tables), h("td", { class: "n" }, bytesText(r.bytes)),
        h("td", { class: "n" }, r.issues ? h("span", { class: "pill-s wrn" }, r.issues) : h("span", { class: "muted-text" }, "None")),
        h("td", { class: "n" }, r.managed ? h("span", { class: "pill-s pass" }, "Managed by metadata")
          : !r.tables ? null
          : btn(r.adopted ? `Adopt more (${r.adopted} adopted)` : "Adopt", "add", () => adoptDialog(r), "tool link small")))))));
}

async function adoptDialog(r) {
  const project = state.meta.project || await rpc("meta_project").catch(() => null);
  if (!project?.path) {
    showDialog("Adopt needs a metadata project", h("p", { class: "text" },
      "Adopting turns each table into a metadata entity that loads from it. Create or open a metadata project first."),
    [btn("Cancel", null, closeDialog, "tool outline"), btn("Open Metadata", null, () => { closeDialog(); go("meta"); }, "tool primary")]);
    return;
  }
  const body = h("div", { class: "scroll" }, h("p", { class: "muted-text" }, `Reading the tables in ${r.lakehouse_name}...`));
  const layerSel = h("select", {});
  let plan = null;
  const adopt = async () => {
    const picked = [...body.querySelectorAll("tr[data-table]")].filter((tr) => tr.querySelector("input[type=checkbox]").checked)
      .map((tr) => ({ schema: tr.dataset.schema || null, table: tr.dataset.table, name: tr.querySelector("input.name").value.trim(),
                      keys: tr.querySelector("input.keys").value.split(",").map((k) => k.trim()).filter(Boolean) }));
    if (!picked.length) { toast("Pick at least one table.", true); return; }
    const res = await attempt(() => rpc("xray_adopt", { workspace_id: r.workspace_id, lakehouse_id: r.lakehouse_id,
      lakehouse_name: r.lakehouse_name, workspace_name: r.workspace_name, tables: picked, layer: layerSel.value }), "Not adopted");
    if (!res) return;
    closeDialog();
    if (res.problems.length) toast(`Adopted ${res.adopted.length}. Skipped: ${res.problems.join("; ")}`, true);
    else toast(`Adopted ${res.adopted.length} table${res.adopted.length === 1 ? "" : "s"} into ${res.layer}. Run them from Metadata.`, false, { log: true });
    state.meta.project = await rpc("meta_project").catch(() => state.meta.project);
    render();
  };
  showDialog(`Adopt ${r.lakehouse_name}`, body,
    [btn("Cancel", null, closeDialog, "tool outline"), btn("Adopt", "add", adopt, "tool primary")]);
  document.querySelector(".dialog")?.classList.add("roomy");
  plan = await attempt(() => rpc("xray_adopt_plan", { workspace_id: r.workspace_id, lakehouse_id: r.lakehouse_id,
    lakehouse_name: r.lakehouse_name, workspace_name: r.workspace_name }), "Could not read the lakehouse");
  if (!plan) { closeDialog(); return; }
  layerSel.replaceChildren(...plan.layers.map((l) => h("option", { value: l, selected: l === plan.layer ? true : null }, l)));
  body.replaceChildren(
    h("p", { class: "text" }, `Each table you pick becomes an entity in the first layer that loads from it: in full each run, or only new rows when it has a timestamp like updated_at. The original tables in ${r.lakehouse_name} are never changed, so your current loads keep working while you move them over.`),
    h("label", { class: "field inline" }, "Layer", layerSel),
    h("table", { class: "list" },
      h("thead", {}, h("tr", {}, ["", "Table", "Entity", "Key", ""].map((c) => h("th", {}, c)))),
      h("tbody", {}, plan.tables.map((t) => h("tr", { "data-table": t.table, "data-schema": t.schema || "" },
        h("td", {}, h("input", { type: "checkbox", checked: t.ok && !t.exists ? true : null, disabled: t.ok ? null : true })),
        h("td", {}, h("span", { class: "mono" }, t.schema ? `${t.schema}.${t.table}` : t.table),
          t.columns ? h("div", { class: "muted-text small" }, `${t.columns.length} columns`) : null),
        h("td", {}, t.ok ? h("input", { class: "name", value: t.exists ? `${t.name}_adopted` : t.name, "aria-label": "Entity name" }) : null),
        h("td", {}, t.ok ? h("input", { class: "keys", value: (t.keys || []).join(", "), "aria-label": "Key columns", list: `cols-${t.table}` }) : null,
          t.ok ? h("datalist", { id: `cols-${t.table}` }, t.columns.map((c) => h("option", { value: c }))) : null),
        h("td", { class: "muted-text small" }, t.why || (t.exists ? `${t.name} exists, so it gets a new name` : "")))))));
}
