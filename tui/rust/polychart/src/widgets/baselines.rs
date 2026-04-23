use ratatui::{
    layout::{Constraint, Direction, Layout, Rect},
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::{Block, BorderType, Borders, Paragraph},
    Frame,
};

use crate::state::AppState;
use crate::state_reader::Stats;
use crate::theme;

pub fn draw_baselines(frame: &mut Frame, area: Rect, app: &AppState) {
    let block = Block::default()
        .title(Line::from(Span::styled(
            " baselines ",
            Style::default().fg(theme::DIM),
        )))
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(theme::BORDER_DIM));
    let inner = block.inner(area);
    frame.render_widget(block, area);

    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(1), Constraint::Length(1)])
        .split(inner);

    let base = app.snapshot.as_ref().and_then(|s| s.base.as_ref());
    let enhanced = app.snapshot.as_ref().and_then(|s| s.enhanced.as_ref());

    frame.render_widget(
        Paragraph::new(strategy_line("BASE    ", base.map(|b| &b.stats))),
        rows[0],
    );
    frame.render_widget(
        Paragraph::new(strategy_line("ENHANCED", enhanced.map(|b| &b.stats))),
        rows[1],
    );
}

fn strategy_line(label: &'static str, stats: Option<&Stats>) -> Line<'static> {
    let default = Stats::default();
    let s = stats.unwrap_or(&default);
    let pnl_style = sign_style(s.total_pnl);
    let roi_style = sign_style(s.roi());
    Line::from(vec![
        Span::styled(
            format!("{label}  "),
            Style::default().add_modifier(Modifier::BOLD),
        ),
        Span::styled(
            format!("PnL {:+.2}  ", s.total_pnl),
            pnl_style,
        ),
        Span::styled(
            format!("ROI {:+.1}%  ", s.roi()),
            roi_style,
        ),
        Span::styled(
            format!(
                "{} tr W={} L={}  ",
                s.total_trades, s.wins, s.losses
            ),
            Style::default().fg(theme::DIM),
        ),
        Span::styled(
            format!("DD ${:.2}", s.max_drawdown),
            Style::default().fg(theme::NEG),
        ),
    ])
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
        let backend = TestBackend::new(80, 4);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| draw_baselines(frame, frame.area(), app))
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
    fn baselines_renders_both_strategies() {
        let app = AppState {
            snapshot: Some(StateSnapshot {
                base: Some(StrategyBlob {
                    stats: Stats {
                        total_pnl: 1.5,
                        total_trades: 4,
                        wins: 2,
                        losses: 2,
                        total_risked: 20.0,
                        max_drawdown: 0.3,
                        ..Default::default()
                    },
                    ..Default::default()
                }),
                enhanced: Some(StrategyBlob {
                    stats: Stats {
                        total_pnl: 3.2,
                        total_trades: 6,
                        wins: 5,
                        losses: 1,
                        total_risked: 25.0,
                        max_drawdown: 0.1,
                        ..Default::default()
                    },
                    ..Default::default()
                }),
                ..Default::default()
            }),
            ..Default::default()
        };
        let text = render(&app);
        assert!(text.contains("BASE"));
        assert!(text.contains("ENHANCED"));
        assert!(text.contains("+1.50"));
        assert!(text.contains("+3.20"));
    }

    #[test]
    fn baselines_zero_state_shows_zero_stats() {
        let app = AppState::new();
        let text = render(&app);
        assert!(text.contains("BASE"));
        assert!(text.contains("ENHANCED"));
        assert!(text.contains("+0.00"));
    }
}
