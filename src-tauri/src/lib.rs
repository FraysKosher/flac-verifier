// Learn more about Tauri commands at https://tauri.app/develop/calling-rust/
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_shell::ShellExt;
use tauri_plugin_shell::process::CommandEvent;

/// Resolve the path to motor_flac.py.
/// In dev mode, look next to the workspace root; in production, use the resource directory.
fn resolve_script_path(app: &AppHandle) -> Result<String, String> {
    // Try resource directory first (production bundle)
    if let Ok(resource_path) = app.path().resource_dir() {
        let script = resource_path.join("motor_flac.py");
        if script.exists() {
            return Ok(script.to_string_lossy().to_string());
        }
    }

    // Dev fallback: check two levels up from the app exe directory
    if let Ok(exe_path) = std::env::current_exe() {
        // In dev mode the exe is deep in target/debug/, climb up to find the workspace root
        let mut dir = exe_path.parent();
        for _ in 0..6 {
            if let Some(d) = dir {
                let candidate = d.join("motor_flac.py");
                if candidate.exists() {
                    return Ok(candidate.to_string_lossy().to_string());
                }
                dir = d.parent();
            }
        }
    }

    // Last resort: current working directory
    if let Ok(cwd) = std::env::current_dir() {
        let candidate = cwd.join("motor_flac.py");
        if candidate.exists() {
            return Ok(candidate.to_string_lossy().to_string());
        }
        // Try parent of cwd
        if let Some(parent) = cwd.parent() {
            let candidate = parent.join("motor_flac.py");
            if candidate.exists() {
                return Ok(candidate.to_string_lossy().to_string());
            }
        }
    }

    Err("Cannot locate motor_flac.py. Make sure it is in the app directory.".to_string())
}

#[tauri::command]
async fn run_analysis(
    app: AppHandle,
    path: String,
    mode: String,
    png: bool,
    pdf: bool,
    seg: Option<u32>,
) -> Result<(), String> {
    let script_path = resolve_script_path(&app)?;

    let mut args: Vec<String> = vec![
        script_path,
        "--ruta".to_string(),
        path,
        "--modo".to_string(),
        mode.clone(),
    ];

    if mode == "segundos" {
        let s = seg.unwrap_or(15).to_string();
        args.push("--seg".to_string());
        args.push(s);
    }

    if png { args.push("--png".to_string()); }
    if pdf { args.push("--pdf".to_string()); }

    // Convert args to &str slices
    let args_ref: Vec<&str> = args.iter().map(|s| s.as_str()).collect();

    // Spawn python subprocess
    let (mut rx, _child) = app
        .shell()
        .command("python")
        .args(&args_ref)
        .spawn()
        .map_err(|e| format!("Failed to spawn Python: {}", e))?;

    let app_handle = app.clone();

    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line_bytes) => {
                    let line = String::from_utf8_lossy(&line_bytes).to_string();
                    let trimmed = line.trim().to_string();
                    if !trimmed.is_empty() {
                        let _ = app_handle.emit("flac://progress", trimmed);
                    }
                }
                CommandEvent::Stderr(line_bytes) => {
                    let line = String::from_utf8_lossy(&line_bytes).to_string();
                    let trimmed = line.trim().to_string();
                    if !trimmed.is_empty() {
                        // Emit stderr as an error event so the frontend can display it
                        let payload = format!("{{\"tipo\":\"stderr\",\"mensaje\":{}}}", 
                            serde_json::to_string(&trimmed).unwrap_or_default());
                        let _ = app_handle.emit("flac://progress", payload);
                    }
                }
                CommandEvent::Terminated(payload) => {
                    let code = payload.code.unwrap_or(-1);
                    let fin = format!("{{\"tipo\":\"terminated\",\"code\":{}}}", code);
                    let _ = app_handle.emit("flac://progress", fin);
                    break;
                }
                _ => {}
            }
        }
    });

    Ok(())
}

fn history_path() -> Result<std::path::PathBuf, String> {
    let appdata = std::env::var("APPDATA")
        .map_err(|_| "APPDATA environment variable not found".to_string())?;
    Ok(std::path::Path::new(&appdata)
        .join("flac-verifier")
        .join("historial.csv"))
}

#[tauri::command]
fn read_history() -> Result<String, String> {
    let path = history_path()?;
    if !path.exists() {
        return Ok(String::new());
    }
    std::fs::read_to_string(&path).map_err(|e| e.to_string())
}

#[tauri::command]
fn clear_history() -> Result<(), String> {
    let path = history_path()?;
    if path.exists() {
        std::fs::remove_file(&path).map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn get_history_path() -> Result<String, String> {
    history_path().map(|p| p.to_string_lossy().to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            let splash = app
                .get_webview_window("splashscreen")
                .expect("splashscreen window missing");
            let main = app
                .get_webview_window("main")
                .expect("main window missing");
            // Show main and close splash after animation completes
            std::thread::spawn(move || {
                std::thread::sleep(std::time::Duration::from_millis(2400));
                main.show().expect("could not show main window");
                splash.close().expect("could not close splash");
            });
            Ok(())
        })
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![run_analysis, read_history, clear_history, get_history_path])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
