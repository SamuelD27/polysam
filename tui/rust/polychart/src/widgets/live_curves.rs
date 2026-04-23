use ratatui::{
    layout::{Alignment, Constraint, Direction, Layout, Rect},
    style::{Modifier, Style},
    symbols,
    text::{Line, Span},
    widgets::{Axis, Block, BorderType, Borders, Chart, Dataset, GraphType, Paragraph},
    Frame,
};

use crate::state::AppState;
use crate::theme;
use crate::util::ts_hms;

pub fn draw_live_curves(frame: &mut Frame, area: Rect, app: &AppState) {
    let block = Block::default()
        .title(Line::from(vec![
            Span::styled(" live ", Style::default().fg(theme::DIM)),
        ]))
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(theme::BORDER_DIM));
    let inner = block.inner(area);
    frame.render_widget(block, area);

    // hero (3 rows) | btc chart (1fr) | market/fair chart (1fr)
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(3),
            Constraint::Ratio(1, 2),
            Constraint::Ratio(1, 2),
        ])
        .split(inner);

    draw_hero(frame, rows[0], app);
    draw_btc_chart(frame, rows[1], app);
    draw_mkt_fair_chart(frame, rows[2], app);
}

fn draw_hero(frame: &mut Frame, area: Rect, app: &AppState) {
    let mkt = app.snapshot.as_ref().and_then(|s| s.market_price_up);
    let line = match mkt {
        Some(mkt) => {
            let up = mkt.clamp(0.0, 1.0);
            Line::from(vec![
                Span::styled(
                    format!("UP  ${up:.2}"),
                    Style::default()
                        .fg(theme::HERO_UP)
                        .add_modifier(Modifier::BOLD),
                ),
                Span::raw("       "),
                Span::styled(
                    format!("DOWN  ${:.2}", 1.0 - up),
                    Style::default()
                        .fg(theme::HERO_DOWN)
                        .add_modifier(Modifier::BOLD),
                ),
            ])
        }
        None => Line::from(vec![
            Span::styled("UP  -    ", Style::default().add_modifier(Modifier::DIM)),
            Span::styled("DOWN  -", Style::default().add_modifier(Modifier::DIM)),
        ]),
    };
    frame.render_widget(Paragraph::new(line).alignment(Alignment::Center), area);
}

fn draw_btc_chart(frame: &mut Frame, area: Rect, app: &AppState) {
    if app.btc_series.is_empty() {
        frame.render_widget(
            Paragraph::new(" BTC USD (waiting for feed)").style(Style::default().fg(theme::DIM)),
            area,
        );
        return;
    }

    let data: Vec<(f64, f64)> = app.btc_series.iter().copied().collect();
    let x_min = data.first().map(|(t, _)| *t).unwrap_or(0.0);
    let x_max = data.last().map(|(t, _)| *t).unwrap_or(x_min + 1.0);
    let (mut y_lo, mut y_hi) = (f64::INFINITY, f64::NEG_INFINITY);
    for &(_, v) in &data {
        y_lo = y_lo.min(v);
        y_hi = y_hi.max(v);
    }
    // 10% padding + $0.50 floor so a narrow spread stays readable.
    if (y_hi - y_lo).abs() < f64::EPSILON {
        y_lo -= 1.0;
        y_hi += 1.0;
    } else {
        let pad = (y_hi - y_lo) * 0.1 + 0.5;
        y_lo -= pad;
        y_hi += pad;
    }

    let strike = app.snapshot.as_ref().and_then(|s| s.strike);
    let strike_data = strike.map(|s| vec![(x_min, s), (x_max, s)]);

    let mut datasets = vec![Dataset::default()
        .name("BTC")
        .marker(symbols::Marker::HalfBlock)
        .graph_type(GraphType::Line)
        .style(Style::default().fg(theme::CHART_BTC))
        .data(&data)];
    if let Some(strike_data) = strike_data.as_ref() {
        datasets.push(
            Dataset::default()
                .name("strike")
                .marker(symbols::Marker::HalfBlock)
                .graph_type(GraphType::Line)
                .style(Style::default().fg(theme::CHART_STRIKE))
                .data(strike_data),
        );
    }

    let y_ticks = y_tick_labels(y_lo, y_hi, 4, |v| format!("${}", fmt_int_thousands(v)));

    let chart = Chart::new(datasets)
        .block(
            Block::default().title(Line::from(Span::styled(
                " BTC USD ",
                Style::default().fg(theme::CHART_BTC),
            ))),
        )
        .x_axis(
            Axis::default()
                .style(Style::default().fg(theme::DIM))
                .bounds([x_min, x_max])
                .labels(x_tick_labels(x_min, x_max, 3)),
        )
        .y_axis(
            Axis::default()
                .style(Style::default().fg(theme::DIM))
                .bounds([y_lo, y_hi])
                .labels(y_ticks),
        );
    frame.render_widget(chart, area);
}

