#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    io::{BufRead, BufReader},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc, Mutex,
    },
    time::{Duration, Instant},
};
use tauri::{Manager, State};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

#[derive(Default)]
struct QuitState {
    pending: AtomicBool,
    approved: AtomicBool,
}

#[derive(Clone, Deserialize, Serialize)]
struct Connection {
    port: u16,
    token: String,
    python: String,
    source: String,
}

struct Backend {
    stopping: AtomicBool,
    child: Mutex<Option<Child>>,
    connection: Mutex<Result<Option<Connection>, String>>,
    client: reqwest::Client,
}

impl Backend {
    fn info(&self) -> Result<Connection, String> {
        self.connection
            .lock()
            .map_err(|e| e.to_string())?
            .clone()?
            .ok_or_else(|| "Python backend is starting…".into())
    }

    fn close(&self) {
        self.stopping.store(true, Ordering::SeqCst);
        if let Ok(mut slot) = self.child.lock() {
            if let Some(mut child) = slot.take() {
                // EOF tells Python to send neutral, close MAVLink, flush logs,
                // and stop its server. This also works if the webview crashed.
                drop(child.stdin.take());
                let deadline = Instant::now() + Duration::from_secs(8);
                loop {
                    match child.try_wait() {
                        Ok(Some(_)) => break,
                        _ if Instant::now() >= deadline => {
                            let _ = child.kill();
                            let _ = child.wait();
                            break;
                        }
                        _ => std::thread::sleep(Duration::from_millis(50)),
                    }
                }
            }
        }
    }
}

fn uv_binary() -> PathBuf {
    if let Some(path) = std::env::var_os("HERON_UV") {
        return path.into();
    }
    let home = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE"));
    if let Some(home) = home {
        for suffix in [".local/bin/uv", ".cargo/bin/uv", ".local/bin/uv.exe"] {
            let path = PathBuf::from(&home).join(suffix);
            if path.is_file() {
                return path;
            }
        }
    }
    for path in ["/opt/homebrew/bin/uv", "/usr/local/bin/uv", "/usr/bin/uv"] {
        if Path::new(path).is_file() {
            return path.into();
        }
    }
    "uv".into()
}

