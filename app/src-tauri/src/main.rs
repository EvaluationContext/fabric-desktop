// Fabric Desktop shell: hosts the UI and forwards calls to the Python sidecar,
// which does all data work on this machine.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::collections::VecDeque;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::{Arc, Mutex};

use serde_json::{json, Value};
use tauri::webview::{NewWindowResponse, WebviewBuilder};
use tauri::{Emitter, LogicalPosition, LogicalSize, Manager, Url, WebviewUrl};

const SIDECAR_EXITED: &str = "The Python sidecar stopped. It will restart on the next action.";

/// Startup check and updater, run with the user's Python before the sidecar starts.
const UPDATER: &str = include_str!("updater.py");

/// The main commit this exe was built from; empty for local builds, which skip updates.
const BUILD_COMMIT: &str = env!("FABRIC_BUILD_COMMIT");

/// Where release builds get their engine: Python with the sidecar and every package it
/// needs, built and (once a certificate is set up) code-signed by CI, so the PC's own
/// Python and its unsigned packages are not involved.
const ENGINE_URL: &str = "https://github.com/KratosDataGod/fabric-desktop/releases/download/latest/fabric-desktop-engine.zip";

/// Release builds run the bundled engine; local builds, and anyone who sets
/// FABRIC_DESKTOP_PYTHON, use a Python of their own with the sidecar pip-installed.
fn bundled() -> bool {
    !BUILD_COMMIT.is_empty() && std::env::var_os("FABRIC_DESKTOP_PYTHON").is_none()
}

/// One folder per build: engine\<commit>\python.exe.
fn engine_root() -> PathBuf {
    data_dir().join("engine")
}

/// This build's engine, or failing that another build's, which can still run the
/// startup check that then updates this exe or the engine.
fn engine_python() -> Option<PathBuf> {
    if !bundled() {
        return None;
    }
    let exact = engine_root().join(BUILD_COMMIT).join("python.exe");
    if exact.exists() {
        return Some(exact);
    }
    std::fs::read_dir(engine_root())
        .ok()?
        .flatten()
        .filter(|e| !e.file_name().to_string_lossy().starts_with('.'))
        .map(|e| e.path().join("python.exe"))
        .find(|p| p.exists())
}

/// The interpreter that runs the sidecar and the startup check.
fn python() -> String {
    if let Ok(path) = std::env::var("FABRIC_DESKTOP_PYTHON") {
        return path;
    }
    engine_python().map(|p| p.to_string_lossy().into_owned()).unwrap_or_else(|| "python".into())
}

fn hide_window(cmd: &mut Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    #[cfg(not(windows))]
    let _ = cmd;
}

/// The app's own data folder, where the sidecar's log and the updater's working folder live.
fn data_dir() -> PathBuf {
    let dir = std::env::var_os("LOCALAPPDATA")
        .map(|d| PathBuf::from(d).join("FabricDesktop"))
        .unwrap_or_else(std::env::temp_dir);
    let _ = std::fs::create_dir_all(&dir);
    dir
}

fn updater(args: &[&str]) -> Command {
    // Run from the app's own data folder so no stray .py file in the current
    // folder can shadow a standard library module.
    let mut cmd = Command::new(python());
    cmd.args(["-u", "-c", UPDATER]).args(args).current_dir(data_dir()).stdin(Stdio::null());
    hide_window(&mut cmd);
    cmd
}

/// Runs an updater command, sending each output line to the UI as a
/// `component-log` event. Returns the last lines of output when it fails.
fn stream(app: &tauri::AppHandle, mut cmd: Command) -> Result<(), String> {
    let mut child = cmd
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("Could not run Python: {e}"))?;
    let mut tail = VecDeque::new();
    for line in BufReader::new(child.stdout.take().expect("updater stdout")).lines() {
        let line = line.unwrap_or_default();
        let _ = app.emit("component-log", &line);
        if tail.len() == 12 {
            tail.pop_front();
        }
        tail.push_back(line);
    }
    let mut stderr = String::new();
    let _ = child.stderr.take().expect("updater stderr").read_to_string(&mut stderr);
    let status = child.wait().map_err(|e| e.to_string())?;
    if status.success() {
        return Ok(());
    }
    tail.extend(stderr.lines().map(str::to_string));
    let start = tail.len().saturating_sub(12);
    Err(tail.iter().skip(start).cloned().collect::<Vec<_>>().join("\n"))
}

