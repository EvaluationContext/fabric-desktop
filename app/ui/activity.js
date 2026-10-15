// What's running, where, and how it went: the environment picker and compute pill in the top
// bar, the activity tray behind the bell, plain-language errors, and the small helpers every
// page uses to show a run the same way. Loaded before app.js; everything here runs after startup.

// ---------- times ----------

function relTime(iso) {
  if (!iso) return "";
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return String(iso);
  const s = Math.round((Date.now() - t.getTime()) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 172800) return "yesterday";
  return t.toLocaleDateString();
}

// A relative time with the exact time on hover.
function when(iso) {
  return h("span", { title: iso ? new Date(iso).toLocaleString() : "" }, relTime(iso));
}

function took(seconds) {
  if (seconds === null || seconds === undefined || seconds === "") return "";
  const s = Number(seconds);
  if (s < 10) return `${Math.round(s * 10) / 10}s`;
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ${Math.round(s % 60)}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

// What a metadata run's status means in one line: the reason when it didn't succeed.
function runReason(r) {
  if (r.status === "ok") return "";
  return r.message || (r.status === "needs_decision" ? "Waiting for your choice" : "");
}

// ---------- plain-language errors ----------

// Sidecar errors arrive as "ExceptionName: message". Known causes become a sentence; the raw
// text stays available under Details.
function friendlyError(err) {
  const raw = String(err?.message || err || "").trim();
  const known = [
    [/FABRIC_DESKTOP_CLIENT_ID|client\) ID is required/i, "Sign-in isn't set up on this PC yet. The app needs its Entra app registration (see the README)."],
    [/InsufficientScopes|AADSTS65001|consent_required/i, "The app's Entra registration is missing a permission this needs. Add the one named in Details (for running or scheduling in Fabric it is Item.Execute.All under Power BI Service), grant consent, then sign in again."],
    [/AADSTS|invalid_grant|interaction_required|\b401\b|Unauthorized|token.*expired/i, "Your sign-in expired. Sign in again and retry."],
    [/\b403\b|Forbidden|InsufficientPrivileges|PrincipalTypeNotSupported/i, "You don't have permission for that in this workspace. A workspace admin can make you a Contributor or above."],
    [/\b404\b|NotFound|ItemNotFound|does not exist/i, "Fabric couldn't find that item. It may have been deleted or renamed."],
    [/\b429\b|TooManyRequests|throttl/i, "Fabric is busy and asked the app to slow down. Try again in a minute."],
    [/ConnectionError|Max retries|timed out|Timeout|NameResolution|getaddrinfo|ECONN|network/i, "Couldn't reach Fabric. Check the internet connection and try again."],
    [/Application Control policy|WinError 4551/i, "Windows blocked part of the local engine (Smart App Control or an App Control policy). Restart Fabric Desktop to see which files and what to do."],
    [/CapacityNotActive|capacity.*paused|CapacityLimitExceeded/i, "The workspace's Fabric capacity is paused or full."],
    [/^The Python sidecar stopped/, "The local engine stopped while doing this. It restarts on your next action; if it keeps stopping, the details below show why."],
  ];
  for (const [re, text] of known) if (re.test(raw)) return { text, details: raw };
  // The sidecar's own errors already explain themselves; only drop the exception name.
  const m = /^(ValueError|LookupError|KeyError|RuntimeError|PermissionError|FileNotFoundError|GateError): ([\s\S]*)$/.exec(raw);
  if (m) return { text: m[2].replace(/^'(.*)'$/, "$1"), details: m[1] === "GateError" ? "" : raw };
  if (/^[A-Z][A-Za-z]*(Error|Exception): /.test(raw)) {
    return { text: "Something went wrong inside the app. The details below help with a bug report.", details: raw };
  }
  return { text: raw, details: "" };
}

// ---------- activity ----------

// Items: {id, status: info|ok|error|failed|running, title, text, details, at, progress, results, url, request}.
// Kept on this PC so the history survives a restart; nothing in them is needed to work.
const activity = { items: [], open: false, filter: "all", seenAt: 0, pollTimer: null };

(function loadActivity() {
  try {
    const saved = JSON.parse(localStorage.getItem("fd.activity") || "{}");
    activity.items = (saved.items || []).map((i) => (i.status === "running" ? { ...i, stale: true } : i));
    activity.seenAt = saved.seenAt || 0;
  } catch { /* private window or cleared storage: start empty */ }
})();

function saveActivity() {
  try {
    localStorage.setItem("fd.activity", JSON.stringify({ items: activity.items.slice(0, 80), seenAt: activity.seenAt }));
  } catch { /* storage full or blocked: the tray still works for this session */ }
}

