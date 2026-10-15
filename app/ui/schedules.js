// Schedules: on this PC (Windows Task Scheduler starts them, even with the app closed) or in
// Fabric (its job scheduler, on capacity). One dialog adds them for a notebook or an environment;
// Monitor > Schedules lists every one in one place.

// What a run of a schedule on this PC did, as a pill.
function scheduleRunPill(last) {
  if (!last) return h("span", { class: "pill-s neu" }, "Not run yet");
  return statusPill(last.status === "ok" ? "ok" : last.status === "skipped" ? "skipped" : "failed");
}

function fabricRunPill(last) {
  if (!last) return h("span", { class: "pill-s neu" }, "Not run yet");
  const s = fabricStatus(last.status);
  return s === "running" ? h("span", { class: "pill-s info" }, last.status === "NotStarted" ? "Queued" : "Running") : statusPill(s);
}

function onOffButton(enabled, toggle) {
  return h("button", { class: `pill-s ${enabled ? "pass" : "neu"} pill-btn`, title: enabled ? "On. Click to pause." : "Paused. Click to turn on.",
    onclick: (e) => { e.stopPropagation(); toggle(!enabled); } }, enabled ? "On" : "Paused");
}

function nextRunText(iso) {
  if (!iso) return "";
  const t = new Date(iso);
  const day = t.toDateString() === new Date().toDateString() ? "Today"
    : t.toDateString() === new Date(Date.now() + 86400000).toDateString() ? "Tomorrow"
    : t.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  return `${day} ${t.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}`;
}

// Rows for this PC's schedules; actions redraw through `changed`.
function pcScheduleRows(rows, changed, { showWhat = false } = {}) {
  return rows.map((s) => h("tr", {},
    h("td", {}, onOffButton(s.enabled, async (on) => {
      if (await attempt(() => rpc("set_pc_schedule_enabled", { schedule_id: s.id, enabled: on }), on ? "Not turned on" : "Not paused")) changed();
    })),
    showWhat ? h("td", {}, h("b", {}, s.label), h("div", { class: "muted-text small" }, s.target.kind === "notebook" ? "Notebook" : "Metadata run")) : null,
    h("td", {}, s.text),
    h("td", {}, s.enabled ? nextRunText(s.next_run) : h("span", { class: "muted-text" }, "Paused")),
    h("td", {}, scheduleRunPill(s.last), s.last ? h("span", { class: "muted-text small" }, " ", when(s.last.started)) : null,
      s.last && s.last.status !== "ok" && s.last.message ? h("div", { class: "reason" }, s.last.message) : null),
    h("td", { class: "n nowrap" },
      btn("Run now", "play", async () => {
        if (await attempt(() => rpc("run_pc_schedule_now", { schedule_id: s.id }), "Not started")) {
          toast(`${s.label} started on this PC. Its result shows here when it finishes.`);
          setTimeout(changed, 4000);
        }
      }, "tool link small"),
      btn("Remove", "delete", async () => {
        if (await attempt(() => rpc("delete_pc_schedule", { schedule_id: s.id }), "Not removed")) changed();
      }, "tool link small"))));
}

const PC_SCHEDULE_NOTE = "Windows starts these runs on this PC, even with Fabric Desktop closed, while you are signed in to Windows. A run missed while the PC was off starts when it is back.";

