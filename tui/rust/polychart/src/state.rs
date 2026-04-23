//! In-memory app state -- rolling deques, latest snapshot, derived views.
//!
//! Owned by the render thread; mutated via short-lived `std::sync::Mutex`
//! locks from background tasks. Never held across `.await` boundaries.

// Many fields and helpers here are forward-declared for the widget and
// tailer phases that land next. Suppress the dead-code lint until then.
#![allow(dead_code)]

use std::collections::{HashMap, VecDeque};

use crate::http::OrderbookSnapshot;
use crate::state_reader::StateSnapshot;

pub const PRICE_HISTORY_CAP: usize = 300;
pub const PNL_SERIES_CAP: usize = 2000;
pub const PNL_VISIBLE_WINDOW: usize = 300;

#[derive(Debug, Default, Clone)]
pub struct AppState {
    pub snapshot: Option<StateSnapshot>,
    pub last_t_zero: Option<i64>,

    // Rolling per-market deques; reset on t_zero rollover.
    pub btc_series: VecDeque<(f64, f64)>,
    pub mkt_series: VecDeque<(f64, f64)>,
    pub fair_series: VecDeque<(f64, f64)>,

    // Persistent per-strategy cumulative PnL: decimates middle past cap,
    // preserves first and last points. Reset only on events.jsonl truncation.
    pub pnl_series: HashMap<String, Vec<(f64, f64)>>,

    // Whether daemon_state/KILL exists as of the last state tick.
    pub kill_active: bool,

    // Latest CLOB orderbook snapshot (None until the first poll).
    pub orderbook: Option<OrderbookSnapshot>,

    pub quit: bool,
}

impl AppState {
    pub fn new() -> Self {
        Self::default()
    }

    /// Apply a fresh state.json snapshot. Detects market rollover via
    /// `t_zero` and clears the rolling price deques when it flips.
    pub fn apply_state_snapshot(&mut self, snap: StateSnapshot) {
        let now = now_secs();
        let t_zero = snap.t_zero;
        if t_zero != self.last_t_zero {
            self.btc_series.clear();
            self.mkt_series.clear();
            self.fair_series.clear();
            self.last_t_zero = t_zero;
        }

        if let Some(btc) = snap.btc_price {
            if btc > 0.0 {
                push_cap(&mut self.btc_series, (now, btc), PRICE_HISTORY_CAP);
            }
        }
        if let Some(mkt) = snap.market_price_up {
            push_cap(&mut self.mkt_series, (now, mkt), PRICE_HISTORY_CAP);
        }
        if let Some(ref blob) = snap.refined {
            if let Some(fair) = blob.fair_price {
                push_cap(&mut self.fair_series, (now, fair), PRICE_HISTORY_CAP);
            }
        }

        self.snapshot = Some(snap);
    }
}

fn push_cap<T>(q: &mut VecDeque<T>, v: T, cap: usize) {
    if q.len() == cap {
        q.pop_front();
    }
    q.push_back(v);
}

/// Seconds-since-epoch as f64; used as the x-value for rolling series.
pub fn now_secs() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state_reader::{StateSnapshot, StrategyBlob};

    fn snap_with(btc: Option<f64>, mkt: Option<f64>, fair: Option<f64>, t_zero: Option<i64>) -> StateSnapshot {
        StateSnapshot {
            btc_price: btc,
            market_price_up: mkt,
            t_zero,
            refined: fair.map(|f| StrategyBlob {
                fair_price: Some(f),
                ..Default::default()
            }),
            ..Default::default()
        }
    }

    #[test]
    fn apply_pushes_to_deques() {
        let mut s = AppState::new();
        s.apply_state_snapshot(snap_with(Some(50000.0), Some(0.5), Some(0.6), Some(100)));
        assert_eq!(s.btc_series.len(), 1);
        assert_eq!(s.mkt_series.len(), 1);
        assert_eq!(s.fair_series.len(), 1);
    }

    #[test]
    fn apply_skips_invalid_btc() {
        let mut s = AppState::new();
        s.apply_state_snapshot(snap_with(None, Some(0.5), None, Some(100)));
        assert_eq!(s.btc_series.len(), 0);
        assert_eq!(s.mkt_series.len(), 1);
    }

    #[test]
    fn apply_skips_zero_btc() {
        let mut s = AppState::new();
        s.apply_state_snapshot(snap_with(Some(0.0), Some(0.5), None, Some(100)));
        assert_eq!(s.btc_series.len(), 0);
    }

    #[test]
    fn rollover_clears_rolling_series_only() {
        let mut s = AppState::new();
        s.apply_state_snapshot(snap_with(Some(50000.0), Some(0.5), Some(0.6), Some(100)));
        s.apply_state_snapshot(snap_with(Some(50001.0), Some(0.51), Some(0.61), Some(100)));
        assert_eq!(s.btc_series.len(), 2);

        // t_zero flips: rollover.
        s.apply_state_snapshot(snap_with(Some(50002.0), Some(0.52), Some(0.62), Some(200)));
        assert_eq!(s.btc_series.len(), 1, "deques must clear on rollover");
        assert_eq!(s.mkt_series.len(), 1);
        assert_eq!(s.fair_series.len(), 1);
    }

    #[test]
    fn deques_cap_at_price_history_cap() {
        let mut s = AppState::new();
        for i in 0..(PRICE_HISTORY_CAP + 10) {
            s.apply_state_snapshot(snap_with(Some(50000.0 + i as f64), None, None, Some(100)));
        }
        assert_eq!(s.btc_series.len(), PRICE_HISTORY_CAP);
        // Oldest entries were evicted; last entry is the freshest push.
        let (_, latest) = s.btc_series.back().copied().unwrap();
        assert_eq!(latest, 50000.0 + (PRICE_HISTORY_CAP + 9) as f64);
    }
}
