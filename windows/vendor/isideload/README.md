# isideload

[![Build isideload](https://github.com/nab138/isideload/actions/workflows/build.yml/badge.svg)](https://github.com/nab138/isideload/actions/workflows/build.yml)

A Rust library for sideloading iOS applications. Designed for use in [CrossCode](https://github.com/nab138/CrossCode).

This also serves as a rust library for accessing Apple's private developer APIs. See [`developer_session.rs`](isideload/src/developer_session.rs) for details.

## Disclaimer

This package uses private Apple Developer APIs. Use at your own risk.

## Usage

To use isideload, add the following to your `Cargo.toml`:

```toml
[dependencies]
# Make sure to use the latest version
isideload = { version = "0.1.8", features = ["vendored-openssl" ] } # Optionally, both vendored features can be enabled to avoid needing OpenSSL installed on your system.
idevice = { version = "0.1.40", features = ["usbmuxd"]} # Used to give isideload an IdeviceProvider. You don't need to use usbmuxd. For more info see https://github.com/jkcoxson/idevice
```

Then, you can use it like so:

```rs
use std::{env, path::PathBuf, sync::Arc};

use idevice::usbmuxd::{UsbmuxdAddr, UsbmuxdConnection};
use isideload::{
    AnisetteConfiguration, AppleAccount, SideloadConfiguration,
    developer_session::DeveloperSession, sideload::sideload_app,
};

#[tokio::main]
async fn main() {
    let args: Vec<String> = env::args().collect();
    let app_path = PathBuf::from(
        args.get(1)
            .expect("Please provide the path to the app to install"),
    );
    let apple_id = args
        .get(2)
        .expect("Please provide the Apple ID to use for installation");
    let apple_password = args.get(3).expect("Please provide the Apple ID password");

    // You don't have to use usbmuxd, you can use any IdeviceProvider
    let usbmuxd = UsbmuxdConnection::default().await;
    if usbmuxd.is_err() {
        panic!("Failed to connect to usbmuxd: {:?}", usbmuxd.err());
    }
    let mut usbmuxd = usbmuxd.unwrap();

    let devs = usbmuxd.get_devices().await.unwrap();
    if devs.is_empty() {
        panic!("No devices found");
    }

    let provider = devs
        .iter()
        .next()
        .unwrap()
        .to_provider(UsbmuxdAddr::from_env_var().unwrap(), "isideload-demo");

    // Change the anisette url and such here
    // Note that right now only remote anisette servers are supported
    let anisette_config = AnisetteConfiguration::default();

    let get_2fa_code = || {
        let mut code = String::new();
        println!("Enter 2FA code:");
        std::io::stdin().read_line(&mut code).unwrap();
        Ok(code.trim().to_string())
    };

    let account = AppleAccount::login(
        || Ok((apple_id.to_string(), apple_password.to_string())),
        get_2fa_code,
        anisette_config,
    )
    .await
    .unwrap();

    let dev_session = DeveloperSession::new(Arc::new(account));

    // You can change the machine name, store directory (for certs, anisette data, & provision files), and logger
    let config = SideloadConfiguration::default().set_machine_name("isideload-demo".to_string());

    sideload_app(&provider, &dev_session, app_path, config)
        .await
        .unwrap()
}
```

See [examples/minimal/src/main.rs](examples/minimal/src/main.rs).

## Licensing

This project is licensed under the MPL-2.0 License. See the [LICENSE](LICENSE) file for details.

## Credits

- The amazing [idevice](https://github.com/jkcoxson/idevice) crate is used to communicate with the device

- Packages from [`apple-private-apis`](https://github.com/SideStore/apple-private-apis) were used for authentication, but the original project was left unfinished. To support isideload, `apple-private-apis` was forked and modified to add missing features. With permission from the original developers, the fork was published to crates.io until the official project is published.

- [ZSign](https://github.com/zhlynn/zsign) was used for code signing with [custom rust bindings](https://github.com/nab138/zsign-rust)

- [Sideloader](https://github.com/Dadoum/Sideloader) was used as a reference for how the private API endpoints work
