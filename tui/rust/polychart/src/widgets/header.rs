use ratatui::{
    layout::Rect,
    style::Style,
    widgets::{Block, BorderType, Borders, Paragraph},
    Frame,
};

use crate::state::AppState;
use crate::theme;

pub fn draw_header(frame: &mut Frame, area: Rect, _app: &AppState) {
    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Thick)
        .border_style(Style::default().fg(theme::BORDER_HEADER));
    frame.render_widget(
        Paragraph::new("polychart: header placeholder").block(block.title(" header ")),
        area,
    );
}
