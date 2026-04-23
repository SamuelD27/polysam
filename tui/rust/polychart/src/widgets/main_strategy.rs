//! Refined-strategy panel: the tall one on the middle right.
//!
//! Row split inside the panel:
//!   - headline (2 rows): market title + stats line
//!   - secondary (1 row): fair / mkt / edge / TP-SL-Res counters
//!   - PnL chart (fills up to the available remainder -- at least 40%
//!     of the panel per README bug 5)
//!   - position block (3 rows, "no position" when flat)
//!   - recent trades table (fills the rest)

use ratatui::{
    layout::{Constraint, Direction, Layout, Rect},
    style::{Modifier, Style},
    symbols,
    text::{Line, Span},
    widgets::{
        Axis, Block, BorderType, Borders, Cell, Chart, Dataset, GraphType, Paragraph, Row, Table,
    },
    Frame,
};

use crate::state::{AppState, PNL_VISIBLE_WINDOW};
use crate::state_reader::{Position, Stats, StrategyBlob, Trade};
use crate::theme;
use crate::util::{humanize_slug, ts_hms};

pub fn draw_main_strategy(frame: &mut Frame, area: Rect, app: &AppState) {
    // Right-side status: refined session PnL so the header uses the full
    // width with meaningful content.
    let refined_pnl = app
        .snapshot
        .as_ref()
        .and_then(|s| s.refined.as_ref())
        .map(|b| b.stats.total_pnl)
        .unwrap_or(0.0);
    let right_title = Line::from(Span::styled(
        format!(" PnL {refined_pnl:+.2} "),
        sign_style(refined_pnl),
    ))
    .right_aligned();

    let block = Block::default()
        .title(Line::from(Span::styled(
            " refined ",
            Style::default()
                .fg(theme::BORDER_MAIN)
                .add_modifier(Modifier::BOLD),
        )))
        .title_top(right_title)
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(theme::BORDER_MAIN));
    let inner = block.inner(area);
    frame.render_widget(block, area);

    let blob = app.snapshot.as_ref().and_then(|s| s.refined.as_ref());
    let stats = blob.map(|b| &b.stats).cloned().unwrap_or_default();
    let fair = blob.and_then(|b| b.fair_price);
    let mkt = app.snapshot.as_ref().and_then(|s| s.market_price_up);
    let slug = app.snapshot.as_ref().and_then(|s| s.slug.as_deref());
    let t_zero = app.snapshot.as_ref().and_then(|s| s.t_zero);
    let pos = blob.and_then(|b| b.open_position.as_ref());
    let trades = blob.map(|b| b.closed_trades.as_slice()).unwrap_or(&[]);

    // PnL chart gets at least 40% of the panel's vertical space (README
    // bug 5: "Aim for the chart to occupy at least 40% of the panel's
    // vertical space"). Remaining fixed rows: headline(2)+secondary(1)
    // +position(3) = 6. Trades table gets whatever remains.
    let fixed = 2 + 1 + 3;
    let h = inner.height as usize;
    let chart_height = ((h * 2) / 5).max(6); // 40% minimum, at least 6 rows
    let chart_height = chart_height.min(h.saturating_sub(fixed + 3)); // leave 3+ rows for trades
    let chart_height = chart_height.max(5);

    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(2),                       // headline
            Constraint::Length(1),                       // secondary
            Constraint::Length(chart_height as u16),     // PnL chart
            Constraint::Length(3),                       // position block
            Constraint::Min(1),                          // trades
        ])
        .split(inner);

    draw_headline(frame, rows[0], slug, t_zero, &stats);
    draw_secondary(frame, rows[1], fair, mkt, blob);
    draw_pnl_chart(frame, rows[2], app);
    draw_position(frame, rows[3], pos, mkt);
    draw_trades(frame, rows[4], trades);
}

