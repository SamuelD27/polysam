//! polychart scaffold.
//!
//! Reads daemon_state/state.json once, prints a one-line sanity summary,
//! and exits. The real TUI loop (ratatui + events.jsonl tailer + CLOB
//! /book poller) is the job of the TUI rewrite session — see
//! `tui/README.md`. This scaffold exists so the build + path wiring is
//! proven before any TUI code is written.

use std::path::{Path, PathBuf};
use std::process::ExitCode;

use anyhow::{Context, Result};
use chrono::{Local, TimeZone};
use serde::Deserialize;

#[derive(Debug, Deserialize)]
struct StateSnapshot {
    #[serde(default)]
    slug: Option<String>,
    #[serde(default)]
    btc_price: Option<f64>,
    #[serde(default)]
    market_price_up: Option<f64>,
    #[serde(default)]
    last_update: Option<f64>,
}

fn resolve_state_path() -> PathBuf {
    // polychart binary lives under tui/rust/polychart/target/release/.
    // daemon_state/ is relative to the repo root. Preferred lookup order:
    //   1. POLYCHART_STATE_DIR env override (tests / alt layouts)
    //   2. CWD/daemon_state (matches launch_daemon.sh which cds to repo)
    //   3. Walk up from the binary path to find a daemon_state sibling
    if let Ok(dir) = std::env::var("POLYCHART_STATE_DIR") {
        return PathBuf::from(dir).join("state.json");
    }
    let cwd_candidate = PathBuf::from("daemon_state/state.json");
    if cwd_candidate.exists() {
        return cwd_candidate;
    }
    if let Ok(exe) = std::env::current_exe() {
        let mut cur: &Path = exe.as_path();
        while let Some(parent) = cur.parent() {
            let candidate = parent.join("daemon_state").join("state.json");
            if candidate.exists() {
                return candidate;
            }
            cur = parent;
        }
    }
    cwd_candidate
}

fn format_ts(ts: f64) -> String {
    let secs = ts.trunc() as i64;
    let nsec = ((ts.fract() * 1_000_000_000.0) as u32).min(999_999_999);
    match Local.timestamp_opt(secs, nsec).single() {
        Some(dt) => dt.format("%Y-%m-%d %H:%M:%S").to_string(),
        None => format!("<unparseable ts={ts}>"),
    }
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("polychart: {e:#}");
            ExitCode::from(2)
        }
    }
}

fn run() -> Result<()> {
    let path = resolve_state_path();
    let bytes = std::fs::read(&path).with_context(|| format!("reading {}", path.display()))?;
    let snap: StateSnapshot =
        serde_json::from_slice(&bytes).with_context(|| format!("parsing {}", path.display()))?;

    let slug = snap.slug.as_deref().unwrap_or("<none>");
    let btc = snap.btc_price.unwrap_or(0.0);
    let mkt = snap.market_price_up;
    let ts = snap.last_update.unwrap_or(0.0);

    println!(
        "polychart alive, read state at t={} slug={} btc=${:.2} mkt_up={}",
        format_ts(ts),
        slug,
        btc,
        mkt.map(|v| format!("{:.3}", v))
            .unwrap_or_else(|| "-".into()),
    );
    Ok(())
}
