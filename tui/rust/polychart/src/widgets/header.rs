use ratatui::{
    layout::{Constraint, Direction, Layout, Rect},
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::{Block, BorderType, Borders, Paragraph},
    Frame,
};

use crate::state::AppState;
use crate::theme;
use crate::util::{block_bar_counts, format_chronometer, humanize_slug, now_secs};

/// The right-side status cluster renders at a fixed width so the middle
/// progress bar can absorb every remaining cell. Cluster contents:
///   "  MM:SS / 05:00  binance ●  rtds ●   ok  "
/// Width empirically: 41 cells including leading padding.
const RIGHT_CLUSTER_WIDTH: u16 = 42;

pub fn draw_header(frame: &mut Frame, area: Rect, app: &AppState) {
    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Thick)
        .border_style(Style::default().fg(theme::BORDER_HEADER));
    let inner = block.inner(area);
    frame.render_widget(block, area);

    // Inside the border we have (area.height - 2) rows. Header target is
    // 2 content rows; CSS sets the outer height to 4 (border + 2 + border).
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(1), Constraint::Length(1)])
        .split(inner);

    frame.render_widget(Paragraph::new(row1(app)), rows[0]);
    draw_row2(frame, rows[1], app);
}

/// Row 1: BTC $NN,NNN  sigma X.XX%  strike $NN,NNN
fn row1(app: &AppState) -> Line<'static> {
    let snap = app.snapshot.as_ref();
    let btc = snap.and_then(|s| s.btc_price).unwrap_or(0.0);
    let sigma = snap.and_then(|s| s.sigma).unwrap_or(0.0);
    let strike = snap.and_then(|s| s.strike).unwrap_or(0.0);

    Line::from(vec![
        Span::styled(
            "BTC ",
            Style::default().fg(theme::BTC_LABEL).add_modifier(Modifier::BOLD),
        ),
        Span::styled(
            format!("${:>12}", fmt_thousands(btc, 2)),
            Style::default().add_modifier(Modifier::BOLD),
        ),
        Span::raw("  "),
        Span::styled("sigma ", Style::default().fg(theme::DIM)),
        Span::styled(format!("{:.2}%", sigma * 100.0), Style::default().fg(theme::SIGMA)),
        Span::raw("  "),
        Span::styled("strike ", Style::default().fg(theme::DIM)),
        Span::styled(
            format!("${}", fmt_thousands(strike, 0)),
            Style::default().fg(theme::DIM),
        ),
    ])
}

/// Format a float with comma thousands separators and a fixed number of
/// decimals. Rust's `format!` doesn't take `,` directly; this is the
/// portable workaround.
fn fmt_thousands(v: f64, decimals: usize) -> String {
    let formatted = format!("{v:.decimals$}");
    let (int_part, frac_part) = match formatted.split_once('.') {
        Some((a, b)) => (a.to_string(), format!(".{b}")),
        None => (formatted, String::new()),
    };
    let sign = if int_part.starts_with('-') { "-" } else { "" };
    let digits: String = int_part.trim_start_matches('-').to_string();
    let mut out = String::with_capacity(digits.len() + digits.len() / 3);
    for (i, ch) in digits.chars().rev().enumerate() {
        if i > 0 && i % 3 == 0 {
            out.push(',');
        }
        out.push(ch);
    }
    let int_with_commas: String = out.chars().rev().collect();
    format!("{sign}{int_with_commas}{frac_part}")
}

