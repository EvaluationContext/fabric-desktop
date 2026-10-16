// Settings, the account menu, first-run sign-in setup, Report a problem, What's new and
// Get started. The values live in the sidecar's app_settings.py: this person's choices, or
// what their organization fixed in its policy file.

const SOURCE_TEXT = {
  policy: "Set by your organization",
  environment: "Set on this PC (FABRIC_DESKTOP_CLIENT_ID)",
  user: "Set by you",
  built_in: "Comes with Fabric Desktop",
  default: "Default",
};

async function loadSettings() {
  state.settings = await rpc("app_settings").catch(() => state.settings || null);
  return state.settings;
}

async function saveSettings(changes, label = "Not saved") {
  const res = await attempt(() => rpc("save_app_settings", { changes }), label);
  if (res) state.settings = res;
  return res;
}

function clientId() {
  return state.settings?.client_id?.value || "";
}

// The link an Entra admin opens once to approve Fabric Desktop for everyone in their organization.
function adminConsentUrl(id = clientId()) {
  return `https://login.microsoftonline.com/organizations/adminconsent?client_id=${encodeURIComponent(id)}`;
}

async function copyText(text, what) {
  try {
    await navigator.clipboard.writeText(text);
    toast(`${what} copied`);
    return true;
  } catch {
    return false;
  }
}

function openLink(url) {
  return attempt(() => invoke("open_link", { url }), "Could not open the link");
}

// ---------- sign-in setup and sign-out ----------

// First sign-in on a PC with no app ID from the build, IT or Settings: say what's needed and
// take the ID here, never through a command prompt.
function signInSetup() {
  return new Promise((resolve) => {
    const input = h("input", { placeholder: "00000000-0000-0000-0000-000000000000", spellcheck: "false" });
    const error = h("p", { class: "field-error", hidden: true });
    const done = (ok) => { closeDialog(); resolve(ok); };
    const save = async () => {
      const res = await rpc("save_app_settings", { changes: { client_id: input.value.trim() } }).catch((e) => {
        error.textContent = friendlyError(e).text;
        error.hidden = false;
        return null;
      });
      if (!res) return;
      state.settings = res;
      done(true);
    };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") save(); });
    showDialog("Set up sign-in", h("div", { class: "setup" },
      h("p", {}, "Fabric Desktop signs in with your work account, the same one you use for Fabric. ",
        "It does that through an app your organization approves once."),
      h("p", {}, "Paste the app (client) ID your Fabric or Microsoft Entra admin gave you. ",
        "If you don't have one, send your admin the setup guide."),
      h("label", { class: "field" }, "App (client) ID", input), error,
      h("p", { class: "muted-text small" }, "Admins can set this for everyone instead, so nobody sees this step. ",
        learnMore("for-it-admins", "How"))),
    [btn("Send the guide to my admin", null, () => openLink("https://github.com/KratosDataGod/fabric-desktop/blob/main/docs/permissions.md"), "tool link"),
      h("span", { class: "spacer" }),
      btn("Cancel", null, () => done(false), "tool outline"),
      btn("Save and sign in", null, save, "tool primary")]);
  });
}

async function signOut() {
  if (!(await confirmDialog("Sign out?", "Fabric Desktop forgets your sign-in on this PC, in the app and in the Fabric views. " +
    "Scheduled runs on this PC wait until someone signs in again. Notebooks and settings stay.", "Sign out"))) return;
  await attempt(() => rpc("sign_out"), "Sign-out failed");
  await invoke("fabric_view_sign_out").catch(() => {});
  Object.assign(state, { user: null, tenant: null, workspaces: null, openWorkspace: null });
  toast("Signed out");
  go("home");
}

// ---------- account menu ----------

function accountMenu(anchor) {
  const items = state.user
    ? [{ info: `Signed in as ${state.user}` }]
    : [{ label: "Sign in", icon: "person", action: () => signIn().then((ok) => ok && render()) }];
  items.push(
    { label: "Settings", icon: "settings", action: () => go("settings") },
    { label: "Help and user guide", icon: "help", action: () => invoke("open_doc", { page: "user-guide.md" }).catch(() => {}) },
    { label: "Report a problem", icon: "bug", action: reportProblem },
    { label: "What's new", icon: "rocket", action: () => showWhatsNew(true) },
    { info: `Fabric Desktop build ${state.build || "unknown"} · ${channelLabel()}` },
  );
  if (state.user) items.push({ label: "Sign out", icon: "signOut", action: signOut });
  showMenu(anchor, items);
}

