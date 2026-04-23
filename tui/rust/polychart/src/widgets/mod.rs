//! Per-panel ratatui draw functions.
//!
//! Each module exposes a single `draw_*` fn that takes a frame + rect +
//! &AppState and renders into the frame. No internal widget state; the
//! render loop is the single reader.

mod baselines;
mod header;
mod live_curves;
mod main_strategy;
mod orderbook;
mod orders_log;

pub use baselines::draw_baselines;
pub use header::draw_header;
pub use live_curves::draw_live_curves;
pub use main_strategy::draw_main_strategy;
pub use orderbook::draw_orderbook;
pub use orders_log::draw_orders_log;