fn draw_headline(
    frame: &mut Frame,
    area: Rect,
    slug: Option<&str>,
    t_zero: Option<i64>,
    stats: &Stats,
) {
    let market = humanize_slug(slug, t_zero);
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(1), Constraint::Length(1)])
        .split(area);

    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled("Market: ", Style::default().fg(theme::DIM)),
            Span::styled(
                market,
                Style::default()
                    .fg(theme::SLUG)
                    .add_modifier(Modifier::BOLD),
            ),
        ])),
        rows[0],
    );

    let pnl_style = sign_style(stats.total_pnl);
    let roi_style = sign_style(stats.roi());
    let streak_style = match stats.streak_type.as_deref() {
        Some("W") => Style::default().fg(theme::POS),
        Some("L") => Style::default().fg(theme::NEG),
        _ => Style::default().fg(theme::DIM),
    };

    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled(
                format!("PnL {:+.2}  ", stats.total_pnl),
                pnl_style.add_modifier(Modifier::BOLD),
            ),
            Span::styled(format!("ROI {:+.1}%  ", stats.roi()), roi_style),
            Span::styled(
                format!("{} tr  ", stats.total_trades),
                Style::default().add_modifier(Modifier::BOLD),
            ),
            Span::styled(
                format!(
                    "W={} L={} {:.0}%  ",
                    stats.wins,
                    stats.losses,
                    stats.win_rate()
                ),
                Style::default().fg(theme::DIM),
            ),
            Span::styled(
                format!("DD ${:.2}  ", stats.max_drawdown),
                Style::default().fg(theme::NEG),
            ),
            Span::styled(
                format!(
                    "streak {} {}",
                    stats.current_streak,
                    stats.streak_type.as_deref().unwrap_or("-")
                ),
                streak_style,
            ),
        ])),
        rows[1],
    );
}

fn draw_secondary(
    frame: &mut Frame,
    area: Rect,
    fair: Option<f64>,
    mkt: Option<f64>,
    blob: Option<&StrategyBlob>,
) {
    let (tp, sl, res) = blob
        .and_then(|b| b.extra.as_ref())
        .map(extract_exit_counts)
        .unwrap_or((0, 0, 0));

    let mut spans = vec![match fair {
        Some(f) => Span::styled(format!("fair {f:.3}  "), Style::default().fg(theme::CHART_FAIR)),
        None => Span::styled("fair -    ", Style::default().fg(theme::CHART_FAIR)),
    }];
    spans.push(match mkt {
        Some(m) => Span::styled(format!("mkt {m:.3}  "), Style::default().fg(theme::CHART_MKT)),
        None => Span::styled("mkt -    ", Style::default().fg(theme::CHART_MKT)),
    });
    if let (Some(f), Some(m)) = (fair, mkt) {
        let edge = f - m;
        let edge_style = if edge.abs() >= 0.1 {
            Style::default().fg(theme::POS)
        } else {
            Style::default().fg(theme::DIM)
        };
        spans.push(Span::styled(format!("edge {edge:+.3}  "), edge_style));
        let (side, color) = if edge > 0.0 {
            ("Up", theme::POS)
        } else if edge < 0.0 {
            ("Down", theme::NEG)
        } else {
            ("-", theme::DIM)
        };
        spans.push(Span::styled(
            format!("side {side}  "),
            Style::default().fg(color),
        ));
    }
    spans.push(Span::styled(
        format!("TP/SL/Res {tp}/{sl}/{res}"),
        Style::default().fg(theme::CHART_MKT),
    ));
    frame.render_widget(Paragraph::new(Line::from(spans)), area);
}

fn extract_exit_counts(extra: &serde_json::Value) -> (i64, i64, i64) {
    let tp = extra.get("tp_count").and_then(|v| v.as_i64()).unwrap_or(0);
    let sl = extra.get("sl_count").and_then(|v| v.as_i64()).unwrap_or(0);
    let res = extra
        .get("resolution_count")
        .and_then(|v| v.as_i64())
        .unwrap_or(0);
    (tp, sl, res)
}

