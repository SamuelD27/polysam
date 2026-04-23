use ratatui::{
    layout::{Constraint, Direction, Layout, Rect},
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::{Block, BorderType, Borders, Cell, Paragraph, Row, Table},
    Frame,
};

use crate::http::{OrderbookSide, OrderbookSnapshot};
use crate::state::AppState;
use crate::theme;

const HARD_DEPTH_CAP: usize = 10;
const BAR_WIDTH: usize = 20;

pub fn draw_orderbook(frame: &mut Frame, area: Rect, app: &AppState) {
    // Right-side status: show either spread or the failure reason so
    // the panel header spans the full width.
    let right_title = match app.orderbook.as_ref() {
        None => Line::from(Span::styled(
            " waiting ",
            Style::default().fg(theme::DIM),
        )),
        Some(snap) => match snap.status {
            OrderbookSide::Ok => Line::from(Span::styled(
                format!(" spread {:.3} ", snap.spread),
                Style::default().fg(theme::DIM),
            )),
            OrderbookSide::Empty => Line::from(Span::styled(
                " empty ",
                Style::default().fg(theme::DIM),
            )),
            OrderbookSide::Error => Line::from(Span::styled(
                format!(" {} ", snap.error.as_deref().unwrap_or("error")),
                Style::default().fg(theme::NEG),
            )),
        },
    };

    let block = Block::default()
        .title(Line::from(Span::styled(
            " orderbook ",
            Style::default().fg(theme::DIM),
        )))
        .title_top(right_title.right_aligned())
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(theme::BORDER_DIM));
    let inner = block.inner(area);
    frame.render_widget(block, area);

    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(2), Constraint::Min(1)])
        .split(inner);

    match app.orderbook.as_ref() {
        None => {
            frame.render_widget(
                Paragraph::new(Span::styled(
                    "book: no poll yet",
                    Style::default().fg(theme::DIM),
                )),
                rows[0],
            );
        }
        Some(snap) => match snap.status {
            OrderbookSide::Error => {
                let reason = snap.error.as_deref().unwrap_or("unknown");
                frame.render_widget(
                    Paragraph::new(Span::styled(
                        format!("no book data (reason: {reason})"),
                        Style::default().fg(theme::DIM),
                    )),
                    rows[0],
                );
            }
            OrderbookSide::Empty => {
                frame.render_widget(
                    Paragraph::new(Span::styled("book empty", Style::default().fg(theme::DIM))),
                    rows[0],
                );
            }
            OrderbookSide::Ok => {
                draw_header_line(frame, rows[0], snap);
                draw_depth_table(frame, rows[1], snap);
            }
        },
    }
}

fn draw_header_line(frame: &mut Frame, area: Rect, snap: &OrderbookSnapshot) {
    let line = Line::from(vec![
        Span::styled(
            format!("bid {:.3}  ", snap.best_bid),
            Style::default().fg(theme::POS),
        ),
        Span::styled(
            format!("ask {:.3}  ", snap.best_ask),
            Style::default().fg(theme::NEG),
        ),
        Span::styled(
            format!("spread {:.3}  mid {:.3}", snap.spread, snap.mid),
            Style::default().fg(theme::DIM),
        ),
    ]);
    frame.render_widget(Paragraph::new(line), area);
}

