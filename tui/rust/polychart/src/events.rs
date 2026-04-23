//! Byte-offset tailer for `daemon_state/events.jsonl`.
//!
//! Invariants ported from the Python `EventsTailer`:
//!   - Opens the file read-only.
//!   - Resumes from the last byte offset; on truncation (size < pos)
//!     resets pos, clears cumulative PnL.
//!   - Defers any trailing partial line until the next tick so we never
//!     dispatch a half-written JSON record and never skip past bytes we
//!     could not parse.
//!   - `pnl_series[strategy]` accumulates cumulative PnL on
//!     `exit_filled` and `resolve`; decimates the middle past
//!     PNL_SERIES_CAP but always preserves the first and last point.
//!
//! Forward compatibility: unknown `type` values are silently dropped.

#![allow(dead_code)]

use std::collections::{HashMap, VecDeque};
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::PathBuf;

use anyhow::Result;
use serde::Deserialize;

use crate::state::PNL_SERIES_CAP;

pub const ACTION_CAP: usize = 50;
const EVENTS_DEQUE_CAP: usize = 200;

/// Trimmed, render-friendly representation of a trade/order event.
#[derive(Debug, Clone)]
pub struct Action {
    pub ts: f64,
    pub kind: ActionKind,
    pub strategy: String,
    pub side: String,
    pub price: f64,
    pub size_usdc: f64,
    pub pnl: Option<f64>,
    pub exit_type: Option<String>,
    pub won: Option<bool>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ActionKind {
    Buy,
    Sell,
    Res,
}

impl ActionKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            ActionKind::Buy => "BUY",
            ActionKind::Sell => "SELL",
            ActionKind::Res => "RES",
        }
    }
}

/// Minimal decode of one events.jsonl line. Unknown `type` values are
/// dropped at the match in `dispatch`. We keep the full JSON around as a
/// fallback when we just want to persist the raw event.
#[derive(Debug, Deserialize)]
struct RawEvent {
    #[serde(default)]
    ts: f64,
    #[serde(default)]
    #[serde(rename = "type")]
    event_type: String,
    #[serde(default)]
    strategy: Option<String>,
    #[serde(default)]
    position: Option<RawPosition>,
    #[serde(default)]
    trade: Option<RawTrade>,
}

#[derive(Debug, Default, Deserialize)]
struct RawPosition {
    #[serde(default)]
    side: String,
    #[serde(default)]
    entry_price: f64,
    #[serde(default)]
    size_usdc: f64,
}

#[derive(Debug, Default, Deserialize)]
struct RawTrade {
    #[serde(default)]
    side: String,
    #[serde(default)]
    exit_price: f64,
    #[serde(default)]
    size_usdc: f64,
    #[serde(default)]
    pnl: f64,
    #[serde(default)]
    exit_type: Option<String>,
    #[serde(default)]
    won: Option<bool>,
}

pub struct EventsTailer {
    path: PathBuf,
    pos: u64,
    pub events: VecDeque<serde_json::Value>,
    pub base_actions: VecDeque<Action>,
    pub enh_actions: VecDeque<Action>,
    pub refined_actions: VecDeque<Action>,
    pub unified_actions: VecDeque<Action>,
    pub pnl_series: HashMap<String, Vec<(f64, f64)>>,
    cum: HashMap<String, f64>,
}

impl EventsTailer {
    pub fn new(path: PathBuf) -> Self {
        Self {
            path,
            pos: 0,
            events: VecDeque::with_capacity(EVENTS_DEQUE_CAP),
            base_actions: VecDeque::with_capacity(ACTION_CAP),
            enh_actions: VecDeque::with_capacity(ACTION_CAP),
            refined_actions: VecDeque::with_capacity(ACTION_CAP),
            unified_actions: VecDeque::with_capacity(ACTION_CAP),
            pnl_series: HashMap::new(),
            cum: HashMap::new(),
        }
    }