function channelLabel() {
  return state.settings?.channel?.value === "preview" ? "Preview channel" : "Stable channel";
}

// ---------- Report a problem ----------

function diagnostics() {
  const errors = activity.items.filter((i) => i.status === "error" || i.status === "failed").slice(0, 10);
  return [
    `Fabric Desktop build: ${state.build || "unknown"} (${channelLabel()})`,
    `Engine Python: ${state.python || "unknown"}`,
    `Tenant: ${state.tenant || "not signed in"}`,
    `Page: ${state.view}`,
    `Windows: ${navigator.userAgent}`,
    "",
    errors.length ? "Recent problems (newest first):" : "No recent problems in Activity.",
    ...errors.map((i) => `- ${new Date(i.at).toISOString()} ${i.title}${i.text ? `: ${i.text}` : ""}${i.details ? `\n    ${String(i.details).slice(0, 400).replace(/\n/g, "\n    ")}` : ""}`),
  ].join("\n");
}

function reportProblem() {
  const what = h("textarea", { rows: 3, placeholder: "What were you doing, and what went wrong?" });
  const info = h("textarea", { rows: 9, class: "mono", readonly: true }, diagnostics());
  const support = state.settings?.support_url?.value || "";
  const report = () => `${what.value.trim() || "(no description)"}\n\n${info.value}`;
  const send = async () => {
    const text = report();
    if (support.startsWith("mailto:")) {
      // Mail apps cap a link's length; the full text is also on the clipboard.
      await copyText(text, "Report");
      openLink(`${support}${support.includes("?") ? "&" : "?"}subject=${encodeURIComponent("Fabric Desktop problem")}&body=${encodeURIComponent(text.slice(0, 1500))}`);
    } else {
      await copyText(text, "Report");
      openLink(support || "https://github.com/KratosDataGod/fabric-desktop/issues/new");
    }
    closeDialog();
  };
  showDialog("Report a problem", h("div", { class: "setup" },
    h("label", { class: "field" }, "What happened", what),
    h("label", { class: "field" }, "Sent with it (no data, file contents or passwords)", info),
    h("p", { class: "muted-text small" }, support
      ? "Send copies all of this and opens your organization's support contact. Paste it there if it isn't filled in."
      : "Send copies all of this and opens a new issue on GitHub. Paste it there.")),
  [btn("Copy only", null, () => copyText(report(), "Report"), "tool outline"), h("span", { class: "spacer" }),
    btn("Cancel", null, closeDialog, "tool outline"), btn("Send", null, send, "tool primary")]);
}

// ---------- What's new ----------

// After an update, the release notes of the build now running, once. From the account menu,
// whenever asked.
function showWhatsNew(asked = false) {
  const notes = state.release?.commit && state.release.commit === state.commit ? state.release.notes : "";
  let seen = "";
  try { seen = localStorage.getItem("fd.whatsnew.seen") || ""; } catch { /* show it */ }
  if (!asked && (!notes || !state.commit || seen === state.commit)) return;
  try { localStorage.setItem("fd.whatsnew.seen", state.commit || ""); } catch { /* shown again next time */ }
  const lines = String(notes || "").split(/\r?\n/).map((l) => l.replace(/^[-*]\s*/, "").trim()).filter(Boolean);
  showDialog("What's new in Fabric Desktop", h("div", { class: "setup" },
    lines.length ? h("ul", { class: "whatsnew" }, lines.map((l) => h("li", {}, l)))
      : h("p", {}, "No notes for this build."),
    h("p", { class: "muted-text small" }, `Build ${state.build || "unknown"} · ${channelLabel()}. `, learnMore("updates", "About updates"))),
  [btn("Got it", null, closeDialog, "tool primary")]);
}

// ---------- Settings page ----------

