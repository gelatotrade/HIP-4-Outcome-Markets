use std::env;

use anyhow::{anyhow, Result};
use secp256k1::SecretKey;

#[derive(Debug, Clone)]
pub struct Config {
    pub network: Network,
    pub user_address: [u8; 20],
    pub api_key: SecretKey,
    pub vault_address: Option<[u8; 20]>,

    pub api_base: String,

    pub signal_socket: String,
    pub control_host: String,
    pub control_port: u16,

    pub max_open_legs: usize,
    pub max_gross_notional_usd: f64,
    pub max_perp_btc: f64,
    pub per_leg_notional_usd: f64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Network {
    Mainnet,
    Testnet,
}

impl Network {
    /// PhantomAgent `source` identifier (per Hyperliquid signing spec).
    pub fn source(self) -> &'static str {
        match self {
            Network::Mainnet => "a",
            Network::Testnet => "b",
        }
    }

    pub fn api_base(self) -> &'static str {
        match self {
            Network::Mainnet => "https://api.hyperliquid.xyz",
            Network::Testnet => "https://api.hyperliquid-testnet.xyz",
        }
    }
}

fn parse_hex_address(s: &str) -> Result<[u8; 20]> {
    let cleaned = s.trim_start_matches("0x");
    if cleaned.len() != 40 {
        return Err(anyhow!(
            "address must be 20 bytes hex (got {} chars)",
            cleaned.len()
        ));
    }
    let bytes = hex::decode(cleaned)?;
    Ok(bytes.try_into().expect("len checked"))
}

fn parse_hex_key(s: &str) -> Result<SecretKey> {
    let cleaned = s.trim_start_matches("0x");
    if cleaned.len() != 64 {
        return Err(anyhow!("private key must be 32 bytes hex"));
    }
    let bytes = hex::decode(cleaned)?;
    SecretKey::from_slice(&bytes).map_err(|e| anyhow!("invalid secp256k1 key: {e}"))
}

impl Config {
    pub fn from_env() -> Result<Self> {
        let user = env::var("HYPERLIQUID_USER_ADDRESS")
            .map_err(|_| anyhow!("HYPERLIQUID_USER_ADDRESS not set"))?;
        let key = env::var("HYPERLIQUID_API_WALLET_KEY")
            .map_err(|_| anyhow!("HYPERLIQUID_API_WALLET_KEY not set"))?;
        let network = match env::var("HYPERLIQUID_NETWORK")
            .unwrap_or_else(|_| "mainnet".into())
            .as_str()
        {
            "testnet" => Network::Testnet,
            _ => Network::Mainnet,
        };
        let vault = env::var("HYPERLIQUID_VAULT_ADDRESS")
            .ok()
            .filter(|s| !s.trim().is_empty())
            .map(|s| parse_hex_address(&s))
            .transpose()?;

        Ok(Self {
            network,
            user_address: parse_hex_address(&user)?,
            api_key: parse_hex_key(&key)?,
            vault_address: vault,
            api_base: network.api_base().to_string(),
            signal_socket: env::var("EXECUTOR_SIGNAL_SOCKET")
                .unwrap_or_else(|_| "/tmp/hip4-exec.sock".into()),
            control_host: env::var("EXECUTOR_CONTROL_HOST").unwrap_or_else(|_| "127.0.0.1".into()),
            control_port: env::var("EXECUTOR_CONTROL_PORT")
                .ok()
                .and_then(|s| s.parse().ok())
                .unwrap_or(8765),
            max_open_legs: env::var("RISK_MAX_OPEN_LEGS")
                .ok()
                .and_then(|s| s.parse().ok())
                .unwrap_or(20),
            max_gross_notional_usd: env::var("RISK_MAX_GROSS_NOTIONAL_USD")
                .ok()
                .and_then(|s| s.parse().ok())
                .unwrap_or(250_000.0),
            max_perp_btc: env::var("RISK_MAX_PERP_BTC")
                .ok()
                .and_then(|s| s.parse().ok())
                .unwrap_or(10.0),
            per_leg_notional_usd: env::var("RISK_PER_LEG_NOTIONAL_USD")
                .ok()
                .and_then(|s| s.parse().ok())
                .unwrap_or(10_000.0),
        })
    }
}