    /// Advance to the current EOF, dispatching every complete line we see.
    /// Returns `Ok(n)` where n is the number of events parsed this call.
    pub fn update(&mut self) -> Result<usize> {
        let mut file = match File::open(&self.path) {
            Ok(f) => f,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(0),
            Err(e) => return Err(e.into()),
        };
        let size = file.metadata()?.len();
        if size < self.pos {
            // Truncation / rotation -- replay from the top.
            self.pos = 0;
            self.cum.clear();
            self.pnl_series.clear();
        }
        file.seek(SeekFrom::Start(self.pos))?;
        let mut buf = Vec::with_capacity((size - self.pos) as usize);
        file.read_to_end(&mut buf)?;
        self.pos += buf.len() as u64;

        // Defer any trailing partial line until the next tick.
        if !buf.is_empty() && !buf.ends_with(b"\n") {
            match buf.iter().rposition(|&b| b == b'\n') {
                None => {
                    // Whole chunk is one partial line; rewind fully.
                    self.pos -= buf.len() as u64;
                    return Ok(0);
                }
                Some(idx) => {
                    let partial_bytes = buf.len() - idx - 1;
                    self.pos -= partial_bytes as u64;
                    buf.truncate(idx + 1);
                }
            }
        }

        let mut n = 0usize;
        for line in buf.split(|&b| b == b'\n') {
            if line.is_empty() {
                continue;
            }
            let Ok(raw) = serde_json::from_slice::<RawEvent>(line) else {
                continue;
            };
            self.events.push_back(
                serde_json::from_slice(line).unwrap_or(serde_json::Value::Null),
            );
            if self.events.len() > EVENTS_DEQUE_CAP {
                self.events.pop_front();
            }
            self.dispatch(&raw);
            n += 1;
        }
        Ok(n)
    }

    fn dispatch(&mut self, ev: &RawEvent) {
        if let Some(action) = action_from_event(ev) {
            match action.strategy.as_str() {
                "base" => push_deque(&mut self.base_actions, action.clone(), ACTION_CAP),
                "enhanced" => push_deque(&mut self.enh_actions, action.clone(), ACTION_CAP),
                "refined" => push_deque(&mut self.refined_actions, action.clone(), ACTION_CAP),
                _ => {}
            }
            push_deque(&mut self.unified_actions, action, ACTION_CAP);
        }
        if ev.event_type == "exit_filled" || ev.event_type == "resolve" {
            let strat = ev.strategy.clone().unwrap_or_default();
            let pnl = ev.trade.as_ref().map(|t| t.pnl).unwrap_or(0.0);
            let total = self.cum.entry(strat.clone()).or_insert(0.0);
            *total += pnl;
            let series = self.pnl_series.entry(strat.clone()).or_default();
            series.push((ev.ts, *total));
            decimate_if_needed(series, PNL_SERIES_CAP);
        }
    }
}

fn push_deque<T>(q: &mut VecDeque<T>, v: T, cap: usize) {
    if q.len() == cap {
        q.pop_front();
    }
    q.push_back(v);
}

/// Preserve the first and last points; evenly sample the middle when
/// `series.len() > cap`. Never drops the running tail.
fn decimate_if_needed(series: &mut Vec<(f64, f64)>, cap: usize) {
    if series.len() <= cap {
        return;
    }
    let first = series[0];
    let last = series[series.len() - 1];
    let middle = &series[1..series.len() - 1];
    let middle_target = cap.saturating_sub(2);
    if middle_target == 0 || middle.is_empty() {
        *series = vec![first, last];
        return;
    }
    let step = (middle.len() / middle_target).max(1);
    let sampled: Vec<(f64, f64)> = middle.iter().step_by(step).take(middle_target).copied().collect();
    let mut new = Vec::with_capacity(2 + sampled.len());
    new.push(first);
    new.extend(sampled);
    new.push(last);
    *series = new;
}

