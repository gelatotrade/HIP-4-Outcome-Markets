//! In-memory state of the execution book.
//! Single-writer (the executor task), many-reader (control HTTP).

use std::collections::HashMap;

use parking_lot::RwLock;
use serde::Serialize;

#[derive(Debug, Clone, Default, Serialize)]
pub struct LegState {
    pub asset: u32,
    pub coin: String,
    pub size: f64,
    pub avg_px: f64,
    pub direction: i8, // +1 long, -1 short
}

#[derive(Debug, Default)]
pub struct Book {
    legs: RwLock<HashMap<u32, LegState>>,
    perp_btc: RwLock<f64>,
    last_error: RwLock<Option<String>>,
    submitted: RwLock<u64>,
    rejected: RwLock<u64>,
}

impl Book {
    pub fn upsert_leg(&self, leg: LegState) {
        self.legs.write().insert(leg.asset, leg);
    }

    pub fn close_leg(&self, asset: u32) {
        self.legs.write().remove(&asset);
    }

    pub fn set_perp_btc(&self, p: f64) {
        *self.perp_btc.write() = p;
    }

    pub fn add_perp_btc(&self, delta: f64) {
        *self.perp_btc.write() += delta;
    }

    pub fn record_submitted(&self) {
        *self.submitted.write() += 1;
    }

    pub fn record_rejected(&self, why: impl Into<String>) {
        *self.rejected.write() += 1;
        *self.last_error.write() = Some(why.into());
    }

    pub fn snapshot(&self) -> BookSnapshot {
        let legs = self.legs.read().clone();
        let gross_notional: f64 = legs.values().map(|l| l.size.abs()).sum();
        BookSnapshot {
            n_legs: legs.len(),
            perp_btc: *self.perp_btc.read(),
            gross_notional,
            submitted: *self.submitted.read(),
            rejected: *self.rejected.read(),
            last_error: self.last_error.read().clone(),
            legs: legs.into_values().collect(),
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct BookSnapshot {
    pub n_legs: usize,
    pub perp_btc: f64,
    pub gross_notional: f64,
    pub submitted: u64,
    pub rejected: u64,
    pub last_error: Option<String>,
    pub legs: Vec<LegState>,
}
