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
    /// The raw response body — kept so callers can pass it to
    /// `extract_oids` and similar parsers without re-fetching.
    #[serde(skip)]
    pub raw: serde_json::Value,
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
            vault_address: self
                .cfg
                .vault_address
                .as_ref()
                .map(|a| format!("0x{}", hex::encode(a))),
        };
        let url = format!("{}/exchange", self.cfg.api_base);
        let resp = self.http.post(&url).json(&req).send().await?;
        let status = resp.status();
        let body: serde_json::Value = resp
            .json()
            .await
            .map_err(|e| anyhow!("decode body ({}): {e}", status))?;
        if !status.is_success() {
            return Err(anyhow!("exchange http {}: {body}", status));
        }
        let mut parsed: ExchangeResponse = serde_json::from_value(body.clone())
            .map_err(|e| anyhow!("decode ExchangeResponse: {e} (body={body})"))?;
        parsed.raw = body.clone();
        if parsed.status != "ok" {
            return Err(anyhow!("exchange status='{}' body={body}", parsed.status));
        }
        Ok(parsed)
    }

    pub async fn info<T: for<'de> serde::Deserialize<'de>>(
        &self,
        payload: serde_json::Value,
    ) -> Result<T> {
        let url = format!("{}/info", self.cfg.api_base);
        let resp = self.http.post(&url).json(&payload).send().await?;
        Ok(resp.json::<T>().await?)
    }
}

/// Extract the order ids assigned to each leg of an order action.
/// Hyperliquid responses look like:
///   {"status":"ok","response":{"type":"order","data":{"statuses":[
///       {"resting":{"oid":12345}},
///       {"filled":{"totalSz":"0.1","avgPx":"30000","oid":12346}},
///       {"error":"price out of bounds"}]}}}
pub fn extract_oids(resp: &serde_json::Value) -> Vec<Option<u64>> {
    resp.get("response")
        .and_then(|r| r.get("data"))
        .and_then(|d| d.get("statuses"))
        .and_then(|s| s.as_array())
        .map(|arr| {
            arr.iter()
                .map(|st| {
                    st.get("resting")
                        .and_then(|r| r.get("oid"))
                        .and_then(|v| v.as_u64())
                        .or_else(|| {
                            st.get("filled")
                                .and_then(|f| f.get("oid"))
                                .and_then(|v| v.as_u64())
                        })
                })
                .collect()
        })
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extract_oids_handles_resting_and_filled() {
        let body = serde_json::json!({
            "status": "ok",
            "response": {"type": "order", "data": {"statuses": [
                {"resting": {"oid": 100}},
                {"filled": {"totalSz": "0.1", "avgPx": "30000", "oid": 101}},
                {"error": "tick size"}
            ]}}
        });
        let oids = extract_oids(&body);
        assert_eq!(oids, vec![Some(100), Some(101), None]);
    }

    #[test]
    fn extract_oids_returns_empty_on_unexpected_shape() {
        assert!(extract_oids(&serde_json::json!({})).is_empty());
        assert!(extract_oids(&serde_json::json!({"status":"err"})).is_empty());
    }
}
