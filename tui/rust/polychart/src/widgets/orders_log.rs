use ratatui::{
    layout::Rect,
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::{Block, BorderType, Borders, List, ListItem},
    Frame,
};

use crate::events::{Action, ActionKind};
use crate::state::AppState;
use crate::theme;
use crate::util::ts_hms;

pub fn draw_orders_log(frame: &mut Frame, area: Rect, app: &AppState) {
    let count = app.refined_actions.len();
    let right_title = Line::from(Span::styled(
        format!(" {count} actions "),
        Style::default().fg(theme::DIM),
    ))
    .right_aligned();

    let block = Block::default()
        .title(Line::from(Span::styled(
            " orders ",
            Style::default().fg(theme::DIM),
        )))
        .title_top(right_title)
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(theme::BORDER_DIM));
    let inner = block.inner(area);

    // Show the tail that fits in the panel; newest at the bottom.
    let capacity = inner.height as usize;
    let total = app.refined_actions.len();
    let skip = total.saturating_sub(capacity);
    let items: Vec<ListItem> = app
        .refined_actions
        .iter()
        .skip(skip)
        .map(|a| ListItem::new(render_action_line(a)))
        .collect();

    frame.render_widget(block, area);
    frame.render_widget(List::new(items), inner);
}

fn render_action_line(a: &Action) -> Line<'static> {
    let tm = ts_hms(a.ts);
    let mut spans = vec![
        Span::styled(tm, Style::default().fg(theme::DIM)),
        Span::raw("  "),
        kind_span(a.kind),
        Span::raw(" "),
        side_span(&a.side),
        Span::raw(" "),
    ];
    match a.kind {
        ActionKind::Buy => {
            spans.push(Span::styled(
                format!("@ ${:.2}", a.price),
                Style::default().fg(theme::CHART_MKT),
            ));
            spans.push(Span::styled(
                format!("   (${:.2})", a.size_usdc),
                Style::default().fg(theme::DIM),
            ));
        }
        ActionKind::Sell => {
            let pnl = a.pnl.unwrap_or(0.0);
            spans.push(Span::styled(
                format!("@ ${:.2}", a.price),
                Style::default().fg(theme::CHART_MKT),
            ));
            spans.push(Span::raw("   "));
            spans.push(Span::styled(
                format!("{pnl:+.2}"),
                sign_style(pnl),
            ));
            spans.push(Span::raw("   "));
            spans.push(Span::styled(
                a.exit_type.clone().unwrap_or_default(),
                Style::default().fg(theme::DIM),
            ));
        }
        ActionKind::Res => {
            let pnl = a.pnl.unwrap_or(0.0);
            let label = match a.won {
                Some(true) => ("WON ", theme::POS),
                Some(false) => ("LOST", theme::NEG),
                None => ("RES ", theme::RES),
            };
            spans.push(Span::styled(
                label.0,
                Style::default().fg(label.1).add_modifier(Modifier::BOLD),
            ));
            spans.push(Span::raw("   "));
            spans.push(Span::styled(format!("{pnl:+.2}"), sign_style(pnl)));
        }
    }
    Line::from(spans)
}

fn kind_span(k: ActionKind) -> Span<'static> {
    match k {
        ActionKind::Buy => Span::styled(
            "BUY ",
            Style::default().fg(theme::BUY).add_modifier(Modifier::BOLD),
        ),
        ActionKind::Sell => Span::styled(
            "SELL",
            Style::default().fg(theme::SELL).add_modifier(Modifier::BOLD),
        ),
        ActionKind::Res => Span::styled(
            "RES ",
            Style::default().fg(theme::RES).add_modifier(Modifier::BOLD),
        ),
    }
}

fn side_span(side: &str) -> Span<'static> {
    match side {
        "Up" => Span::styled("Up  ", Style::default().fg(theme::POS)),
        "Down" => Span::styled("Down", Style::default().fg(theme::NEG)),
        other => Span::styled(
            format!("{other:<4}"),
            Style::default().fg(theme::CHART_MKT),
        ),
    }
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
    use ratatui::{backend::TestBackend, Terminal};
    use std::collections::VecDeque;

    fn buy(ts: f64) -> Action {
        Action {
            ts,
            kind: ActionKind::Buy,
            strategy: "refined".into(),
            side: "Up".into(),
            price: 0.42,
            size_usdc: 5.0,
            pnl: None,
            exit_type: None,
            won: None,
        }
    }

    fn sell(ts: f64, pnl: f64) -> Action {
        Action {
            ts,
            kind: ActionKind::Sell,
            strategy: "refined".into(),
            side: "Up".into(),
            price: 0.55,
            size_usdc: 5.0,
            pnl: Some(pnl),
            exit_type: Some("TP".into()),
            won: None,
        }
    }

    fn res(ts: f64, won: bool, pnl: f64) -> Action {
        Action {
            ts,
            kind: ActionKind::Res,
            strategy: "refined".into(),
            side: "Up".into(),
            price: 1.0,
            size_usdc: 5.0,
            pnl: Some(pnl),
            exit_type: None,
            won: Some(won),
        }
    }

    fn render(app: &AppState) -> String {
        let backend = TestBackend::new(80, 10);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| draw_orders_log(frame, frame.area(), app))
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
    fn orders_log_renders_buy_sell_res_lines() {
        let mut app = AppState::new();
        app.refined_actions = VecDeque::from(vec![
            buy(1.0),
            sell(2.0, 0.65),
            res(3.0, true, 1.0),
        ]);
        let text = render(&app);
        assert!(text.contains("BUY"));
        assert!(text.contains("SELL"));
        assert!(text.contains("WON"));
    }

    #[test]
    fn orders_log_tail_fits_in_panel() {
        let mut app = AppState::new();
        let mut deque = VecDeque::new();
        for i in 0..30 {
            deque.push_back(buy(i as f64));
        }
        app.refined_actions = deque;
        // Shouldn't panic at 80x10 even with 30 actions queued.
        let _ = render(&app);
    }
}