function logActivity(item) {
  const id = item.id || `a${Date.now()}${Math.random().toString(36).slice(2, 6)}`;
  const i = activity.items.findIndex((x) => x.id === id);
  const next = { at: new Date().toISOString(), ...(i >= 0 ? activity.items[i] : {}), ...item, id, stale: false };
  if (i >= 0) activity.items.splice(i, 1);
  activity.items.unshift(next);
  activity.items = activity.items.slice(0, 80);
  saveActivity();
  renderBell();
  if (activity.open) renderTray();
  return next;
}

const PROBLEM = new Set(["error", "failed"]);

function renderBell() {
  const bell = document.getElementById("bell");
  if (!bell) return;
  const running = activity.items.filter((i) => i.status === "running" && !i.stale).length;
  const problems = activity.items.filter((i) => PROBLEM.has(i.status) && new Date(i.at).getTime() > activity.seenAt).length;
  const count = running + problems;
  const parts = [running ? `${running} running` : "", problems ? `${problems} new problem${problems === 1 ? "" : "s"}` : ""].filter(Boolean);
  bell.setAttribute("aria-label", `Activity${parts.length ? `, ${parts.join(", ")}` : ""}`);
  bell.title = parts.length ? `Activity: ${parts.join(", ")}` : "Activity";
  bell.classList.toggle("on", activity.open);
  bell.replaceChildren(icon("alert", 20), ...(count ? [h("span", { class: `badge${problems ? " bad" : ""}` }, count)] : []));
}

function toggleTray(open = !activity.open) {
  activity.open = open;
  if (open) activity.seenAt = Date.now();
  saveActivity();
  renderBell();
  renderTray();
  placeFabricView();
}

function renderTray() {
  const root = document.getElementById("tray-root");
  if (!activity.open) { root.replaceChildren(); return; }
  const running = activity.items.filter((i) => i.status === "running" && !i.stale);
  const problems = activity.items.filter((i) => PROBLEM.has(i.status));
  const shown = activity.filter === "running" ? running : activity.filter === "problems" ? problems : activity.items;
  const chip = (id, label) => h("button", { class: `chip${activity.filter === id ? " on" : ""}`, onclick: () => { activity.filter = id; renderTray(); } }, label);
  root.replaceChildren(h("aside", { class: "tray", "aria-label": "Activity" },
    h("div", { class: "tray-head" }, h("h2", {}, "Activity"), h("span", { class: "spacer" }),
      btn("Clear finished", null, () => {
        activity.items = activity.items.filter((i) => i.status === "running" && !i.stale);
        saveActivity(); renderBell(); renderTray();
      }, "tool link small"),
      h("button", { class: "tool small", "aria-label": "Close", onclick: () => toggleTray(false) }, icon("dismiss", 16))),
    h("div", { class: "tray-chips" }, chip("all", "All"), chip("running", `Running ${running.length}`), chip("problems", `Problems ${problems.length}`)),
    h("div", { class: "tray-list" }, shown.length ? shown.map(trayItem)
      : h("p", { class: "muted-text tray-empty" }, activity.filter === "all" ? "Nothing yet. Runs, publishes and problems show up here." : "Nothing here."))));
}

const TRAY_LABEL = { running: ["Running", "info"], ok: ["Done", "pass"], info: ["Done", "pass"], failed: ["Failed", "fail"], error: ["Couldn't finish", "fail"] };

function trayItem(i) {
  const [label, cls] = i.stale ? ["Unknown", "neu"] : TRAY_LABEL[i.status] || ["", "neu"];
  const links = [];
  if (i.results) links.push(btn("See results", null, () => { toggleTray(false); showRunResults(i.results, i.results.find((r) => r.status === "needs_decision")); }, "tool link small"));
  if (i.monitor) links.push(btn("Open in Monitor", null, () => { toggleTray(false); go("monitor"); }, "tool link small"));
  if (i.url) links.push(btn("Open in Fabric", null, () => { toggleTray(false); openInFabric(i.url); }, "tool link small"));
  if (i.details) links.push(btn("Copy details", null, () => attempt(() => navigator.clipboard.writeText(i.details)).then(() => toast("Copied", false, { quiet: true })), "tool link small"));
  return h("div", { class: `tray-item${PROBLEM.has(i.status) ? " bad" : ""}` },
    h("div", { class: "row" }, h("span", { class: `pill-s ${cls}` }, label), h("b", { class: "grow" }, i.title || ""), h("span", { class: "muted-text small" }, when(i.at))),
    i.status === "running" && !i.stale ? h("div", { class: "progress", role: "progressbar", "aria-label": i.title },
      h("div", { class: i.progress === undefined ? "bar indeterminate" : "bar", style: i.progress === undefined ? "" : `width:${Math.round(i.progress * 100)}%` })) : null,
    i.text ? h("div", { class: "tray-text" }, i.text) : null,
    i.stale ? h("div", { class: "muted-text small" }, "The app was closed while this ran. Monitor has its final state.") : null,
    i.details ? h("details", {}, h("summary", {}, "Details"), h("pre", {}, i.details)) : null,
    links.length ? h("div", { class: "row wrap" }, links) : null);
}

