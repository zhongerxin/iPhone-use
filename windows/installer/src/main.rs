use std::{path::{Path, PathBuf}, process::Command, sync::Arc};
use idevice::usbmuxd::{UsbmuxdAddr, UsbmuxdConnection};
use isideload::{AnisetteConfiguration, AppleAccount, SideloadConfiguration, SideloadLogger, Error};
use isideload::{developer_session::DeveloperSession, sideload::sideload_app};
use serde_json::{json, Value};

struct Progress;
fn auth_failure(error: icloud_auth::Error) -> String {
    use icloud_auth::Error as Auth;
    use omnisette::AnisetteError as Ani;
    use tokio_tungstenite::tungstenite::Error as Ws;
    match error {
        Auth::AuthSrpWithMessage(code, _) => format!("Apple authentication error code {code}"),
        Auth::ErrorGettingAnisette(error) => match error {
            Ani::WsError(error) => match *error {
                Ws::Http(response) => format!("Anisette WebSocket HTTP status {}", response.status()),
                Ws::Tls(_) => "Anisette WebSocket TLS failure".into(),
                Ws::Io(error) => format!("Anisette WebSocket IO failure: {:?}", error.kind()),
                _ => "Anisette WebSocket protocol failure".into(),
            },
            Ani::ReqwestError(error) => format!("Anisette HTTP failure: connect={}, timeout={}, decode={}, status={:?}",
                error.is_connect(), error.is_timeout(), error.is_decode(), error.status().map(|s| s.as_u16())),
            Ani::IOError(error) => format!("Anisette local IO failure: {:?}", error.kind()),
            Ani::SerdeError(_) => "Anisette JSON response failure".into(),
            Ani::InvalidArgument(message) if message.starts_with("Provisioning response ") => message,
            _ => "Anisette provisioning failed".into(),
        },
        Auth::Bad2faCode => "Apple verification code was rejected".into(),
        Auth::ReqwestError(error) => format!("Apple HTTP failure: connect={}, timeout={}, status={:?}",
            error.is_connect(), error.is_timeout(), error.status().map(|s| s.as_u16())),
        _ => "Apple login failed or was cancelled. No credentials were logged.".into(),
    }
}
impl SideloadLogger for Progress {
    fn log(&self, message: &str) { println!("{message}"); }
    fn error(&self, _: &Error) { eprintln!("Signing or installation failed; see final error category."); }
}

fn dialog(root: &Path, mode: &str, extra: Option<&str>) -> Result<Value, String> {
    let mut command = Command::new(root.join(".venv/Scripts/python.exe"));
    command.arg(root.join("scripts/auth_dialog.py")).arg(mode);
    if let Some(value) = extra { command.arg(value); }
    let output = command.output().map_err(|_| "Could not open login dialog")?;
    if !output.status.success() { return Err("User cancelled the dialog".into()); }
    serde_json::from_slice(&output.stdout).map_err(|_| "Invalid dialog response".into())
}

