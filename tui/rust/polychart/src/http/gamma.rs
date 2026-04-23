//! Polymarket Gamma API: slug -> (yes_token_id, no_token_id).
//!
//! Cached forever per slug (5-minute markets are time-unique). `resolve`
//! returns `None` on any failure; the caller schedules a retry.

#![allow(dead_code)]

use std::collections::HashMap;
use std::sync::Mutex;

use reqwest::Client;
use serde::Deserialize;

const GAMMA_EVENTS_URL: &str = "https://gamma-api.polymarket.com/events";
const GAMMA_MARKETS_URL: &str = "https://gamma-api.polymarket.com/markets";

#[derive(Debug, Clone)]
pub struct TokenIds {
    pub yes_token_id: String,
    pub no_token_id: String,
}

pub struct TokenResolver {
    client: Client,
    cache: Mutex<HashMap<String, TokenIds>>,
}

impl TokenResolver {
    pub fn new() -> Self {
        Self {
            client: Client::builder()
                .timeout(std::time::Duration::from_secs(5))
                .build()
                .unwrap_or_default(),
            cache: Mutex::new(HashMap::new()),
        }
    }

    pub async fn resolve(&self, slug: &str) -> Option<TokenIds> {
        if let Some(hit) = self.cache.lock().ok().and_then(|c| c.get(slug).cloned()) {
            return Some(hit);
        }
        let tokens = match self.fetch_events(slug).await {
            Some(t) => Some(t),
            None => self.fetch_markets(slug).await,
        }?;
        if let Ok(mut c) = self.cache.lock() {
            c.insert(slug.to_string(), tokens.clone());
        }
        Some(tokens)
    }

    async fn fetch_events(&self, slug: &str) -> Option<TokenIds> {
        let url = format!("{GAMMA_EVENTS_URL}?slug={slug}");
        let resp = self.client.get(&url).send().await.ok()?;
        if !resp.status().is_success() {
            return None;
        }
        let events: Vec<Event> = resp.json().await.ok()?;
        events
            .into_iter()
            .flat_map(|e| e.markets)
            .find_map(|m| parse_market(&m))
    }

    async fn fetch_markets(&self, slug: &str) -> Option<TokenIds> {
        let url = format!("{GAMMA_MARKETS_URL}?slug={slug}");
        let resp = self.client.get(&url).send().await.ok()?;
        if !resp.status().is_success() {
            return None;
        }
        let markets: Vec<Market> = resp.json().await.ok()?;
        markets.iter().find_map(parse_market)
    }
}

impl Default for TokenResolver {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Debug, Deserialize)]
struct Event {
    #[serde(default)]
    markets: Vec<Market>,
}

#[derive(Debug, Deserialize)]
struct Market {
    #[serde(default, rename = "clobTokenIds")]
    clob_token_ids: Option<serde_json::Value>,
}

/// `clobTokenIds` may be a JSON-encoded string OR a raw array in the wild.
/// Both shapes are accepted (matches the Python TokenResolver).
fn parse_market(m: &Market) -> Option<TokenIds> {
    let tokens: Vec<String> = match m.clob_token_ids.as_ref()? {
        serde_json::Value::String(s) => serde_json::from_str(s).ok()?,
        serde_json::Value::Array(arr) => arr
            .iter()
            .filter_map(|v| v.as_str().map(str::to_string))
            .collect(),
        _ => return None,
    };
    if tokens.len() < 2 {
        return None;
    }
    Some(TokenIds {
        yes_token_id: tokens[0].clone(),
        no_token_id: tokens[1].clone(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_market_accepts_string_json() {
        let m = Market {
            clob_token_ids: Some(serde_json::json!("[\"yes-id\",\"no-id\"]")),
        };
        let t = parse_market(&m).unwrap();
        assert_eq!(t.yes_token_id, "yes-id");
        assert_eq!(t.no_token_id, "no-id");
    }

    #[test]
    fn parse_market_accepts_raw_array() {
        let m = Market {
            clob_token_ids: Some(serde_json::json!(["yes-id", "no-id"])),
        };
        let t = parse_market(&m).unwrap();
        assert_eq!(t.yes_token_id, "yes-id");
    }

    #[test]
    fn parse_market_rejects_short_array() {
        let m = Market {
            clob_token_ids: Some(serde_json::json!(["only-one"])),
        };
        assert!(parse_market(&m).is_none());
    }

    #[test]
    fn parse_market_rejects_none() {
        let m = Market {
            clob_token_ids: None,
        };
        assert!(parse_market(&m).is_none());
    }
}
