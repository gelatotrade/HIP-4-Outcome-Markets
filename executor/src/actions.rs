//! Hyperliquid `/exchange` actions and the `action_hash` used as
//! the EIP-712 `connectionId` of a PhantomAgent.
//!
//! Hyperliquid's wire format is JSON with a strict key order; the
//! `action_hash` is computed over the *msgpack* serialisation of the
//! action with that same key order, followed by an 8-byte big-endian
//! nonce and the vault byte. We mirror their layout exactly so signed
//! requests are accepted.

use serde::{Deserialize, Serialize};
use tiny_keccak::{Hasher, Keccak};

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Tif {
    Alo,
    Ioc,
    Gtc,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct LimitOrderType {
    pub tif: Tif,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(untagged)]
pub enum OrderType {
    Limit { limit: LimitOrderType },
}

/// One concrete order. `a` = asset id, `b` = is_buy, `p` = price string,
/// `s` = size string, `r` = reduce_only, `t` = order type.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct OrderRequest {
    pub a: u32,
    pub b: bool,
    pub p: String,
    pub s: String,
    pub r: bool,
    pub t: OrderType,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "camelCase")]
pub enum Action {
    #[serde(rename = "order")]
    Order {
        orders: Vec<OrderRequest>,
        grouping: String,
    },
    #[serde(rename = "cancel")]
    Cancel { cancels: Vec<CancelRequest> },
    #[serde(rename = "cancelByCloid")]
    CancelByCloid { cancels: Vec<CancelByCloidRequest> },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CancelRequest {
    pub a: u32,
    pub o: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CancelByCloidRequest {
    pub asset: u32,
    pub cloid: String,
}

/// Bytes appended for the vault component of `action_hash`.
/// `0x00` if no vault, `0x01 || vault_address` if vault.
fn vault_bytes(vault: Option<&[u8; 20]>) -> Vec<u8> {
    match vault {
        None => vec![0u8],
        Some(addr) => {
            let mut v = Vec::with_capacity(21);
            v.push(1u8);
            v.extend_from_slice(addr);
            v
        }
    }
}

/// `action_hash` per Hyperliquid SDK:
///     keccak256(msgpack(action) || nonce_be_u64 || vault_bytes)
pub fn action_hash(action: &Action, nonce: u64, vault: Option<&[u8; 20]>) -> [u8; 32] {
    // rmp-serde's `to_vec_named` uses the field names from `serde(rename = ...)`,
    // matching Hyperliquid's expected packing of the action.
    let mut data = rmp_serde::to_vec_named(action).expect("serialise action");
    data.extend_from_slice(&nonce.to_be_bytes());
    data.extend_from_slice(&vault_bytes(vault));

    let mut hasher = Keccak::v256();
    hasher.update(&data);
    let mut out = [0u8; 32];
    hasher.finalize(&mut out);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn order_action_serialises_with_expected_keys() {
        let order = OrderRequest {
            a: 0,
            b: true,
            p: "30000".into(),
            s: "0.1".into(),
            r: false,
            t: OrderType::Limit {
                limit: LimitOrderType { tif: Tif::Gtc },
            },
        };
        let action = Action::Order {
            orders: vec![order],
            grouping: "na".into(),
        };
        let json = serde_json::to_string(&action).unwrap();
        // Order matters for the wire format expected by /exchange
        assert!(json.contains("\"type\":\"order\""));
        assert!(json.contains("\"grouping\":\"na\""));
        assert!(json.contains("\"a\":0"));
        assert!(json.contains("\"tif\":\"gtc\""));
    }

    #[test]
    fn action_hash_changes_with_nonce_and_vault() {
        let action = Action::Cancel {
            cancels: vec![CancelRequest { a: 0, o: 42 }],
        };
        let h1 = action_hash(&action, 1_700_000_000_000, None);
        let h2 = action_hash(&action, 1_700_000_000_001, None);
        let h3 = action_hash(&action, 1_700_000_000_000, Some(&[0xAB; 20]));
        assert_ne!(h1, h2, "different nonce → different hash");
        assert_ne!(h1, h3, "vault byte appended → different hash");
        assert_ne!(h2, h3);
    }

    #[test]
    fn action_hash_is_deterministic() {
        let action = Action::Order {
            orders: vec![OrderRequest {
                a: 5,
                b: false,
                p: "0.42".into(),
                s: "100".into(),
                r: false,
                t: OrderType::Limit {
                    limit: LimitOrderType { tif: Tif::Ioc },
                },
            }],
            grouping: "na".into(),
        };
        let h1 = action_hash(&action, 9_999, None);
        let h2 = action_hash(&action, 9_999, None);
        assert_eq!(h1, h2);
    }
}