async fn run(root: PathBuf, check_only: bool, anisette_only: bool) -> Result<(), String> {
    let prepared = root.join("runtime/prepared/WebDriverAgentRunner-Runner.app");
    let bundle = isideload::bundle::Bundle::new(prepared.clone()).map_err(|e| e.to_string())?;
    if !bundle.app_extensions().is_empty() { return Err("Unexpected provisioned app extensions in WDA".into()); }
    if !prepared.join("PlugIns/WebDriverAgentRunner.xctest/WebDriverAgentRunner").is_file() {
        return Err("WDA test executable is missing".into());
    }
    if bundle.bundle_identifier() != Some("com.iphoneuse.windows.wda") {
        return Err("Prepared bundle identifier is unexpected".into());
    }
    println!("WDA test bundle classification OK; nested XCTest code retained.");
    let mut usb = UsbmuxdConnection::default().await.map_err(|_| "Cannot connect to Apple USB service")?;
    let devices = usb.get_devices().await.map_err(|_| "Cannot discover USB devices")?;
    // Never silently choose among several phones.
    if devices.len() != 1 { return Err(format!("Expected exactly one connected device, found {}", devices.len())); }
    println!("Connected device found.");
    if check_only { return Ok(()); }
    let provider = devices[0].to_provider(UsbmuxdAddr::from_env_var().map_err(|_| "Invalid USB service address")?, "wda-installer");
    let auth_root = root.clone();
    let otp_root = root.clone();
    let state = root.join("runtime/signing");
    std::fs::create_dir_all(&state).map_err(|_| "Cannot create signing state directory")?;
    let server = std::env::var("WDA_ANISETTE_URL").unwrap_or_else(|_| "https://ani.sidestore.io".into());
    if !["https://ani.sidestore.io", "https://ani.sidestore.app", "https://ani.sidestore.zip"].contains(&server.as_str()) {
        return Err("Unsupported signing service; choose a verified SideStore HTTPS endpoint".into());
    }
    let config = AnisetteConfiguration::default()
        .set_configuration_path(state.clone())
        .set_anisette_url_v3(server);
    if anisette_only {
        println!("Checking signing service provisioning without Apple account login.");
        let mut provider = omnisette::AnisetteHeaders::get_anisette_headers_provider(config)
            .map_err(|e| auth_failure(icloud_auth::Error::ErrorGettingAnisette(e)))?;
        tokio::time::timeout(std::time::Duration::from_secs(60), provider.provider.get_anisette_headers(false))
            .await.map_err(|_| "Signing service provisioning exceeded 60 seconds")?
            .map_err(|e| auth_failure(icloud_auth::Error::ErrorGettingAnisette(e)))?;
        println!("Signing service provisioning succeeded; no Apple credentials were requested.");
        return Ok(());
    }
    println!("Initializing signing service, then opening Apple login dialog.");
    let account = AppleAccount::login(
        move || {
            let value = dialog(&auth_root, "credentials", None)?;
            Ok((value["apple_id"].as_str().ok_or("Missing account")?.into(),
                value["password"].as_str().ok_or("Missing password")?.into()))
        },
        move || {
            println!("Apple verification code required; enter it in the local dialog.");
            let value = dialog(&otp_root, "otp", None)?;
            Ok(value["code"].as_str().ok_or("Missing verification code")?.into())
        }, config,
    ).await.map_err(auth_failure)?;
    println!("Apple login succeeded.");
    let mut session = DeveloperSession::new(Arc::new(account));
    let teams = session.list_teams().await.map_err(|_| "Cannot retrieve developer teams")?;
    if teams.is_empty() { return Err("No Apple developer team available".into()); }
    let selected = if teams.len() == 1 { 0 } else {
        let choices = serde_json::to_string(&teams.iter().map(|t| t.team_id.clone()).collect::<Vec<_>>()).unwrap();
        let result = dialog(&root, "team", Some(&choices))?;
        result["index"].as_u64().ok_or("No team selected")? as usize
    };
    let team = teams.get(selected).ok_or("Invalid team selection")?;
    let expected_bundle = format!("com.iphoneuse.windows.wda.{}", team.team_id);
    session.set_team(team.clone());
    let logger = Progress;
    let config = SideloadConfiguration::new().set_store_dir(state)
        .set_logger(&logger).set_machine_name("Windows iPhone Control".into())
        .set_revoke_cert(false);
    println!("Registering dedicated WDA app ID, signing, and installing. Existing apps are not removed.");
    sideload_app(&provider, &session, prepared, config).await.map_err(|e| match e {
        Error::Auth(code, _) | Error::DeveloperSession(code, _) => format!("Apple developer service error code {code}"),
        Error::Certificate(_) => "Certificate setup failed; no existing certificate was revoked".into(),
        Error::InvalidBundle(message) => format!("Invalid WDA bundle: {message}"),
        Error::ZSignError(_) => "Native codesigning failed".into(),
        Error::IdeviceError(_) => "Device transfer/installation failed; check trust, app limit, and device connection".into(),
        _ => "Signing or installation failed".into(),
    })?;
    std::fs::write(root.join("runtime/install-result.json"), json!({"ok":true,"bundle_id":expected_bundle}).to_string())
        .map_err(|_| "Installed, but could not save result")?;
    println!("WDA installed successfully. Next: trust the developer certificate if prompted, then start XCTest.");
    Ok(())
}

#[tokio::main]
async fn main() {
    let args: Vec<String> = std::env::args().collect();
    let root = args.get(1).map(PathBuf::from).unwrap_or_else(|| std::env::current_dir().unwrap());
    match run(root, args.iter().any(|arg| arg == "--check"), args.iter().any(|arg| arg == "--anisette-check")).await {
        Ok(()) => {},
        Err(message) => { eprintln!("{message}"); std::process::exit(1); }
    }
}
