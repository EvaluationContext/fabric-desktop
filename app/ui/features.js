// Where every page, command and preview flag is declared, so a new feature is added in one
// place and lands somewhere people already look. Rules (docs/ui-guidelines.md):
//   - The left rail holds at most RAIL_LIMIT pages. A new feature becomes a tab or group inside
//     an existing page; the rail grows only for a new kind of work.
//   - Every page and action registers commands, so the command bar (Ctrl+K) finds it.
//   - A feature not yet tried on a real tenant is marked preview: it shows a Preview tag, and
//     people can hide all of them in Settings.
// Loaded before app.js; the functions here run only after every script has loaded.

const RAIL_LIMIT = 8;
const features = { list: [], views: {}, commands: [] };

// f: {id, rail?: {label, icon, order, bottom?, also?: [view ids that light this rail item]},
//     views?: {viewId: render function}, commands?: [{label, icon, keywords, run, preview}], preview?}
function registerFeature(f) {
  features.list.push(f);
  Object.assign(features.views, f.views || {});
  for (const c of f.commands || []) features.commands.push({ preview: f.preview, ...c, feature: f.id });
  const main = features.list.filter((x) => x.rail && !x.rail.bottom).length;
  if (main > RAIL_LIMIT) console.warn(`The left rail has ${main} pages; the limit is ${RAIL_LIMIT}. Put ${f.id} inside an existing page.`);
}

function railFeatures(bottom = false) {
  return features.list.filter((f) => f.rail && !!f.rail.bottom === bottom).sort((a, b) => a.rail.order - b.rail.order);
}

function railItem(f) {
  const view = Object.keys(f.views || {})[0];
  const lit = [view, ...(f.rail.also || [])].includes(state.view);
  return h("button", { class: `rail-item${lit ? " active" : ""}`, onclick: () => go(view) }, icon(f.rail.icon, 22), f.rail.label);
}

// ---------- preview ----------

// Features not yet tried on a live Fabric tenant. Each loses its tag once it has been.
const PREVIEW_FEATURES = {
  fabricRun: "Run in Fabric",
  schedules: "Schedules",
  cost: "Cost",
  shortcuts: "OneLake shortcuts",
  access: "OneLake access",
  history: "Table history and restore",
  health: "Table health",
  model: "Semantic model",
  tests: "Tests",
  xray: "Estate X-ray",
};

function previewShown() {
  return state.settings?.show_preview?.value !== false;
}

function previewTag() {
  return h("span", { class: "preview-tag", title: "In preview: this may change as it's tried on more tenants. Settings can hide preview features." }, "Preview");
}

// The node with a Preview tag, or null when this person hides preview features.
function preview(id, node) {
  if (!PREVIEW_FEATURES[id]) return node;
  if (!previewShown()) return null;
  node.append(previewTag());
  return node;
}

// A small "Learn more" link to a section of the user guide.
function learnMore(anchor, label = "Learn more") {
  return h("a", { href: "#", class: "learn-more", onclick: (e) => {
    e.preventDefault();
    attempt(() => invoke("open_doc", { page: `user-guide.md${anchor ? `#${anchor}` : ""}` }), "Could not open the guide");
  } }, label);
}

// ---------- command bar (Ctrl+K) ----------

const palette = { notebooks: null, selected: 0 };

// Everything the bar can find for this text: commands, then notebooks, workspaces and entities.
function paletteItems(text) {
  const q = text.trim().toLowerCase();
  const hit = (...parts) => !q || parts.join(" ").toLowerCase().includes(q);
  const items = [];
  for (const f of railFeatures().concat(railFeatures(true))) {
    if (hit("go to", f.rail.label)) items.push({ group: "Pages", label: f.rail.label, icon: f.rail.icon, run: () => go(Object.keys(f.views)[0]) });
  }
  for (const c of features.commands) {
    if (c.preview && !previewShown()) continue;
    if (c.when && !c.when()) continue;
    if (hit(c.label, c.keywords || "")) items.push({ group: "Actions", label: c.label, icon: c.icon || "flash", run: c.run });
  }
  if (q) {
    for (const n of (palette.notebooks || []).filter((n) => hit(n.name)).slice(0, 6)) {
      items.push({ group: "Notebooks", label: n.name, icon: "notebook", run: () => openNotebook(n.name) });
    }
    for (const ws of (state.workspaces || []).filter((w) => hit(w.displayName)).slice(0, 6)) {
      items.push({ group: "Workspaces", label: ws.displayName, icon: "workspace", run: () => openWorkspace(ws) });
    }
    for (const e of (state.meta?.project?.entities || []).filter((e) => hit(e.name)).slice(0, 6)) {
      items.push({ group: "Entities", label: e.name, icon: "table", run: () => openEntity(e.name) });
    }
    items.push({ group: "Filter", label: `Show only items matching "${text.trim()}" on this page`, icon: "dataFunnel",
      run: () => { state.query = text.trim(); syncSearchBox(); render(); } });
  }
  return items;
}

function openPalette(initial = "") {
  closeMenu();
  const input = h("input", { type: "text", value: initial, placeholder: "Search pages, actions, notebooks, workspaces and entities",
    "aria-label": "Command bar" });
  const list = h("div", { class: "palette-list", role: "listbox" });
  const close = () => { document.getElementById("palette")?.remove(); };
  const draw = () => {
    const items = paletteItems(input.value);
    palette.selected = Math.min(palette.selected, Math.max(0, items.length - 1));
    let group = null;
    list.replaceChildren(...(items.length ? items.flatMap((it, i) => {
      const head = it.group !== group ? h("div", { class: "palette-group" }, (group = it.group)) : null;
      const row = h("button", { class: `palette-item${i === palette.selected ? " on" : ""}`, role: "option",
        onmousemove: () => { if (palette.selected !== i) { palette.selected = i; draw(); } },
        onclick: () => { close(); it.run(); } }, icon(it.icon, 16), it.label);
      return [head, row];
    }) : [h("div", { class: "palette-empty" }, "Nothing matches. Try fewer letters.")]));
    list.querySelector(".on")?.scrollIntoView({ block: "nearest" });
    return items;
  };
  input.addEventListener("input", () => { palette.selected = 0; draw(); });
  input.addEventListener("keydown", (e) => {
    const items = paletteItems(input.value);
    if (e.key === "ArrowDown") { e.preventDefault(); palette.selected = Math.min(items.length - 1, palette.selected + 1); draw(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); palette.selected = Math.max(0, palette.selected - 1); draw(); }
    else if (e.key === "Enter" && items[palette.selected]) { e.preventDefault(); close(); items[palette.selected].run(); }
    else if (e.key === "Escape") { e.preventDefault(); close(); }
  });
  close();
  const box = h("div", { id: "palette", class: "palette-overlay", onmousedown: (e) => { if (e.target === box) close(); } },
    h("div", { class: "palette", role: "dialog", "aria-label": "Command bar" },
      h("label", { class: "palette-input" }, icon("search", 18), input, h("kbd", {}, "Esc")), list));
  document.body.append(box);
  palette.selected = 0;
  draw();
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
  // Notebook names come from the engine; the list redraws when they arrive.
  rpc("list_notebooks").then((n) => { palette.notebooks = n || []; if (box.isConnected) draw(); }).catch(() => {});
}

// The top bar's box shows the page filter, if any, and opens the command bar.
function syncSearchBox() {
  const box = document.getElementById("search");
  if (!box) return;
  box.value = state.query || "";
  document.querySelector(".search")?.classList.toggle("filtering", !!state.query);
}
