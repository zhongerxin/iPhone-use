// This file was made using https://github.com/Dadoum/Sideloader as a reference for the apple private endpoints

use crate::Error;
use icloud_auth::{AppleAccount, Error as ICloudError};
use plist::{Date, Dictionary, Value};
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use uuid::Uuid;

pub struct DeveloperSession {
    pub account: Arc<AppleAccount>,
    team: Option<DeveloperTeam>,
}

impl DeveloperSession {
    pub fn new(account: Arc<AppleAccount>) -> Self {
        DeveloperSession {
            account,
            team: None,
        }
    }

    pub async fn send_developer_request(
        &self,
        url: &str,
        body: Option<Dictionary>,
    ) -> Result<Dictionary, Error> {
        let mut request = Dictionary::new();
        request.insert(
            "clientId".to_string(),
            Value::String("XABBG36SBA".to_string()),
        );
        request.insert(
            "protocolVersion".to_string(),
            Value::String("QH65B2".to_string()),
        );
        request.insert(
            "requestId".to_string(),
            Value::String(Uuid::new_v4().to_string().to_uppercase()),
        );
        request.insert(
            "userLocale".to_string(),
            Value::Array(vec![Value::String("en_US".to_string())]),
        );
        if let Some(body) = body {
            for (key, value) in body {
                request.insert(key, value);
            }
        }

        let response = self
            .account
            .send_request(url, Some(request))
            .await
            .map_err(|e| {
                if let ICloudError::AuthSrpWithMessage(code, message) = e {
                    Error::DeveloperSession(code, format!("Developer request failed: {}", message))
                } else {
                    Error::Generic("Failed to send developer request".to_string())
                }
            })?;

        let status_code = response
            .get("resultCode")
            .and_then(|v| v.as_unsigned_integer())
            .unwrap_or(0);
        if status_code != 0 {
            let description = response
                .get("userString")
                .and_then(|v| v.as_string())
                .or_else(|| response.get("resultString").and_then(|v| v.as_string()))
                .unwrap_or("(null)");
            return Err(Error::DeveloperSession(
                status_code as i64,
                description.to_string(),
            ));
        }
        Ok(response)
    }

    pub async fn list_teams(&self) -> Result<Vec<DeveloperTeam>, Error> {
        let url = "https://developerservices2.apple.com/services/QH65B2/listTeams.action?clientId=XABBG36SBA";
        let response = self.send_developer_request(url, None).await?;

        let teams = response
            .get("teams")
            .and_then(|v| v.as_array())
            .ok_or(Error::Parse("teams".to_string()))?;

        let mut result = Vec::new();
        for team in teams {
            let dict = team
                .as_dictionary()
                .ok_or(Error::Parse("team".to_string()))?;
            let name = dict
                .get("name")
                .and_then(|v| v.as_string())
                .ok_or(Error::Parse("name".to_string()))?
                .to_string();
            let team_id = dict
                .get("teamId")
                .and_then(|v| v.as_string())
                .ok_or(Error::Parse("teamId".to_string()))?
                .to_string();
            result.push(DeveloperTeam {
                _name: name,
                team_id,
            });
        }
        Ok(result)
    }

    pub async fn get_team(&self) -> Result<DeveloperTeam, Error> {
        if let Some(team) = &self.team {
            return Ok(team.clone());
        }
        let teams = self.list_teams().await?;
        if teams.is_empty() {
            return Err(Error::DeveloperSession(
                -1,
                "No developer teams found".to_string(),
            ));
        }
        // TODO: Handle multiple teams
        Ok(teams[0].clone())
    }

    pub fn set_team(&mut self, team: DeveloperTeam) {
        self.team = Some(team);
    }

