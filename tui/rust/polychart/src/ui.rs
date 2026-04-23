//! Top-level frame split + draw dispatch.
//!
//! Layout (matches `tui/python/dashboard.tcss`):
//!
//!   row 1: header                     (4 rows, full width)
//!   row 2: live curves | main strategy   (2fr, split 1:1)
//!   row 3: orderbook  | baselines + orders log (1fr, split 1:1)
//!     within the right cell:
//!       top: baselines  (4 rows)
//!       bottom: orders log (fills remainder)

#![allow(dead_code)]

use ratatui::{
    layout::{Constraint, Direction, Layout, Rect},
    Frame,
};

use crate::state::AppState;
use crate::widgets::{
    draw_baselines, draw_header, draw_live_curves, draw_main_strategy, draw_orderbook,
    draw_orders_log,
};

pub fn draw(frame: &mut Frame, app: &AppState) {
    let area = frame.area();
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(4),       // header
            Constraint::Ratio(3, 5),     // 2fr  of remaining
            Constraint::Ratio(2, 5),     // 1fr  of remaining
        ])
        .split(area);

    draw_header(frame, rows[0], app);

    let middle = split_horizontally(rows[1]);
    draw_live_curves(frame, middle[0], app);
    draw_main_strategy(frame, middle[1], app);

    let bottom = split_horizontally(rows[2]);
    draw_orderbook(frame, bottom[0], app);

    let right = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(4), Constraint::Min(0)])
        .split(bottom[1]);
    draw_baselines(frame, right[0], app);
    draw_orders_log(frame, right[1], app);
}

fn split_horizontally(area: Rect) -> std::rc::Rc<[Rect]> {
    Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Percentage(50), Constraint::Percentage(50)])
        .split(area)
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::{backend::TestBackend, Terminal};

    #[test]
    fn layout_renders_without_panic() {
        let app = AppState::new();
        let backend = TestBackend::new(180, 50);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal.draw(|frame| draw(frame, &app)).unwrap();
        let buf = terminal.backend().buffer().clone();
        // Six panel borders means six distinct titles somewhere in the
        // buffer; assert at least one glyph survived per panel.
        let text = buf
            .content
            .iter()
            .map(|c| c.symbol())
            .collect::<String>();
        for expected in [
            "header", "live", "refined", "orderbook", "baselines", "orders",
        ] {
            assert!(
                text.contains(expected),
                "expected '{expected}' in rendered buffer"
            );
        }
    }

    #[test]
    fn layout_tolerates_small_terminal() {
        // Minimum realistic size: constraints must not panic.
        let app = AppState::new();
        let backend = TestBackend::new(80, 24);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal.draw(|frame| draw(frame, &app)).unwrap();
    }
}
