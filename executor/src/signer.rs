//! EIP-712 PhantomAgent signing for Hyperliquid L1 actions.
//!
//! Hyperliquid uses an EIP-712 envelope around an `Agent` struct:
//!     domain   = { name: "Exchange", version: "1", chainId: 1337,
//!                  verifyingContract: 0x0000…0000 }
//!     primaryType = "Agent"
//!     types.Agent = [{ name: "source", type: "string" },
//!                    { name: "connectionId", type: "bytes32" }]
//!     message  = { source: "a"|"b", connectionId: action_hash }
//!
//! The digest is keccak256(0x1901 || domainSeparator || structHash) and
//! is signed with secp256k1 / ECDSA. The `v` byte is the recovery id
//! plus 27 (Ethereum convention).

use anyhow::Result;
use secp256k1::{ecdsa::RecoverableSignature, Message, Secp256k1, SecretKey};
use serde::{Deserialize, Serialize};
use tiny_keccak::{Hasher, Keccak};

use crate::config::Network;

const HL_CHAIN_ID: u64 = 1337;
const VERIFYING_CONTRACT: [u8; 20] = [0u8; 20];

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Signature {
    pub r: String, // 0x-prefixed
    pub s: String, // 0x-prefixed
    pub v: u8,
}

fn keccak(data: &[u8]) -> [u8; 32] {
    let mut h = Keccak::v256();
    h.update(data);
    let mut out = [0u8; 32];
    h.finalize(&mut out);
    out
}

/// keccak256 of the EIP-712 type string for the Agent struct.
fn agent_type_hash() -> [u8; 32] {
    keccak(b"Agent(string source,bytes32 connectionId)")
}

/// keccak256 of the EIP-712Domain type string.
fn eip712_domain_type_hash() -> [u8; 32] {
    keccak(b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
}

fn domain_separator() -> [u8; 32] {
    let mut buf = Vec::with_capacity(32 * 5);
    buf.extend_from_slice(&eip712_domain_type_hash());
    buf.extend_from_slice(&keccak(b"Exchange"));
    buf.extend_from_slice(&keccak(b"1"));
    let mut chain = [0u8; 32];
    chain[24..].copy_from_slice(&HL_CHAIN_ID.to_be_bytes());
    buf.extend_from_slice(&chain);
    let mut addr = [0u8; 32];
    addr[12..].copy_from_slice(&VERIFYING_CONTRACT);
    buf.extend_from_slice(&addr);
    keccak(&buf)
}

fn struct_hash(network: Network, connection_id: &[u8; 32]) -> [u8; 32] {
    let mut buf = Vec::with_capacity(32 * 3);
    buf.extend_from_slice(&agent_type_hash());
    buf.extend_from_slice(&keccak(network.source().as_bytes()));
    buf.extend_from_slice(connection_id);
    keccak(&buf)
}

pub fn digest(network: Network, action_hash: &[u8; 32]) -> [u8; 32] {
    let mut buf = Vec::with_capacity(2 + 32 + 32);
    buf.extend_from_slice(&[0x19, 0x01]);
    buf.extend_from_slice(&domain_separator());
    buf.extend_from_slice(&struct_hash(network, action_hash));
    keccak(&buf)
}

pub fn sign(network: Network, action_hash: &[u8; 32], key: &SecretKey) -> Result<Signature> {
    let secp = Secp256k1::new();
    let digest_bytes = digest(network, action_hash);
    let msg = Message::from_digest(digest_bytes);
    let sig: RecoverableSignature = secp.sign_ecdsa_recoverable(&msg, key);
    let (rec_id, compact) = sig.serialize_compact();
    let r = &compact[..32];
    let s = &compact[32..];
    Ok(Signature {
        r: format!("0x{}", hex::encode(r)),
        s: format!("0x{}", hex::encode(s)),
        v: (rec_id.to_i32() as u8) + 27,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn k() -> SecretKey {
        // Deterministic test key (NOT a real wallet). All ones.
        SecretKey::from_slice(&[1u8; 32]).unwrap()
    }

    #[test]
    fn domain_separator_is_constant() {
        // Spot check: domain_separator must not depend on network or message.
        let d1 = domain_separator();
        let d2 = domain_separator();
        assert_eq!(d1, d2);
    }

    #[test]
    fn signature_is_deterministic_per_input() {
        let action_hash = [42u8; 32];
        let s1 = sign(Network::Mainnet, &action_hash, &k()).unwrap();
        let s2 = sign(Network::Mainnet, &action_hash, &k()).unwrap();
        assert_eq!(s1.r, s2.r);
        assert_eq!(s1.s, s2.s);
        assert_eq!(s1.v, s2.v);
    }

    #[test]
    fn mainnet_and_testnet_produce_different_signatures() {
        let action_hash = [7u8; 32];
        let main = sign(Network::Mainnet, &action_hash, &k()).unwrap();
        let test = sign(Network::Testnet, &action_hash, &k()).unwrap();
        assert_ne!(main.r, test.r);
    }

    #[test]
    fn signature_format_is_0x_prefixed_32_bytes() {
        let sig = sign(Network::Mainnet, &[1u8; 32], &k()).unwrap();
        assert!(sig.r.starts_with("0x") && sig.r.len() == 66);
        assert!(sig.s.starts_with("0x") && sig.s.len() == 66);
        assert!(sig.v == 27 || sig.v == 28);
    }
}