fn action_from_event(ev: &RawEvent) -> Option<Action> {
    let strategy = ev.strategy.clone().unwrap_or_default();
    match ev.event_type.as_str() {
        "entry_filled" => {
            let pos = ev.position.as_ref()?;
            Some(Action {
                ts: ev.ts,
                kind: ActionKind::Buy,
                strategy,
                side: pos.side.clone(),
                price: pos.entry_price,
                size_usdc: pos.size_usdc,
                pnl: None,
                exit_type: None,
                won: None,
            })
        }
        "exit_filled" => {
            let tr = ev.trade.as_ref()?;
            Some(Action {
                ts: ev.ts,
                kind: ActionKind::Sell,
                strategy,
                side: tr.side.clone(),
                price: tr.exit_price,
                size_usdc: tr.size_usdc,
                pnl: Some(tr.pnl),
                exit_type: tr.exit_type.clone(),
                won: None,
            })
        }
        "resolve" => {
            let tr = ev.trade.as_ref()?;
            Some(Action {
                ts: ev.ts,
                kind: ActionKind::Res,
                strategy,
                side: tr.side.clone(),
                price: tr.exit_price,
                size_usdc: tr.size_usdc,
                pnl: Some(tr.pnl),
                exit_type: tr.exit_type.clone().or(Some("RESOLUTION".into())),
                won: tr.won,
            })
        }
        _ => None,
    }
}

