//! Acceptance smoke test: spin up the binary against a fake
//! daemon_state/ directory, verify it boots and exits under Ctrl-C.
//! This is the integration analog of the 14-item P3 checklist --
//! everything that is testable without a live daemon.

use std::io::Write;
use std::process::{Command, Stdio};

fn project_root() -> std::path::PathBuf {
    // tests/ lives under rust/polychart/, repo root is three up.
    let here = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
    here.parent().unwrap().parent().unwrap().parent().unwrap().to_path_buf()
}

fn binary() -> std::path::PathBuf {
    // Prefer release build for realism, fall back to debug.
    let root = project_root();
    for candidate in [
        root.join("tui/target/release/polychart"),
        root.join("tui/target/debug/polychart"),
    ] {
        if candidate.exists() {
            return candidate;
        }
    }
    panic!("polychart binary not built; run `cargo build` first");
}

#[test]
fn polychart_once_mode_prints_line() {
    let tmp = tempfile::tempdir().unwrap();
    let state_dir = tmp.path().join("daemon_state");
    std::fs::create_dir(&state_dir).unwrap();
    let state_file = state_dir.join("state.json");
    let mut f = std::fs::File::create(&state_file).unwrap();
    writeln!(f, r#"{{"btc_price": 50000.0, "slug": "btc-test"}}"#).unwrap();

    let output = Command::new(binary())
        .arg("--once")
        .env("POLYCHART_STATE_DIR", state_dir)
        .output()
        .expect("spawn");
    assert!(output.status.success(), "non-zero exit: {output:?}");
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("btc=$50000.00"), "unexpected stdout: {stdout}");
    assert!(stdout.contains("slug=btc-test"), "unexpected stdout: {stdout}");
}

#[test]
fn polychart_does_not_write_to_state_dir() {
    let tmp = tempfile::tempdir().unwrap();
    let state_dir = tmp.path().join("daemon_state");
    std::fs::create_dir(&state_dir).unwrap();
    std::fs::write(
        state_dir.join("state.json"),
        r#"{"btc_price": 1.0, "slug": "x"}"#,
    )
    .unwrap();
    std::fs::write(state_dir.join("events.jsonl"), "").unwrap();

    let before: Vec<std::path::PathBuf> = std::fs::read_dir(&state_dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .collect();

    let _ = Command::new(binary())
        .arg("--once")
        .env("POLYCHART_STATE_DIR", &state_dir)
        .output()
        .expect("spawn");

    let after: Vec<std::path::PathBuf> = std::fs::read_dir(&state_dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .collect();
    assert_eq!(before, after, "daemon_state changed during polychart run");
}

#[test]
fn polychart_missing_state_file_is_handled() {
    let tmp = tempfile::tempdir().unwrap();
    let state_dir = tmp.path().join("daemon_state");
    std::fs::create_dir(&state_dir).unwrap();
    // No state.json exists.
    let output = Command::new(binary())
        .arg("--once")
        .env("POLYCHART_STATE_DIR", &state_dir)
        .stderr(Stdio::piped())
        .output()
        .expect("spawn");
    // --once with missing file prints "waiting for state.json" and exits 0.
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        output.status.success() || stdout.contains("waiting"),
        "unexpected failure: stdout={stdout} stderr={:?}",
        String::from_utf8_lossy(&output.stderr)
    );
}

// Note: Ctrl-C handling from the TUI mode is exercised manually
// against a live daemon in Phase 3; automating it needs a pty harness
// that isn't worth the extra dependency surface here.
