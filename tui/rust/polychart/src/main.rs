//! polychart — Rust ratatui TUI for polymarket-hustle.
//!
//! The TUI renderer itself lands in later phases (app.rs + ui.rs).
//! Until then, `--once` prints a single state.json line and exits, and the
//! default mode prints one line per second so the build + state-reader
//! wiring is observable without ratatui.

mod state;
mod state_reader;

use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::time::Duration;

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

fn run_loop(path: PathBuf) -> Result<()> {
    loop {
        match read_state_json(&path) {
            Ok(snap) => print_line(&snap),
            Err(e) => eprintln!("read error: {e:#}"),
        }
        std::thread::sleep(Duration::from_secs(1));
    }
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let state_dir = resolve_state_dir();
    let state_path = state_dir.join("state.json");

    let result = match args.iter().map(String::as_str).collect::<Vec<_>>().as_slice() {
        ["--once"] | [] if args.is_empty() && cfg!(test) => run_once(&state_path),
        ["--once"] => run_once(&state_path),
        [] => run_loop(state_path),
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
