//! HTTP client for Hyperliquid `/info` and `/exchange` endpoints.
//! Posts signed `Action`s and surfaces the typed responses.

use anyhow::{anyhow, Result};
use serde::{Deserialize, Serialize};

use crate::actions::{action_hash, Action};
use crate::config::Config;
use crate::signer::{sign, Signature};

#[derive(Debug, Clone, Serialize)]
pub struct ExchangeRequest<'a> {
    pub action: &'a Action,
    pub nonce: u64,
    pub signature: Signature,
    #[serde(rename = "vaultAddress", skip_serializing_if = "Option::is_none")]
    pub vault_address: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ExchangeResponse {
    pub status: String,
    #[serde(default)]
    pub response: serde_json::Value,
}

pub struct HlClient {
    cfg: Config,
    http: reqwest::Client,
}

impl HlClient {
    pub fn new(cfg: Config) -> Result<Self> {
        let http = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(10))
            .pool_max_idle_per_host(8)
            .build()?;
        Ok(Self { cfg, http })
    }

    pub fn cfg(&self) -> &Config {
        &self.cfg
    }

    pub async fn submit(&self, action: Action, nonce: u64) -> Result<ExchangeResponse> {
        let vault_ref = self.cfg.vault_address.as_ref();
        let hash = action_hash(&action, nonce, vault_ref);
        let signature = sign(self.cfg.network, &hash, &self.cfg.api_key)?;
        let req = ExchangeRequest {
            action: &action,
            nonce,
            signature,
            vault_address: self.cfg.vault_address.as_ref().map(|a| format!("0x{}", hex::encode(a))),
        };
        let url = format!("{}/exchange", self.cfg.api_base);
        let resp = self.http.post(&url).json(&req).send().await?;
        let status = resp.status();
        let body: serde_json::Value = resp.json().await
            .map_err(|e| anyhow!("decode body ({}): {e}", status))?;
        if !status.is_success() {
            return Err(anyhow!("exchange http {}: {body}", status));
        }
        let parsed: ExchangeResponse = serde_json::from_value(body.clone())
            .map_err(|e| anyhow!("decode ExchangeResponse: {e} (body={body})"))?;
        if parsed.status != "ok" {
            return Err(anyhow!("exchange status='{}' body={body}", parsed.status));
        }
        Ok(parsed)
    }

    pub async fn info<T: for<'de> serde::Deserialize<'de>>(&self, payload: serde_json::Value)
        -> Result<T>
    {
        let url = format!("{}/info", self.cfg.api_base);
        let resp = self.http.post(&url).json(&payload).send().await?;
        Ok(resp.json::<T>().await?)
    }
}

/// Monotonic millisecond nonce. Hyperliquid requires nonce > previous;
/// we keep it across the process via an atomic counter seeded from
/// system time.
pub fn next_nonce() -> u64 {
    use std::sync::atomic::{AtomicU64, Ordering};
    static LAST: AtomicU64 = AtomicU64::new(0);
    let now_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0);
    let prev = LAST.load(Ordering::SeqCst);
    let next = now_ms.max(prev + 1);
    LAST.store(next, Ordering::SeqCst);
    next
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn next_nonce_is_monotonic() {
        let mut last = 0u64;
        for _ in 0..1000 {
            let n = next_nonce();
            assert!(n > last, "{} not > {}", n, last);
            last = n;
        }
    }
}