/// Row 2: humanized title | stretched bar | right-justified cluster.
///
/// Layout math: the title takes whatever width it needs (but capped so
/// the bar always gets at least 10 cells); the right cluster is
/// RIGHT_CLUSTER_WIDTH; the bar fills the rest.
fn draw_row2(frame: &mut Frame, rect: Rect, app: &AppState) {
    let snap = app.snapshot.as_ref();
    let slug = snap.and_then(|s| s.slug.as_deref());
    let t_zero = snap.and_then(|s| s.t_zero);
    let title = humanize_slug(slug, t_zero);

    // Minimum title width so the text doesn't get cropped on narrow
    // terminals; cap so the bar always has room.
    let title_width = (title.chars().count() as u16).min(rect.width / 3).max(14);
    let right_width = RIGHT_CLUSTER_WIDTH.min(rect.width.saturating_sub(title_width + 4));
    let bar_width = rect.width.saturating_sub(title_width + right_width);

    let cols = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Length(title_width),
            Constraint::Length(bar_width),
            Constraint::Length(right_width),
        ])
        .split(rect);

    // Title
    frame.render_widget(
        Paragraph::new(Line::from(Span::styled(
            title,
            Style::default().fg(theme::SLUG),
        ))),
        cols[0],
    );

    // Stretched bar. Reserve 2 cells for the "[" "]" wrap, the rest is blocks.
    let (_, fraction) = format_chronometer(t_zero, now_secs());
    let inner_bar = cols[1].width.saturating_sub(3) as usize; // "[" + bar + "] "
    let (filled, empty) = block_bar_counts(inner_bar, fraction);
    let bar_line = Line::from(vec![
        Span::styled("[", Style::default().fg(theme::DIM)),
        Span::styled("█".repeat(filled), Style::default().fg(theme::DIM)),
        Span::styled("░".repeat(empty), Style::default().fg(theme::DIM)),
        Span::styled("] ", Style::default().fg(theme::DIM)),
    ]);
    frame.render_widget(Paragraph::new(bar_line), cols[1]);

    // Right cluster
    let (chrono_str, _) = format_chronometer(t_zero, now_secs());
    let connections = snap.map(|s| s.connections.clone()).unwrap_or_default();
    let kill = app.kill_active;

    let mut cluster: Vec<Span> = Vec::new();
    cluster.push(Span::styled(chrono_str, Style::default().fg(theme::BTC_LABEL)));
    cluster.push(Span::raw("  "));
    cluster.push(Span::styled("binance ", Style::default().fg(theme::DIM)));
    cluster.push(Span::styled(
        "●",
        Style::default().fg(if connections.binance {
            theme::PILL_ON
        } else {
            theme::PILL_OFF
        }),
    ));
    cluster.push(Span::raw("  "));
    cluster.push(Span::styled("rtds ", Style::default().fg(theme::DIM)));
    cluster.push(Span::styled(
        "●",
        Style::default().fg(if connections.rtds {
            theme::PILL_ON
        } else {
            theme::PILL_OFF
        }),
    ));
    cluster.push(Span::raw("  "));
    if kill {
        cluster.push(Span::styled(
            " KILL ",
            Style::default()
                .fg(theme::KILL_FG)
                .bg(theme::KILL_BG)
                .add_modifier(Modifier::BOLD),
        ));
    } else {
        cluster.push(Span::styled(
            "  ok  ",
            Style::default().fg(theme::OK_FG).add_modifier(Modifier::DIM),
        ));
    }

    frame.render_widget(
        Paragraph::new(Line::from(cluster)).alignment(ratatui::layout::Alignment::Right),
        cols[2],
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state_reader::{Connections, StateSnapshot};
    use ratatui::{backend::TestBackend, Terminal};

    fn fake_state(slug: &str, t_zero: i64) -> AppState {
        AppState {
            snapshot: Some(StateSnapshot {
                slug: Some(slug.to_string()),
                t_zero: Some(t_zero),
                btc_price: Some(77_856.42),
                sigma: Some(0.0003),
                strike: Some(77_800.0),
                connections: Connections {
                    binance: true,
                    rtds: true,
                },
                ..Default::default()
            }),
            ..Default::default()
        }
    }

    #[test]
    fn header_renders_both_rows() {
        let app = fake_state("btc-updown-5m-1776921900", 1776921900);
        let backend = TestBackend::new(180, 4);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| draw_header(frame, frame.area(), &app))
            .unwrap();
        let text: String = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect();
        assert!(text.contains("BTC "), "BTC label missing");
        assert!(text.contains("sigma "), "sigma label missing");
        assert!(text.contains("strike "), "strike label missing");
        assert!(text.contains("BTC 5min "), "humanized title missing");
        assert!(text.contains("/ 05:00"), "chronometer suffix missing");
        assert!(text.contains("binance"), "binance pill missing");
        assert!(text.contains("rtds"), "rtds pill missing");
        assert!(text.contains("ok"), "ok status missing");
    }

    #[test]
    fn header_with_kill_shows_magenta_label() {
        let mut app = fake_state("btc-updown-5m-1776921900", 1776921900);
        app.kill_active = true;
        let backend = TestBackend::new(180, 4);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| draw_header(frame, frame.area(), &app))
            .unwrap();
        let text: String = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect();
        assert!(text.contains("KILL"), "KILL label missing when active");
    }

    #[test]
    fn header_waiting_when_state_empty() {
        let app = AppState::new();
        let backend = TestBackend::new(180, 4);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| draw_header(frame, frame.area(), &app))
            .unwrap();
        let text: String = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect();
        assert!(text.contains("waiting for market"));
    }

    #[test]
    fn header_progress_bar_stretches_with_width() {
        let app = fake_state("btc-updown-5m-1776921900", 1776921900);
        let wide = count_bar_chars(&app, 240);
        let narrow = count_bar_chars(&app, 120);
        assert!(
            wide > narrow,
            "progress bar at 240 cols ({wide}) should exceed 120 cols ({narrow})"
        );
    }

    fn count_bar_chars(app: &AppState, width: u16) -> usize {
        let backend = TestBackend::new(width, 4);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| draw_header(frame, frame.area(), app))
            .unwrap();
        let text: String = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect();
        text.chars().filter(|&c| c == '█' || c == '░').count()
    }
}
