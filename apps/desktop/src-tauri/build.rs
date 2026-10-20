fn main() {
    // Every app command gets a generated `allow-<command>` permission, so each one must be
    // granted explicitly: some to the bundled connect page, others to the server's pages.
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "get_server",
            "set_server",
            "list_roots",
            "add_root",
            "remove_root",
            "list_dir",
            "read_file",
            "open_external",
            "notify",
        ]),
    ))
    .expect("failed to run tauri-build");
}
