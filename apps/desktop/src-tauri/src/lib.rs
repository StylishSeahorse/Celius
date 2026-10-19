//! Celius desktop shell: a native window around the web app served by your Celius server.
//!
//! Security model:
//! - The bundled connect page may only read and set the server address.
//! - The server's pages get a runtime capability scoped to that one origin, allowing only
//!   the folder commands below and opening http(s) links in the system browser.
//! - Folder access is read-only, limited to folders the user approved in the native
//!   folder picker, and every path is checked by [`folders::resolve_within`].

mod config;
mod folders;

use std::path::PathBuf;
use std::sync::Mutex;

use tauri::ipc::{CapabilityBuilder, Response};
use tauri::menu::{MenuBuilder, MenuItemBuilder, PredefinedMenuItem, SubmenuBuilder};
use tauri::{AppHandle, Manager, State, Url};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_opener::OpenerExt;

use config::{Config, Root};

/// The bundled connect page, as served by Tauri on each platform.
#[cfg(windows)]
const CONNECT_PAGE: &str = "http://tauri.localhost/index.html?change=1";
#[cfg(not(windows))]
const CONNECT_PAGE: &str = "tauri://localhost/index.html?change=1";

const SERVER_PERMISSIONS: &[&str] = &[
    "allow-list-roots",
    "allow-add-root",
    "allow-remove-root",
    "allow-list-dir",
    "allow-read-file",
    "allow-open-external",
];

struct AppState {
    config_path: PathBuf,
    config: Mutex<Config>,
    granted: Mutex<Vec<String>>,
}

impl AppState {
    fn root(&self, id: &str) -> Result<Root, String> {
        self.config
            .lock()
            .unwrap()
            .roots
            .iter()
            .find(|r| r.id == id)
            .cloned()
            .ok_or_else(|| "That folder is no longer approved".to_string())
    }
}

/// Let pages from `origin` (and only them) use the folder and link commands.
fn grant_server(app: &AppHandle, state: &AppState, origin: &str) -> Result<(), String> {
    let mut granted = state.granted.lock().unwrap();
    if granted.iter().any(|o| o == origin) {
        return Ok(());
    }
    let mut capability = CapabilityBuilder::new(format!("server-{}", granted.len()))
        .remote(format!("{origin}/*"))
        .local(false)
        .window("main");
    for permission in SERVER_PERMISSIONS {
        capability = capability.permission(*permission);
    }
    app.add_capability(capability).map_err(|e| e.to_string())?;
    granted.push(origin.to_string());
    Ok(())
}

#[tauri::command]
fn get_server(state: State<AppState>) -> Option<String> {
    state.config.lock().unwrap().server.clone()
}

#[tauri::command]
fn set_server(app: AppHandle, state: State<AppState>, url: String) -> Result<String, String> {
    let origin = config::normalise_origin(&url)?;
    grant_server(&app, &state, &origin)?;
    let mut config = state.config.lock().unwrap();
    config.server = Some(origin.clone());
    config::save(&state.config_path, &config)?;
    Ok(origin)
}

#[tauri::command]
fn list_roots(state: State<AppState>) -> Vec<Root> {
    state.config.lock().unwrap().roots.clone()
}

/// Ask the user to approve a folder with the native picker. `None` if they cancel.
#[tauri::command]
async fn add_root(app: AppHandle, state: State<'_, AppState>) -> Result<Option<Root>, String> {
    let (tx, rx) = std::sync::mpsc::channel();
    app.dialog()
        .file()
        .set_title("Approve a folder for Celius")
        .pick_folder(move |folder| {
            let _ = tx.send(folder);
        });
    let picked = tauri::async_runtime::spawn_blocking(move || rx.recv().ok().flatten())
        .await
        .map_err(|e| e.to_string())?;
    let Some(folder) = picked else {
        return Ok(None);
    };
    let path = folder
        .into_path()
        .map_err(|e| e.to_string())?
        .canonicalize()
        .map_err(|e| e.to_string())?;
    let mut config = state.config.lock().unwrap();
    if let Some(existing) = config.roots.iter().find(|r| r.path == path) {
        return Ok(Some(existing.clone()));
    }
    let root = Root {
        id: format!(
            "{:x}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ),
        name: path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_else(|| path.display().to_string()),
        path,
    };
    config.roots.push(root.clone());
    config::save(&state.config_path, &config)?;
    Ok(Some(root))
}

#[tauri::command]
fn remove_root(state: State<AppState>, id: String) -> Result<(), String> {
    let mut config = state.config.lock().unwrap();
    config.roots.retain(|r| r.id != id);
    config::save(&state.config_path, &config)
}

#[tauri::command]
fn list_dir(
    state: State<AppState>,
    root_id: String,
    path: String,
) -> Result<Vec<folders::Entry>, String> {
    folders::list_dir(&state.root(&root_id)?.path, &path)
}

/// File bytes as a binary IPC response (an ArrayBuffer in JavaScript, not a JSON array).
#[tauri::command]
fn read_file(state: State<AppState>, root_id: String, path: String) -> Result<Response, String> {
    Ok(Response::new(folders::read_file(
        &state.root(&root_id)?.path,
        &path,
    )?))
}

#[tauri::command]
fn open_external(app: AppHandle, url: String) -> Result<(), String> {
    let parsed = Url::parse(&url).map_err(|_| "Invalid link".to_string())?;
    if parsed.scheme() != "http" && parsed.scheme() != "https" {
        return Err("Only web links can be opened".into());
    }
    app.opener()
        .open_url(parsed.as_str(), None::<&str>)
        .map_err(|e| e.to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            get_server,
            set_server,
            list_roots,
            add_root,
            remove_root,
            list_dir,
            read_file,
            open_external
        ])
        .setup(|app| {
            let config_path = app.path().app_config_dir()?.join("config.json");
            let config = config::load(&config_path);
            let server = config.server.clone();
            app.manage(AppState {
                config_path,
                config: Mutex::new(config),
                granted: Mutex::new(Vec::new()),
            });
            if let Some(origin) = server {
                let state = app.state::<AppState>();
                if let Err(error) = grant_server(app.handle(), &state, &origin) {
                    eprintln!("could not grant {origin}: {error}");
                }
            }

            let change = MenuItemBuilder::with_id("change-server", "Change server…").build(app)?;
            let reload = MenuItemBuilder::with_id("reload", "Reload")
                .accelerator("CmdOrCtrl+R")
                .build(app)?;
            let quit = PredefinedMenuItem::quit(app, None)?;
            let celius = SubmenuBuilder::new(app, "Celius")
                .items(&[&change, &reload])
                .separator()
                .item(&quit)
                .build()?;
            let edit = SubmenuBuilder::new(app, "Edit")
                .undo()
                .redo()
                .separator()
                .cut()
                .copy()
                .paste()
                .select_all()
                .build()?;
            let menu = MenuBuilder::new(app).items(&[&celius, &edit]).build()?;
            app.set_menu(menu)?;
            Ok(())
        })
        .on_menu_event(|app, event| {
            let Some(window) = app.get_webview_window("main") else {
                return;
            };
            match event.id().as_ref() {
                "change-server" => {
                    if let Ok(url) = Url::parse(CONNECT_PAGE) {
                        let _ = window.navigate(url);
                    }
                }
                "reload" => {
                    let _ = window.eval("window.location.reload()");
                }
                _ => {}
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Celius");
}