function settingRow(title, help, control, source) {
  return h("div", { class: "setting" },
    h("div", { class: "setting-text" }, h("b", {}, title), help ? h("div", { class: "muted-text small" }, help) : null,
      source ? h("div", { class: "setting-source" }, SOURCE_TEXT[source] || source) : null),
    h("div", { class: "setting-control" }, control));
}

async function renderSettings() {
  setView(h("div", { class: "page settings" }, h("h1", {}, "Settings"), h("p", { class: "muted-text" }, "Loading...")));
  const s = await loadSettings();
  if (state.view !== "settings") return;
  if (!s) {
    setView(h("div", { class: "page settings" }, h("h1", {}, "Settings"),
      h("p", { class: "muted-text" }, "Settings load once the local engine is running.")));
    return;
  }
  const locked = (key) => s[key].source === "policy";

  // Account
  const idInput = h("input", { value: s.client_id.source === "user" ? s.client_id.value : "", spellcheck: "false",
    placeholder: s.client_id.value || "00000000-0000-0000-0000-000000000000", disabled: locked("client_id") ? true : null });
  const account = h("section", { class: "card pad" }, h("h2", {}, "Account"),
    settingRow(state.user ? `Signed in as ${state.user}` : "Not signed in",
      state.user ? (state.tenant ? `Directory ${state.tenant}` : "") : "Sign in to see your Fabric workspaces and lakehouses.",
      state.user ? btn("Sign out", "signOut", signOut, "tool outline")
        : btn("Sign in", "person", async () => { if (await signIn()) render(); }, "tool primary")),
    settingRow("App (client) ID", "The Microsoft Entra app Fabric Desktop signs in with. Leave it empty to use the one that comes with the app.",
      h("div", { class: "row" }, idInput,
        locked("client_id") ? null : btn("Save", null, async () => {
          if (await saveSettings({ client_id: idInput.value.trim() })) { toast("Saved. Sign in again to use it."); render(); }
        }, "tool outline")),
      s.client_id.value ? s.client_id.source : null),
    s.client_id.value ? settingRow("Admin approval",
      "If sign-in says your organization needs to approve Fabric Desktop, send this link to a Microsoft Entra admin. They open it once for everyone.",
      btn("Copy the link", "copyArrowRight", async () => {
        if (!(await copyText(adminConsentUrl(), "Link"))) toast(adminConsentUrl());
      }, "tool outline")) : null);

  // Updates
  const channel = (value, label, help) => h("label", { class: `choice-row${s.channel.value === value ? " selected" : ""}` },
    h("input", { type: "radio", name: "channel", value, checked: s.channel.value === value ? true : null, disabled: locked("channel") ? true : null,
      onchange: async () => { if (await saveSettings({ channel: value })) { toast(`${label}: takes effect the next time Fabric Desktop starts`); render(); } } }),
    h("span", {}, h("b", {}, label), h("span", { class: "muted-text small" }, help)));
  const updates = h("section", { class: "card pad" }, h("h2", {}, "Updates"),
    h("p", { class: "muted-text small" }, "Fabric Desktop checks for an update each time it starts and installs it before opening. ",
      `This is build ${state.build || "unknown"}. `, learnMore("updates")),
    h("div", { class: "choices" },
      channel("stable", "Stable", "Builds that have been checked and released. Recommended."),
      channel("preview", "Preview", "Every new build as soon as it's ready. For testers; things may change or break.")),
    s.channel.source === "default" ? null : h("div", { class: "setting-source" }, SOURCE_TEXT[s.channel.source]),
    h("div", { class: "row" }, btn("What's new in this build", "rocket", () => showWhatsNew(true), "tool link small")));

  // Preview features
  const previews = h("section", { class: "card pad" }, h("h2", {}, "Preview features"),
    settingRow("Show features in preview",
      `These work but haven't been tried on many Fabric tenants yet, so they may change: ${Object.values(PREVIEW_FEATURES).join(", ")}.`,
      h("label", { class: "switch" }, h("input", { type: "checkbox", checked: previewShown() ? true : null, disabled: locked("show_preview") ? true : null,
        onchange: async (e) => { if (await saveSettings({ show_preview: e.target.checked })) render(); } }), h("span", {}, previewShown() ? "On" : "Off")),
      s.show_preview.source === "policy" ? "policy" : null));

  // Notifications
  const notify = h("section", { class: "card pad" }, h("h2", {}, "Notifications"),
    settingRow("Teams alerts", "Post late and failed tables to a Teams channel. The bell always shows them.",
      btn("Set up", "alert", () => alertsDialog(), "tool outline")));

  // Help and about
  const about = h("section", { class: "card pad" }, h("h2", {}, "Help and about"),
    h("div", { class: "row wrap" },
      btn("User guide", "help", () => invoke("open_doc", { page: "user-guide.md" }).catch(() => {}), "tool outline"),
      btn("Report a problem", "bug", reportProblem, "tool outline"),
      btn("Guide for IT admins", "shield", () => invoke("open_doc", { page: "permissions.md" }).catch(() => {}), "tool outline")),
    h("dl", { class: "about" },
      h("dt", {}, "Build"), h("dd", {}, state.build || "unknown"),
      h("dt", {}, "Engine Python"), h("dd", {}, state.python || "unknown"),
      h("dt", {}, "Organization settings file"), h("dd", { class: "mono" }, s.policy_file)));

  setView(h("div", { class: "page settings" },
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Settings"),
      h("p", { class: "sub" }, "How Fabric Desktop signs in, updates and talks to you. ", learnMore("settings")))),
    account, updates, previews, notify, about));
}

