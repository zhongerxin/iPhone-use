// This file was made using https://github.com/Dadoum/Sideloader as a reference.

use crate::Error;
use crate::bundle::Bundle;
use std::fs::File;
use std::path::PathBuf;
use zip::ZipArchive;

pub struct Application {
    pub bundle: Bundle,
    //pub temp_path: PathBuf,
}

impl Application {
    pub fn new(path: PathBuf) -> Result<Self, Error> {
        if !path.exists() {
            return Err(Error::InvalidBundle(
                "Application path does not exist".to_string(),
            ));
        }

        let mut bundle_path = path.clone();
        //let mut temp_path = PathBuf::new();

        if path.is_file() {
            let temp_dir = std::env::temp_dir();
            let temp_path = temp_dir
                .join(path.file_name().unwrap().to_string_lossy().to_string() + "_extracted");
            if temp_path.exists() {
                std::fs::remove_dir_all(&temp_path).map_err(Error::Filesystem)?;
            }
            std::fs::create_dir_all(&temp_path).map_err(Error::Filesystem)?;

            let file = File::open(&path).map_err(Error::Filesystem)?;
            let mut archive = ZipArchive::new(file).map_err(|e| {
                Error::Generic(format!("Failed to open application archive: {}", e))
            })?;
            archive.extract(&temp_path).map_err(|e| {
                Error::Generic(format!("Failed to extract application archive: {}", e))
            })?;

            let payload_folder = temp_path.join("Payload");
            if payload_folder.exists() && payload_folder.is_dir() {
                let app_dirs: Vec<_> = std::fs::read_dir(&payload_folder)
                    .map_err(|e| {
                        Error::Generic(format!("Failed to read Payload directory: {}", e))
                    })?
                    .filter_map(Result::ok)
                    .filter(|entry| entry.file_type().map(|ft| ft.is_dir()).unwrap_or(false))
                    .filter(|entry| entry.path().extension().is_some_and(|ext| ext == "app"))
                    .collect();
                if app_dirs.len() == 1 {
                    bundle_path = app_dirs[0].path();
                } else if app_dirs.is_empty() {
                    return Err(Error::InvalidBundle(
                        "No .app directory found in Payload".to_string(),
                    ));
                } else {
                    return Err(Error::InvalidBundle(
                        "Multiple .app directories found in Payload".to_string(),
                    ));
                }
            } else {
                return Err(Error::InvalidBundle(
                    "No Payload directory found in the application archive".to_string(),
                ));
            }
        }
        let bundle = Bundle::new(bundle_path)?;

        Ok(Application {
            bundle, /*temp_path*/
        })
    }
}
