//! Colour palette mirrored from `python/dashboard.py` + `python/dashboard.tcss`.
//!
//! The Python impl uses Rich/Textual styles with TrueColour hex; ratatui's
//! `Color::Rgb` maps 1:1.

#![allow(dead_code)]

use ratatui::style::Color;

// Background / panels
pub const BG: Color = Color::Rgb(0x0f, 0x17, 0x2a);       // slate-950
pub const BORDER_HEADER: Color = Color::Rgb(0x3b, 0x82, 0xf6); // blue-500
pub const BORDER_MAIN: Color = Color::Rgb(0x06, 0xb6, 0xd4);   // cyan-500
pub const BORDER_DIM: Color = Color::Rgb(0x47, 0x55, 0x69);    // slate-600

// Text palette
pub const BTC_LABEL: Color = Color::Rgb(0x22, 0xd3, 0xee);    // cyan-400
pub const DIM: Color = Color::Rgb(0x94, 0xa3, 0xb8);          // slate-400
pub const SLUG: Color = Color::Yellow;
pub const SIGMA: Color = Color::Magenta;

// Chart palette
pub const CHART_BTC: Color = Color::Cyan;
pub const CHART_MKT: Color = Color::White;
pub const CHART_FAIR: Color = Color::Yellow;
pub const CHART_STRIKE: Color = Color::Rgb(0xff, 0x6b, 0x00); // amber line

// PnL / hero
pub const POS: Color = Color::Green;
pub const NEG: Color = Color::Red;
pub const HERO_UP: Color = Color::Rgb(0x4a, 0xde, 0x80);      // green-400
pub const HERO_DOWN: Color = Color::Rgb(0xf8, 0x71, 0x71);    // red-400

// Action kinds
pub const BUY: Color = Color::LightGreen;
pub const SELL: Color = Color::LightYellow;
pub const RES: Color = Color::Cyan;

// Pills
pub const PILL_ON: Color = Color::Green;
pub const PILL_OFF: Color = Color::Red;
pub const KILL_BG: Color = Color::Rgb(0xd9, 0x46, 0xef);     // fuchsia-500
pub const KILL_FG: Color = Color::White;
pub const OK_FG: Color = Color::Green;
