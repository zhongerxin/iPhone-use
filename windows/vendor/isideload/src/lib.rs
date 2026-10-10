pub mod application;
pub mod bundle;
pub mod certificate;
pub mod developer_session;
pub mod device;
pub mod sideload;

use std::io::Error as IOError;

pub use icloud_auth::{AnisetteConfiguration, AppleAccount};

use developer_session::DeveloperTeam;
use idevice::IdeviceError;
use thiserror::Error as ThisError;
use zsign_rust::ZSignError;

#[derive(Debug, ThisError)]
pub enum Error {
    #[error("Authentication error {0}: {1}")]
    Auth(i64, String),
    #[error("Developer session error {0}: {1}")]
    DeveloperSession(i64, String),
    #[error("Error: {0}")]
    Generic(String),
    #[error("Failed to parse: {0}")]
    Parse(String),
    #[error("Invalid bundle: {0}")]
    InvalidBundle(String),
    #[error("Certificate error: {0}")]
    Certificate(String),
    #[error(transparent)]
    Filesystem(#[from] IOError),
    #[error(transparent)]
    IdeviceError(#[from] IdeviceError),
    #[error(transparent)]
    ZSignError(#[from] ZSignError),
}

pub trait SideloadLogger: Send + Sync {
    fn log(&self, message: &str);
    fn error(&self, error: &Error);
}

pub struct DefaultLogger;

impl SideloadLogger for DefaultLogger {
    fn log(&self, message: &str) {
        println!("{message}");
    }

    fn error(&self, error: &Error) {
        eprintln!("Error: {}", error);
    }
}

/// Sideload configuration options.
pub struct SideloadConfiguration<'a> {
    /// An arbitrary machine name to appear on the certificate (e.x. "YCode")
    pub machine_name: String,
    /// Logger for reporting progress and errors
    pub logger: &'a dyn SideloadLogger,
    /// Directory used to store intermediate artifacts (profiles, certs, etc.). This directory will not be cleared at the end.
    pub store_dir: std::path::PathBuf,
    /// Whether or not to revoke the certificate immediately after installation
    pub revoke_cert: bool,
}

impl Default for SideloadConfiguration<'_> {
    fn default() -> Self {
        SideloadConfiguration::new()
    }
}

impl<'a> SideloadConfiguration<'a> {
    pub fn new() -> Self {
        SideloadConfiguration {
            machine_name: "isideload".to_string(),
            logger: &DefaultLogger,
            store_dir: std::env::current_dir().unwrap(),
            revoke_cert: false,
        }
    }

    pub fn set_machine_name(mut self, machine_name: String) -> Self {
        self.machine_name = machine_name;
        self
    }

    pub fn set_logger(mut self, logger: &'a dyn SideloadLogger) -> Self {
        self.logger = logger;
        self
    }

    pub fn set_store_dir(mut self, store_dir: std::path::PathBuf) -> Self {
        self.store_dir = store_dir;
        self
    }

    pub fn set_revoke_cert(mut self, revoke_cert: bool) -> Self {
        self.revoke_cert = revoke_cert;
        self
    }
}
