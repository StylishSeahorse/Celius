//! Read-only access to folders the user approved with the native folder picker.
//!
//! Every path from the web page is relative to an approved root and is resolved with
//! [`resolve_within`]: absolute paths, drive prefixes and `..` are refused, and the final
//! path (after following symlinks) must still be inside the root.

use std::fs;
use std::path::{Component, Path, PathBuf};

use serde::Serialize;

pub const MAX_READ_BYTES: u64 = 100 * 1024 * 1024;
const MAX_ENTRIES: usize = 5000;

#[derive(Debug, Serialize, PartialEq)]
pub struct Entry {
    pub name: String,
    /// Path relative to the root, with `/` separators.
    pub path: String,
    pub is_dir: bool,
    pub size: u64,
}

pub fn resolve_within(root: &Path, relative: &str) -> Result<PathBuf, String> {
    let relative = relative.replace('\\', "/");
    let rel = Path::new(&relative);
    for component in rel.components() {
        match component {
            Component::Normal(_) | Component::CurDir => {}
            _ => return Err("That path isn't allowed".into()),
        }
    }
    let base = root
        .canonicalize()
        .map_err(|_| "The approved folder is no longer available".to_string())?;
    let target = base
        .join(rel)
        .canonicalize()
        .map_err(|_| "That file or folder doesn't exist".to_string())?;
    if !target.starts_with(&base) {
        return Err("That path leads outside the approved folder".into());
    }
    Ok(target)
}

pub fn list_dir(root: &Path, relative: &str) -> Result<Vec<Entry>, String> {
    let base = root.canonicalize().map_err(|e| e.to_string())?;
    let dir = resolve_within(root, relative)?;
    if !dir.is_dir() {
        return Err("That isn't a folder".into());
    }
    let mut entries = Vec::new();
    for item in fs::read_dir(&dir).map_err(|e| e.to_string())?.flatten() {
        let name = item.file_name().to_string_lossy().into_owned();
        if name == ".git" || name == "node_modules" {
            continue;
        }
        // Follow symlinks only if they stay inside the root; skip anything else.
        let Ok(real) = item.path().canonicalize() else {
            continue;
        };
        if !real.starts_with(&base) {
            continue;
        }
        let Ok(meta) = fs::metadata(&real) else {
            continue;
        };
        let rel = real.strip_prefix(&base).unwrap_or(&real);
        entries.push(Entry {
            name,
            path: rel.to_string_lossy().replace('\\', "/"),
            is_dir: meta.is_dir(),
            size: if meta.is_dir() { 0 } else { meta.len() },
        });
        if entries.len() >= MAX_ENTRIES {
            break;
        }
    }
    entries.sort_by(|a, b| {
        b.is_dir
            .cmp(&a.is_dir)
            .then_with(|| a.name.to_lowercase().cmp(&b.name.to_lowercase()))
    });
    Ok(entries)
}

pub fn read_file(root: &Path, relative: &str) -> Result<Vec<u8>, String> {
    let path = resolve_within(root, relative)?;
    let meta = fs::metadata(&path).map_err(|e| e.to_string())?;
    if !meta.is_file() {
        return Err("That isn't a file".into());
    }
    if meta.len() > MAX_READ_BYTES {
        return Err("The file is too large to attach".into());
    }
    fs::read(&path).map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_tree(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("celius-test-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("root/src")).unwrap();
        fs::create_dir_all(dir.join("outside")).unwrap();
        fs::write(dir.join("root/README.md"), b"# hi").unwrap();
        fs::write(dir.join("root/src/app.py"), b"print(1)").unwrap();
        fs::write(dir.join("outside/secret.txt"), b"secret").unwrap();
        dir
    }

    #[test]
    fn paths_stay_inside_the_root() {
        let dir = temp_tree("resolve");
        let root = dir.join("root");
        assert!(resolve_within(&root, "src/app.py").is_ok());
        assert!(resolve_within(&root, "").is_ok());
        for bad in [
            "../outside/secret.txt",
            "src/../../outside/secret.txt",
            "/etc/passwd",
            "..\\outside\\secret.txt",
        ] {
            assert!(
                resolve_within(&root, bad).is_err(),
                "{bad} should be refused"
            );
        }
        assert_eq!(read_file(&root, "src/app.py").unwrap(), b"print(1)");
        assert!(read_file(&root, "src").is_err());
    }

    #[cfg(unix)]
    #[test]
    fn symlinks_cannot_escape() {
        let dir = temp_tree("symlink");
        let root = dir.join("root");
        std::os::unix::fs::symlink(dir.join("outside"), root.join("escape")).unwrap();
        std::os::unix::fs::symlink(root.join("src"), root.join("inner")).unwrap();
        assert!(read_file(&root, "escape/secret.txt").is_err());
        assert_eq!(read_file(&root, "inner/app.py").unwrap(), b"print(1)");
        let names: Vec<String> = list_dir(&root, "")
            .unwrap()
            .into_iter()
            .map(|e| e.name)
            .collect();
        assert!(
            !names.contains(&"escape".to_string()),
            "escaping links are hidden: {names:?}"
        );
        assert!(names.contains(&"inner".to_string()));
    }

    #[test]
    fn listing_puts_folders_first() {
        let dir = temp_tree("list");
        let entries = list_dir(&dir.join("root"), "").unwrap();
        assert_eq!(
            entries[0],
            Entry {
                name: "src".into(),
                path: "src".into(),
                is_dir: true,
                size: 0
            }
        );
        assert_eq!(entries[1].name, "README.md");
        assert_eq!(entries[1].size, 4);
    }
}
