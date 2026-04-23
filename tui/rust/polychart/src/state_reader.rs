//! Read-only deserializer for `daemon_state/state.json`.
//!
//! Mirrors the schema emitted by `daemon_base_v1.DaemonState.to_dict()`.
//! Unknown fields are silently ignored (forward compat).

// Fields on inner structs (Position, Trade, extra) are consumed by the
// main-strategy and orderbook widgets that land in later phases.
#![allow(dead_code)]

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::Deserialize;

#[derive(Debug, Default, Deserialize, Clone)]
pub struct StateSnapshot {
    #[serde(default)]
    pub btc_price: Option<f64>,
    #[serde(default)]
    pub btc_ts: Option<f64>,
    #[serde(default)]
    pub sigma: Option<f64>,
    #[serde(default)]
    pub t_zero: Option<i64>,
    #[serde(default)]
    pub strike: Option<f64>,
    #[serde(default)]
    pub slug: Option<String>,
    #[serde(default)]
    pub market_price_up: Option<f64>,
    #[serde(default)]
    pub market_price_ts: Option<f64>,
    #[serde(default)]
    pub rtds_last_msg_ts: Option<f64>,
    #[serde(default)]
    pub connections: Connections,
    #[serde(default)]
    pub base: Option<StrategyBlob>,
    #[serde(default)]
    pub enhanced: Option<StrategyBlob>,
    #[serde(default)]
    pub refined: Option<StrategyBlob>,
    #[serde(default)]
    pub last_update: Option<f64>,
}

#[derive(Debug, Default, Deserialize, Clone)]
pub struct Connections {
    #[serde(default)]
    pub binance: bool,
    #[serde(default)]
    pub rtds: bool,
}

#[derive(Debug, Default, Deserialize, Clone)]
pub struct StrategyBlob {
    #[serde(default)]
    pub fair_price: Option<f64>,
    #[serde(default)]
    pub open_position: Option<Position>,
    #[serde(default)]
    pub closed_trades: Vec<Trade>,
    #[serde(default)]
    pub stats: Stats,
    #[serde(default)]
    pub extra: Option<serde_json::Value>,
}

#[derive(Debug, Deserialize, Clone)]
pub struct Position {
    #[serde(default)]
    pub side: String,
    #[serde(default)]
    pub entry_price: f64,
    #[serde(default)]
    pub edge: f64,
    #[serde(default)]
    pub size_usdc: f64,
    #[serde(default)]
    pub size_shares: f64,
}

#[derive(Debug, Deserialize, Clone)]
pub struct Trade {
    #[serde(default)]
    pub side: String,
    #[serde(default)]
    pub entry_price: f64,
    #[serde(default)]
    pub exit_price: f64,
    #[serde(default)]
    pub pnl: f64,
    #[serde(default)]
    pub size_usdc: f64,
    #[serde(default)]
    pub won: Option<bool>,
    #[serde(default)]
    pub resolved_time: Option<f64>,
    #[serde(default)]
    pub exit_type: Option<String>,
    #[serde(default)]
    pub hold_time_s: Option<f64>,
}

#[derive(Debug, Default, Deserialize, Clone)]
pub struct Stats {
    #[serde(default)]
    pub total_pnl: f64,
    #[serde(default)]
    pub total_trades: u64,
    #[serde(default)]
    pub wins: u64,
    #[serde(default)]
    pub losses: u64,
    #[serde(default)]
    pub max_drawdown: f64,
    #[serde(default)]
    pub current_streak: i64,
    #[serde(default)]
    pub streak_type: Option<String>,
    #[serde(default)]
    pub total_risked: f64,
}

impl Stats {
    pub fn win_rate(&self) -> f64 {
        if self.total_trades == 0 {
            0.0
        } else {
            100.0 * self.wins as f64 / self.total_trades as f64
        }
    }

    pub fn roi(&self) -> f64 {
        if self.total_risked == 0.0 {
            0.0
        } else {
            self.total_pnl / self.total_risked * 100.0
        }
    }
}