// One dialog for a notebook's or an environment's schedules, on this PC or in Fabric.
//   o.pc = { target, query, intro }   o.fabric = { intro, list(), add(schedule), remove(id), unavailable }
async function openScheduleDialog(o) {
  let mode = o.mode === "fabric" && !o.fabric?.unavailable ? "fabric" : "pc";
  const list = h("div", { class: "scroll schedule-list" });
  const intro = h("p", { class: "text" });
  const form = scheduleForm();
  const kind = form.fields[0].querySelector("select");
  const zoneField = form.fields[4];
  const hideZone = () => { if (mode === "pc") zoneField.hidden = true; };
  kind.addEventListener("change", hideZone);
  const seg = h("div", { class: "seg", role: "group", "aria-label": "Runs on" });

  const showPc = (rows) => {
    list.replaceChildren(rows?.length ? h("table", { class: "mini" }, h("tbody", {}, pcScheduleRows(rows, draw)))
      : h("p", { class: "muted-text" }, "No schedules on this PC yet."));
  };
  const showFabric = (res) => {
    if (!res) { list.replaceChildren(); return; }
    list.replaceChildren(res.schedules.length ? h("table", { class: "mini" }, h("tbody", {}, res.schedules.map((s) => h("tr", {},
      h("td", {}, s.enabled ? h("span", { class: "pill-s pass" }, "On") : h("span", { class: "pill-s neu" }, "Paused")),
      h("td", {}, s.text),
      h("td", { class: "n" }, btn("Remove", "delete", async () => {
        const next = await attempt(() => o.fabric.remove(s.id), "Not removed");
        if (next) showFabric(next);
      }, "tool link small"))))))
      : h("p", { class: "muted-text" }, res.published === false ? "Not in Fabric yet. Adding a schedule publishes it." : "No schedules in Fabric yet."));
  };
  async function draw() {
    seg.replaceChildren(...[["pc", "This PC"], ["fabric", "Fabric"]].map(([v, label]) => h("button", {
      class: mode === v ? "on" : null, disabled: v === "fabric" && o.fabric?.unavailable ? true : null,
      title: v === "fabric" ? o.fabric?.unavailable || "Fabric runs it on capacity, even when this PC is off" : "Runs on this PC, with its data and tools",
      onclick: () => { if (mode !== v) { mode = v; draw(); } } }, label)));
    intro.textContent = mode === "pc" ? `${o.pc.intro} ${PC_SCHEDULE_NOTE}` : o.fabric.intro;
    form.sync();
    hideZone();
    list.replaceChildren(h("p", { class: "muted-text" }, "Loading schedules..."));
    if (mode === "pc") {
      showPc(await attempt(() => rpc("pc_schedules_for", o.pc.query), "Could not read schedules"));
    } else {
      if (!(await ensureSignedIn())) return;
      showFabric(await attempt(() => o.fabric.list(), "Could not read schedules"));
    }
  }
  const add = async () => {
    const schedule = form.value();
    if (mode === "pc") {
      const res = await attempt(() => rpc("add_pc_schedule", { target: o.pc.target, schedule }), "Schedule not added");
      if (!res) return;
      toast(res.windows ? "Scheduled on this PC. Windows starts it at those times, even with the app closed."
        : "Saved, but only Windows can start schedules on this PC.", !res.windows);
    } else {
      toast(o.fabric.adding || "Adding the schedule in Fabric...");
      const res = await attempt(() => o.fabric.add(schedule), "Schedule not added");
      if (!res) return;
      toast(o.fabric.added || "Scheduled in Fabric.");
    }
    draw();
  };
  showDialog(o.title, h("div", { class: "scroll" },
    h("div", { class: "compute-row" }, h("span", { class: "muted-text small" }, "Runs on"), seg),
    intro, list, h("b", {}, "Add a schedule"), form.fields,
    h("p", { class: "muted-text small" }, "Every schedule, on this PC and in Fabric, is listed in Monitor > Schedules.")),
  [btn("Close", null, closeDialog, "tool outline"), btn("Add schedule", "add", add, "tool primary")]);
  draw();
}

// ---------- Monitor > Schedules ----------

const schedulesPage = { allWorkspaces: false, data: null, timer: null };

function schedulesPanel() {
  const box = h("div", { class: "monitor-scroll schedules-panel" }, h("p", { class: "muted-text monitor-empty" }, "Loading schedules..."));
  loadSchedules(box, false);
  return box;
}

async function loadSchedules(box, refresh) {
  clearTimeout(schedulesPage.timer);
  const res = await attempt(() => rpc("all_schedules", { refresh, all_workspaces: schedulesPage.allWorkspaces }), "Could not read schedules");
  if (!res || state.view !== "monitor" || monitorPage.panel !== "schedules") return;
  schedulesPage.data = res;
  drawSchedules(box);
  if (res.fabric.pending) schedulesPage.timer = setTimeout(() => loadSchedules(box, false), 1500);
}