fn launch(backend: &Backend, resources: PathBuf, data_dir: PathBuf) -> Result<(), String> {
    std::fs::create_dir_all(&data_dir).map_err(|e| e.to_string())?;
    let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let development =
        cfg!(debug_assertions) && repo.join("backend/heron/mission/backend.py").is_file();
    let source = if development {
        repo.join("backend")
    } else {
        resources.join("backend")
    };
    let fiu = if development {
        repo.join("FIU-basemap")
    } else {
        resources.join("FIU-basemap")
    };
    let dev_python = if cfg!(windows) {
        repo.join(".venv/Scripts/python.exe")
    } else {
        repo.join(".venv/bin/python")
    };
    let mut command = if let Some(python) = std::env::var_os("HERON_PYTHON") {
        Command::new(python)
    } else if development && dev_python.is_file() {
        Command::new(dev_python)
    } else {
        let requirements = if development {
            repo.join("backend/requirements.txt")
        } else {
            resources.join("backend/requirements.txt")
        };
        let mut command = Command::new(uv_binary());
        command
            .args([
                "run",
                "--no-project",
                "--python",
                ">=3.14",
                "--with-requirements",
            ])
            .arg(requirements)
            .arg("python");
        command
    };
    command
        .args([
            "-u",
            "-m",
            "heron.mission.backend",
            "--managed",
            "--no-ip-setup",
            "--fiu-root",
        ])
        .arg(fiu)
        .env("PYTHONPATH", &source)
        .env("PYTHONUNBUFFERED", "1")
        .env("HERON_CACHE_DIR", data_dir.join("tiles"))
        .current_dir(&data_dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(target_os = "macos")]
    {
        // uv-managed Python does not inherit Homebrew Python's dylib search
        // paths. pyhid loads libhidapi by name, including in Finder launches.
        let existing = std::env::var("DYLD_FALLBACK_LIBRARY_PATH").unwrap_or_default();
        let paths = if existing.is_empty() {
            "/opt/homebrew/lib:/usr/local/lib:/usr/lib".to_string()
        } else {
            format!("{existing}:/opt/homebrew/lib:/usr/local/lib:/usr/lib")
        };
        command.env("DYLD_FALLBACK_LIBRARY_PATH", paths);
    }
    // Terminal interrupts belong to the desktop quit coordinator. Python
    // must remain alive until the armed-vehicle confirmation is answered.
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    // Forward supported mission/connection options from the Python launcher.
    let mut arguments = std::env::args().skip(1);
    while let Some(argument) = arguments.next() {
        if argument == "--mission-file" || argument == "--connection" {
            if let Some(value) = arguments.next() {
                command.arg(argument).arg(value);
            }
        }
    }
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000 | 0x00000200); // NO_WINDOW | NEW_PROCESS_GROUP.
    }
    let mut slot = backend.child.lock().map_err(|e| e.to_string())?;
    if backend.stopping.load(Ordering::SeqCst) {
        return Err("Application is closing".into());
    }
    let mut child = command.spawn().map_err(|e| format!(
        "Could not start Python: {e}. Install Python 3.14+ and uv, or set HERON_PYTHON to an environment with backend/requirements.txt installed."))?;
    let stdout = child.stdout.take().ok_or("Missing Python stdout")?;
    let stderr = child.stderr.take().ok_or("Missing Python stderr")?;
    let diagnostics = Arc::new(Mutex::new(String::new()));
    let captured = diagnostics.clone();
    std::thread::spawn(move || {
        for line in BufReader::new(stderr).lines().map_while(Result::ok) {
            eprintln!("{line}");
            if let Ok(mut buffer) = captured.lock() {
                if buffer.len() > 16_384 {
                    buffer.clear();
                }
                buffer.push_str(&line);
                buffer.push('\n');
            }
        }
    });
    *slot = Some(child);
    drop(slot);
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let mut line = String::new();
        let result = BufReader::new(stdout)
            .read_line(&mut line)
            .map_err(|e| e.to_string())
            .and_then(|_| serde_json::from_str::<Connection>(&line).map_err(|e| e.to_string()));
        let _ = tx.send(result);
    });
    let ready = rx
        .recv_timeout(Duration::from_secs(120))
        .map_err(|e| e.to_string())?;
    match ready {
        Ok(connection) => {
            *backend.connection.lock().map_err(|e| e.to_string())? = Ok(Some(connection));
            Ok(())
        }
        Err(error) => Err(format!(
            "Python startup failed: {error}\n{}",
            diagnostics.lock().map_err(|e| e.to_string())?
        )),
    }
}

#[tauri::command]
fn backend_info(backend: State<'_, Arc<Backend>>) -> Result<Connection, String> {
    backend.info()
}

#[tauri::command]
async fn backend_request(
    backend: State<'_, Arc<Backend>>,
    action: Option<String>,
    data: Option<Value>,
) -> Result<Value, String> {
    let info = backend.info()?;
    let url = format!("http://127.0.0.1:{}", info.port);
    let request = if let Some(action) = action {
        let mut body = data.unwrap_or_else(|| serde_json::json!({}));
        let object = body
            .as_object_mut()
            .ok_or("Command data must be an object")?;
        object.insert("action".into(), Value::String(action));
        backend.client.post(format!("{url}/command")).json(&body)
    } else {
        backend.client.get(format!("{url}/state"))
    };
    let response = request
        .bearer_auth(&info.token)
        .send()
        .await
        .map_err(|e| format!("Python backend unavailable: {e}"))?;
    let status = response.status();
    let value: Value = response.json().await.map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(value["error"]
            .as_str()
            .unwrap_or("Backend request failed")
            .into());
    }
    Ok(value)
}

#[cfg(target_os = "macos")]
fn shell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}

