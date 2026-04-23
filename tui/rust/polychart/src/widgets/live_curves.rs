use ratatui::{
    layout::Rect,
    style::Style,
    widgets::{Block, BorderType, Borders, Paragraph},
    Frame,
};

use crate::state::AppState;
use crate::theme;

pub fn draw_live_curves(frame: &mut Frame, area: Rect, _app: &AppState) {
    let block = Block::default()
        .title(" live ")
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(theme::BORDER_DIM));
    frame.render_widget(
        Paragraph::new("live curves placeholder").block(block),
        area,
    );
}