struct Sidecar {
    // Shared with SidecarState so Stop can kill the process while a call is blocked on it.
    child: Arc<Mutex<Child>>,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    next_id: u64,
    log: PathBuf,
}

impl Sidecar {
    fn start() -> std::io::Result<Self> {
        // A windowed exe has no stderr to hand down, and Python started without one
        // exits at once. Send it to a log file instead, so crashes can be explained.
        let log = data_dir().join("sidecar.log");
        let stderr = std::fs::File::create(&log).map(Stdio::from).unwrap_or_else(|_| Stdio::null());
        let mut cmd = Command::new(python());
        cmd.args(["-m", "fabric_desktop.server"])
            .current_dir(data_dir())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(stderr);
        hide_window(&mut cmd);
        let mut child = cmd.spawn()?;
        let stdin = child.stdin.take().expect("sidecar stdin");
        let stdout = BufReader::new(child.stdout.take().expect("sidecar stdout"));
        Ok(Self { child: Arc::new(Mutex::new(child)), stdin, stdout, next_id: 1, log })
    }

    /// The exit message plus the last thing the sidecar wrote to its log, which is
    /// usually the Python error that stopped it.
    fn exited(&self) -> String {
        let text = std::fs::read_to_string(&self.log).unwrap_or_default();
        let tail: Vec<&str> = text.lines().filter(|l| !l.trim().is_empty()).collect();
        let tail = tail[tail.len().saturating_sub(3)..].join("\n");
        if tail.is_empty() {
            SIDECAR_EXITED.to_string()
        } else {
            format!("{SIDECAR_EXITED}\n{tail}\nFull log: {}", self.log.display())
        }
    }

    fn call(&mut self, method: &str, params: Value) -> Result<Value, String> {
        let id = self.next_id;
        self.next_id += 1;
        let request = json!({ "id": id, "method": method, "params": params });
        if writeln!(self.stdin, "{request}").and_then(|_| self.stdin.flush()).is_err() {
            return Err(self.exited());
        }

        let mut line = String::new();
        let read = self.stdout.read_line(&mut line).map_err(|e| e.to_string())?;
        if read == 0 {
            return Err(self.exited());
        }
        let response: Value = serde_json::from_str(&line).map_err(|e| e.to_string())?;
        if let Some(error) = response.get("error") {
            return Err(error.as_str().unwrap_or("Unknown sidecar error").to_string());
        }
        Ok(response.get("result").cloned().unwrap_or(Value::Null))
    }
}

struct SidecarState {
    sidecar: Mutex<Option<Sidecar>>,
    child: Mutex<Option<Arc<Mutex<Child>>>>,
}

#[tauri::command]
async fn rpc(
    state: tauri::State<'_, SidecarState>,
    method: String,
    params: Option<Value>,
) -> Result<Value, String> {
    let mut guard = state.sidecar.lock().map_err(|e| e.to_string())?;
    if guard.is_none() {
        let sidecar = Sidecar::start().map_err(|e| format!("Could not start the Python sidecar: {e}"))?;
        *state.child.lock().map_err(|e| e.to_string())? = Some(sidecar.child.clone());
        *guard = Some(sidecar);
    }
    let result = guard
        .as_mut()
        .expect("sidecar started")
        .call(&method, params.unwrap_or_else(|| json!({})));
    if matches!(&result, Err(e) if e.starts_with(SIDECAR_EXITED)) {
        *guard = None;
    }
    result
}

