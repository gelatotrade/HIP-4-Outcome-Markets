//! HIP-4 Execution Daemon — public library surface.
//!
//! Modules:
//!   * `signer`   — EIP-712 PhantomAgent signing for L1 actions
//!   * `actions`  — typed actions + msgpack action_hash
//!   * `client`   — HTTP client for /info and /exchange
//!   * `risk`     — risk gates enforced before any signed submission
//!   * `ipc`      — Unix-socket NDJSON signal stream + HTTP control plane
//!   * `state`    — in-memory state of open orders / positions
//!   * `config`   — env-var configuration

pub mod actions;
pub mod client;
pub mod config;
pub mod ipc;
pub mod nonce;
pub mod reconcile;
pub mod risk;
pub mod signer;
pub mod state;
