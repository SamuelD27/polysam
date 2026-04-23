//! Polymarket CLOB /book poller for the orderbook panel.
//!
//! - 2s base cadence; backs off to 10s after 3 consecutive failures.
//! - Recovers to base cadence on first success.
//! - Network failures NEVER raise; they surface through the
//!   `OrderbookSnapshot::status == Error` path with a reason string.

#![allow(dead_code)]

use std::time::Duration;

use reqwest::Client;
use serde::Deserialize;

pub const CLOB_BOOK_URL: &str = "https://clob.polymarket.com/book";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrderbookSide {
    Ok,
    Empty,
    Error,
}

#[derive(Debug, Clone)]
pub struct OrderbookSnapshot {
    pub status: OrderbookSide,
    pub bids: Vec<(f64, f64)>, // (price, size) descending by price
    pub asks: Vec<(f64, f64)>, // (price, size) ascending by price
    pub best_bid: f64,
    pub best_ask: f64,
    pub spread: f64,
    pub mid: f64,
    pub error: Option<String>,
}

impl OrderbookSnapshot {
    pub(crate) fn ok(bids: Vec<(f64, f64)>, asks: Vec<(f64, f64)>) -> Self {
        let best_bid = bids.first().map(|(p, _)| *p).unwrap_or(0.0);
        let best_ask = asks.first().map(|(p, _)| *p).unwrap_or(0.0);
        let spread = if best_bid > 0.0 && best_ask > 0.0 {
            best_ask - best_bid
        } else {
            0.0
        };
        let mid = if best_bid > 0.0 && best_ask > 0.0 {
            (best_bid + best_ask) / 2.0
        } else {
            0.0
        };
        let status = if bids.is_empty() && asks.is_empty() {
            OrderbookSide::Empty
        } else {
            OrderbookSide::Ok
        };
        Self {
            status,
            bids,
            asks,
            best_bid,
            best_ask,
            spread,
            mid,
            error: None,
        }
    }

    pub(crate) fn error(reason: impl Into<String>) -> Self {
        Self {
            status: OrderbookSide::Error,
            bids: Vec::new(),
            asks: Vec::new(),
            best_bid: 0.0,
            best_ask: 0.0,
            spread: 0.0,
            mid: 0.0,
            error: Some(reason.into()),
        }
    }
}

pub struct OrderbookPoller {
    client: Client,
    token_id: String,
    pub base_interval: Duration,
    pub backoff_interval: Duration,
    pub failure_threshold: usize,
    consecutive_failures: usize,
    pub current_interval: Duration,
}

impl OrderbookPoller {
    pub fn new(token_id: String) -> Self {
        Self {
            client: Client::builder()
                .timeout(Duration::from_secs(3))
                .build()
                .unwrap_or_default(),
            token_id,
            base_interval: Duration::from_secs(2),
            backoff_interval: Duration::from_secs(10),
            failure_threshold: 3,
            consecutive_failures: 0,
            current_interval: Duration::from_secs(2),
        }
    }

    pub async fn poll_once(&mut self) -> OrderbookSnapshot {
        let url = format!("{CLOB_BOOK_URL}?token_id={}", self.token_id);
        let res = match self.client.get(&url).send().await {
            Ok(r) => r,
            Err(e) if e.is_timeout() => return self.record_failure("timeout"),
            Err(e) => return self.record_failure(e.to_string()),
        };
        let status = res.status();
        if !status.is_success() {
            return self.record_failure(status.as_u16().to_string());
        }
        let payload: RawBook = match res.json().await {
            Ok(p) => p,
            Err(_) => return self.record_failure("bad-json"),
        };

        let mut bids: Vec<(f64, f64)> = payload
            .bids
            .unwrap_or_default()
            .into_iter()
            .filter_map(|l| Some((l.price.as_f64()?, l.size.as_f64()?)))
            .collect();
        let mut asks: Vec<(f64, f64)> = payload
            .asks
            .unwrap_or_default()
            .into_iter()
            .filter_map(|l| Some((l.price.as_f64()?, l.size.as_f64()?)))
            .collect();
        bids.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
        asks.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));

        self.consecutive_failures = 0;
        self.current_interval = self.base_interval;
        OrderbookSnapshot::ok(bids, asks)
    }

    fn record_failure(&mut self, reason: impl Into<String>) -> OrderbookSnapshot {
        self.consecutive_failures += 1;
        if self.consecutive_failures >= self.failure_threshold {
            self.current_interval = self.backoff_interval;
        }
        OrderbookSnapshot::error(reason)
    }
}

#[derive(Debug, Deserialize)]
struct RawBook {
    #[serde(default)]
    bids: Option<Vec<RawLevel>>,
    #[serde(default)]
    asks: Option<Vec<RawLevel>>,
}

#[derive(Debug, Deserialize)]
struct RawLevel {
    price: StrOrNumber,
    size: StrOrNumber,
}

/// CLOB returns price/size as string OR number in the wild. Accept both.
#[derive(Debug, Deserialize)]
#[serde(untagged)]
enum StrOrNumber {
    Number(f64),
    Str(String),
}

impl StrOrNumber {
    fn as_f64(&self) -> Option<f64> {
        match self {
            StrOrNumber::Number(n) => Some(*n),
            StrOrNumber::Str(s) => s.parse().ok(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn snapshot_ok_derives_spread_and_mid() {
        let snap = OrderbookSnapshot::ok(vec![(0.42, 10.0), (0.40, 5.0)], vec![(0.44, 8.0)]);
        assert_eq!(snap.status, OrderbookSide::Ok);
        assert!((snap.spread - 0.02).abs() < 1e-9);
        assert!((snap.mid - 0.43).abs() < 1e-9);
    }

    #[test]
    fn snapshot_empty_vs_ok() {
        let empty = OrderbookSnapshot::ok(vec![], vec![]);
        assert_eq!(empty.status, OrderbookSide::Empty);
        let ok = OrderbookSnapshot::ok(vec![(0.5, 1.0)], vec![]);
        assert_eq!(ok.status, OrderbookSide::Ok);
    }

    #[test]
    fn str_or_number_handles_both_shapes() {
        let n: StrOrNumber = serde_json::from_str("0.42").unwrap();
        let s: StrOrNumber = serde_json::from_str("\"0.42\"").unwrap();
        assert_eq!(n.as_f64(), Some(0.42));
        assert_eq!(s.as_f64(), Some(0.42));
    }

    #[test]
    fn poller_records_failure_threshold() {
        let mut p = OrderbookPoller::new("tid".into());
        p.failure_threshold = 3;
        // simulate three failures via the record_failure path
        for _ in 0..2 {
            p.record_failure("test");
            assert_eq!(p.current_interval, p.base_interval);
        }
        p.record_failure("test");
        assert_eq!(p.current_interval, p.backoff_interval);
    }
}