/// Stops a running notebook cell by ending the sidecar process. The blocked
/// `rpc` call then returns an error and the next call starts a fresh sidecar.
#[tauri::command]
fn stop_kernel(state: tauri::State<'_, SidecarState>) -> Result<(), String> {
    if let Some(child) = state.child.lock().map_err(|e| e.to_string())?.take() {
        let _ = child.lock().map_err(|e| e.to_string())?.kill();
    }
    Ok(())
}

/// Reports this exe's build, the Python and sidecar it will use, and the
/// latest release. Fails only when Python itself cannot run.
#[tauri::command]
async fn check_components() -> Result<Value, String> {
    let mut report = if bundled() && engine_python().is_none() {
        // Nothing to run the check with until the engine is installed; the UI installs
        // it and then checks again.
        json!({
            "python": null, "python_ok": true,
            "sidecar": { "version": null, "commit": null, "editable": false },
            "latest": { "commit": null, "error": null },
        })
    } else {
        let out = updater(&["check"])
            .output()
            .map_err(|e| format!("Python was not found ({e})."))?;
        if !out.status.success() {
            let stderr = String::from_utf8_lossy(&out.stderr);
            return Err(format!("Python could not run the startup check. {}", stderr.trim()));
        }
        serde_json::from_slice(&out.stdout)
            .map_err(|_| "Python gave an unexpected answer to the startup check.".to_string())?
    };
    report["bundled"] = json!(bundled());
    // Set by the exe that updated us, so the check can tell whether the update took.
    let args: Vec<String> = std::env::args().collect();
    let updated_to = args.iter().position(|a| a == "--updated-to").and_then(|i| args.get(i + 1));
    report["app"] = json!({
        "version": env!("CARGO_PKG_VERSION"),
        "commit": if BUILD_COMMIT.is_empty() { Value::Null } else { json!(BUILD_COMMIT) },
        "updated_to": updated_to,
    });
    Ok(report)
}

/// Installs the sidecar from the commit this exe was built from, so the two always match:
/// the bundled engine for release builds, else pip into the user's Python. Returns the
/// engine's build, which can be newer than this exe when the release has moved on.
#[tauri::command]
async fn update_sidecar(app: tauri::AppHandle, state: tauri::State<'_, SidecarState>) -> Result<Value, String> {
    if BUILD_COMMIT.is_empty() {
        return Err("This is a local build, so it cannot pick a sidecar version. Install it with pip.".into());
    }
    // A running sidecar holds its files open, which blocks pip and the engine swap on Windows.
    stop_kernel(state.clone())?;
    *state.sidecar.lock().map_err(|e| e.to_string())? = None;
    if bundled() {
        return install_engine(&app).map(|commit| json!({ "engine": commit }));
    }
    stream(&app, updater(&["install-sidecar", BUILD_COMMIT]))?;
    Ok(Value::Null)
}

/// Runs one of Windows' own tools (curl, tar), returning its error output when it fails.
fn run_tool(mut cmd: Command, what: &str) -> Result<(), String> {
    hide_window(&mut cmd);
    let out = cmd.stdin(Stdio::null()).output().map_err(|e| format!("Could not {what}: {e}"))?;
    if out.status.success() {
        return Ok(());
    }
    Err(format!("Could not {what}: {}", String::from_utf8_lossy(&out.stderr).trim()))
}

