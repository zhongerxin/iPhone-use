use crate::{anisette::AnisetteData, Error};
use aes::{
    cipher::{block_padding::Pkcs7, consts::U16},
    Aes256,
};
use aes_gcm::{aead::KeyInit, AeadInPlace, AesGcm, Nonce};
use base64::{engine::general_purpose, Engine};
use cbc::cipher::{BlockDecryptMut, KeyIvInit};
use hmac::Mac;
use omnisette::AnisetteConfiguration;
use reqwest::{
    header::{HeaderMap, HeaderName, HeaderValue},
    Certificate, Client, ClientBuilder, Response,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use srp::{
    client::{SrpClient, SrpClientVerifier},
    groups::G_2048,
};
use std::str::FromStr;
use tokio::sync::Mutex;

const GSA_ENDPOINT: &str = "https://gsa.apple.com/grandslam/GsService2";
const APPLE_ROOT: &[u8] = include_bytes!("./apple_root.der");

#[derive(Debug, Serialize, Deserialize)]
pub struct InitRequestBody {
    #[serde(rename = "A2k")]
    a_pub: plist::Value,
    cpd: plist::Dictionary,
    #[serde(rename = "o")]
    operation: String,
    ps: Vec<String>,
    #[serde(rename = "u")]
    username: String,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct RequestHeader {
    #[serde(rename = "Version")]
    version: String,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct InitRequest {
    #[serde(rename = "Header")]
    header: RequestHeader,
    #[serde(rename = "Request")]
    request: InitRequestBody,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct ChallengeRequestBody {
    #[serde(rename = "M1")]
    m: plist::Value,
    cpd: plist::Dictionary,
    c: String,
    #[serde(rename = "o")]
    operation: String,
    #[serde(rename = "u")]
    username: String,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct ChallengeRequest {
    #[serde(rename = "Header")]
    header: RequestHeader,
    #[serde(rename = "Request")]
    request: ChallengeRequestBody,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct AuthTokenRequestBody {
    app: Vec<String>,
    c: plist::Value,
    cpd: plist::Dictionary,
    #[serde(rename = "o")]
    operation: String,
    t: String,
    u: String,
    checksum: plist::Value,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct AuthTokenRequest {
    #[serde(rename = "Header")]
    header: RequestHeader,
    #[serde(rename = "Request")]
    request: AuthTokenRequestBody,
}

pub struct AppleAccount {
    //TODO: move this to omnisette
    pub anisette: Mutex<AnisetteData>,
    // pub spd:  Option<plist::Dictionary>,
    //mutable spd
    pub spd: Option<plist::Dictionary>,
    pub apple_id: String,
    client: Client,
}

#[derive(Clone, Debug)]
pub struct AppToken {
    pub app_tokens: plist::Dictionary,
    pub auth_token: String,
    pub app: String,
}
//Just make it return a custom enum, with LoggedIn(account: AppleAccount) or Needs2FA(FinishLoginDel: fn(i32) -> TFAResponse)
#[repr(C)]
#[derive(Debug)]
pub enum LoginState {
    LoggedIn,
    // NeedsSMS2FASent(Send2FAToDevices),
    NeedsDevice2FA,
    Needs2FAVerification,
    NeedsSMS2FA,
    NeedsSMS2FAVerification(VerifyBody),
    NeedsExtraStep(String),
    NeedsLogin,
}

#[derive(Serialize, Debug, Clone)]
struct VerifyCode {
    code: String,
}

#[derive(Serialize, Debug, Clone)]
struct PhoneNumber {
    id: u32,
}

#[derive(Serialize, Debug, Clone)]
#[serde(rename_all = "camelCase")]
pub struct VerifyBody {
    phone_number: PhoneNumber,
    mode: String,
    security_code: Option<VerifyCode>,
}

#[repr(C)]
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TrustedPhoneNumber {
    pub number_with_dial_code: String,
    pub last_two_digits: String,
    pub push_mode: String,
    pub id: u32,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AuthenticationExtras {
    pub trusted_phone_numbers: Vec<TrustedPhoneNumber>,
    pub recovery_url: Option<String>,
    pub cant_use_phone_number_url: Option<String>,
    pub dont_have_access_url: Option<String>,
    pub recovery_web_url: Option<String>,
    pub repair_phone_number_url: Option<String>,
    pub repair_phone_number_web_url: Option<String>,
    #[serde(skip)]
    pub new_state: Option<LoginState>,
}

async fn parse_response(
    res: Result<Response, reqwest::Error>,
) -> Result<plist::Dictionary, crate::Error> {
    let response = res?;
    let status = response.status();
    let final_url = response.url().to_string();
    let content_type = response
        .headers()
        .get("content-type")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("<missing>")
        .to_string();
    let location = response
        .headers()
        .get("location")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("<missing>")
        .to_string();
    let body = response.text().await?;
    let body_kind = if body.trim_start().to_ascii_lowercase().starts_with("<html") {
        "html"
    } else {
        "non-plist"
    };
    let res: plist::Dictionary = plist::from_bytes(body.as_bytes()).map_err(|_| {
        crate::Error::AuthSrpWithMessage(
            status.as_u16() as i64,
            format!(
                "Apple response diagnostic: status={}, content-type={}, final-url={}, location={}, body-kind={}",
                status, content_type, final_url, location, body_kind
            ),
        )
    })?;
    let res: plist::Value = res.get("Response").unwrap().to_owned();
    match res {
        plist::Value::Dictionary(dict) => Ok(dict),
        _ => Err(crate::Error::Parse),
    }
}

impl AppleAccount {
    pub async fn new(
        config: AnisetteConfiguration,
        apple_id: String,
    ) -> Result<Self, crate::Error> {
        let anisette = AnisetteData::new(config).await?;
        Self::new_with_anisette(anisette, apple_id)
    }

    pub fn new_with_anisette(
        anisette: AnisetteData,
        apple_id: String,
    ) -> Result<Self, crate::Error> {
        let client = ClientBuilder::new()
            .add_root_certificate(Certificate::from_der(APPLE_ROOT)?)
            // uncomment when debugging w/ charles proxy
            // .danger_accept_invalid_certs(true)
            .http1_title_case_headers()
            .connection_verbose(true)
            .build()?;

        Ok(AppleAccount {
            client,
            anisette: Mutex::new(anisette),
            apple_id,
            spd: None,
        })
    }

    pub async fn login(
        appleid_closure: impl Fn() -> Result<(String, String), String>,
        tfa_closure: impl Fn() -> Result<String, String>,
        config: AnisetteConfiguration,
    ) -> Result<AppleAccount, Error> {
        let anisette = AnisetteData::new(config).await?;
        AppleAccount::login_with_anisette(appleid_closure, tfa_closure, anisette).await
    }

    pub async fn get_anisette(&self) -> AnisetteData {
        let mut locked = self.anisette.lock().await;
        if locked.needs_refresh() {
            *locked = locked.refresh().await.unwrap();
        }
        locked.clone()
    }

    pub async fn get_app_token(&self, app_name: &str) -> Result<AppToken, Error> {
        let spd = self.spd.as_ref().unwrap();
        let dsid = spd.get("adsid").unwrap().as_string().unwrap();
        let auth_token = spd.get("GsIdmsToken").unwrap().as_string().unwrap();

        let valid_anisette = self.get_anisette().await;

        let sk = spd.get("sk").unwrap().as_data().unwrap();
        let c = spd.get("c").unwrap().as_data().unwrap();

        let checksum = Self::create_checksum(&sk.to_vec(), dsid, app_name);

        let mut gsa_headers = HeaderMap::new();
        gsa_headers.insert(
            "Content-Type",
            HeaderValue::from_str("text/x-xml-plist").unwrap(),
        );
        gsa_headers.insert("Accept", HeaderValue::from_str("*/*").unwrap());
        gsa_headers.insert(
            "User-Agent",
            HeaderValue::from_str("akd/1.0 CFNetwork/978.0.7 Darwin/18.7.0").unwrap(),
        );
        gsa_headers.insert(
            "X-MMe-Client-Info",
            HeaderValue::from_str(&valid_anisette.get_header("x-mme-client-info")?).unwrap(),
        );

        let header = RequestHeader {
            version: "1.0.1".to_string(),
        };
        let body = AuthTokenRequestBody {
            cpd: valid_anisette.to_plist(true, false, false),
            app: vec![app_name.to_string()],
            c: plist::Value::Data(c.to_vec()),
            operation: "apptokens".to_owned(),
            t: auth_token.to_string(),
            u: dsid.to_string(),
            checksum: plist::Value::Data(checksum),
        };

        let packet = AuthTokenRequest {
            header: header.clone(),
            request: body,
        };

        let mut buffer = Vec::new();
        plist::to_writer_xml(&mut buffer, &packet)?;
        let buffer = String::from_utf8(buffer).unwrap();

        let res = self
            .client
            .post(GSA_ENDPOINT)
            .headers(gsa_headers.clone())
            .body(buffer)
            .send()
            .await;
        let res = parse_response(res).await?;
        let err_check = Self::check_error(&res);
        if err_check.is_err() {
            return Err(err_check.err().unwrap());
        }

        let encrypted_token = res
            .get("et")
            .ok_or(Error::Parse)?
            .as_data()
            .ok_or(Error::Parse)?;

        if encrypted_token.len() < 3 + 16 + 16 {
            return Err(Error::Parse);
        }
        let header = &encrypted_token[0..3];
        if header != b"XYZ" {
            return Err(Error::AuthSrpWithMessage(
                0,
                "Encrypted token is in an unknown format.".to_string(),
            ));
        }
        let iv = &encrypted_token[3..19];
        let ciphertext_and_tag = &encrypted_token[19..];

        if sk.len() != 32 {
            return Err(Error::Parse);
        }
        if iv.len() != 16 {
            return Err(Error::Parse);
        }

        let key = aes_gcm::Key::<AesGcm<Aes256, U16>>::from_slice(sk);
        let cipher = AesGcm::<Aes256, U16>::new(key);
        let nonce = Nonce::<U16>::from_slice(iv);

        let mut buf = ciphertext_and_tag.to_vec();

        cipher
            .decrypt_in_place(nonce, header, &mut buf)
            .map_err(|_| {
                Error::AuthSrpWithMessage(
                    0,
                    "Failed to decrypt app token (AES-256/GCM aes-gcm).".to_string(),
                )
            })?;

        let decrypted_token: plist::Dictionary =
            plist::from_bytes(&buf).map_err(|_| Error::Parse)?;

        let t_val = decrypted_token.get("t").ok_or(Error::Parse)?;
        let app_tokens = t_val.as_dictionary().ok_or(Error::Parse)?;
        let app_token_dict = app_tokens.get(app_name).ok_or(Error::Parse)?;
        let app_token = app_token_dict.as_dictionary().ok_or(Error::Parse)?;
        let token = app_token
            .get("token")
            .and_then(|v| v.as_string())
            .ok_or(Error::Parse)?;

        Ok(AppToken {
            app_tokens: app_tokens.clone(),
            auth_token: token.to_string(),
            app: app_name.to_string(),
        })
    }

    fn create_checksum(session_key: &Vec<u8>, dsid: &str, app_name: &str) -> Vec<u8> {
        <hmac::Hmac<Sha256> as hmac::Mac>::new_from_slice(session_key.as_slice())
            .unwrap()
            .chain_update("apptokens".as_bytes())
            .chain_update(dsid.as_bytes())
            .chain_update(app_name.as_bytes())
            .finalize()
            .into_bytes()
            .to_vec()
    }

    /// # Arguments
    ///
    /// * `appleid_closure` - A closure that takes no arguments and returns a tuple of the Apple ID and password
    /// * `tfa_closure` - A closure that takes no arguments and returns the 2FA code
    /// * `anisette` - AnisetteData
    /// # Examples
    ///
    /// ```
    /// use icloud_auth::AppleAccount;
    /// use omnisette::AnisetteData;
    ///
    /// let anisette = AnisetteData::new();
    /// let account = AppleAccount::login(
    ///   || Ok(("test@waffle.me", "password"))
    ///   || Ok("123123"),
    ///  anisette
    /// );
    /// ```
    /// Note: You would not provide the 2FA code like this, you would have to actually ask input for it.
    //TODO: add login_with_anisette and login, where login autodetcts anisette
    pub async fn login_with_anisette<
        F: Fn() -> Result<(String, String), String>,
        G: Fn() -> Result<String, String>,
    >(
        appleid_closure: F,
        tfa_closure: G,
        anisette: AnisetteData,
    ) -> Result<AppleAccount, Error> {
        let (username, password) = appleid_closure().map_err(|e| {
            Error::AuthSrpWithMessage(0, format!("Failed to get Apple ID credentials: {}", e))
        })?;
        let mut _self = AppleAccount::new_with_anisette(anisette, username.clone())?;

        let mut response = _self.login_email_pass(&username, &password).await?;
        loop {
            match response {
                LoginState::NeedsDevice2FA => response = _self.send_2fa_to_devices().await?,
                LoginState::Needs2FAVerification => {
                    response = _self
                        .verify_2fa(tfa_closure().map_err(|e| {
                            Error::AuthSrpWithMessage(0, format!("Failed to get 2FA code: {}", e))
                        })?)
                        .await?
                }
                LoginState::NeedsSMS2FA => response = _self.send_sms_2fa_to_devices(1).await?,
                LoginState::NeedsSMS2FAVerification(body) => {
                    response = _self
                        .verify_sms_2fa(
                            tfa_closure().map_err(|e| {
                                Error::AuthSrpWithMessage(
                                    0,
                                    format!("Failed to get SMS 2FA code: {}", e),
                                )
                            })?,
                            body,
                        )
                        .await?
                }
                LoginState::NeedsLogin => {
                    response = _self.login_email_pass(&username, &password).await?
                }
                LoginState::LoggedIn => return Ok(_self),
                LoginState::NeedsExtraStep(step) => {
                    if _self.get_pet().is_some() {
                        return Ok(_self);
                    } else {
                        return Err(Error::ExtraStep(step));
                    }
                }
            }
        }
    }

    pub fn get_pet(&self) -> Option<String> {
        Some(
            self.spd.as_ref().unwrap().get("t")?
                .as_dictionary()
                .unwrap()
                .get("com.apple.gs.idms.pet")
                .unwrap()
                .as_dictionary()
                .unwrap()
                .get("token")
                .unwrap()
                .as_string()
                .unwrap()
                .to_string(),
        )
    }

    pub fn get_name(&self) -> (String, String) {
        (
            self.spd
                .as_ref()
                .unwrap()
                .get("fn")
                .unwrap()
                .as_string()
                .unwrap()
                .to_string(),
            self.spd
                .as_ref()
                .unwrap()
                .get("ln")
                .unwrap()
                .as_string()
                .unwrap()
                .to_string(),
        )
    }

    pub async fn login_email_pass(
        &mut self,
        username: &str,
        password: &str,
    ) -> Result<LoginState, Error> {
        let srp_client = SrpClient::<Sha256>::new(&G_2048);
        let a: Vec<u8> = (0..32).map(|_| rand::random::<u8>()).collect();
        let a_pub = srp_client.compute_public_ephemeral(&a);

        let valid_anisette = self.get_anisette().await;

        let mut gsa_headers = HeaderMap::new();
        gsa_headers.insert(
            "Content-Type",
            HeaderValue::from_str("text/x-xml-plist").unwrap(),
        );
        gsa_headers.insert("Accept", HeaderValue::from_str("*/*").unwrap());
        gsa_headers.insert(
            "User-Agent",
            HeaderValue::from_str("akd/1.0 CFNetwork/978.0.7 Darwin/18.7.0").unwrap(),
        );
        gsa_headers.insert(
            "X-MMe-Client-Info",
            HeaderValue::from_str(&valid_anisette.get_header("x-mme-client-info")?).unwrap(),
        );

        let header = RequestHeader {
            version: "1.0.1".to_string(),
        };
        let body = InitRequestBody {
            a_pub: plist::Value::Data(a_pub),
            cpd: valid_anisette.to_plist(true, false, false),
            operation: "init".to_string(),
            ps: vec!["s2k".to_string(), "s2k_fo".to_string()],
            username: username.to_string(),
        };

        let packet = InitRequest {
            header: header.clone(),
            request: body,
        };

        let mut buffer = Vec::new();
        plist::to_writer_xml(&mut buffer, &packet)?;
        let buffer = String::from_utf8(buffer).unwrap();

        // println!("{:?}", gsa_headers.clone());
        // println!("{:?}", buffer);

        let res = self
            .client
            .post(GSA_ENDPOINT)
            .headers(gsa_headers.clone())
            .body(buffer)
            .send()
            .await;

        let res = parse_response(res).await?;
        let err_check = Self::check_error(&res);
        if err_check.is_err() {
            return Err(err_check.err().unwrap());
        }
        // println!("{:?}", res);
        let salt = res.get("s").unwrap().as_data().unwrap();
        let b_pub = res.get("B").unwrap().as_data().unwrap();
        let iters = res.get("i").unwrap().as_signed_integer().unwrap();
        let c = res.get("c").unwrap().as_string().unwrap();

        let hashed_password = Sha256::digest(password.as_bytes());

        let mut password_buf = [0u8; 32];
        pbkdf2::pbkdf2::<hmac::Hmac<Sha256>>(
            &hashed_password,
            salt,
            iters as u32,
            &mut password_buf,
        );

        let verifier: SrpClientVerifier<Sha256> = srp_client
            .process_reply(&a, username.as_bytes(), &password_buf, salt, b_pub)
            .unwrap();

        let m = verifier.proof();

        let body = ChallengeRequestBody {
            m: plist::Value::Data(m.to_vec()),
            c: c.to_string(),
            cpd: valid_anisette.to_plist(true, false, false),
            operation: "complete".to_string(),
            username: username.to_string(),
        };

        let packet = ChallengeRequest {
            header,
            request: body,
        };

        let mut buffer = Vec::new();
        plist::to_writer_xml(&mut buffer, &packet)?;
        let buffer = String::from_utf8(buffer).unwrap();

        let res = self
            .client
            .post(GSA_ENDPOINT)
            .headers(gsa_headers.clone())
            .body(buffer)
            .send()
            .await;

        let res = parse_response(res).await?;
        let err_check = Self::check_error(&res);
        if err_check.is_err() {
            return Err(err_check.err().unwrap());
        }
        // println!("{:?}", res);
        let m2 = res.get("M2").unwrap().as_data().unwrap();
        verifier.verify_server(m2).unwrap();

        let spd = res.get("spd").unwrap().as_data().unwrap();
        let decrypted_spd = Self::decrypt_cbc(&verifier, spd);
        let decoded_spd: plist::Dictionary = plist::from_bytes(&decrypted_spd).unwrap();

        let status = res.get("Status").unwrap().as_dictionary().unwrap();

        self.spd = Some(decoded_spd);

        if let Some(plist::Value::String(s)) = status.get("au") {
            return match s.as_str() {
                "trustedDeviceSecondaryAuth" => Ok(LoginState::NeedsDevice2FA),
                "secondaryAuth" => Ok(LoginState::NeedsSMS2FA),
                _unk => Ok(LoginState::NeedsExtraStep(_unk.to_string())),
            };
        }

        Ok(LoginState::LoggedIn)
    }

    fn create_session_key(usr: &SrpClientVerifier<Sha256>, name: &str) -> Vec<u8> {
        <hmac::Hmac<Sha256> as hmac::Mac>::new_from_slice(usr.key())
            .unwrap()
            .chain_update(name.as_bytes())
            .finalize()
            .into_bytes()
            .to_vec()
    }

    fn decrypt_cbc(usr: &SrpClientVerifier<Sha256>, data: &[u8]) -> Vec<u8> {
        let extra_data_key = Self::create_session_key(usr, "extra data key:");
        let extra_data_iv = Self::create_session_key(usr, "extra data iv:");
        let extra_data_iv = &extra_data_iv[..16];

        cbc::Decryptor::<aes::Aes256>::new_from_slices(&extra_data_key, extra_data_iv)
            .unwrap()
            .decrypt_padded_vec_mut::<Pkcs7>(data)
            .unwrap()
    }

    pub async fn send_2fa_to_devices(&self) -> Result<LoginState, crate::Error> {
        let headers = self.build_2fa_headers(false);

        let res = self
            .client
            .get("https://gsa.apple.com/auth/verify/trusteddevice")
            .headers(headers.await)
            .send()
            .await?;

        if !res.status().is_success() {
            return Err(Error::AuthSrp);
        }

        Ok(LoginState::Needs2FAVerification)
    }

    pub async fn send_sms_2fa_to_devices(&self, phone_id: u32) -> Result<LoginState, crate::Error> {
        let headers = self.build_2fa_headers(true);

        let body = VerifyBody {
            phone_number: PhoneNumber { id: phone_id },
            mode: "sms".to_string(),
            security_code: None,
        };

        let res = self
            .client
            .get("https://gsa.apple.com/auth")
            .headers(headers.await)
            .send()
            .await?;

        if !res.status().is_success() {
            return Err(Error::AuthSrp);
        }

        Ok(LoginState::NeedsSMS2FAVerification(body))
    }

    pub async fn get_auth_extras(&self) -> Result<AuthenticationExtras, Error> {
        let headers = self.build_2fa_headers(true);

        let req = self
            .client
            .get("https://gsa.apple.com/auth")
            .headers(headers.await)
            .header("Accept", "application/json")
            .send()
            .await?;
        let status = req.status().as_u16();
        let mut new_state = req.json::<AuthenticationExtras>().await?;
        if status == 201 {
            new_state.new_state = Some(LoginState::NeedsSMS2FAVerification(VerifyBody {
                phone_number: PhoneNumber {
                    id: new_state.trusted_phone_numbers.first().unwrap().id,
                },
                mode: "sms".to_string(),
                security_code: None,
            }));
        }

        Ok(new_state)
    }

    pub async fn verify_2fa(&self, code: String) -> Result<LoginState, Error> {
        // println!("Verifying 2fa Code {}", code.clone());
        let headers = self.build_2fa_headers(false);
        // println!("Recieved code: {}", code);
        let res = self
            .client
            .get("https://gsa.apple.com/grandslam/GsService2/validate")
            .headers(headers.await)
            .header(
                HeaderName::from_str("security-code").unwrap(),
                HeaderValue::from_str(&code).unwrap(),
            )
            .send()
            .await?;

        let res: plist::Dictionary = plist::from_bytes(res.text().await?.as_bytes())?;

        Self::check_error(&res)?;

        Ok(LoginState::NeedsLogin)
    }

    pub async fn verify_sms_2fa(
        &self,
        code: String,
        mut body: VerifyBody,
    ) -> Result<LoginState, Error> {
        let headers = self.build_2fa_headers(true).await;
        // println!("Recieved code: {}", code);

        body.security_code = Some(VerifyCode { code });

        let res = self
            .client
            .post("https://gsa.apple.com/auth/verify/phone/securitycode")
            .headers(headers)
            .header("accept", "application/json")
            .json(&body)
            .send()
            .await?;

        if res.status() != 200 {
            return Err(Error::Bad2faCode);
        }

        Ok(LoginState::NeedsLogin)
    }

    fn check_error(res: &plist::Dictionary) -> Result<(), Error> {
        let res = match res.get("Status") {
            Some(plist::Value::Dictionary(d)) => d,
            _ => res,
        };

        if res.get("ec").unwrap().as_signed_integer().unwrap() != 0 {
            return Err(Error::AuthSrpWithMessage(
                res.get("ec").unwrap().as_signed_integer().unwrap(),
                res.get("em").unwrap().as_string().unwrap().to_owned(),
            ));
        }

        Ok(())
    }

    pub async fn build_2fa_headers(&self, sms: bool) -> HeaderMap {
        let spd = self.spd.as_ref().unwrap();
        let dsid = spd.get("adsid").unwrap().as_string().unwrap();
        let token = spd.get("GsIdmsToken").unwrap().as_string().unwrap();

        let identity_token = general_purpose::STANDARD.encode(format!("{}:{}", dsid, token));

        let valid_anisette = self.get_anisette().await;

        let mut headers = HeaderMap::new();
        valid_anisette
            .generate_headers(false, true, true)
            .iter()
            .for_each(|(k, v)| {
                headers.append(
                    HeaderName::from_bytes(k.as_bytes()).unwrap(),
                    HeaderValue::from_str(v).unwrap(),
                );
            });

        if !sms {
            headers.insert(
                "Content-Type",
                HeaderValue::from_str("text/x-xml-plist").unwrap(),
            );
            headers.insert("Accept", HeaderValue::from_str("text/x-xml-plist").unwrap());
        }
        headers.insert("User-Agent", HeaderValue::from_str("Xcode").unwrap());
        headers.insert("Accept-Language", HeaderValue::from_str("en-us").unwrap());
        headers.append(
            "X-Apple-Identity-Token",
            HeaderValue::from_str(&identity_token).unwrap(),
        );

        headers.insert(
            "Loc",
            HeaderValue::from_str(&valid_anisette.get_header("x-apple-locale").unwrap()).unwrap(),
        );

        headers
    }

    pub async fn send_request(
        &self,
        url: &str,
        body: Option<plist::Dictionary>,
    ) -> Result<plist::Dictionary, Error> {
        let spd = self.spd.as_ref().unwrap();
        let app_token = self.get_app_token("com.apple.gs.xcode.auth").await?;
        let valid_anisette = self.get_anisette().await;

        let mut headers = HeaderMap::new();
        headers.insert("Content-Type", HeaderValue::from_static("text/x-xml-plist"));
        headers.insert("Accept", HeaderValue::from_static("text/x-xml-plist"));
        headers.insert("Accept-Language", HeaderValue::from_static("en-us"));
        headers.insert("User-Agent", HeaderValue::from_static("Xcode"));
        headers.insert(
            "X-Apple-I-Identity-Id",
            HeaderValue::from_str(spd.get("adsid").unwrap().as_string().unwrap()).unwrap(),
        );
        headers.insert(
            "X-Apple-GS-Token",
            HeaderValue::from_str(&app_token.auth_token).unwrap(),
        );

        for (k, v) in valid_anisette.generate_headers(false, true, true) {
            headers.insert(
                HeaderName::from_bytes(k.as_bytes()).unwrap(),
                HeaderValue::from_str(&v).unwrap(),
            );
        }

        if let Ok(locale) = valid_anisette.get_header("x-apple-locale") {
            headers.insert("X-Apple-Locale", HeaderValue::from_str(&locale).unwrap());
        }

        let response = if let Some(body) = body {
            let mut buf = Vec::new();
            plist::to_writer_xml(&mut buf, &body)?;
            self.client
                .post(url)
                .headers(headers)
                .body(buf)
                .send()
                .await?
        } else {
            self.client.get(url).headers(headers).send().await?
        };

        let response = response.text().await?;

        let response: plist::Dictionary = plist::from_bytes(response.as_bytes())?;
        Ok(response)
    }
}