    pub async fn list_devices(
        &self,
        device_type: DeveloperDeviceType,
        team: &DeveloperTeam,
    ) -> Result<Vec<DeveloperDevice>, Error> {
        let url = dev_url(device_type, "listDevices");
        let mut body = Dictionary::new();
        body.insert("teamId".to_string(), Value::String(team.team_id.clone()));
        let response = self.send_developer_request(&url, Some(body)).await?;

        let devices = response
            .get("devices")
            .and_then(|v| v.as_array())
            .ok_or(Error::Parse("devices".to_string()))?;

        let mut result = Vec::new();
        for device in devices {
            let dict = device
                .as_dictionary()
                .ok_or(Error::Parse("device".to_string()))?;
            let device_id = dict
                .get("deviceId")
                .and_then(|v| v.as_string())
                .ok_or(Error::Parse("deviceId".to_string()))?
                .to_string();
            let name = dict
                .get("name")
                .and_then(|v| v.as_string())
                .ok_or(Error::Parse("name".to_string()))?
                .to_string();
            let device_number = dict
                .get("deviceNumber")
                .and_then(|v| v.as_string())
                .ok_or(Error::Parse("deviceNumber".to_string()))?
                .to_string();
            result.push(DeveloperDevice {
                _device_id: device_id,
                _name: name,
                device_number,
            });
        }
        Ok(result)
    }

    pub async fn add_device(
        &self,
        device_type: DeveloperDeviceType,
        team: &DeveloperTeam,
        device_name: &str,
        udid: &str,
    ) -> Result<DeveloperDevice, Error> {
        let url = dev_url(device_type, "addDevice");
        let mut body = Dictionary::new();
        body.insert("teamId".to_string(), Value::String(team.team_id.clone()));
        body.insert("name".to_string(), Value::String(device_name.to_string()));
        body.insert("deviceNumber".to_string(), Value::String(udid.to_string()));

        let response = self.send_developer_request(&url, Some(body)).await?;

        let device_dict = response
            .get("device")
            .and_then(|v| v.as_dictionary())
            .ok_or(Error::Parse("device".to_string()))?;

        let device_id = device_dict
            .get("deviceId")
            .and_then(|v| v.as_string())
            .ok_or(Error::Parse("deviceId".to_string()))?
            .to_string();
        let name = device_dict
            .get("name")
            .and_then(|v| v.as_string())
            .ok_or(Error::Parse("name".to_string()))?
            .to_string();
        let device_number = device_dict
            .get("deviceNumber")
            .and_then(|v| v.as_string())
            .ok_or(Error::Parse("deviceNumber".to_string()))?
            .to_string();

        Ok(DeveloperDevice {
            _device_id: device_id,
            _name: name,
            device_number,
        })
    }

    pub async fn list_all_development_certs(
        &self,
        device_type: DeveloperDeviceType,
        team: &DeveloperTeam,
    ) -> Result<Vec<DevelopmentCertificate>, Error> {
        let url = dev_url(device_type, "listAllDevelopmentCerts");
        let mut body = Dictionary::new();
        body.insert("teamId".to_string(), Value::String(team.team_id.clone()));

        let response = self.send_developer_request(&url, Some(body)).await?;

        let certs = response
            .get("certificates")
            .and_then(|v| v.as_array())
            .ok_or(Error::Parse("certificates".to_string()))?;

        let mut result = Vec::new();
        for cert in certs {
            let dict = cert
                .as_dictionary()
                .ok_or(Error::Parse("certificate".to_string()))?;
            let name = dict
                .get("name")
                .and_then(|v| v.as_string())
                .ok_or(Error::Parse("name".to_string()))?
                .to_string();
            let certificate_id = dict
                .get("certificateId")
                .and_then(|v| v.as_string())
                .ok_or(Error::Parse("certificateId".to_string()))?
                .to_string();
            let serial_number = dict
                .get("serialNumber")
                .and_then(|v| v.as_string())
                .ok_or(Error::Parse("serialNumber".to_string()))?
                .to_string();
            let machine_name = dict
                .get("machineName")
                .and_then(|v| v.as_string())
                .unwrap_or("")
                .to_string();
            let cert_content = dict
                .get("certContent")
                .and_then(|v| v.as_data())
                .ok_or(Error::Parse("certContent".to_string()))?
                .to_vec();

            result.push(DevelopmentCertificate {
                name,
                certificate_id,
                serial_number,
                machine_name,
                cert_content,
            });
        }
        Ok(result)
    }

    pub async fn revoke_development_cert(
        &self,
        device_type: DeveloperDeviceType,
        team: &DeveloperTeam,
        serial_number: &str,
    ) -> Result<(), Error> {
        let url = dev_url(device_type, "revokeDevelopmentCert");
        let mut body = Dictionary::new();
        body.insert("teamId".to_string(), Value::String(team.team_id.clone()));
        body.insert(
            "serialNumber".to_string(),
            Value::String(serial_number.to_string()),
        );

        self.send_developer_request(&url, Some(body)).await?;
        Ok(())
    }