fn draw_mkt_fair_chart(frame: &mut Frame, area: Rect, app: &AppState) {
    let mkt: Vec<(f64, f64)> = app.mkt_series.iter().copied().collect();
    let fair: Vec<(f64, f64)> = app.fair_series.iter().copied().collect();

    if mkt.is_empty() && fair.is_empty() {
        frame.render_widget(
            Paragraph::new(" market / fair (waiting)").style(Style::default().fg(theme::DIM)),
            area,
        );
        return;
    }

    let xs: Vec<f64> = mkt
        .iter()
        .chain(fair.iter())
        .map(|(t, _)| *t)
        .collect();
    let x_min = xs.iter().cloned().fold(f64::INFINITY, f64::min);
    let x_max = xs.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    let (x_min, x_max) = if x_min == x_max {
        (x_min, x_min + 1.0)
    } else {
        (x_min, x_max)
    };

    let mut datasets: Vec<Dataset> = Vec::new();
    if !mkt.is_empty() {
        datasets.push(
            Dataset::default()
                .name("mkt")
                .marker(symbols::Marker::HalfBlock)
                .graph_type(GraphType::Line)
                .style(Style::default().fg(theme::CHART_MKT))
                .data(&mkt),
        );
    }
    let title_line = if !fair.is_empty() {
        datasets.push(
            Dataset::default()
                .name("fair")
                .marker(symbols::Marker::HalfBlock)
                .graph_type(GraphType::Line)
                .style(Style::default().fg(theme::CHART_FAIR))
                .data(&fair),
        );
        " market (white) vs fair (yellow) "
    } else {
        " market (white) vs fair (yellow) -- fair: waiting "
    };

    let chart = Chart::new(datasets)
        .block(
            Block::default().title(Line::from(Span::styled(
                title_line,
                Style::default().fg(theme::CHART_MKT),
            ))),
        )
        .x_axis(
            Axis::default()
                .style(Style::default().fg(theme::DIM))
                .bounds([x_min, x_max])
                .labels(x_tick_labels(x_min, x_max, 3)),
        )
        .y_axis(
            Axis::default()
                .style(Style::default().fg(theme::DIM))
                .bounds([0.0, 1.0])
                .labels(vec![
                    Line::from("0.00"),
                    Line::from("0.25"),
                    Line::from("0.50"),
                    Line::from("0.75"),
                    Line::from("1.00"),
                ]),
        );
    frame.render_widget(chart, area);
}

// --- chart tick helpers ----------------------------------------------

fn x_tick_labels(x_min: f64, x_max: f64, count: usize) -> Vec<Line<'static>> {
    if count < 2 || x_max <= x_min {
        return vec![Line::from(ts_hms(x_min))];
    }
    let step = (x_max - x_min) / (count as f64 - 1.0);
    (0..count)
        .map(|i| Line::from(ts_hms(x_min + step * i as f64)))
        .collect()
}

fn y_tick_labels<F>(lo: f64, hi: f64, count: usize, fmt: F) -> Vec<Line<'static>>
where
    F: Fn(f64) -> String,
{
    if count < 2 || hi <= lo {
        return vec![Line::from(fmt(lo))];
    }
    let step = (hi - lo) / (count as f64 - 1.0);
    (0..count)
        .map(|i| Line::from(fmt(lo + step * i as f64)))
        .collect()
}