#[tauri::command]
async fn network_setup(backend: State<'_, Arc<Backend>>) -> Result<String, String> {
    let info = backend.info()?;
    tauri::async_runtime::spawn_blocking(move || {
        #[cfg(target_os = "linux")]
        let script = Path::new(&info.source).join("heron/utils/network_setup.py");
        #[cfg(target_os = "macos")]
        let output = {
            // The administrator helper cannot read macOS-protected project
            // folders. Resolve the interpreter before elevation and pass the
            // stdlib-only utility as code, without loading the venv or site.
            let python = std::fs::canonicalize(&info.python)
                .map_err(|e| format!("Network setup interpreter unavailable: {e}"))?;
            let source = include_str!("../../../backend/heron/utils/network_setup.py");
            let shell = format!(
                "cd /private/tmp && PATH=/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin:/usr/bin:/bin:/usr/sbin:/sbin {} -I -S -c {}",
                shell_quote(&python.to_string_lossy()),
                shell_quote(source)
            );
            let apple_string = serde_json::to_string(&shell).map_err(|e| e.to_string())?;
            Command::new("osascript")
                .args([
                    "-e",
                    &format!("do shell script {apple_string} with administrator privileges"),
                ])
                .output()
        };
        #[cfg(target_os = "linux")]
        let output = Command::new("pkexec")
            .arg(&info.python)
            .arg(script)
            .output();
        #[cfg(not(any(target_os = "macos", target_os = "linux")))]
        return Err("Configure 192.168.2.1/24 on the Ethernet adapter manually on Windows.".into());
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        {
            let output = output.map_err(|e| format!("Network setup could not start: {e}"))?;
            let text = format!(
                "{}{}",
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            );
            if output.status.success() {
                Ok(text)
            } else {
                Err(text)
            }
        }
    })
    .await
    .map_err(|e| e.to_string())?
}

fn quit_dialog(app: tauri::AppHandle, armed: bool) {
    let message = if armed {
        "Vehicle is armed.\nAre you sure you want to quit?"
    } else {
        "The vehicle's armed state could not be verified.\nAre you sure you want to quit?"
    };
    let mut dialog = app
        .dialog()
        .message(message)
        .title("Quit Ground Station")
        .kind(MessageDialogKind::Warning)
        .buttons(MessageDialogButtons::OkCancelCustom(
            "Quit".into(),
            "Cancel".into(),
        ));
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.set_focus();
        dialog = dialog.parent(&window);
    }
    eprintln!("[Quit] Waiting for confirmation (armed: {armed})");
    dialog.show(move |confirmed| {
        eprintln!("[Quit] Confirmed: {confirmed}");
        if confirmed {
            tauri::async_runtime::spawn(finish_quit(app, true));
        } else {
            app.state::<Arc<QuitState>>()
                .pending
                .store(false, Ordering::SeqCst);
        }
    });
}

fn quit_menu(app: &tauri::AppHandle) -> tauri::Result<tauri::menu::Menu<tauri::Wry>> {
    use tauri::menu::{Menu, MenuItem, MenuItemKind, Submenu};
    let menu = Menu::default(app)?;
    let quit = MenuItem::with_id(
        app,
        "confirm-quit",
        "Quit Ground Station",
        true,
        Some("CmdOrCtrl+Q"),
    )?;
    let mut replaced = false;
    // The predefined macOS Quit item calls NSApplication.terminate directly,
    // bypassing Tauri's ExitRequested handler. Route it through our coordinator.
    for item in menu.items()? {
        if let Some(submenu) = item.as_submenu() {
            for (position, item) in submenu.items()?.iter().enumerate() {
                if let MenuItemKind::Predefined(predefined) = item {
                    let label = predefined.text()?.replace('&', "");
                    if matches!(label.split_whitespace().next(), Some("Quit" | "Exit")) {
                        submenu.remove(item)?;
                        submenu.insert(&quit, position)?;
                        replaced = true;
                    }
                }
            }
        }
    }
    if !replaced {
        menu.append(&Submenu::with_items(app, "File", true, &[&quit])?)?;
    }
    Ok(menu)
}

