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

/// FABRIC_DESKTOP_PYTHON points at the interpreter that has the sidecar installed.
fn python() -> String {
    std::env::var("FABRIC_DESKTOP_PYTHON").unwrap_or_else(|_| "python".into())
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
    let out = updater(&["check"])
        .output()
        .map_err(|e| format!("Python was not found ({e})."))?;
    if !out.status.success() {
        let stderr = String::from_utf8_lossy(&out.stderr);
        return Err(format!("Python could not run the startup check. {}", stderr.trim()));
    }
    let mut report: Value = serde_json::from_slice(&out.stdout)
        .map_err(|_| "Python gave an unexpected answer to the startup check.".to_string())?;
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

/// Installs the sidecar from the commit this exe was built from, so the two always match.
#[tauri::command]
async fn update_sidecar(app: tauri::AppHandle, state: tauri::State<'_, SidecarState>) -> Result<(), String> {
    if BUILD_COMMIT.is_empty() {
        return Err("This is a local build, so it cannot pick a sidecar version. Install it with pip.".into());
    }
    // A running sidecar holds its files open, which blocks pip on Windows.
    stop_kernel(state.clone())?;
    *state.sidecar.lock().map_err(|e| e.to_string())? = None;
    stream(&app, updater(&["install-sidecar", BUILD_COMMIT]))
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

// ---- Fabric's own Monitoring hub, shown in a second webview docked into the Monitor page.
// Fabric pages refuse to load in an iframe, but a separate webview is a browser of its own.

const FABRIC_VIEW: &str = "fabric";
const FABRIC_MONITOR: &str = "https://app.fabric.microsoft.com/monitoringhub?experience=fabric-developer";

/// Sign-in and Fabric's own pages stay in the view; any other link opens in the default browser.
fn stays_in_view(url: &Url) -> bool {
    let host = url.host_str().unwrap_or_default();
    ["microsoft.com", "microsoftonline.com", "powerbi.com", "live.com", "msauth.net", "msftauth.net"]
        .iter()
        .any(|d| host == *d || host.ends_with(&format!(".{d}")))
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

/// Shows the Fabric view over the page's placeholder, creating it the first time.
/// Coordinates are CSS pixels inside the window, which are Tauri's logical pixels.
#[tauri::command]
async fn fabric_view_show(app: tauri::AppHandle, x: f64, y: f64, width: f64, height: f64) -> Result<(), String> {
    let position = LogicalPosition::new(x, y);
    let size = LogicalSize::new(width.max(1.0), height.max(1.0));
    if let Some(view) = app.get_webview(FABRIC_VIEW) {
        view.set_position(position).map_err(|e| e.to_string())?;
        view.set_size(size).map_err(|e| e.to_string())?;
        return view.show().map_err(|e| e.to_string());
    }
    let window = app.get_window("main").ok_or("The main window is not open")?;
    let url = Url::parse(FABRIC_MONITOR).map_err(|e| e.to_string())?;
    let builder = WebviewBuilder::new(FABRIC_VIEW, WebviewUrl::External(url)).on_new_window(|url, _| {
        if stays_in_view(&url) {
            NewWindowResponse::Allow
        } else {
            let _ = open_in_browser(&url);
            NewWindowResponse::Deny
        }
    });
    window.add_child(builder, position, size).map(|_| ()).map_err(|e| e.to_string())
}

#[tauri::command]
async fn fabric_view_hide(app: tauri::AppHandle) -> Result<(), String> {
    match app.get_webview(FABRIC_VIEW) {
        Some(view) => view.hide().map_err(|e| e.to_string()),
        None => Ok(()),
    }
}

/// back, reload, home, or browser (open the current page in the default browser).
#[tauri::command]
async fn fabric_view_action(app: tauri::AppHandle, action: String) -> Result<(), String> {
    let view = app.get_webview(FABRIC_VIEW).ok_or("The Fabric view is not open")?;
    match action.as_str() {
        "back" => view.eval("history.back()").map_err(|e| e.to_string()),
        "reload" => view.reload().map_err(|e| e.to_string()),
        "home" => view.navigate(Url::parse(FABRIC_MONITOR).map_err(|e| e.to_string())?).map_err(|e| e.to_string()),
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