fn draw_pnl_chart(frame: &mut Frame, area: Rect, app: &AppState) {
    let series = app
        .pnl_series
        .get("refined")
        .cloned()
        .unwrap_or_default();
    if series.is_empty() {
        frame.render_widget(
            Paragraph::new(" refined PnL  (no trades)").style(Style::default().fg(theme::DIM)),
            area,
        );
        return;
    }

    let visible: Vec<(f64, f64)> = series
        .iter()
        .rev()
        .take(PNL_VISIBLE_WINDOW)
        .copied()
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect();
    let session_total = series.last().map(|(_, v)| *v).unwrap_or(0.0);
    let (x_min, x_max) = (
        visible.first().map(|(t, _)| *t).unwrap_or(0.0),
        visible.last().map(|(t, _)| *t).unwrap_or(1.0),
    );
    let (x_min, x_max) = if x_min == x_max {
        (x_min, x_min + 1.0)
    } else {
        (x_min, x_max)
    };

    let (mut y_lo, mut y_hi) = (f64::INFINITY, f64::NEG_INFINITY);
    for &(_, v) in &visible {
        y_lo = y_lo.min(v);
        y_hi = y_hi.max(v);
    }
    if (y_hi - y_lo).abs() < f64::EPSILON {
        y_lo -= 0.5;
        y_hi += 0.5;
    } else {
        let pad = (y_hi - y_lo) * 0.1;
        y_lo -= pad;
        y_hi += pad;
    }

    let colour = if session_total > 0.0 {
        theme::POS
    } else if session_total < 0.0 {
        theme::NEG
    } else {
        theme::CHART_MKT
    };

    let title = format!(
        " refined PnL  session ${session_total:+.2}  (viewing last {}) ",
        visible.len()
    );
    let datasets = vec![Dataset::default()
        .name("pnl")
        .marker(symbols::Marker::HalfBlock)
        .graph_type(GraphType::Line)
        .style(Style::default().fg(colour))
        .data(&visible)];

    let chart = Chart::new(datasets)
        .block(
            Block::default().title(Line::from(Span::styled(
                title,
                Style::default().fg(colour),
            ))),
        )
        .x_axis(
            Axis::default()
                .style(Style::default().fg(theme::DIM))
                .bounds([x_min, x_max])
                .labels(vec![
                    Line::from(ts_hms(x_min)),
                    Line::from(ts_hms((x_min + x_max) / 2.0)),
                    Line::from(ts_hms(x_max)),
                ]),
        )
        .y_axis(
            Axis::default()
                .style(Style::default().fg(theme::DIM))
                .bounds([y_lo, y_hi])
                .labels(vec![
                    Line::from(format!("${y_lo:+.2}")),
                    Line::from(format!("${y_hi:+.2}")),
                ]),
        );
    frame.render_widget(chart, area);
}

fn draw_position(frame: &mut Frame, area: Rect, pos: Option<&Position>, mkt: Option<f64>) {
    let block = Block::default()
        .title(Line::from(Span::styled(
            " position ",
            Style::default().fg(theme::DIM),
        )))
        .borders(Borders::TOP);
    let inner = block.inner(area);
    frame.render_widget(block, area);

    let Some(pos) = pos else {
        frame.render_widget(
            Paragraph::new(Span::styled(
                "no position",
                Style::default().fg(theme::DIM).add_modifier(Modifier::DIM),
            )),
            inner,
        );
        return;
    };
    let side_style = if pos.side == "Up" {
        Style::default().fg(theme::POS).add_modifier(Modifier::BOLD)
    } else {
        Style::default().fg(theme::NEG).add_modifier(Modifier::BOLD)
    };
    let mut spans = vec![
        Span::styled("OPEN ", Style::default().fg(theme::SLUG).add_modifier(Modifier::BOLD)),
        Span::styled(format!("{}  ", pos.side), side_style),
        Span::styled(format!("entry {:.3}  ", pos.entry_price), Style::default().fg(theme::CHART_MKT)),
        Span::styled(format!("size ${:.2}  ", pos.size_usdc), Style::default().fg(theme::CHART_MKT)),
        Span::styled(format!("edge {:.3}", pos.edge), Style::default().fg(theme::CHART_BTC)),
    ];
    if let Some(mkt) = mkt {
        let realizable = if pos.side == "Up" { mkt } else { 1.0 - mkt };
        let favor = realizable - pos.entry_price;
        spans.push(Span::raw("  "));
        spans.push(Span::styled(
            format!("realizable {realizable:.3}  favor {favor:+.3}"),
            sign_style(favor),
        ));
    }
    frame.render_widget(Paragraph::new(Line::from(spans)), inner);
}