fn draw_depth_table(frame: &mut Frame, area: Rect, snap: &OrderbookSnapshot) {
    // Adaptive depth: keep the spread row centred, each side gets half.
    let avail = area.height.saturating_sub(1) as usize; // reserve spread row
    let depth = (avail / 2).clamp(1, HARD_DEPTH_CAP);

    let top_asks: Vec<_> = snap.asks.iter().take(depth).copied().collect();
    let top_bids: Vec<_> = snap.bids.iter().take(depth).copied().collect();
    let max_size = top_asks
        .iter()
        .chain(top_bids.iter())
        .map(|(_, s)| *s)
        .fold(0.0_f64, f64::max);
    let max_size = if max_size <= 0.0 { 1.0 } else { max_size };

    let mut rows: Vec<Row> = Vec::with_capacity(depth * 2 + 1);

    // Asks: highest ask rendered first (closest to mid at the bottom of
    // the ask block).
    for (px, sz) in top_asks.into_iter().rev() {
        rows.push(level_row("ASK", px, sz, max_size, theme::NEG));
    }
    rows.push(Row::new([
        Cell::from(Span::styled("---", Style::default().fg(theme::DIM))),
        Cell::from(Span::styled("spread", Style::default().fg(theme::DIM))),
        Cell::from(format!("{:.3}", snap.spread)),
        Cell::from(""),
    ]));
    for (px, sz) in top_bids {
        rows.push(level_row("BID", px, sz, max_size, theme::POS));
    }

    let widths = [
        Constraint::Length(4),  // side
        Constraint::Length(7),  // price
        Constraint::Length(8),  // size
        Constraint::Min(5),     // depth bar
    ];
    let table = Table::new(rows, widths).column_spacing(1);
    frame.render_widget(table, area);
}

fn level_row(
    label: &'static str,
    px: f64,
    sz: f64,
    max_size: f64,
    colour: ratatui::style::Color,
) -> Row<'static> {
    let bar = depth_bar(sz, max_size, BAR_WIDTH);
    Row::new([
        Cell::from(Span::styled(
            label,
            Style::default().fg(colour).add_modifier(Modifier::BOLD),
        )),
        Cell::from(format!("{px:.3}")),
        Cell::from(format!("{sz:.1}")),
        Cell::from(Span::styled(bar, Style::default().fg(colour))),
    ])
}

/// Scaled depth bar using `█`. `BAR_WIDTH` glyphs at size==max, minimum
/// 1 glyph for non-zero sizes so tiny levels still register visually.
pub fn depth_bar(size: f64, max_size: f64, width: usize) -> String {
    if size <= 0.0 || max_size <= 0.0 {
        return String::new();
    }
    let raw = (width as f64) * (size / max_size);
    let n = raw.round().max(1.0) as usize;
    let n = n.min(width);
    "█".repeat(n)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::http::OrderbookSnapshot;
    use ratatui::{backend::TestBackend, Terminal};

    fn render(snap: Option<OrderbookSnapshot>) -> String {
        let mut app = AppState::new();
        app.orderbook = snap;
        let backend = TestBackend::new(60, 25);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| draw_orderbook(frame, frame.area(), &app))
            .unwrap();
        terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect()
    }

    #[test]
    fn no_poll_yet_placeholder() {
        let text = render(None);
        assert!(text.contains("book: no poll yet"));
    }

    #[test]
    fn error_shows_reason() {
        let text = render(Some(OrderbookSnapshot::error("403")));
        assert!(text.contains("no book data (reason: 403)"));
    }

    #[test]
    fn empty_shows_empty() {
        let text = render(Some(OrderbookSnapshot::ok(vec![], vec![])));
        assert!(text.contains("book empty"));
    }

    #[test]
    fn ok_renders_block_char_bars() {
        let snap =
            OrderbookSnapshot::ok(vec![(0.42, 10.0), (0.40, 5.0)], vec![(0.44, 8.0), (0.46, 3.0)]);
        let text = render(Some(snap));
        assert!(text.contains("bid 0.420"), "bid label missing: {text:.200}");
        assert!(text.contains("ask 0.440"));
        assert!(text.contains('█'), "depth bar block chars missing");
        assert!(!text.contains('#'), "ASCII fallback must not appear");
    }

    #[test]
    fn depth_bar_zero_size_is_empty() {
        assert_eq!(depth_bar(0.0, 10.0, 20), "");
    }

    #[test]
    fn depth_bar_full_size_saturates() {
        assert_eq!(depth_bar(10.0, 10.0, 20), "█".repeat(20));
    }

    #[test]
    fn depth_bar_small_nonzero_rounds_to_one() {
        assert_eq!(depth_bar(0.1, 100.0, 20), "█");
    }

    #[test]
    fn depth_bar_proportional() {
        // half size should be roughly half the bar
        let bar = depth_bar(5.0, 10.0, 20);
        assert_eq!(bar.chars().count(), 10);
    }
}