/// Convenience for tests: resolve a default events.jsonl path.
#[cfg(test)]
pub fn default_events_path() -> PathBuf {
    crate::state_reader::resolve_state_dir().join("events.jsonl")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use tempfile::NamedTempFile;

    fn write_events(events: &[serde_json::Value]) -> NamedTempFile {
        let mut tmp = NamedTempFile::new().unwrap();
        for ev in events {
            writeln!(tmp, "{ev}").unwrap();
        }
        tmp.flush().unwrap();
        tmp
    }

    fn entry(ts: f64, strategy: &str, pnl: f64) -> serde_json::Value {
        serde_json::json!({
            "ts": ts, "type": "entry_filled", "strategy": strategy,
            "position": {"side": "Up", "entry_price": 0.5, "size_usdc": pnl.abs() + 1.0},
        })
    }
    fn exit(ts: f64, strategy: &str, pnl: f64) -> serde_json::Value {
        serde_json::json!({
            "ts": ts, "type": "exit_filled", "strategy": strategy,
            "trade": {"side": "Up", "exit_price": 0.6, "size_usdc": 10.0,
                      "pnl": pnl, "exit_type": "TP"},
        })
    }

    #[test]
    fn tailer_picks_up_initial_events() {
        let tmp = write_events(&[entry(1.0, "refined", 0.0)]);
        let mut t = EventsTailer::new(tmp.path().into());
        assert_eq!(t.update().unwrap(), 1);
        assert_eq!(t.refined_actions.len(), 1);
    }

    #[test]
    fn tailer_picks_up_appended_events() {
        let tmp = write_events(&[entry(1.0, "refined", 0.0)]);
        let mut t = EventsTailer::new(tmp.path().into());
        t.update().unwrap();
        let mut f = std::fs::OpenOptions::new().append(true).open(tmp.path()).unwrap();
        writeln!(f, "{}", exit(2.0, "refined", 0.5)).unwrap();
        f.sync_all().unwrap();
        t.update().unwrap();
        assert_eq!(t.refined_actions.len(), 2);
    }

    #[test]
    fn tailer_idempotent_without_new_events() {
        let tmp = write_events(&[exit(1.0, "refined", 1.0)]);
        let mut t = EventsTailer::new(tmp.path().into());
        t.update().unwrap();
        t.update().unwrap();
        t.update().unwrap();
        assert_eq!(t.pnl_series["refined"].len(), 1);
    }

    #[test]
    fn tailer_resets_on_truncation() {
        let tmp = write_events(&[exit(1.0, "refined", 1.0)]);
        let mut t = EventsTailer::new(tmp.path().into());
        t.update().unwrap();
        assert_eq!(t.pnl_series["refined"].len(), 1);

        std::fs::write(tmp.path(), "").unwrap();
        t.update().unwrap();
        assert_eq!(t.pos, 0);
        assert!(t.pnl_series.is_empty(), "PnL series clears on truncation");
    }

    #[test]
    fn tailer_handles_missing_file() {
        let path = PathBuf::from("/tmp/definitely-not-a-real-file-polychart.jsonl");
        let mut t = EventsTailer::new(path);
        assert_eq!(t.update().unwrap(), 0);
    }

    #[test]
    fn tailer_skips_malformed_lines() {
        let mut tmp = NamedTempFile::new().unwrap();
        writeln!(tmp, "{}", exit(1.0, "refined", 0.5)).unwrap();
        writeln!(tmp, "not-json-line").unwrap();
        writeln!(tmp, "{}", exit(2.0, "refined", 0.5)).unwrap();
        tmp.flush().unwrap();
        let mut t = EventsTailer::new(tmp.path().into());
        t.update().unwrap();
        assert_eq!(t.pnl_series["refined"].len(), 2);
    }

    #[test]
    fn tailer_defers_partial_trailing_line() {
        let mut tmp = NamedTempFile::new().unwrap();
        // Complete line + half of the next one.
        let line1 = format!("{}\n", exit(1.0, "refined", 1.0));
        tmp.write_all(line1.as_bytes()).unwrap();
        tmp.write_all(b"{\"ts\": 2,").unwrap(); // partial
        tmp.flush().unwrap();

        let mut t = EventsTailer::new(tmp.path().into());
        t.update().unwrap();
        assert_eq!(t.pnl_series["refined"].len(), 1);

        // Complete the write.
        let mut f = std::fs::OpenOptions::new().append(true).open(tmp.path()).unwrap();
        writeln!(f, r#" "type": "exit_filled", "strategy": "refined", "trade": {{"pnl": 0.5}}}}"#).unwrap();
        f.sync_all().unwrap();
        t.update().unwrap();
        assert_eq!(t.pnl_series["refined"].len(), 2);
    }

    #[test]
    fn pnl_series_sums_per_strategy() {
        let tmp = write_events(&[
            exit(1.0, "refined", 1.5),
            exit(2.0, "base", -0.25),
            serde_json::json!({
                "ts": 3.0, "type": "resolve", "strategy": "refined",
                "trade": {"pnl": 0.5, "won": true},
            }),
        ]);
        let mut t = EventsTailer::new(tmp.path().into());
        t.update().unwrap();
        let refined: Vec<f64> = t.pnl_series["refined"].iter().map(|(_, v)| *v).collect();
        let base: Vec<f64> = t.pnl_series["base"].iter().map(|(_, v)| *v).collect();
        assert_eq!(refined, vec![1.5, 2.0]);
        assert_eq!(base, vec![-0.25]);
    }

    #[test]
    fn pnl_series_ignores_non_realised_events() {
        let tmp = write_events(&[
            serde_json::json!({"ts": 1, "type": "market_rollover", "slug": "x"}),
            entry(2.0, "refined", 0.0),
        ]);
        let mut t = EventsTailer::new(tmp.path().into());
        t.update().unwrap();
        assert!(t.pnl_series.is_empty());
    }

    #[test]
    fn decimation_preserves_first_and_last() {
        let mut s: Vec<(f64, f64)> = (0..10).map(|i| (i as f64, 0.1 * i as f64)).collect();
        decimate_if_needed(&mut s, 4);
        assert!(s.len() <= 4);
        assert_eq!(s[0].0, 0.0);
        assert_eq!(s.last().unwrap().0, 9.0);
    }
}