fn draw_trades(frame: &mut Frame, area: Rect, trades: &[Trade]) {
    let header = Row::new([
        Cell::from("t"),
        Cell::from("side"),
        Cell::from("in->out"),
        Cell::from("size"),
        Cell::from("pnl"),
        Cell::from("ROI"),
        Cell::from("why"),
        Cell::from("hold"),
    ])
    .style(Style::default().fg(theme::DIM));

    const RECENT_TRADES_CAP: usize = 5;
    let rows: Vec<Row> = trades
        .iter()
        .rev()
        .take(RECENT_TRADES_CAP)
        .map(|t| {
            let pnl = t.pnl;
            let roi = if t.size_usdc > 0.0 {
                pnl / t.size_usdc * 100.0
            } else {
                0.0
            };
            let hold = t
                .hold_time_s
                .map(|h| format!("{h:.0}s"))
                .unwrap_or_else(|| "-".into());
            let t_label = t
                .resolved_time
                .map(|ts| ts_hms(ts).chars().take(5).collect::<String>())
                .unwrap_or_else(|| "--:--".into());
            let side_style = if t.side == "Up" {
                Style::default().fg(theme::POS)
            } else {
                Style::default().fg(theme::NEG)
            };
            Row::new([
                Cell::from(t_label).style(Style::default().fg(theme::DIM)),
                Cell::from(t.side.clone()).style(side_style),
                Cell::from(format!("{:.3}->{:.3}", t.entry_price, t.exit_price)),
                Cell::from(format!("${:.0}", t.size_usdc)),
                Cell::from(format!("{pnl:+.2}")).style(sign_style(pnl)),
                Cell::from(format!("{roi:+.1}%")).style(sign_style(roi)),
                Cell::from(t.exit_type.clone().unwrap_or_default()),
                Cell::from(hold),
            ])
        })
        .collect();

    let widths = [
        Constraint::Length(6),
        Constraint::Length(5),
        Constraint::Length(14),
        Constraint::Length(7),
        Constraint::Length(9),
        Constraint::Length(8),
        Constraint::Length(6),
        Constraint::Length(6),
    ];
    let table = Table::new(rows, widths)
        .header(header)
        .block(Block::default().title(Line::from(Span::styled(
            " recent trades ",
            Style::default().fg(theme::DIM),
        ))))
        .column_spacing(1);
    frame.render_widget(table, area);
}

fn sign_style(v: f64) -> Style {
    if v > 0.0 {
        Style::default().fg(theme::POS)
    } else if v < 0.0 {
        Style::default().fg(theme::NEG)
    } else {
        Style::default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state_reader::{StateSnapshot, Stats, StrategyBlob};
    use ratatui::{backend::TestBackend, Terminal};

    fn render(app: &AppState) -> String {
        let backend = TestBackend::new(120, 30);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| draw_main_strategy(frame, frame.area(), app))
            .unwrap();
        terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect::<String>()
    }

    fn state(blob: Option<StrategyBlob>) -> AppState {
        AppState {
            snapshot: Some(StateSnapshot {
                slug: Some("btc-updown-5m-1776921900".into()),
                t_zero: Some(1776921900),
                market_price_up: Some(0.52),
                refined: blob,
                ..Default::default()
            }),
            ..Default::default()
        }
    }

    #[test]
    fn main_renders_without_state() {
        let app = AppState::new();
        let _ = render(&app);
    }

    #[test]
    fn main_renders_stats_line() {
        let app = state(Some(StrategyBlob {
            fair_price: Some(0.61),
            stats: Stats {
                total_trades: 10,
                wins: 7,
                losses: 3,
                total_pnl: 4.25,
                total_risked: 50.0,
                current_streak: 2,
                streak_type: Some("W".into()),
                ..Default::default()
            },
            ..Default::default()
        }));
        let text = render(&app);
        assert!(text.contains("PnL +4.25"));
        assert!(text.contains("10 tr"));
        assert!(text.contains("W=7 L=3"));
        assert!(text.contains("fair 0.610"));
        assert!(text.contains("mkt 0.520"));
    }

    #[test]
    fn main_pnl_chart_shows_session_total() {
        let mut app = state(Some(StrategyBlob {
            stats: Stats {
                total_pnl: 3.5,
                total_trades: 2,
                ..Default::default()
            },
            ..Default::default()
        }));
        app.pnl_series
            .insert("refined".into(), vec![(1.0, 1.5), (2.0, 3.5)]);
        let text = render(&app);
        assert!(
            text.contains("session $+3.50"),
            "session total missing: {text:.200}"
        );
    }

    #[test]
    fn main_handles_no_pnl_series() {
        let app = state(Some(StrategyBlob::default()));
        let text = render(&app);
        assert!(text.contains("no trades"));
    }
}