function drawSchedules(box) {
  const res = schedulesPage.data;
  const redraw = () => loadSchedules(box, false);
  const fabric = res.fabric || {};
  const pc = res.pc.filter((s) => matches(`${s.label} ${s.text}`));
  const fab = (fabric.items || []).filter((s) => matches(`${s.name} ${s.workspace_name} ${s.type} ${s.text}`));
  const failing = [...pc.filter((s) => s.last && s.last.status === "failed"),
                   ...fab.filter((s) => s.last && fabricStatus(s.last.status) === "failed")].length;
  const scope = (all, label) => h("button", { class: `chip${schedulesPage.allWorkspaces === all ? " on" : ""}`,
    onclick: () => { schedulesPage.allWorkspaces = all; loadSchedules(box, false); } }, label);

  fill(box, 
    h("div", { class: "schedule-summary" },
      h("span", {}, h("b", {}, res.pc.length + (fabric.items || []).length), " schedules"),
      h("span", {}, h("b", {}, res.pc.filter((s) => s.enabled).length), " on this PC"),
      h("span", {}, h("b", {}, (fabric.items || []).filter((s) => s.enabled).length), " in Fabric"),
      failing ? h("span", { class: "bad" }, h("b", {}, failing), " failed last time") : null),
    h("h3", { class: "schedule-head" }, "On this PC"),
    res.windows ? null : h("p", { class: "muted-text small" }, "Only Windows starts these. On this system they are saved but nothing runs them."),
    pc.length ? h("table", { class: "list" },
      h("thead", {}, h("tr", {}, ["", "What", "When", "Next run", "Last run", ""].map((c) => h("th", {}, c)))),
      h("tbody", {}, pcScheduleRows(pc, redraw, { showWhat: true })))
      : h("p", { class: "muted-text" }, res.pc.length ? "No schedule on this PC matches the search."
        : "None yet. Use Schedule on a notebook, or on Metadata, and pick This PC."),
    h("div", { class: "schedule-head row" },
      h("h3", {}, "In Fabric"),
      h("div", { class: "row", role: "group", "aria-label": "Workspaces" },
        scope(false, "Workspaces I work in"), scope(true, "All workspaces")),
      h("span", { class: "spacer" }),
      fabric.pending ? h("span", { class: "muted-text small" }, "Reading schedules from Fabric...")
        : fabric.scanned ? h("span", { class: "muted-text small" }, `${fabric.workspaces} workspace${fabric.workspaces === 1 ? "" : "s"} read `, when(fabric.scanned)) : null,
      btn("Refresh", "sync", () => loadSchedules(box, true), "tool link small")),
    fabric.error ? h("p", { class: "muted-text" }, `Couldn't read Fabric's schedules: ${friendlyError(fabric.error).text}`)
      : !fabric.pending && !schedulesPage.allWorkspaces ? h("p", { class: "muted-text small" },
        "Pinned workspaces, your environments' workspaces, and the ones your notebooks publish to.") : null,
    fab.length ? h("table", { class: "list" },
      h("thead", {}, h("tr", {}, ["", "What", "Workspace", "When", "Last run", ""].map((c) => h("th", {}, c)))),
      h("tbody", {}, fab.map((s) => h("tr", {},
        h("td", {}, onOffButton(s.enabled, async (on) => {
          if (await attempt(() => rpc("set_fabric_schedule_enabled", { workspace_id: s.workspace_id, item_id: s.item_id,
            job_type: s.job_type, schedule_id: s.id, enabled: on, configuration: s.configuration }), on ? "Not turned on" : "Not paused")) {
            s.enabled = on; drawSchedules(box);
          }
        })),
        h("td", {}, h("div", { class: "row tight" }, itemIcon(s.item_type, 16), h("b", {}, s.name)), h("div", { class: "muted-text small" }, s.type)),
        h("td", {}, s.workspace_name),
        h("td", {}, s.text),
        h("td", {}, fabricRunPill(s.last), s.last ? h("span", { class: "muted-text small" }, " ", when(s.last.started)) : null,
          s.last?.message ? h("div", { class: "reason" }, s.last.message) : null),
        h("td", { class: "n nowrap" },
          btn("Run now", "play", async () => {
            const r = await attempt(() => rpc("run_fabric_item_now", { workspace_id: s.workspace_id, item_id: s.item_id,
              job_type: s.job_type, name: s.name, workspace_name: s.workspace_name }), "Not started");
            if (r) toast(`${s.name} started in Fabric. Follow it under Runs.`);
          }, "tool link small"),
          btn("Open", "publish", () => openInFabric(fabricItemLink({ type: s.item_type, item_id: s.item_id, workspace_id: s.workspace_id })), "tool link small"),
          btn("Remove", "delete", async () => {
            if (await attempt(() => rpc("delete_fabric_schedule", { workspace_id: s.workspace_id, item_id: s.item_id,
              job_type: s.job_type, schedule_id: s.id }), "Not removed")) redraw();
          }, "tool link small"))))))
      : fabric.pending ? null : h("p", { class: "muted-text" }, (fabric.items || []).length ? "No Fabric schedule matches the search."
        : "No schedules in these workspaces."));
}
