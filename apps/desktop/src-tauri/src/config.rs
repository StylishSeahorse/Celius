//! Settings stored in the app's config directory: the server address and approved folders.

use std::fs;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Config {
    /// Origin of the Celius server, e.g. `https://ai.example.com`.
    pub server: Option<String>,
    /// Folders the user explicitly approved for attaching files.
    #[serde(default)]
    pub roots: Vec<Root>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Root {
    pub id: String,
    pub name: String,
    pub path: PathBuf,
}

pub fn load(path: &PathBuf) -> Config {
    fs::read_to_string(path)
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_default()
}

pub fn save(path: &PathBuf, config: &Config) -> Result<(), String> {
    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    let text = serde_json::to_string_pretty(config).map_err(|e| e.to_string())?;
    let tmp = path.with_extension("json.tmp");
    fs::write(&tmp, text).map_err(|e| e.to_string())?;
    fs::rename(&tmp, path).map_err(|e| e.to_string())
}

/// Only `http(s)://host[:port]`, normalised to an origin (no path, query or credentials).
pub fn normalise_origin(raw: &str) -> Result<String, String> {
    let url =
        url::Url::parse(raw.trim()).map_err(|_| "That isn't a valid web address".to_string())?;
    if url.scheme() != "http" && url.scheme() != "https" {
        return Err("Only http:// and https:// servers are supported".into());
    }
    if !url.username().is_empty() || url.password().is_some() || url.host_str().is_none() {
        return Err("The address may not contain credentials".into());
    }
    Ok(url.origin().ascii_serialization())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn origins_are_normalised_and_restricted() {
        assert_eq!(
            normalise_origin("http://192.168.0.204:8000/x?y").unwrap(),
            "http://192.168.0.204:8000"
        );
        assert_eq!(
            normalise_origin("https://AI.Example.com").unwrap(),
            "https://ai.example.com"
        );
        assert!(normalise_origin("file:///etc/passwd").is_err());
        assert!(normalise_origin("javascript:alert(1)").is_err());
        assert!(normalise_origin("https://user:pw@example.com").is_err());
    }
}