/// Downloads the latest engine and unpacks it to engine\<its commit>, then removes
/// other builds' engines. Uses curl.exe and tar.exe, which come with Windows.
fn install_engine(app: &tauri::AppHandle) -> Result<String, String> {
    let say = |line: &str| {
        let _ = app.emit("component-log", line);
    };
    let root = engine_root();
    std::fs::create_dir_all(&root).map_err(|e| e.to_string())?;
    let zip = root.join("download.zip");
    let staging = root.join(".new");
    let nocache = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);

    say("Downloading the engine (Python and its packages, about 150 MB)…");
    let mut curl = Command::new("curl.exe");
    curl.args(["-L", "--fail", "-sS", "-H", "Cache-Control: no-cache", "-o"])
        .arg(&zip)
        .arg(format!("{ENGINE_URL}?nocache={nocache}"));
    run_tool(curl, "download the engine")?;

    say("Unpacking the engine…");
    let _ = std::fs::remove_dir_all(&staging);
    std::fs::create_dir_all(&staging).map_err(|e| e.to_string())?;
    let mut tar = Command::new("tar.exe");
    tar.arg("-xf").arg(&zip).arg("-C").arg(&staging);
    run_tool(tar, "unpack the engine")?;
    let _ = std::fs::remove_file(&zip);

    let info: Value = std::fs::read_to_string(staging.join("engine.json"))
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .ok_or("The engine download has no engine.json, so it is not a Fabric Desktop engine.")?;
    let commit = info["commit"].as_str().unwrap_or_default().to_string();
    if commit.len() != 40 || !commit.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err("The engine download does not say which build it is.".into());
    }
    let dest = root.join(&commit);
    let _ = std::fs::remove_dir_all(&dest);
    std::fs::rename(&staging, &dest).map_err(|e| format!("Could not put the engine in place: {e}"))?;

    // Engines of other builds are no longer needed; one still in use goes next time.
    for entry in std::fs::read_dir(&root).into_iter().flatten().flatten() {
        let name = entry.file_name().to_string_lossy().into_owned();
        if name != commit && name != BUILD_COMMIT && entry.path().is_dir() {
            let _ = std::fs::remove_dir_all(entry.path());
        }
    }
    say(&format!("Installed engine build {}.", &commit[..7]));
    Ok(commit)
}

/// Downloads the latest exe (refusing any build but `commit`), swaps it in place of this
/// one and starts it.
#[tauri::command]
async fn update_app(app: tauri::AppHandle, commit: String) -> Result<(), String> {
    if commit.len() != 40 || !commit.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(format!("{commit} is not a build commit"));
    }
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let new = exe.with_extension("exe.new");
    let old = exe.with_extension("exe.old");
    let dest = new.to_string_lossy().into_owned();
    stream(&app, updater(&["download-app", &dest, &commit]))?;
    // Windows lets a running exe be renamed but not overwritten.
    let _ = std::fs::remove_file(&old);
    std::fs::rename(&exe, &old).map_err(|e| format!("Could not replace {}: {e}", exe.display()))?;
    if let Err(e) = std::fs::rename(&new, &exe) {
        let _ = std::fs::rename(&old, &exe);
        return Err(format!("Could not replace {}: {e}", exe.display()));
    }
    Command::new(&exe)
        .args(["--updated-to", &commit])
        .spawn()
        .map_err(|e| format!("Updated, but could not restart: {e}"))?;
    app.exit(0);
    Ok(())
}

// ---- Fabric's own pages (the Monitoring hub, the OneLake catalog), shown in one second webview
// docked into the page. Fabric pages refuse to load in an iframe, but a separate webview is a
// browser of its own. Both pages share this one view, so they share one Fabric sign-in.

const FABRIC_VIEW: &str = "fabric";
const FABRIC_MONITOR: &str = "https://app.fabric.microsoft.com/monitoringhub?experience=fabric-developer";
const FABRIC_CATALOG: &str = "https://app.fabric.microsoft.com/onelake/explore?experience=fabric-developer";

/// Hosts of the sign-in and of Fabric's own pages.
const FABRIC_HOSTS: [&str; 6] = ["microsoft.com", "microsoftonline.com", "powerbi.com", "live.com", "msauth.net", "msftauth.net"];

fn fabric_host(host: &str) -> bool {
    let host = host.trim_start_matches('.');
    FABRIC_HOSTS.iter().any(|d| host == *d || host.ends_with(&format!(".{d}")))
}

/// Sign-in and Fabric's own pages stay in the view; any other link opens in the default browser.
fn stays_in_view(url: &Url) -> bool {
    fabric_host(url.host_str().unwrap_or_default())
}

