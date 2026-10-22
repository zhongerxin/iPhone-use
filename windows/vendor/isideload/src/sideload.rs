// This file was made using https://github.com/Dadoum/Sideloader as a reference.

use idevice::IdeviceService;
use idevice::lockdown::LockdownClient;
use idevice::provider::IdeviceProvider;
use zsign_rust::ZSignOptions;

use crate::application::Application;
use crate::device::install_app;
use crate::{DeveloperTeam, Error, SideloadConfiguration, SideloadLogger};
use crate::{
    certificate::CertificateIdentity,
    developer_session::{DeveloperDeviceType, DeveloperSession},
};
use std::{io::Write, path::PathBuf};

fn error_and_return(logger: &dyn SideloadLogger, error: Error) -> Result<(), Error> {
    logger.error(&error);
    Err(error)
}

/// Signs and installs an `.ipa` or `.app` onto a device.
///
/// # Arguments
/// - `device_provider` - [`idevice::provider::IdeviceProvider`] for the device
/// - `dev_session` - Authenticated Apple developer session ([`crate::developer_session::DeveloperSession`]).
/// - `app_path` - Path to the `.ipa` file or `.app` bundle to sign and install
/// - `config` - Sideload configuration options ([`crate::SideloadConfiguration`])
pub async fn sideload_app(
    device_provider: &impl IdeviceProvider,
    dev_session: &DeveloperSession,
    app_path: PathBuf,
    config: SideloadConfiguration<'_>,
) -> Result<(), Error> {
    let logger = config.logger;
    let mut lockdown_client = match LockdownClient::connect(device_provider).await {
        Ok(l) => l,
        Err(e) => {
            return error_and_return(logger, Error::IdeviceError(e));
        }
    };

    if let Ok(pairing_file) = device_provider.get_pairing_file().await {
        lockdown_client
            .start_session(&pairing_file)
            .await
            .map_err(Error::IdeviceError)?;
    }

    let device_name = lockdown_client
        .get_value(Some("DeviceName"), None)
        .await
        .map_err(Error::IdeviceError)?
        .as_string()
        .ok_or(Error::Generic(
            "Failed to convert DeviceName to string".to_string(),
        ))?
        .to_string();

    let device_uuid = lockdown_client
        .get_value(Some("UniqueDeviceID"), None)
        .await
        .map_err(Error::IdeviceError)?
        .as_string()
        .ok_or(Error::Generic(
            "Failed to convert UniqueDeviceID to string".to_string(),
        ))?
        .to_string();

    let team = match dev_session.get_team().await {
        Ok(t) => t,
        Err(e) => {
            return error_and_return(logger, e);
        }
    };

    logger.log("Successfully retrieved team");

    ensure_device_registered(logger, dev_session, &team, &device_uuid, &device_name).await?;

    let cert = match CertificateIdentity::new(
        &config.store_dir,
        dev_session,
        dev_session.account.apple_id.clone(),
        config.machine_name,
    )
    .await
    {
        Ok(c) => c,
        Err(e) => {
            return error_and_return(logger, e);
        }
    };

    logger.log("Successfully acquired certificate");

    let mut list_app_id_response = match dev_session
        .list_app_ids(DeveloperDeviceType::Ios, &team)
        .await
    {
        Ok(ids) => ids,
        Err(e) => {
            return error_and_return(logger, e);
        }
    };

    let mut app = Application::new(app_path)?;
    let is_sidestore = app.bundle.bundle_identifier().unwrap_or("") == "com.SideStore.SideStore";
    let main_app_bundle_id = match app.bundle.bundle_identifier() {
        Some(id) => id.to_string(),
        None => {
            return error_and_return(
                logger,
                Error::InvalidBundle("No bundle identifier found in IPA".to_string()),
            );
        }
    };
    let main_app_id_str = format!("{}.{}", main_app_bundle_id, team.team_id);
    let main_app_name = match app.bundle.bundle_name() {
        Some(name) => name.to_string(),
        None => {
            return error_and_return(
                logger,
                Error::InvalidBundle("No bundle name found in IPA".to_string()),
            );
        }
    };

    let extensions = app.bundle.app_extensions_mut();
    // for each extension, ensure it has a unique bundle identifier that starts with the main app's bundle identifier
    for ext in extensions.iter_mut() {
        if let Some(id) = ext.bundle_identifier() {
            if !(id.starts_with(&main_app_bundle_id) && id.len() > main_app_bundle_id.len()) {
                return error_and_return(
                    logger,
                    Error::InvalidBundle(format!(
                        "Extension {} is not part of the main app bundle identifier: {}",
                        ext.bundle_name().unwrap_or("Unknown"),
                        id
                    )),
                );
            } else {
                ext.set_bundle_identifier(&format!(
                    "{}{}",
                    main_app_id_str,
                    &id[main_app_bundle_id.len()..]
                ));
            }
        }
    }
    app.bundle.set_bundle_identifier(&main_app_id_str);

    let extension_refs: Vec<_> = app.bundle.app_extensions().iter().collect();
    let mut bundles_with_app_id = vec![&app.bundle];
    bundles_with_app_id.extend(extension_refs);

    let app_ids_to_register = bundles_with_app_id
        .iter()
        .filter(|bundle| {
            let bundle_id = bundle.bundle_identifier().unwrap_or("");
            !list_app_id_response
                .app_ids
                .iter()
                .any(|app_id| app_id.identifier == bundle_id)
        })
        .collect::<Vec<_>>();

    if let Some(available) = list_app_id_response.available_quantity
        && app_ids_to_register.len() > available.try_into().unwrap()
    {
        return error_and_return(
            logger,
            Error::InvalidBundle(format!(
                "This app requires {} app ids, but you only have {} available",
                app_ids_to_register.len(),
                available
            )),
        );
    }

    for bundle in app_ids_to_register {
        let id = bundle.bundle_identifier().unwrap_or("");
        let name = bundle.bundle_name().unwrap_or("");
        if let Err(e) = dev_session
            .add_app_id(DeveloperDeviceType::Ios, &team, name, id)
            .await
        {
            return error_and_return(logger, e);
        }
    }
    list_app_id_response = match dev_session
        .list_app_ids(DeveloperDeviceType::Ios, &team)
        .await
    {
        Ok(ids) => ids,
        Err(e) => {
            return error_and_return(logger, e);
        }
    };

    let mut app_ids: Vec<_> = list_app_id_response
        .app_ids
        .into_iter()
        .filter(|app_id| {
            bundles_with_app_id
                .iter()
                .any(|bundle| app_id.identifier == bundle.bundle_identifier().unwrap_or(""))
        })
        .collect();
    let main_app_id = match app_ids
        .iter()
        .find(|app_id| app_id.identifier == main_app_id_str)
        .cloned()
    {
        Some(id) => id,
        None => {
            return error_and_return(
                logger,
                Error::Generic(format!(
                    "Main app ID {} not found in registered app IDs",
                    main_app_id_str
                )),
            );
        }
    };

    logger.log("Successfully registered app IDs");

    for app_id in app_ids.iter_mut() {
        let app_group_feature_enabled = app_id
            .features
            .get(
                "APG3427HIY", /* Gotta love apple and their magic strings! */
            )
            .and_then(|v| v.as_boolean())
            .ok_or(Error::Generic(
                "App group feature not found in app id".to_string(),
            ))?;
        if !app_group_feature_enabled {
            let mut body = plist::Dictionary::new();
            body.insert("APG3427HIY".to_string(), plist::Value::Boolean(true));
            let new_features = match dev_session
                .update_app_id(DeveloperDeviceType::Ios, &team, app_id, &body)
                .await
            {
                Ok(new_feats) => new_feats,
                Err(e) => {
                    return error_and_return(logger, e);
                }
            };
            app_id.features = new_features;
        }
    }

    let group_identifier = format!("group.{}", main_app_id_str);

    if is_sidestore {
        app.bundle.app_info.insert(
            "ALTAppGroups".to_string(),
            plist::Value::Array(vec![plist::Value::String(group_identifier.clone())]),
        );
    }

    let app_groups = match dev_session
        .list_application_groups(DeveloperDeviceType::Ios, &team)
        .await
    {
        Ok(groups) => groups,
        Err(e) => {
            return error_and_return(logger, e);
        }
    };

    let matching_app_groups = app_groups
        .iter()
        .filter(|group| group.identifier == group_identifier.clone())
        .collect::<Vec<_>>();

    let app_group = if matching_app_groups.is_empty() {
        match dev_session
            .add_application_group(
                DeveloperDeviceType::Ios,
                &team,
                &group_identifier,
                &main_app_name,
            )
            .await
        {
            Ok(group) => group,
            Err(e) => {
                return error_and_return(logger, e);
            }
        }
    } else {
        matching_app_groups[0].clone()
    };

    //let mut provisioning_profiles: HashMap<String, ProvisioningProfile> = HashMap::new();
    for app_id in app_ids {
        let assign_res = dev_session
            .assign_application_group_to_app_id(
                DeveloperDeviceType::Ios,
                &team,
                &app_id,
                &app_group,
            )
            .await;
        if assign_res.is_err() {
            return error_and_return(logger, assign_res.err().unwrap());
        }
        // let provisioning_profile = match account
        //     // This doesn't seem right to me, but it's what Sideloader does... Shouldn't it be downloading the provisioning profile for this app ID, not the main?
        //     .download_team_provisioning_profile(DeveloperDeviceType::Ios, &team, &main_app_id)
        //     .await
        // {
        //     Ok(pp /* tee hee */) => pp,
        //     Err(e) => {
        //         return emit_error_and_return(
        //             &window,
        //             &format!("Failed to download provisioning profile: {:?}", e),
        //         );
        //     }
        // };
        // provisioning_profiles.insert(app_id.identifier.clone(), provisioning_profile);
    }

    logger.log("Successfully registered app groups");

    let provisioning_profile = match dev_session
        .download_team_provisioning_profile(DeveloperDeviceType::Ios, &team, &main_app_id)
        .await
    {
        Ok(pp /* tee hee */) => pp,
        Err(e) => {
            return error_and_return(logger, e);
        }
    };

    let profile_path = config
        .store_dir
        .join(format!("{}.mobileprovision", main_app_id_str));

    if profile_path.exists() {
        std::fs::remove_file(&profile_path).map_err(Error::Filesystem)?;
    }

    let mut file = std::fs::File::create(&profile_path).map_err(Error::Filesystem)?;
    file.write_all(&provisioning_profile.encoded_profile)
        .map_err(Error::Filesystem)?;

    // Without this, zsign complains it can't find the provision file
    #[cfg(target_os = "windows")]
    {
        file.sync_all().map_err(|e| Error::Filesystem(e))?;
        drop(file);
    }

    app.bundle.write_info()?;
    for ext in app.bundle.app_extensions_mut() {
        ext.write_info()?;
    }

    match ZSignOptions::new(app.bundle.bundle_dir.to_str().unwrap())
        .with_cert_file(cert.get_certificate_file_path().to_str().unwrap())
        .with_pkey_file(cert.get_private_key_file_path().to_str().unwrap())
        .with_prov_file(profile_path.to_str().unwrap())
        .sign()
    {
        Ok(_) => {}
        Err(e) => {
            return error_and_return(logger, Error::ZSignError(e));
        }
    };

    logger.log("App signed!");

    logger.log("Installing app (Transfer)... 0%");

    let res = install_app(device_provider, &app.bundle.bundle_dir, |percentage| {
        logger.log(&format!("Installing app... {}%", percentage));
    })
    .await;
    if let Err(e) = res {
        return error_and_return(logger, e);
    }

    if config.revoke_cert {
        dev_session
            .revoke_development_cert(DeveloperDeviceType::Ios, &team, &cert.get_serial_number()?)
            .await?;
        logger.log("Certificate revoked");
    }

    Ok(())
}

pub async fn ensure_device_registered(
    logger: &dyn SideloadLogger,
    dev_session: &DeveloperSession,
    team: &DeveloperTeam,
    uuid: &str,
    name: &str,
) -> Result<(), Error> {
    let devices = dev_session
        .list_devices(DeveloperDeviceType::Ios, team)
        .await;
    if let Err(e) = devices {
        return error_and_return(logger, e);
    }
    let devices = devices.unwrap();
    if !devices.iter().any(|d| d.device_number == uuid) {
        logger.log("Device not found in your account");
        // TODO: Actually test!
        dev_session
            .add_device(DeveloperDeviceType::Ios, team, name, uuid)
            .await?;
        logger.log("Successfully added device to your account");
    }
    logger.log("Device is a development device");
    Ok(())
}