// Jobs the app started in Fabric: follow every unfinished one until it ends, wherever the user is.
async function trackFabricJobs() {
  clearTimeout(activity.pollTimer);
  const runs = await rpc("fabric_runs", { limit: 50 }).catch(() => null);
  if (!runs) return;
  let open = 0;
  for (const r of runs) {
    const id = `fabric:${r.job_id}`;
    const item = activity.items.find((i) => i.id === id);
    const done = FABRIC_DONE.has(r.status);
    if (!done) {
      open++;
      if (!item || item.status !== "running" || item.stale) {
        logActivity({ id, status: "running", title: item?.title || `${r.name} · ${r.kind}`, text: `In Fabric on ${r.workspace_name || "the workspace"}`, url: r.url, at: r.started_at, monitor: true });
      }
    } else if (item && item.status === "running" && !item.request) {
      // Metadata runs (item.request) finish through watchMetaFabricRun, which also reads their results.
      const ok = r.status === "Completed";
      logActivity({ id, status: ok ? "ok" : "failed", text: ok ? "Finished in Fabric" : `${r.status} in Fabric. ${r.message || ""}`.trim(), at: r.finished_at || new Date().toISOString() });
      toast(`${item.title}: ${ok ? "finished" : r.status.toLowerCase()} in Fabric`, !ok, { quiet: true });
    }
  }
  if (open) activity.pollTimer = setTimeout(trackFabricJobs, 15000);
}

// ---------- environment and compute in the top bar ----------

function envWhere(p, env) {
  const cfg = p?.environments?.[env] || {};
  const local = (cfg.target || (env === "dev" ? "local" : "onelake")) === "local";
  const ws = cfg.workspace_name || cfg.workspace_id;
  if (!p || local) return { fabric: false, text: "Runs on this PC", short: "This PC" };
  if (!ws) return { fabric: false, text: "No workspace yet", short: "Not set up" };
  if (cfg.compute === "fabric") return { fabric: true, text: `Runs in Fabric · ${ws}`, short: `Fabric · ${ws}` };
  return { fabric: false, text: `Runs on this PC · writes to ${ws}`, short: `This PC · writes to ${ws}` };
}

function setGlobalEnv(env) {
  state.meta.env = env;
  state.meta.runs = null;
  try { localStorage.setItem("fd.env", env); } catch { /* not remembered: fine */ }
  render();
}

function renderEnvBar() {
  const slot = document.getElementById("env-slot");
  if (!slot || !state.meta) return;
  const env = state.meta.env;
  const where = envWhere(state.meta.project, env);
  const select = h("select", { class: "env-select", "aria-label": "Environment", onchange: (e) => setGlobalEnv(e.target.value) },
    Object.entries(ENV_LABEL).map(([id, label]) => h("option", { value: id, selected: env === id ? true : null }, label)));
  slot.replaceChildren(
    h("label", { class: "env-label" }, h("span", { class: "muted-text" }, "Environment"), select),
    h("button", { class: `pill compute${where.fabric ? " fabric" : ""}`, title: "Where this environment runs. Change it in Metadata > Environments and promote.",
      onclick: () => goMeta("environments") }, icon(where.fabric ? "cloud" : "desktop", 14), where.text));
}

// ---------- one way to show a run ----------

// An entity name that wraps after its layer ("silver." then "orders"), never mid-word.
function entityName(name) {
  const [layer, ...rest] = String(name).split(".");
  return rest.length ? [`${layer}.`, h("wbr"), rest.join(".")] : name;
}

// A metadata run row for the entity page: status, where and when, and the reason if it failed.
function runLine(r, onOpen) {
  const where = r.compute === "fabric" ? "Fabric" : "This PC";
  const reason = runReason(r);
  return h("div", { class: "run-line" },
    statusPill(r.status),
    h("div", { class: "grow" },
      h("div", {}, `${ENV_LABEL[r.env] || r.env} · ${where} · `, when(r.started_at)),
      h("div", { class: reason ? "reason" : "muted-text small" },
        reason || `${r.rows_written ?? 0} rows written${r.quarantined ? `, ${r.quarantined} quarantined` : ""} · ${took(r.seconds)}`)),
    onOpen ? btn("Details", null, onOpen, "tool link small") : null);
}