    pub async fn submit_development_csr(
        &self,
        device_type: DeveloperDeviceType,
        team: &DeveloperTeam,
        csr_content: String,
        machine_name: String,
    ) -> Result<String, Error> {
        let url = dev_url(device_type, "submitDevelopmentCSR");
        let mut body = Dictionary::new();
        body.insert("teamId".to_string(), Value::String(team.team_id.clone()));
        body.insert("csrContent".to_string(), Value::String(csr_content));
        body.insert(
            "machineId".to_string(),
            Value::String(uuid::Uuid::new_v4().to_string().to_uppercase()),
        );
        body.insert("machineName".to_string(), Value::String(machine_name));

        let response = self.send_developer_request(&url, Some(body)).await?;
        let cert_dict = response
            .get("certRequest")
            .and_then(|v| v.as_dictionary())
            .ok_or(Error::Parse("certRequest".to_string()))?;
        let id = cert_dict
            .get("certRequestId")
            .and_then(|v| v.as_string())
            .ok_or(Error::Parse("certRequestId".to_string()))?
            .to_string();

        Ok(id)
    }

    pub async fn list_app_ids(
        &self,
        device_type: DeveloperDeviceType,
        team: &DeveloperTeam,
    ) -> Result<ListAppIdsResponse, Error> {
        let url = dev_url(device_type, "listAppIds");
        let mut body = Dictionary::new();
        body.insert("teamId".to_string(), Value::String(team.team_id.clone()));

        let response = self.send_developer_request(&url, Some(body)).await?;

        let app_ids = response
            .get("appIds")
            .and_then(|v| v.as_array())
            .ok_or(Error::Parse("appIds".to_string()))?;

        let mut result = Vec::new();
        for app_id in app_ids {
            let dict = app_id
                .as_dictionary()
                .ok_or(Error::Parse("appId".to_string()))?;
            let name = dict
                .get("name")
                .and_then(|v| v.as_string())
                .ok_or(Error::Parse("name".to_string()))?
                .to_string();
            let app_id_id = dict
                .get("appIdId")
                .and_then(|v| v.as_string())
                .ok_or(Error::Parse("appIdId".to_string()))?
                .to_string();
            let identifier = dict
                .get("identifier")
                .and_then(|v| v.as_string())
                .ok_or(Error::Parse("identifier".to_string()))?
                .to_string();
            let features = dict
                .get("features")
                .and_then(|v| v.as_dictionary())
                .ok_or(Error::Parse("features".to_string()))?;
            let expiration_date = if dict.contains_key("expirationDate") {
                Some(
                    dict.get("expirationDate")
                        .and_then(|v| v.as_date())
                        .ok_or(Error::Parse("expirationDate".to_string()))?,
                )
            } else {
                None
            };

            result.push(AppId {
                name,
                app_id_id,
                identifier,
                features: features.clone(),
                expiration_date,
            });
        }

        let max_quantity = if response.contains_key("maxQuantity") {
            Some(
                response
                    .get("maxQuantity")
                    .and_then(|v| v.as_unsigned_integer())
                    .ok_or(Error::Parse("maxQuantity".to_string()))?,
            )
        } else {
            None
        };

        let available_quantity = if response.contains_key("availableQuantity") {
            Some(
                response
                    .get("availableQuantity")
                    .and_then(|v| v.as_unsigned_integer())
                    .ok_or(Error::Parse("availableQuantity".to_string()))?,
            )
        } else {
            None
        };

        Ok(ListAppIdsResponse {
            app_ids: result,
            max_quantity,
            available_quantity,
        })
    }

    pub async fn add_app_id(
        &self,
        device_type: DeveloperDeviceType,
        team: &DeveloperTeam,
        name: &str,
        identifier: &str,
    ) -> Result<(), Error> {
        let url = dev_url(device_type, "addAppId");
        let mut body = Dictionary::new();
        body.insert("teamId".to_string(), Value::String(team.team_id.clone()));
        body.insert("name".to_string(), Value::String(name.to_string()));
        body.insert(
            "identifier".to_string(),
            Value::String(identifier.to_string()),
        );

        self.send_developer_request(&url, Some(body)).await?;

        Ok(())
    }

