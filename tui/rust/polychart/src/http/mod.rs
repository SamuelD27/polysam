//! HTTP surfaces polled by the dashboard: Polymarket CLOB and Gamma.
//!
//! All HTTP happens in background tokio tasks -- the render loop never
//! blocks on network. Failures are reported through shared state as
//! error snapshots with a `reason` string, never raised.

#![allow(dead_code)]

pub mod clob;
pub mod gamma;

pub use clob::{OrderbookPoller, OrderbookSide, OrderbookSnapshot};
pub use gamma::TokenResolver;
