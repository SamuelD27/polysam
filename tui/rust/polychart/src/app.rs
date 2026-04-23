//! ratatui event loop, terminal lifecycle, background task spawning.
//!
//! The shared `AppState` lives behind a `std::sync::Mutex`. Rules:
//!   - Render thread takes the lock only long enough to snapshot for drawing.
//!   - Background tasks take the lock only for short mutations; NEVER
//!     across an `.await`.
//!   - Ctrl-C: the render loop checks `app.quit` every tick AND listens
//!     for a crossterm Ctrl-C event.

use std::io::{self, Stdout};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::Result;
use crossterm::{
    event::{self, DisableMouseCapture, EnableMouseCapture, Event, KeyCode, KeyModifiers},
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use ratatui::{backend::CrosstermBackend, Terminal};

use crate::events::EventsTailer;
use crate::http::{OrderbookPoller, TokenResolver};
use crate::state::AppState;
use crate::state_reader::{read_state_json_async, resolve_state_dir};
use crate::ui;

/// RAII guard that restores the terminal even on panic.
struct TerminalGuard;

impl TerminalGuard {
    fn new() -> io::Result<Self> {
        enable_raw_mode()?;
        execute!(io::stdout(), EnterAlternateScreen, EnableMouseCapture)?;
        Ok(Self)
    }
}

impl Drop for TerminalGuard {
    fn drop(&mut self) {
        let _ = execute!(io::stdout(), LeaveAlternateScreen, DisableMouseCapture);
        let _ = disable_raw_mode();
    }
}

pub async fn run() -> Result<()> {
    let state_dir = resolve_state_dir();
    let state_path = state_dir.join("state.json");
    let events_path = state_dir.join("events.jsonl");
    let kill_path = state_dir.join("KILL");

    let shared: Arc<Mutex<AppState>> = Arc::new(Mutex::new(AppState::new()));

    // state.json poller
    {
        let shared = Arc::clone(&shared);
        let path = state_path.clone();
        let kill_path = kill_path.clone();
        tokio::spawn(async move {
            let mut ticker = tokio::time::interval(Duration::from_millis(500));
            ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
            loop {
                ticker.tick().await;
                let kill = tokio::fs::metadata(&kill_path).await.is_ok();
                match read_state_json_async(&path).await {
                    Ok(Some(snap)) => {
                        if let Ok(mut app) = shared.lock() {
                            app.apply_state_snapshot(snap);
                            app.kill_active = kill;
                        }
                    }
                    Ok(None) => {
                        if let Ok(mut app) = shared.lock() {
                            app.kill_active = kill;
                        }
                    }
                    Err(e) => {
                        // Non-fatal: a mid-rename read can still fail;
                        // next tick will pick it up.
                        eprintln!("polychart: state read error: {e:#}");
                    }
                }
            }
        });
    }

    // events.jsonl tailer
    {
        let shared = Arc::clone(&shared);
        let path = events_path.clone();
        tokio::spawn(async move {
            let mut tailer = EventsTailer::new(path);
            let mut ticker = tokio::time::interval(Duration::from_millis(500));
            ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
            loop {
                ticker.tick().await;
                // Tailer's update() is synchronous + fast (file read);
                // run it inside spawn_blocking to stay off the reactor.
                let result = tokio::task::spawn_blocking(move || {
                    let mut t = tailer;
                    let n = t.update();
                    (t, n)
                })
                .await;
                let (returned, _n) = match result {
                    Ok(pair) => pair,
                    Err(_) => return,
                };
                tailer = returned;
                if let Ok(mut app) = shared.lock() {
                    // Swap tailer's series into the app's PnL map each tick.
                    app.pnl_series = tailer.pnl_series.clone();
                }
            }
        });
    }

    // CLOB orderbook poller: resolves the YES token once per slug, then
    // polls /book on its own cadence (2s base, 10s backoff).
    {
        let shared = Arc::clone(&shared);
        tokio::spawn(async move {
            let resolver = std::sync::Arc::new(TokenResolver::new());
            let mut poller: Option<OrderbookPoller> = None;
            let mut current_slug: Option<String> = None;
            loop {
                // Figure out what slug we should be polling.
                let desired_slug = {
                    let app = match shared.lock() {
                        Ok(a) => a,
                        Err(_) => return,
                    };
                    app.snapshot.as_ref().and_then(|s| s.slug.clone())
                };

                // Rebuild the poller on slug change.
                if desired_slug != current_slug {
                    current_slug = desired_slug.clone();
                    poller = None;
                    if let Some(ref slug) = current_slug {
                        if let Some(tokens) = resolver.resolve(slug).await {
                            poller = Some(OrderbookPoller::new(tokens.yes_token_id));
                        }
                    }
                }

                if let Some(p) = poller.as_mut() {
                    let snap = p.poll_once().await;
                    let interval = p.current_interval;
                    if let Ok(mut app) = shared.lock() {
                        app.orderbook = Some(snap);
                    }
                    tokio::time::sleep(interval).await;
                } else {
                    // No poller yet (slug missing or unresolvable);
                    // check again in a second.
                    tokio::time::sleep(Duration::from_secs(1)).await;
                }
            }
        });
    }

    run_render_loop(shared).await
}

async fn run_render_loop(shared: Arc<Mutex<AppState>>) -> Result<()> {
    let _guard = TerminalGuard::new()?;
    let backend = CrosstermBackend::new(io::stdout());
    let mut terminal: Terminal<CrosstermBackend<Stdout>> = Terminal::new(backend)?;
    terminal.clear()?;

    let tick_rate = Duration::from_millis(100);

    loop {
        // Drain keyboard events non-blockingly.
        while event::poll(Duration::ZERO)? {
            match event::read()? {
                Event::Key(k) => {
                    let ctrl_c =
                        k.modifiers.contains(KeyModifiers::CONTROL) && k.code == KeyCode::Char('c');
                    if ctrl_c || matches!(k.code, KeyCode::Char('q')) {
                        return Ok(());
                    }
                }
                Event::Resize(_, _) => {
                    // Terminal handles the next draw at the new size automatically.
                }
                _ => {}
            }
        }

        // Snapshot-and-draw.
        let snapshot = take_render_snapshot(&shared);
        terminal.draw(|frame| ui::draw(frame, &snapshot))?;

        tokio::time::sleep(tick_rate).await;
    }
}

/// Clone just what the widgets need out of AppState so the lock is held
/// for microseconds.
fn take_render_snapshot(shared: &Arc<Mutex<AppState>>) -> AppState {
    let app = shared.lock().expect("app state mutex poisoned");
    app.clone()
}
