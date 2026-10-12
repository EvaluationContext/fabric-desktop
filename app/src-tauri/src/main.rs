// Fabric Desktop shell: hosts the UI and forwards calls to the Python sidecar,
// which does all data work on this machine.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::{Arc, Mutex};

use serde_json::{json, Value};

const SIDECAR_EXITED: &str = "The Python sidecar stopped. It will restart on the next action.";

struct Sidecar {
    // Shared with SidecarState so Stop can kill the process while a call is blocked on it.
    child: Arc<Mutex<Child>>,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    next_id: u64,
}

impl Sidecar {
    fn start() -> std::io::Result<Self> {
        // FABRIC_DESKTOP_PYTHON points at the interpreter that has the sidecar installed.
        let python = std::env::var("FABRIC_DESKTOP_PYTHON").unwrap_or_else(|_| "python".into());
        let mut cmd = Command::new(python);
        cmd.args(["-m", "fabric_desktop.server"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }
        let mut child = cmd.spawn()?;
        let stdin = child.stdin.take().expect("sidecar stdin");
        let stdout = BufReader::new(child.stdout.take().expect("sidecar stdout"));
        Ok(Self { child: Arc::new(Mutex::new(child)), stdin, stdout, next_id: 1 })
    }

    fn call(&mut self, method: &str, params: Value) -> Result<Value, String> {
        let id = self.next_id;
        self.next_id += 1;
        let request = json!({ "id": id, "method": method, "params": params });
        writeln!(self.stdin, "{request}").map_err(|_| SIDECAR_EXITED.to_string())?;
        self.stdin.flush().map_err(|_| SIDECAR_EXITED.to_string())?;

        let mut line = String::new();
        let read = self.stdout.read_line(&mut line).map_err(|e| e.to_string())?;
        if read == 0 {
            return Err(SIDECAR_EXITED.to_string());
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
    if matches!(&result, Err(e) if e == SIDECAR_EXITED) {
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

fn main() {
    tauri::Builder::default()
        .manage(SidecarState { sidecar: Mutex::new(None), child: Mutex::new(None) })
        .invoke_handler(tauri::generate_handler![rpc, stop_kernel])
        .run(tauri::generate_context!())
        .expect("error while running Fabric Desktop");
}