    pub async fn update_app_id(
        &self,
        device_type: DeveloperDeviceType,
        team: &DeveloperTeam,
        app_id: &AppId,
        features: &Dictionary,
    ) -> Result<Dictionary, Error> {
        let url = dev_url(device_type, "updateAppId");
        let mut body = Dictionary::new();
        body.insert(
            "appIdId".to_string(),
            Value::String(app_id.app_id_id.clone()),
        );
        body.insert("teamId".to_string(), Value::String(team.team_id.clone()));

        for (key, value) in features {
            body.insert(key.clone(), value.clone());
        }

        let response = self.send_developer_request(&url, Some(body)).await?;
        let cert_dict = response
            .get("appId")
            .and_then(|v| v.as_dictionary())
            .ok_or(Error::Parse("appId".to_string()))?;
        let feats = cert_dict
            .get("features")
            .and_then(|v| v.as_dictionary())
            .ok_or(Error::Parse("features".to_string()))?;

        Ok(feats.clone())
    }

    pub async fn delete_app_id(
        &self,
        device_type: DeveloperDeviceType,
        team: &DeveloperTeam,
        app_id_id: String,
    ) -> Result<(), Error> {
        let url = dev_url(device_type, "deleteAppId");
        let mut body = Dictionary::new();
        body.insert("teamId".to_string(), Value::String(team.team_id.clone()));
        body.insert("appIdId".to_string(), Value::String(app_id_id.clone()));

        self.send_developer_request(&url, Some(body)).await?;

        Ok(())
    }

    pub async fn list_application_groups(
        &self,
        device_type: DeveloperDeviceType,
        team: &DeveloperTeam,
    ) -> Result<Vec<ApplicationGroup>, Error> {
        let url = dev_url(device_type, "listApplicationGroups");
        let mut body = Dictionary::new();
        body.insert("teamId".to_string(), Value::String(team.team_id.clone()));

        let response = self.send_developer_request(&url, Some(body)).await?;

        let app_groups = response
            .get("applicationGroupList")
            .and_then(|v| v.as_array())
            .ok_or(Error::Parse("applicationGroupList".to_string()))?;

        let mut result = Vec::new();
        for app_group in app_groups {
            let dict = app_group
                .as_dictionary()
                .ok_or(Error::Parse("applicationGroup".to_string()))?;
            let application_group = dict
                .get("applicationGroup")
                .and_then(|v| v.as_string())
                .ok_or(Error::Parse("applicationGroup".to_string()))?
                .to_string();
            let name = dict
                .get("name")
                .and_then(|v| v.as_string())
                .ok_or(Error::Parse("name".to_string()))?
                .to_string();
            let identifier = dict
                .get("identifier")
                .and_then(|v| v.as_string())
                .ok_or(Error::Parse("identifier".to_string()))?
                .to_string();

            result.push(ApplicationGroup {
                application_group,
                _name: name,
                identifier,
            });
        }

        Ok(result)
    }

    pub async fn add_application_group(
        &self,
        device_type: DeveloperDeviceType,
        team: &DeveloperTeam,
        group_identifier: &str,
        name: &str,
    ) -> Result<ApplicationGroup, Error> {
        let url = dev_url(device_type, "addApplicationGroup");
        let mut body = Dictionary::new();
        body.insert("teamId".to_string(), Value::String(team.team_id.clone()));
        body.insert("name".to_string(), Value::String(name.to_string()));
        body.insert(
            "identifier".to_string(),
            Value::String(group_identifier.to_string()),
        );

        let response = self.send_developer_request(&url, Some(body)).await?;
        let app_group_dict = response
            .get("applicationGroup")
            .and_then(|v| v.as_dictionary())
            .ok_or(Error::Parse("applicationGroup".to_string()))?;
        let application_group = app_group_dict
            .get("applicationGroup")
            .and_then(|v| v.as_string())
            .ok_or(Error::Parse("applicationGroup".to_string()))?
            .to_string();
        let name = app_group_dict
            .get("name")
            .and_then(|v| v.as_string())
            .ok_or(Error::Parse("name".to_string()))?
            .to_string();
        let identifier = app_group_dict
            .get("identifier")
            .and_then(|v| v.as_string())
            .ok_or(Error::Parse("identifier".to_string()))?
            .to_string();

        Ok(ApplicationGroup {
            application_group,
            _name: name,
            identifier,
        })
    }

