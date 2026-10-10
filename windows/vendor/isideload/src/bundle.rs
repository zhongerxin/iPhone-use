// This file was made using https://github.com/Dadoum/Sideloader as a reference.

use crate::Error;
use plist::{Dictionary, Value};
use std::{
    fs,
    path::{Path, PathBuf},
};

pub struct Bundle {
    pub app_info: Dictionary,
    pub bundle_dir: PathBuf,

    app_extensions: Vec<Bundle>,
    _frameworks: Vec<Bundle>,
    _libraries: Vec<String>,
}

impl Bundle {
    pub fn new(bundle_dir: PathBuf) -> Result<Self, Error> {
        let mut bundle_path = bundle_dir;
        // Remove trailing slash/backslash
        if let Some(path_str) = bundle_path.to_str()
            && (path_str.ends_with('/') || path_str.ends_with('\\'))
        {
            bundle_path = PathBuf::from(&path_str[..path_str.len() - 1]);
        }

        let info_plist_path = bundle_path.join("Info.plist");
        assert_bundle(
            info_plist_path.exists(),
            &format!("No Info.plist here: {}", info_plist_path.display()),
        )?;

        let plist_data = fs::read(&info_plist_path)
            .map_err(|e| Error::InvalidBundle(format!("Failed to read Info.plist: {}", e)))?;

        let app_info = plist::from_bytes(&plist_data)
            .map_err(|e| Error::InvalidBundle(format!("Failed to parse Info.plist: {}", e)))?;

        // Load app extensions from PlugIns directory
        let plug_ins_dir = bundle_path.join("PlugIns");
        let app_extensions = if plug_ins_dir.exists() {
            fs::read_dir(&plug_ins_dir)
                .map_err(|e| {
                    Error::InvalidBundle(format!("Failed to read PlugIns directory: {}", e))
                })?
                .filter_map(|entry| entry.ok())
                .filter(|entry| {
                    entry.file_type().map(|ft| ft.is_dir()).unwrap_or(false)
                        && entry.path().join("Info.plist").exists()
                        // XCTest plugins are signed code, not provisioned app extensions.
                        // zsign recursively signs .xctest and nested frameworks itself.
                        && entry.path().extension().is_some_and(|ext| ext == "appex")
                })
                .filter_map(|entry| Bundle::new(entry.path()).ok())
                .collect()
        } else {
            Vec::new()
        };

        // Load frameworks from Frameworks directory
        let frameworks_dir = bundle_path.join("Frameworks");
        let frameworks = if frameworks_dir.exists() {
            fs::read_dir(&frameworks_dir)
                .map_err(|e| {
                    Error::InvalidBundle(format!("Failed to read Frameworks directory: {}", e))
                })?
                .filter_map(|entry| entry.ok())
                .filter(|entry| {
                    entry.file_type().map(|ft| ft.is_dir()).unwrap_or(false)
                        && entry.path().join("Info.plist").exists()
                })
                .filter_map(|entry| Bundle::new(entry.path()).ok())
                .collect()
        } else {
            Vec::new()
        };

        // Find all .dylib files in the bundle directory (recursive)
        let libraries = find_dylibs(&bundle_path, &bundle_path)?;

        Ok(Bundle {
            app_info,
            bundle_dir: bundle_path,
            app_extensions,
            _frameworks: frameworks,
            _libraries: libraries,
        })
    }

    pub fn set_bundle_identifier(&mut self, id: &str) {
        self.app_info.insert(
            "CFBundleIdentifier".to_string(),
            Value::String(id.to_string()),
        );
    }

    pub fn bundle_identifier(&self) -> Option<&str> {
        self.app_info
            .get("CFBundleIdentifier")
            .and_then(|v| v.as_string())
    }

    pub fn bundle_name(&self) -> Option<&str> {
        self.app_info
            .get("CFBundleName")
            .and_then(|v| v.as_string())
    }

    pub fn app_extensions(&self) -> &[Bundle] {
        &self.app_extensions
    }

    pub fn app_extensions_mut(&mut self) -> &mut [Bundle] {
        &mut self.app_extensions
    }

    pub fn write_info(&self) -> Result<(), Error> {
        let info_plist_path = self.bundle_dir.join("Info.plist");
        let result = plist::to_file_binary(&info_plist_path, &self.app_info);

        if result.is_err() {
            return Err(Error::InvalidBundle(format!(
                "Failed to write Info.plist: {}",
                result.unwrap_err()
            )));
        }
        Ok(())
    }
}

fn assert_bundle(condition: bool, msg: &str) -> Result<(), Error> {
    if !condition {
        Err(Error::InvalidBundle(msg.to_string()))
    } else {
        Ok(())
    }
}

fn find_dylibs(dir: &Path, bundle_root: &Path) -> Result<Vec<String>, Error> {
    let mut libraries = Vec::new();

    fn collect_dylibs(
        dir: &Path,
        bundle_root: &Path,
        libraries: &mut Vec<String>,
    ) -> Result<(), Error> {
        let entries = fs::read_dir(dir).map_err(|e| {
            Error::InvalidBundle(format!("Failed to read directory {}: {}", dir.display(), e))
        })?;

        for entry in entries {
            let entry = entry.map_err(|e| {
                Error::InvalidBundle(format!("Failed to read directory entry: {}", e))
            })?;

            let path = entry.path();
            let file_type = entry
                .file_type()
                .map_err(|e| Error::InvalidBundle(format!("Failed to get file type: {}", e)))?;

            if file_type.is_file() {
                if let Some(name) = path.file_name().and_then(|n| n.to_str())
                    && name.ends_with(".dylib")
                {
                    // Get relative path from bundle root
                    if let Ok(relative_path) = path.strip_prefix(bundle_root)
                        && let Some(relative_str) = relative_path.to_str()
                    {
                        libraries.push(relative_str.to_string());
                    }
                }
            } else if file_type.is_dir() {
                collect_dylibs(&path, bundle_root, libraries)?;
            }
        }
        Ok(())
    }

    collect_dylibs(dir, bundle_root, &mut libraries)?;
    Ok(libraries)
}
