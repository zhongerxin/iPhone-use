// This file was made using https://github.com/Dadoum/Sideloader as a reference.

use hex;
use openssl::{
    hash::MessageDigest,
    pkey::{PKey, Private},
    rsa::Rsa,
    x509::{X509, X509Name, X509ReqBuilder},
};
use sha1::{Digest, Sha1};
use std::{
    fs,
    path::{Path, PathBuf},
};

use crate::Error;
use crate::developer_session::{DeveloperDeviceType, DeveloperSession, DeveloperTeam};

#[derive(Debug, Clone)]
pub struct CertificateIdentity {
    pub certificate: Option<X509>,
    pub private_key: PKey<Private>,
    pub key_file: PathBuf,
    pub cert_file: PathBuf,
    pub machine_name: String,
}

impl CertificateIdentity {
    pub async fn new(
        configuration_path: &Path,
        dev_session: &DeveloperSession,
        apple_id: String,
        machine_name: String,
    ) -> Result<Self, Error> {
        let mut hasher = Sha1::new();
        hasher.update(apple_id.as_bytes());
        let hash_string = hex::encode(hasher.finalize()).to_lowercase();
        let key_path = configuration_path.join("keys").join(hash_string);
        fs::create_dir_all(&key_path).map_err(Error::Filesystem)?;

        let key_file = key_path.join("key.pem");
        let cert_file = key_path.join("cert.pem");
        let teams = dev_session.list_teams().await?;
        let team = teams
            .first()
            .ok_or(Error::Certificate("No teams found".to_string()))?;
        let private_key = if key_file.exists() {
            let key_data = fs::read_to_string(&key_file)
                .map_err(|e| Error::Certificate(format!("Failed to read key file: {}", e)))?;
            PKey::private_key_from_pem(key_data.as_bytes())
                .map_err(|e| Error::Certificate(format!("Failed to load private key: {}", e)))?
        } else {
            let rsa = Rsa::generate(2048)
                .map_err(|e| Error::Certificate(format!("Failed to generate RSA key: {}", e)))?;
            let key = PKey::from_rsa(rsa)
                .map_err(|e| Error::Certificate(format!("Failed to create private key: {}", e)))?;
            let pem_data = key
                .private_key_to_pem_pkcs8()
                .map_err(|e| Error::Certificate(format!("Failed to encode private key: {}", e)))?;
            fs::write(&key_file, pem_data).map_err(Error::Filesystem)?;
            key
        };

        let mut cert_identity = CertificateIdentity {
            certificate: None,
            private_key,
            key_file,
            cert_file,
            machine_name,
        };

        if let Ok(cert) = cert_identity
            .find_matching_certificate(dev_session, team)
            .await
        {
            cert_identity.certificate = Some(cert.clone());

            let cert_pem = cert.to_pem().map_err(|e| {
                Error::Certificate(format!("Failed to encode certificate to PEM: {}", e))
            })?;
            fs::write(&cert_identity.cert_file, cert_pem).map_err(Error::Filesystem)?;

            return Ok(cert_identity);
        }

        cert_identity
            .request_new_certificate(dev_session, team)
            .await?;
        Ok(cert_identity)
    }

    async fn find_matching_certificate(
        &self,
        dev_session: &DeveloperSession,
        team: &DeveloperTeam,
    ) -> Result<X509, Error> {
        let certificates = dev_session
            .list_all_development_certs(DeveloperDeviceType::Ios, team)
            .await
            .map_err(|e| Error::Certificate(format!("Failed to list certificates: {:?}", e)))?;

        let our_public_key = self
            .private_key
            .public_key_to_der()
            .map_err(|e| Error::Certificate(format!("Failed to get public key: {}", e)))?;

        for cert in certificates
            .iter()
            .filter(|c| c.machine_name == self.machine_name)
        {
            if let Ok(x509_cert) = X509::from_der(&cert.cert_content)
                && let Ok(cert_public_key) = x509_cert.public_key()
                && let Ok(cert_public_key_der) = cert_public_key.public_key_to_der()
                && cert_public_key_der == our_public_key
            {
                return Ok(x509_cert);
            }
        }
        Err(Error::Certificate(
            "No matching certificate found".to_string(),
        ))
    }

