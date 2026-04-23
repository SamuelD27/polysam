//! polychart -- Rust ratatui TUI for polymarket-hustle.
//!
//! `--once` prints a single state.json line and exits (dev utility).
//! The default mode launches the ratatui event loop.

mod app;
mod events;
mod state;
mod state_reader;
mod theme;
mod ui;
mod util;
mod widgets;

use std::path::{Path, PathBuf};
use std::process::ExitCode;

use anyhow::Result;
use chrono::{Local, TimeZone};

use crate::state_reader::{read_state_json, resolve_state_dir, StateSnapshot};

fn format_ts(ts: Option<f64>) -> String {
    let Some(ts) = ts else { return "-".into() };
    let secs = ts.trunc() as i64;
    let nsec = ((ts.fract() * 1_000_000_000.0) as u32).min(999_999_999);
    match Local.timestamp_opt(secs, nsec).single() {
        Some(dt) => dt.format("%H:%M:%S").to_string(),
        None => format!("<bad ts={ts}>"),
    }
}

fn print_line(snap: &Option<StateSnapshot>) {
    match snap {
        None => println!("waiting for state.json"),
        Some(s) => {
            let slug = s.slug.as_deref().unwrap_or("-");
            let btc = s.btc_price.unwrap_or(0.0);
            let mkt = s
                .market_price_up
                .map(|v| format!("{v:.3}"))
                .unwrap_or_else(|| "-".into());
            println!(
                "t={} slug={} btc=${:.2} mkt_up={} binance={} rtds={}",
                format_ts(s.last_update),
                slug,
                btc,
                mkt,
                s.connections.binance,
                s.connections.rtds,
            );
        }
    }
}

fn run_once(path: &Path) -> Result<()> {
    let snap = read_state_json(path)?;
    print_line(&snap);
    Ok(())
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();

    let result: Result<()> = match args.iter().map(String::as_str).collect::<Vec<_>>().as_slice() {
        [] => app::run().await,
        ["--once"] => {
            let state_path: PathBuf = resolve_state_dir().join("state.json");
            run_once(&state_path)
        }
        other => {
            eprintln!("polychart: unexpected args: {other:?}");
            return ExitCode::from(2);
        }
    };

    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("polychart: {e:#}");
            ExitCode::from(2)
        }
    }
}