fn open_in_browser(url: &Url) -> Result<(), String> {
    if !matches!(url.scheme(), "https" | "http") {
        return Err(format!("Not opening {url}"));
    }
    let mut cmd = if cfg!(windows) {
        let mut c = Command::new("rundll32");
        c.args(["url.dll,FileProtocolHandler", url.as_str()]);
        c
    } else {
        let mut c = Command::new("xdg-open");
        c.arg(url.as_str());
        c
    };
    hide_window(&mut cmd);
    cmd.spawn().map(|_| ()).map_err(|e| format!("Could not open the browser: {e}"))
}

/// The page the Fabric view shows: "monitor", "catalog", or an https link to a Fabric item.
fn fabric_page(page: &str, tenant: Option<&str>) -> Result<Url, String> {
    let mut url = match page {
        "monitor" => Url::parse(FABRIC_MONITOR),
        "catalog" => Url::parse(FABRIC_CATALOG),
        link => Url::parse(link),
    }
    .map_err(|e| e.to_string())?;
    if url.scheme() != "https" || url.host_str() != Some("app.fabric.microsoft.com") {
        return Err(format!("Not a Fabric page: {page}"));
    }
    // With the tenant of the account signed in to the app, Fabric skips its
    // "Enter your work or school email" page and goes straight to that directory's sign-in.
    if let Some(tenant) = tenant.filter(|t| t.len() == 36 && t.chars().all(|c| c.is_ascii_hexdigit() || c == '-')) {
        url.query_pairs_mut().append_pair("ctid", tenant).append_pair("noSignUpCheck", "1");
    }
    Ok(url)
}

/// Which page the Fabric view was last sent to, so switching between Monitor and OneLake
/// navigates only when the page changes.
static FABRIC_SHOWN: Mutex<String> = Mutex::new(String::new());

/// Shows the Fabric view over the page's placeholder, creating it the first time.
/// Coordinates are CSS pixels inside the window, which are Tauri's logical pixels.
#[tauri::command]
async fn fabric_view_show(
    app: tauri::AppHandle,
    x: f64,
    y: f64,
    width: f64,
    height: f64,
    tenant: Option<String>,
    page: Option<String>,
) -> Result<(), String> {
    let page = page.unwrap_or_else(|| "monitor".into());
    let target = fabric_page(&page, tenant.as_deref())?;
    let position = LogicalPosition::new(x, y);
    let size = LogicalSize::new(width.max(1.0), height.max(1.0));
    if let Some(view) = app.get_webview(FABRIC_VIEW) {
        view.set_position(position).map_err(|e| e.to_string())?;
        view.set_size(size).map_err(|e| e.to_string())?;
        let changed = { let mut shown = FABRIC_SHOWN.lock().unwrap(); std::mem::replace(&mut *shown, page.clone()) != page };
        if changed {
            view.navigate(target).map_err(|e| e.to_string())?;
        }
        return view.show().map_err(|e| e.to_string());
    }
    let window = app.get_window("main").ok_or("The main window is not open")?;
    // Start blank: the saved sign-in goes in before the first Fabric request.
    let blank = Url::parse("about:blank").map_err(|e| e.to_string())?;
    let builder = WebviewBuilder::new(FABRIC_VIEW, WebviewUrl::External(blank)).on_new_window(|url, _| {
        if stays_in_view(&url) {
            NewWindowResponse::Allow
        } else {
            let _ = open_in_browser(&url);
            NewWindowResponse::Deny
        }
    });
    #[cfg(windows)]
    let builder = match sso::environment(&app) {
        Ok(environment) => builder.with_environment(environment),
        Err(e) => {
            eprintln!("Fabric view: Windows sign-in is not available ({e}); Fabric will ask for the account.");
            builder
        }
    };
    let view = window.add_child(builder, position, size).map_err(|e| e.to_string())?;
    for cookie in signin::load() {
        let _ = view.set_cookie(cookie);
    }
    *FABRIC_SHOWN.lock().unwrap() = page;
    view.navigate(target).map_err(|e| e.to_string())?;
    signin::keep_saving(app.clone());
    Ok(())
}

