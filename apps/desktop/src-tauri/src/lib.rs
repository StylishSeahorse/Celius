//! Celius desktop shell: a native window around the web app served by your Celius server.
//!
//! Security model:
//! - The bundled connect page may only read and set the server address.
//! - The server's pages get a runtime capability scoped to that one origin, allowing only
//!   the folder commands below and opening http(s) links in the system browser.
//! - Folder access is read-only, limited to folders the user approved in the native
//!   folder picker, and every path is checked by [`folders::resolve_within`].
//! - Notifications: the server's pages may show a native notification (title and body
//!   only, length-limited) when a reply finishes while the window is in the background.
//!
//! Closing the window keeps Celius in the tray, so replies can still notify; quit from the
//! tray or the Celius menu.

mod config;
mod folders;

use std::path::PathBuf;
use std::sync::Mutex;

use tauri::ipc::{CapabilityBuilder, Response};
use tauri::menu::{MenuBuilder, MenuItemBuilder, PredefinedMenuItem, SubmenuBuilder};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, State, Url, WindowEvent};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_notification::NotificationExt;
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
    "allow-notify",
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

/// A native notification ("Your reply is ready"). Text only, clipped.
#[tauri::command]
fn notify(app: AppHandle, title: String, body: String) -> Result<(), String> {
    let title: String = title.chars().take(120).collect();
    let body: String = body.chars().take(300).collect();
    app.notification()
        .builder()
        .title(if title.is_empty() { "Celius".into() } else { title })
        .body(body)
        .show()
        .map_err(|e| e.to_string())
}

fn show_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_notification::init())
        .invoke_handler(tauri::generate_handler![
            get_server,
            set_server,
            list_roots,
            add_root,
            remove_root,
            list_dir,
            read_file,
            open_external,
            notify
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

            let show = MenuItemBuilder::with_id("tray-show", "Show Celius").build(app)?;
            let exit = MenuItemBuilder::with_id("tray-quit", "Quit Celius").build(app)?;
            let tray_menu = MenuBuilder::new(app).items(&[&show, &exit]).build()?;
            let mut tray = TrayIconBuilder::with_id("main")
                .tooltip("Celius")
                .menu(&tray_menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "tray-show" => show_window(app),
                    "tray-quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        show_window(tray.app_handle());
                    }
                });
            if let Some(icon) = app.default_window_icon() {
                tray = tray.icon(icon.clone());
            }
            tray.build(app)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing hides to the tray, so a reply that finishes later can still notify.
            if let WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "main" {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
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