// ---------- Get started (Home, until there's a metadata project) ----------

function markDone(step) {
  try { localStorage.setItem(`fd.done.${step}`, "1"); } catch { /* not remembered */ }
}

function getStarted(project, notebookCount) {
  let hidden = false;
  const done = (step) => { try { return localStorage.getItem(`fd.done.${step}`) === "1"; } catch { return false; } };
  try { hidden = localStorage.getItem("fd.getstarted.hidden") === "1"; } catch { /* shown */ }
  if (hidden || project?.path) return null;
  const steps = [
    [!!state.user, "Sign in with your work account", "So the app can see your Fabric workspaces.", () => signIn().then((ok) => ok && render())],
    [done("lakehouse"), "Open a lakehouse", "Browse its tables and files, or query them with SQL.", () => go("onelake")],
    [notebookCount > 0, "Run a notebook on this PC", "Python and Spark-style code, run on your own processor.", () => newNotebook()],
    [false, "Set up Metadata", "Describe your medallion tables once and let Fabric Desktop build and run them.", () => go("metadata")],
  ];
  const box = h("section", { class: "card pad get-started" },
    h("div", { class: "row" }, h("h2", { class: "grow" }, "Get started"),
      btn("Hide", null, () => { try { localStorage.setItem("fd.getstarted.hidden", "1"); } catch { /* */ } box.remove(); }, "tool link small")),
    h("ol", {}, steps.map(([ok, title, help, run]) => h("li", { class: ok ? "done" : null },
      h("span", { class: "step-mark" }, ok ? icon("ok", 16) : null),
      h("div", { class: "grow" }, h("b", {}, title), h("div", { class: "muted-text small" }, help)),
      ok ? null : btn("Start", null, run, "tool outline small")))),
    h("p", { class: "muted-text small" }, learnMore("get-started", "Read the user guide")));
  return box;
}

registerFeature({
  id: "settings",
  rail: { label: "Settings", icon: "settings", order: 100, bottom: true },
  views: { settings: renderSettings },
  commands: [
    { label: "Open Settings", icon: "settings", keywords: "preferences options channel app id", run: () => go("settings") },
    { label: "Sign out", icon: "signOut", keywords: "log out account", run: signOut, when: () => !!state.user },
    { label: "Sign in", icon: "person", keywords: "log in account", run: () => signIn().then((ok) => ok && render()), when: () => !state.user },
    { label: "Report a problem", icon: "bug", keywords: "bug support help feedback", run: reportProblem },
    { label: "What's new", icon: "rocket", keywords: "release notes update changelog", run: () => showWhatsNew(true) },
    { label: "Open the user guide", icon: "help", keywords: "help docs documentation", run: () => invoke("open_doc", { page: "user-guide.md" }).catch(() => {}) },
  ],
});