/// Keeps the Fabric view's sign-in between launches. Microsoft's sign-in cookies are usually
/// session cookies, which WebView2 drops when the app closes, so every launch asked to sign
/// in again. The app saves them every few seconds, encrypted for this Windows user with DPAPI
/// (as Edge and Chrome protect their own cookies), and puts them back when the view opens.
mod signin {
    use std::path::PathBuf;
    use std::sync::Mutex;
    use std::time::Duration;

    use serde_json::{json, Value};
    use tauri::webview::cookie::{time::OffsetDateTime, Cookie, Expiration, SameSite};
    use tauri::Manager;

    fn file() -> PathBuf {
        super::data_dir().join("fabric-view-signin.bin")
    }

    /// Serialises the sign-in and Fabric cookies; other sites' cookies are left out.
    pub fn encode(cookies: &[Cookie<'_>]) -> Vec<u8> {
        let list: Vec<Value> = cookies
            .iter()
            .filter(|c| c.domain().is_some_and(super::fabric_host))
            .map(|c| {
                json!({
                    "name": c.name(), "value": c.value(), "domain": c.domain(), "path": c.path(),
                    "secure": c.secure().unwrap_or(false), "http_only": c.http_only().unwrap_or(false),
                    "same_site": match c.same_site() { Some(SameSite::Strict) => "strict", Some(SameSite::Lax) => "lax", _ => "none" },
                    "expires": c.expires_datetime().map(|t| t.unix_timestamp()),
                })
            })
            .collect();
        serde_json::to_vec(&list).unwrap_or_default()
    }

    /// The saved cookies that have not expired. Session cookies come back as session cookies.
    pub fn decode(bytes: &[u8], now: i64) -> Vec<Cookie<'static>> {
        let list: Vec<Value> = serde_json::from_slice(bytes).unwrap_or_default();
        list.iter()
            .filter_map(|c| {
                let text = |k: &str| c.get(k).and_then(Value::as_str).map(str::to_owned);
                let expires = c.get("expires").and_then(Value::as_i64);
                if expires.is_some_and(|t| t <= now) {
                    return None;
                }
                let mut cookie = Cookie::build((text("name")?, text("value")?))
                    .domain(text("domain")?)
                    .path(text("path").unwrap_or_else(|| "/".into()))
                    .secure(c.get("secure").and_then(Value::as_bool).unwrap_or(false))
                    .http_only(c.get("http_only").and_then(Value::as_bool).unwrap_or(false))
                    .same_site(match c.get("same_site").and_then(Value::as_str) {
                        Some("strict") => SameSite::Strict,
                        Some("lax") => SameSite::Lax,
                        _ => SameSite::None,
                    })
                    .build();
                cookie.set_expires(match expires.and_then(|t| OffsetDateTime::from_unix_timestamp(t).ok()) {
                    Some(t) => Expiration::DateTime(t),
                    None => Expiration::Session,
                });
                Some(cookie)
            })
            .collect()
    }