    pub async fn assign_application_group_to_app_id(
        &self,
        device_type: DeveloperDeviceType,
        team: &DeveloperTeam,
        app_id: &AppId,
        app_group: &ApplicationGroup,
    ) -> Result<(), Error> {
        let url = dev_url(device_type, "assignApplicationGroupToAppId");
        let mut body = Dictionary::new();
        body.insert("teamId".to_string(), Value::String(team.team_id.clone()));
        body.insert(
            "appIdId".to_string(),
            Value::String(app_id.app_id_id.clone()),
        );
        body.insert(
            "applicationGroups".to_string(),
            Value::String(app_group.application_group.clone()),
        );

        self.send_developer_request(&url, Some(body)).await?;

        Ok(())
    }

    pub async fn download_team_provisioning_profile(
        &self,
        device_type: DeveloperDeviceType,
        team: &DeveloperTeam,
        app_id: &AppId,
    ) -> Result<ProvisioningProfile, Error> {
        let url = dev_url(device_type, "downloadTeamProvisioningProfile");
        let mut body = Dictionary::new();
        body.insert("teamId".to_string(), Value::String(team.team_id.clone()));
        body.insert(
            "appIdId".to_string(),
            Value::String(app_id.app_id_id.clone()),
        );

        let response = self.send_developer_request(&url, Some(body)).await?;

        let profile = response
            .get("provisioningProfile")
            .and_then(|v| v.as_dictionary())
            .ok_or(Error::Parse("provisioningProfile".to_string()))?;
        let name = profile
            .get("name")
            .and_then(|v| v.as_string())
            .ok_or(Error::Parse("name".to_string()))?
            .to_string();
        let provisioning_profile_id = profile
            .get("provisioningProfileId")
            .and_then(|v| v.as_string())
            .ok_or(Error::Parse("provisioningProfileId".to_string()))?
            .to_string();
        let encoded_profile = profile
            .get("encodedProfile")
            .and_then(|v| v.as_data())
            .ok_or(Error::Parse("encodedProfile".to_string()))?
            .to_vec();

        Ok(ProvisioningProfile {
            _name: name,
            _provisioning_profile_id: provisioning_profile_id,
            encoded_profile,
        })
    }
}

#[derive(Debug, Clone)]
pub enum DeveloperDeviceType {
    Any,
    Ios,
    Tvos,
    Watchos,
}

impl DeveloperDeviceType {
    pub fn url_segment(&self) -> &'static str {
        match self {
            DeveloperDeviceType::Any => "",
            DeveloperDeviceType::Ios => "ios/",
            DeveloperDeviceType::Tvos => "tvos/",
            DeveloperDeviceType::Watchos => "watchos/",
        }
    }
}

fn dev_url(device_type: DeveloperDeviceType, endpoint: &str) -> String {
    format!(
        "https://developerservices2.apple.com/services/QH65B2/{}{}.action?clientId=XABBG36SBA",
        device_type.url_segment(),
        endpoint
    )
}

#[derive(Debug, Clone)]
pub struct DeveloperDevice {
    pub _device_id: String,
    pub _name: String,
    pub device_number: String,
}

#[derive(Debug, Clone)]
pub struct DeveloperTeam {
    pub _name: String,
    pub team_id: String,
}

#[derive(Debug, Clone)]
pub struct DevelopmentCertificate {
    pub name: String,
    pub certificate_id: String,
    pub serial_number: String,
    pub machine_name: String,
    pub cert_content: Vec<u8>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AppId {
    pub app_id_id: String,
    pub identifier: String,
    pub name: String,
    pub features: Dictionary,
    pub expiration_date: Option<Date>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ListAppIdsResponse {
    pub app_ids: Vec<AppId>,
    pub max_quantity: Option<u64>,
    pub available_quantity: Option<u64>,
}

#[derive(Debug, Clone)]
pub struct ApplicationGroup {
    pub application_group: String,
    pub _name: String,
    pub identifier: String,
}

#[derive(Debug, Clone)]
pub struct ProvisioningProfile {
    pub _provisioning_profile_id: String,
    pub _name: String,
    pub encoded_profile: Vec<u8>,
}