/// Resolve the state.json path using:
///   1. `POLYCHART_STATE_DIR` env override
///   2. `./daemon_state/state.json` (launch_daemon.sh cds to repo root)
///   3. walk up from the binary path for a `daemon_state` sibling
pub fn resolve_state_dir() -> PathBuf {
    if let Ok(dir) = std::env::var("POLYCHART_STATE_DIR") {
        return PathBuf::from(dir);
    }
    let cwd = PathBuf::from("daemon_state");
    if cwd.exists() {
        return cwd;
    }
    if let Ok(exe) = std::env::current_exe() {
        let mut cur: &Path = exe.as_path();
        while let Some(parent) = cur.parent() {
            let candidate = parent.join("daemon_state");
            if candidate.exists() {
                return candidate;
            }
            cur = parent;
        }
    }
    cwd
}

/// Synchronous read + parse; returns Ok(None) if the file is missing.
pub fn read_state_json(path: &Path) -> Result<Option<StateSnapshot>> {
    match std::fs::read(path) {
        Ok(bytes) => {
            let snap = serde_json::from_slice(&bytes)
                .with_context(|| format!("parsing {}", path.display()))?;
            Ok(Some(snap))
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e).with_context(|| format!("reading {}", path.display())),
    }
}

/// Async read. Tokio task uses this to avoid blocking on slow disks.
pub async fn read_state_json_async(path: &Path) -> Result<Option<StateSnapshot>> {
    match tokio::fs::read(path).await {
        Ok(bytes) => {
            let snap = serde_json::from_slice(&bytes)
                .with_context(|| format!("parsing {}", path.display()))?;
            Ok(Some(snap))
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e).with_context(|| format!("reading {}", path.display())),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn snapshot_parses_minimal_json() {
        let j = r#"{"btc_price": 50000.0, "slug": "btc-updown-5m-1000"}"#;
        let s: StateSnapshot = serde_json::from_str(j).unwrap();
        assert_eq!(s.btc_price, Some(50000.0));
        assert_eq!(s.slug.as_deref(), Some("btc-updown-5m-1000"));
        assert_eq!(s.sigma, None);
        assert!(!s.connections.binance);
    }

    #[test]
    fn snapshot_handles_null_fields() {
        let j = r#"{"btc_price": null, "sigma": null, "strike": null, "slug": null}"#;
        let s: StateSnapshot = serde_json::from_str(j).unwrap();
        assert_eq!(s.btc_price, None);
        assert_eq!(s.sigma, None);
    }

    #[test]
    fn snapshot_ignores_unknown_fields() {
        let j = r#"{"btc_price": 100.0, "totally_new_field": "ignore me"}"#;
        let s: StateSnapshot = serde_json::from_str(j).unwrap();
        assert_eq!(s.btc_price, Some(100.0));
    }

    #[test]
    fn stats_win_rate_and_roi() {
        let s = Stats {
            total_trades: 4,
            wins: 3,
            losses: 1,
            total_pnl: 2.0,
            total_risked: 20.0,
            ..Default::default()
        };
        assert_eq!(s.win_rate(), 75.0);
        assert_eq!(s.roi(), 10.0);
    }

    #[test]
    fn stats_zero_trades_is_safe() {
        let s = Stats::default();
        assert_eq!(s.win_rate(), 0.0);
        assert_eq!(s.roi(), 0.0);
    }

    #[test]
    fn resolve_state_dir_honours_env() {
        std::env::set_var("POLYCHART_STATE_DIR", "/tmp/some/where");
        let p = resolve_state_dir();
        assert_eq!(p, PathBuf::from("/tmp/some/where"));
        std::env::remove_var("POLYCHART_STATE_DIR");
    }

    #[test]
    fn read_state_json_missing_returns_ok_none() {
        let out = read_state_json(Path::new("/tmp/definitely-does-not-exist-polychart")).unwrap();
        assert!(out.is_none());
    }
}
