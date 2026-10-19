//! Celius desktop shell: a native window around the web app served by your Celius server.
//!
//! Security model: the remote server's pages get no access to Tauri's IPC (no capability
//! grants a remote URL), so the desktop app adds no attack surface to the web app. Native
//! features (e.g. approved folders in phase 4) will be exposed through narrowly scoped,
//! explicitly listed commands.

use tauri::menu::{MenuBuilder, MenuItemBuilder, PredefinedMenuItem, SubmenuBuilder};
use tauri::{Manager, Url};

/// The bundled connect page, as served by Tauri on each platform.
#[cfg(windows)]
const CONNECT_PAGE: &str = "http://tauri.localhost/index.html?change=1";
#[cfg(not(windows))]
const CONNECT_PAGE: &str = "tauri://localhost/index.html?change=1";

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
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