    pub fn load() -> Vec<Cookie<'static>> {
        let Ok(sealed) = std::fs::read(file()) else { return Vec::new() };
        match dpapi::unprotect(&sealed) {
            Ok(bytes) => decode(&bytes, OffsetDateTime::now_utc().unix_timestamp()),
            Err(e) => {
                eprintln!("Fabric view: could not read the saved sign-in ({e})");
                Vec::new()
            }
        }
    }

    static LAST: Mutex<Vec<u8>> = Mutex::new(Vec::new());
    static STARTED: Mutex<bool> = Mutex::new(false);

    fn save(cookies: &[Cookie<'_>]) {
        let bytes = encode(cookies);
        let mut last = LAST.lock().unwrap();
        if *last == bytes {
            return;
        }
        match dpapi::protect(&bytes) {
            Ok(sealed) => {
                let tmp = file().with_extension("tmp");
                if std::fs::write(&tmp, sealed).and_then(|_| std::fs::rename(&tmp, file())).is_ok() {
                    *last = bytes;
                }
            }
            Err(e) => eprintln!("Fabric view: could not save the sign-in ({e})"),
        }
    }

    /// Reads the view's cookies on a thread of its own: on Windows, reading them from a
    /// command or an event handler deadlocks (WebView2 answers on the main thread).
    pub fn keep_saving(app: tauri::AppHandle) {
        let mut started = STARTED.lock().unwrap();
        if *started || !dpapi::AVAILABLE {
            return;
        }
        *started = true;
        std::thread::spawn(move || loop {
            std::thread::sleep(Duration::from_secs(5));
            if let Some(view) = app.get_webview(super::FABRIC_VIEW) {
                if let Ok(cookies) = view.cookies() {
                    save(&cookies);
                }
            }
        });
    }

    #[cfg(windows)]
    mod dpapi {
        use windows::Win32::Foundation::{LocalFree, HLOCAL};
        use windows::Win32::Security::Cryptography::{
            CryptProtectData, CryptUnprotectData, CRYPTPROTECT_UI_FORBIDDEN, CRYPT_INTEGER_BLOB,
        };

        pub const AVAILABLE: bool = true;

        fn run(data: &[u8], seal: bool) -> Result<Vec<u8>, String> {
            let input = CRYPT_INTEGER_BLOB { cbData: data.len() as u32, pbData: data.as_ptr() as *mut u8 };
            let mut output = CRYPT_INTEGER_BLOB::default();
            unsafe {
                if seal {
                    CryptProtectData(&input, windows::core::w!("Fabric Desktop sign-in"), None, None, None,
                                     CRYPTPROTECT_UI_FORBIDDEN, &mut output)
                } else {
                    CryptUnprotectData(&input, None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, &mut output)
                }
                .map_err(|e| e.to_string())?;
                let bytes = std::slice::from_raw_parts(output.pbData, output.cbData as usize).to_vec();
                LocalFree(Some(HLOCAL(output.pbData as _)));
                Ok(bytes)
            }
        }

        pub fn protect(data: &[u8]) -> Result<Vec<u8>, String> {
            run(data, true)
        }

        pub fn unprotect(data: &[u8]) -> Result<Vec<u8>, String> {
            run(data, false)
        }
    }

    /// Only Windows has DPAPI; elsewhere (development builds) nothing is saved.
    #[cfg(not(windows))]
    mod dpapi {
        pub const AVAILABLE: bool = false;

        pub fn protect(_: &[u8]) -> Result<Vec<u8>, String> {
            Err("not supported on this platform".into())
        }

        pub fn unprotect(_: &[u8]) -> Result<Vec<u8>, String> {
            Err("not supported on this platform".into())
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn keeps_sign_in_cookies_and_drops_expired_and_foreign_ones() {
            let session = Cookie::build(("ESTSAUTH", "abc")).domain("login.microsoftonline.com").path("/")
                .secure(true).http_only(true).same_site(SameSite::None).build();
            let mut expired = Cookie::build(("old", "1")).domain(".powerbi.com").path("/").build();
            expired.set_expires(OffsetDateTime::from_unix_timestamp(100).unwrap());
            let foreign = Cookie::build(("ad", "x")).domain("example.com").path("/").build();
            let back = decode(&encode(&[session, expired, foreign]), 1_000);
            assert_eq!(back.len(), 1);
            assert_eq!((back[0].name(), back[0].value(), back[0].domain()), ("ESTSAUTH", "abc", Some("login.microsoftonline.com")));
            assert_eq!(back[0].expires(), Some(Expiration::Session));
            assert_eq!((back[0].http_only(), back[0].secure(), back[0].same_site()), (Some(true), Some(true), Some(SameSite::None)));
        }
    }
}

/// The Fabric view gets its own WebView2 environment that may sign in with the Windows account
/// (AllowSingleSignOnUsingOSPrimaryAccount), so a work account signed in to Windows needs no
/// password. It uses its own profile folder: WebView2 refuses two option sets on one folder.
#[cfg(windows)]
mod sso {
    use webview2_com::Microsoft::Web::WebView2::Win32::{
        CreateCoreWebView2EnvironmentWithOptions, ICoreWebView2Environment, ICoreWebView2EnvironmentOptions,
    };
    use webview2_com::{CoreWebView2EnvironmentOptions, CreateCoreWebView2EnvironmentCompletedHandler};
    use windows::core::{HSTRING, PCWSTR};
    use windows::Win32::Foundation::{E_POINTER, E_UNEXPECTED};

    /// COM objects are not Send; this one is only created and used on the main thread.
    struct OnMainThread(ICoreWebView2Environment);
    unsafe impl Send for OnMainThread {}

    pub fn environment(app: &tauri::AppHandle) -> Result<ICoreWebView2Environment, String> {
        let folder = super::data_dir().join("fabric-view");
        let (tx, rx) = std::sync::mpsc::channel();
        app.run_on_main_thread(move || {
            let _ = tx.send(create(&folder).map(OnMainThread));
        })
        .map_err(|e| e.to_string())?;
        rx.recv().map_err(|e| e.to_string())?.map(|env| env.0)
    }

    fn create(folder: &std::path::Path) -> Result<ICoreWebView2Environment, String> {
        let options = CoreWebView2EnvironmentOptions::default();
        unsafe {
            // Same arguments wry uses for the main view.
            options.set_additional_browser_arguments(
                "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection".to_string(),
            );
            options.set_allow_single_sign_on_using_os_primary_account(true);
        }
        let (tx, rx) = std::sync::mpsc::channel();
        unsafe {
            CreateCoreWebView2EnvironmentWithOptions(
                PCWSTR::null(),
                &HSTRING::from(folder.as_os_str()),
                &ICoreWebView2EnvironmentOptions::from(options),
                &CreateCoreWebView2EnvironmentCompletedHandler::create(Box::new(move |error_code, environment| {
                    let result = (|| {
                        error_code?;
                        environment.ok_or_else(|| windows::core::Error::from(E_POINTER))
                    })();
                    tx.send(result).map_err(|_| windows::core::Error::from(E_UNEXPECTED))
                })),
            )
        }
        .map_err(|e| e.to_string())?;
        webview2_com::wait_with_pump(rx).map_err(|e| e.to_string())?.map_err(|e| e.to_string())
    }
}

#[tauri::command]
async fn fabric_view_hide(app: tauri::AppHandle) -> Result<(), String> {
    match app.get_webview(FABRIC_VIEW) {
        Some(view) => view.hide().map_err(|e| e.to_string()),
        None => Ok(()),
    }
}

/// back, reload, home (the page's own start: page is "monitor" or "catalog"),
/// or browser (open the current page in the default browser).
#[tauri::command]
async fn fabric_view_action(app: tauri::AppHandle, action: String, page: Option<String>) -> Result<(), String> {
    let view = app.get_webview(FABRIC_VIEW).ok_or("The Fabric view is not open")?;
    match action.as_str() {
        "back" => view.eval("history.back()").map_err(|e| e.to_string()),
        "reload" => view.reload().map_err(|e| e.to_string()),
        "home" => {
            let page = page.unwrap_or_else(|| "monitor".into());
            let url = fabric_page(&page, None)?;
            *FABRIC_SHOWN.lock().unwrap() = page;
            view.navigate(url).map_err(|e| e.to_string())
        }
        "browser" => open_in_browser(&view.url().map_err(|e| e.to_string())?),
        _ => Err(format!("unknown action {action}")),
    }
}

fn main() {
    // Left behind by the last update; the old process has exited by now.
    if let Ok(exe) = std::env::current_exe() {
        let _ = std::fs::remove_file(exe.with_extension("exe.old"));
        let _ = std::fs::remove_file(exe.with_extension("exe.new"));
    }
    tauri::Builder::default()
        .manage(SidecarState { sidecar: Mutex::new(None), child: Mutex::new(None) })
        .invoke_handler(tauri::generate_handler![
            rpc, stop_kernel, check_components, update_sidecar, update_app,
            fabric_view_show, fabric_view_hide, fabric_view_action
        ])
        .run(tauri::generate_context!())
        .expect("error while running Fabric Desktop");
}
