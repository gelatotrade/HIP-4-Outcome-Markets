//! Pre-submission risk gates. The executor refuses to sign anything that
//! breaches these caps, no matter what the upstream Python signal said.

use thiserror::Error;

use crate::config::Config;
use crate::state::BookSnapshot;

#[derive(Debug, Error)]
pub enum RiskError {
    #[error("max open legs ({max}) would be exceeded ({current} open + 1)")]
    MaxLegs { current: usize, max: usize },
    #[error("max gross notional ${max:.0} would be exceeded (current ${current:.0} + ${add:.0})")]
    MaxNotional { current: f64, add: f64, max: f64 },
    #[error("max perp |BTC| {max:.4} would be exceeded (would be {would_be:+.4})")]
    MaxPerp { would_be: f64, max: f64 },
    #[error("per-leg notional ${requested:.0} exceeds cap ${max:.0}")]
    LegNotional { requested: f64, max: f64 },
    #[error("kill switch engaged — refusing all new orders")]
    Killed,
}

#[derive(Debug, Clone, Copy)]
pub struct ProposedLeg {
    pub notional_usd: f64,
    pub perp_delta_btc: f64,
}

pub fn check(
    cfg: &Config,
    book: &BookSnapshot,
    killed: bool,
    leg: ProposedLeg,
) -> Result<(), RiskError> {
    if killed {
        return Err(RiskError::Killed);
    }
    if leg.notional_usd > cfg.per_leg_notional_usd {
        return Err(RiskError::LegNotional {
            requested: leg.notional_usd,
            max: cfg.per_leg_notional_usd,
        });
    }
    if book.n_legs + 1 > cfg.max_open_legs {
        return Err(RiskError::MaxLegs {
            current: book.n_legs,
            max: cfg.max_open_legs,
        });
    }
    if book.gross_notional + leg.notional_usd > cfg.max_gross_notional_usd {
        return Err(RiskError::MaxNotional {
            current: book.gross_notional,
            add: leg.notional_usd,
            max: cfg.max_gross_notional_usd,
        });
    }
    let would_be = book.perp_btc + leg.perp_delta_btc;
    if would_be.abs() > cfg.max_perp_btc {
        return Err(RiskError::MaxPerp {
            would_be,
            max: cfg.max_perp_btc,
        });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Network;
    use secp256k1::SecretKey;

    fn cfg() -> Config {
        Config {
            network: Network::Mainnet,
            user_address: [0u8; 20],
            api_key: SecretKey::from_slice(&[1u8; 32]).unwrap(),
            vault_address: None,
            api_base: "https://api.hyperliquid.xyz".into(),
            signal_socket: "/tmp/x".into(),
            control_host: "127.0.0.1".into(),
            control_port: 8765,
            state_dir: "/tmp/hip4-test".into(),
            reconcile_interval_secs: 5,
            max_open_legs: 5,
            max_gross_notional_usd: 50_000.0,
            max_perp_btc: 2.0,
            per_leg_notional_usd: 10_000.0,
        }
    }

    fn book(n: usize, gross: f64, perp: f64) -> BookSnapshot {
        BookSnapshot {
            n_legs: n,
            perp_btc: perp,
            gross_notional: gross,
            submitted: 0,
            rejected: 0,
            last_error: None,
            legs: vec![],
        }
    }

    #[test]
    fn allows_within_caps() {
        let leg = ProposedLeg {
            notional_usd: 5_000.0,
            perp_delta_btc: 0.5,
        };
        assert!(check(&cfg(), &book(0, 0.0, 0.0), false, leg).is_ok());
    }

    #[test]
    fn rejects_when_killed() {
        let leg = ProposedLeg {
            notional_usd: 1.0,
            perp_delta_btc: 0.0,
        };
        assert!(matches!(
            check(&cfg(), &book(0, 0.0, 0.0), true, leg),
            Err(RiskError::Killed)
        ));
    }

    #[test]
    fn rejects_oversize_leg() {
        let leg = ProposedLeg {
            notional_usd: 20_000.0,
            perp_delta_btc: 0.0,
        };
        assert!(matches!(
            check(&cfg(), &book(0, 0.0, 0.0), false, leg),
            Err(RiskError::LegNotional { .. })
        ));
    }

    #[test]
    fn rejects_when_max_legs_exceeded() {
        let leg = ProposedLeg {
            notional_usd: 1_000.0,
            perp_delta_btc: 0.0,
        };
        assert!(matches!(
            check(&cfg(), &book(5, 0.0, 0.0), false, leg),
            Err(RiskError::MaxLegs { .. })
        ));
    }

    #[test]
    fn rejects_when_gross_notional_exceeded() {
        let leg = ProposedLeg {
            notional_usd: 9_000.0,
            perp_delta_btc: 0.0,
        };
        assert!(matches!(
            check(&cfg(), &book(2, 45_000.0, 0.0), false, leg),
            Err(RiskError::MaxNotional { .. })
        ));
    }

    #[test]
    fn rejects_when_perp_cap_exceeded() {
        let leg = ProposedLeg {
            notional_usd: 1_000.0,
            perp_delta_btc: 1.6,
        };
        assert!(matches!(
            check(&cfg(), &book(0, 0.0, 0.5), false, leg),
            Err(RiskError::MaxPerp { .. })
        ));
    }
}
