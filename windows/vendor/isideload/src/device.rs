use idevice::{
    IdeviceService, afc::AfcClient, installation_proxy::InstallationProxyClient,
    provider::IdeviceProvider,
};
use std::pin::Pin;
use std::{future::Future, path::Path};

use crate::Error;

/// Installs an ***already signed*** app onto your device.
/// To sign and install an app, see [`crate::sideload::sideload_app`]
pub async fn install_app(
    provider: &impl IdeviceProvider,
    app_path: &Path,
    progress_callback: impl Fn(u64),
) -> Result<(), Error> {
    let mut afc_client = AfcClient::connect(provider)
        .await
        .map_err(Error::IdeviceError)?;

    let dir = format!(
        "PublicStaging/{}",
        app_path.file_name().unwrap().to_string_lossy()
    );
    afc_upload_dir(&mut afc_client, app_path, &dir).await?;

    let mut instproxy_client = InstallationProxyClient::connect(provider)
        .await
        .map_err(Error::IdeviceError)?;

    let mut options = plist::Dictionary::new();
    options.insert("PackageType".to_string(), "Developer".into());
    instproxy_client
        .install_with_callback(
            dir,
            Some(plist::Value::Dictionary(options)),
            async |(percentage, _)| {
                progress_callback(percentage);
            },
            (),
        )
        .await
        .map_err(Error::IdeviceError)?;

    Ok(())
}

fn afc_upload_dir<'a>(
    afc_client: &'a mut AfcClient,
    path: &'a Path,
    afc_path: &'a str,
) -> Pin<Box<dyn Future<Output = Result<(), Error>> + Send + 'a>> {
    Box::pin(async move {
        let entries = std::fs::read_dir(path).map_err(Error::Filesystem)?;
        afc_client
            .mk_dir(afc_path)
            .await
            .map_err(Error::IdeviceError)?;
        for entry in entries {
            let entry = entry.map_err(Error::Filesystem)?;
            let path = entry.path();
            if path.is_dir() {
                let new_afc_path = format!(
                    "{}/{}",
                    afc_path,
                    path.file_name().unwrap().to_string_lossy()
                );
                afc_upload_dir(afc_client, &path, &new_afc_path).await?;
            } else {
                let mut file_handle = afc_client
                    .open(
                        format!(
                            "{}/{}",
                            afc_path,
                            path.file_name().unwrap().to_string_lossy()
                        ),
                        idevice::afc::opcode::AfcFopenMode::WrOnly,
                    )
                    .await
                    .map_err(Error::IdeviceError)?;
                let bytes = std::fs::read(&path).map_err(Error::Filesystem)?;
                file_handle
                    .write_entire(&bytes)
                    .await
                    .map_err(Error::IdeviceError)?;
                file_handle.close().await.map_err(Error::IdeviceError)?;
            }
        }
        Ok(())
    })
}