async fn finish_quit(app: tauri::AppHandle, confirmed_armed: bool) {
    let backend = app.state::<Arc<Backend>>().inner().clone();
    if let Ok(info) = backend.info() {
        let result = backend
            .client
            .post(format!("http://127.0.0.1:{}/shutdown", info.port))
            .bearer_auth(&info.token)
            .timeout(Duration::from_secs(3))
            .json(&serde_json::json!({"confirmed_armed": confirmed_armed}))
            .send()
            .await;
        match result {
            Ok(response) if response.status() == reqwest::StatusCode::CONFLICT => {
                // The vehicle may have armed after the initial status check.
                quit_dialog(app, true);
                return;
            }
            Ok(response) if response.status().is_success() => {}
            _ if !confirmed_armed => {
                quit_dialog(app, false);
                return;
            }
            _ => {} // User approved; stdin EOF is the fallback cleanup path.
        }
    }
    let cleanup = backend.clone();
    let _ = tauri::async_runtime::spawn_blocking(move || cleanup.close()).await;
    app.state::<Arc<QuitState>>()
        .approved
        .store(true, Ordering::SeqCst);
    app.exit(0);
}

fn request_quit(app: tauri::AppHandle) {
    let quit = app.state::<Arc<QuitState>>();
    if quit.approved.load(Ordering::SeqCst) || quit.pending.swap(true, Ordering::SeqCst) {
        return;
    }
    tauri::async_runtime::spawn(async move {
        let backend = app.state::<Arc<Backend>>().inner().clone();
        let armed = if let Ok(info) = backend.info() {
            match backend
                .client
                .post(format!("http://127.0.0.1:{}/prepare-quit", info.port))
                .bearer_auth(&info.token)
                .timeout(Duration::from_secs(3))
                .json(&serde_json::json!({}))
                .send()
                .await
            {
                Ok(response) if response.status().is_success() => response
                    .json::<Value>()
                    .await
                    .ok()
                    .and_then(|value| value["armed"].as_bool()),
                _ => None,
            }
        } else {
            None
        };
        match armed {
            Some(false) => finish_quit(app, false).await,
            Some(true) => quit_dialog(app, true),
            None => quit_dialog(app, false),
        }
    });
}

fn install_quit_handlers(app: &tauri::AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    let signal_app = app.clone();
    ctrlc::set_handler(move || request_quit(signal_app.clone()))?;
    if let Some(path) = std::env::var_os("HERON_QUIT_REQUEST") {
        let app = app.clone();
        std::thread::spawn(move || {
            let mut last_request = String::new();
            loop {
                if app
                    .state::<Arc<QuitState>>()
                    .approved
                    .load(Ordering::SeqCst)
                {
                    break;
                }
                if let Ok(request) = std::fs::read_to_string(&path) {
                    if !request.is_empty() && request != last_request {
                        last_request = request;
                        request_quit(app.clone());
                    }
                }
                std::thread::sleep(Duration::from_millis(100));
            }
        });
    }
    Ok(())
}

fn main() {
    let backend = Arc::new(Backend {
        stopping: AtomicBool::new(false),
        child: Mutex::new(None),
        connection: Mutex::new(Ok(None)),
        client: reqwest::Client::builder()
            .no_proxy()
            .timeout(Duration::from_secs(15))
            .build()
            .expect("HTTP client"),
    });
    let launch_backend = backend.clone();
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(backend.clone())
        .manage(Arc::new(QuitState::default()))
        .menu(quit_menu)
        .on_menu_event(|app, event| {
            if event.id().as_ref() == "confirm-quit" {
                request_quit(app.clone());
            }
        })
        .invoke_handler(tauri::generate_handler![
            backend_info,
            backend_request,
            network_setup
        ])
        .setup(move |app| {
            install_quit_handlers(app.handle())?;
            let resources = app.path().resource_dir()?;
            let data = app.path().app_data_dir()?;
            let backend = launch_backend.clone();
            std::thread::spawn(move || {
                if let Err(error) = launch(&backend, resources, data) {
                    *backend.connection.lock().expect("backend state") = Err(error);
                    backend.close();
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Tauri application");
    app.run(move |app, event| match event {
        tauri::RunEvent::WindowEvent {
            event: tauri::WindowEvent::CloseRequested { api, .. },
            ..
        } => {
            api.prevent_close();
            request_quit(app.clone());
        }
        tauri::RunEvent::ExitRequested { api, .. } => {
            if !app
                .state::<Arc<QuitState>>()
                .approved
                .load(Ordering::SeqCst)
            {
                api.prevent_exit();
                request_quit(app.clone());
            }
        }
        tauri::RunEvent::Exit => backend.close(),
        _ => {}
    });
}