    async fn request_new_certificate(
        &mut self,
        dev_session: &DeveloperSession,
        team: &DeveloperTeam,
    ) -> Result<(), Error> {
        let mut req_builder = X509ReqBuilder::new()
            .map_err(|e| Error::Certificate(format!("Failed to create request builder: {}", e)))?;
        let mut name_builder = X509Name::builder()
            .map_err(|e| Error::Certificate(format!("Failed to create name builder: {}", e)))?;

        name_builder
            .append_entry_by_text("C", "US")
            .map_err(|e| Error::Certificate(format!("Failed to set country: {}", e)))?;
        name_builder
            .append_entry_by_text("ST", "STATE")
            .map_err(|e| Error::Certificate(format!("Failed to set state: {}", e)))?;
        name_builder
            .append_entry_by_text("L", "LOCAL")
            .map_err(|e| Error::Certificate(format!("Failed to set locality: {}", e)))?;
        name_builder
            .append_entry_by_text("O", "ORGNIZATION")
            .map_err(|e| Error::Certificate(format!("Failed to set organization: {}", e)))?;
        name_builder
            .append_entry_by_text("CN", "CN")
            .map_err(|e| Error::Certificate(format!("Failed to set common name: {}", e)))?;

        req_builder
            .set_subject_name(&name_builder.build())
            .map_err(|e| Error::Certificate(format!("Failed to set subject name: {}", e)))?;
        req_builder
            .set_pubkey(&self.private_key)
            .map_err(|e| Error::Certificate(format!("Failed to set public key: {}", e)))?;
        req_builder
            .sign(&self.private_key, MessageDigest::sha256())
            .map_err(|e| Error::Certificate(format!("Failed to sign request: {}", e)))?;

        let csr_pem = req_builder
            .build()
            .to_pem()
            .map_err(|e| Error::Certificate(format!("Failed to encode CSR: {}", e)))?;

        let certificate_id = dev_session
            .submit_development_csr(
                DeveloperDeviceType::Ios,
                team,
                String::from_utf8_lossy(&csr_pem).to_string(),
                self.machine_name.clone(),
            )
            .await
            .map_err(|e| {
                let is_7460 = match &e {
                    Error::DeveloperSession(code, _) => *code == 7460,
                    _ => false,
                };
                if is_7460 {
                    Error::Certificate("You have too many certificates!".to_string())
                } else {
                    Error::Certificate(format!("Failed to submit CSR: {:?}", e))
                }
            })?;

        let certificates = dev_session
            .list_all_development_certs(DeveloperDeviceType::Ios, team)
            .await?;

        let apple_cert = certificates
            .iter()
            .find(|cert| cert.certificate_id == certificate_id)
            .ok_or(Error::Certificate(
                "Certificate not found after submission".to_string(),
            ))?;

        let certificate = X509::from_der(&apple_cert.cert_content)
            .map_err(|e| Error::Certificate(format!("Failed to parse certificate: {}", e)))?;

        // Write certificate to disk
        let cert_pem = certificate.to_pem().map_err(|e| {
            Error::Certificate(format!("Failed to encode certificate to PEM: {}", e))
        })?;
        fs::write(&self.cert_file, cert_pem).map_err(Error::Filesystem)?;

        self.certificate = Some(certificate);

        Ok(())
    }

    pub fn get_certificate_file_path(&self) -> &Path {
        &self.cert_file
    }

    pub fn get_private_key_file_path(&self) -> &Path {
        &self.key_file
    }

    pub fn get_serial_number(&self) -> Result<String, Error> {
        let cert = match &self.certificate {
            Some(c) => c,
            None => {
                return Err(Error::Certificate(
                    "No certificate available to get serial number".to_string(),
                ));
            }
        };
        let num = cert
            .serial_number()
            .to_bn()
            .map_err(|e| {
                Error::Certificate(format!("Failed to convert serial number to bn: {}", e))
            })?
            .to_hex_str()
            .map_err(|e| {
                Error::Certificate(format!(
                    "Failed to convert serial number to hex string: {}",
                    e
                ))
            })?
            .to_string();

        Ok(num.trim_start_matches("0").to_string())
    }
}