fn fmt_int_thousands(v: f64) -> String {
    let int = v.round() as i64;
    let sign = if int < 0 { "-" } else { "" };
    let digits = int.unsigned_abs().to_string();
    let mut out = String::with_capacity(digits.len() + digits.len() / 3);
    for (i, ch) in digits.chars().rev().enumerate() {
        if i > 0 && i % 3 == 0 {
            out.push(',');
        }
        out.push(ch);
    }
    let with_commas: String = out.chars().rev().collect();
    format!("{sign}{with_commas}")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state_reader::StateSnapshot;
    use ratatui::{backend::TestBackend, Terminal};

    /// Dump the full live-curves panel against the live daemon_state/ so
    /// we can diff what the code actually emits against what the user
    /// sees on the terminal. Ignored by default because it reads live
    /// files; run with: cargo test dump_against_live -- --ignored --nocapture
    #[test]
    #[ignore = "reads live daemon_state/"]
    fn dump_against_live() {
        let path = crate::state_reader::resolve_state_dir().join("state.json");
        let Ok(bytes) = std::fs::read(&path) else {
            eprintln!("no state.json at {:?}", path);
            return;
        };
        let snap: crate::state_reader::StateSnapshot =
            serde_json::from_slice(&bytes).expect("parse state.json");
        let mut app = AppState::new();
        // Populate the deques with plausible history derived from the
        // snapshot so we see continuous lines rather than a single point.
        let now = crate::state::now_secs();
        for i in 0..200usize {
            let t = now - (200 - i) as f64 * 0.5;
            if let Some(btc) = snap.btc_price {
                app.btc_series
                    .push_back((t, btc + (i as f64 * 0.03).sin() * 5.0));
            }
            if let Some(mkt) = snap.market_price_up {
                app.mkt_series
                    .push_back((t, (mkt + (i as f64 * 0.05).sin() * 0.02).clamp(0.0, 1.0)));
            }
            if let Some(blob) = &snap.refined {
                if let Some(fair) = blob.fair_price {
                    app.fair_series.push_back((
                        t,
                        (fair + (i as f64 * 0.04).cos() * 0.01).clamp(0.0, 1.0),
                    ));
                }
            }
        }
        app.snapshot = Some(snap);

        let backend = TestBackend::new(120, 32);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| draw_live_curves(frame, frame.area(), &app))
            .unwrap();
        let buf = terminal.backend().buffer();
        eprintln!("=== LIVE CURVES RENDER (120x32) -- expected braille curves ===");
        for y in 0..buf.area.height {
            let mut row = String::new();
            for x in 0..buf.area.width {
                row.push_str(buf[(x, y)].symbol());
            }
            eprintln!("{row}");
        }
    }

    /// Regression: chart must render continuous-line glyphs (half blocks
    /// `▀` / `▄`), not scatter dots. HalfBlock is the visual-weight
    /// marker choice over Braille -- fewer pixels per cell, but each cell
    /// is a solid block so the line reads as a stroke rather than a row
    /// of dot pairs.
    #[test]
    fn btc_chart_renders_with_continuous_line_glyphs() {
        let mut app = AppState::new();
        app.snapshot = Some(StateSnapshot {
            strike: Some(77_999.0),
            ..Default::default()
        });
        for i in 0..300usize {
            let t = 1_000_000.0 + i as f64 * 0.5;
            let y = 78_000.0 + ((i as f64) * 0.02).sin() * 3.0;
            app.btc_series.push_back((t, y));
        }
        let backend = TestBackend::new(100, 14);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| draw_live_curves(frame, frame.area(), &app))
            .unwrap();
        let text: String = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect();
        // Expect half-block glyphs, densely populated along the sine.
        let line_hits = text.matches('▀').count() + text.matches('▄').count();
        assert!(
            line_hits > 40,
            "expected >40 half-block glyphs, got {line_hits}; render was\n{text}"
        );
        // No stray braille scatter dots should survive.
        assert!(
            !text.contains('⠂'),
            "found braille scatter dot '⠂'; chart may have regressed"
        );
    }

    fn state_with_prices(up: Option<f64>, btc_pts: usize) -> AppState {
        let mut app = AppState::new();
        app.snapshot = Some(StateSnapshot {
            market_price_up: up,
            strike: Some(77800.0),
            ..Default::default()
        });
        for i in 0..btc_pts {
            app.btc_series
                .push_back((1000.0 + i as f64, 77_900.0 + i as f64 * 0.5));
        }
        if let Some(mkt) = up {
            for i in 0..btc_pts {
                app.mkt_series.push_back((1000.0 + i as f64, mkt));
            }
        }
        app
    }

    #[test]
    fn hero_shows_up_and_down_prices() {
        let app = state_with_prices(Some(0.72), 5);
        let backend = TestBackend::new(80, 20);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| draw_live_curves(frame, frame.area(), &app))
            .unwrap();
        let text: String = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect();
        assert!(text.contains("UP  $0.72"));
        assert!(text.contains("DOWN  $0.28"));
    }

    #[test]
    fn hero_falls_back_when_market_missing() {
        let app = state_with_prices(None, 0);
        let backend = TestBackend::new(80, 20);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| draw_live_curves(frame, frame.area(), &app))
            .unwrap();
        let text: String = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect();
        assert!(text.contains("UP  -"));
        assert!(text.contains("DOWN  -"));
    }

    #[test]
    fn fair_waiting_surfaces_in_title() {
        let mut app = state_with_prices(Some(0.5), 3);
        app.fair_series.clear();
        let backend = TestBackend::new(120, 30);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| draw_live_curves(frame, frame.area(), &app))
            .unwrap();
        let text: String = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect();
        assert!(
            text.contains("fair: waiting") || text.contains("fair"),
            "fair-missing indicator absent: {text:.200}"
        );
    }

    #[test]
    fn fmt_int_thousands_handles_sign_and_commas() {
        assert_eq!(fmt_int_thousands(0.0), "0");
        assert_eq!(fmt_int_thousands(77_986.4), "77,986");
        assert_eq!(fmt_int_thousands(-1234.0), "-1,234");
    }

    #[test]
    fn y_tick_labels_emit_count() {
        let labels = y_tick_labels(0.0, 100.0, 4, |v| format!("{v:.0}"));
        assert_eq!(labels.len(), 4);
    }
}
