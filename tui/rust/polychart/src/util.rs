//! Pure formatting helpers. No ratatui or tokio dependence; covered by
//! unit tests under each public fn.

#![allow(dead_code)]

use chrono::{Local, TimeZone};

/// Parse `t_zero` out of a Polymarket slug like `btc-updown-5m-1776912300`.
/// Returns `None` when the slug doesn't follow the expected shape.
pub fn t_zero_from_slug(slug: &str) -> Option<i64> {
    slug.rsplit('-').next().and_then(|s| s.parse::<i64>().ok())
}

/// "BTC 5min HH:MM-HH:MM" (local time) for the current 5-minute market.
pub fn humanize_slug(slug: Option<&str>, t_zero: Option<i64>) -> String {
    let t = t_zero.or_else(|| slug.and_then(t_zero_from_slug));
    let Some(t_zero) = t else {
        return slug.unwrap_or("waiting for market").to_string();
    };
    let open = Local.timestamp_opt(t_zero, 0).single();
    let close = Local.timestamp_opt(t_zero + 300, 0).single();
    match (open, close) {
        (Some(o), Some(c)) => format!(
            "BTC 5min {}-{}",
            o.format("%H:%M"),
            c.format("%H:%M"),
        ),
        _ => slug.unwrap_or("waiting for market").to_string(),
    }
}

/// Chronometer "MM:SS / 05:00" from `t_zero`. Elapsed time clamps to
/// [0, 5*60]; returns "00:00 / 05:00" when `t_zero` is None.
pub fn format_chronometer(t_zero: Option<i64>, now: i64) -> (String, f64) {
    let Some(tz) = t_zero else {
        return ("00:00 / 05:00".to_string(), 0.0);
    };
    let elapsed = (now - tz).max(0);
    let clamped = elapsed.min(300);
    let mm = clamped / 60;
    let ss = clamped % 60;
    let fraction = clamped as f64 / 300.0;
    (format!("{mm:02}:{ss:02} / 05:00"), fraction)
}

/// Given a total bar width and a fraction in [0, 1], render the
/// "filled/empty" pair of block counts. Always produces `total` chars;
/// the empty count clamps so `filled + empty == total`.
pub fn block_bar_counts(total: usize, fraction: f64) -> (usize, usize) {
    if total == 0 {
        return (0, 0);
    }
    let f = fraction.clamp(0.0, 1.0);
    let filled = ((total as f64) * f).round() as usize;
    let filled = filled.min(total);
    (filled, total - filled)
}

/// HH:MM:SS label from unix seconds (local time).
pub fn ts_hms(ts: f64) -> String {
    let secs = ts.trunc() as i64;
    let nsec = ((ts.fract() * 1_000_000_000.0) as u32).min(999_999_999);
    match Local.timestamp_opt(secs, nsec).single() {
        Some(dt) => dt.format("%H:%M:%S").to_string(),
        None => "--:--:--".to_string(),
    }
}

/// Current wall clock in seconds since epoch (for chronometer diffs).
pub fn now_secs() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn t_zero_parse_happy_path() {
        assert_eq!(
            t_zero_from_slug("btc-updown-5m-1776912300"),
            Some(1776912300)
        );
    }

    #[test]
    fn t_zero_parse_missing() {
        assert_eq!(t_zero_from_slug("weird-slug-no-number"), None);
    }

    #[test]
    fn humanize_with_t_zero_formats_range() {
        // 2026-04-23 20:05:00 local -- rely on the system TZ but verify shape
        let out = humanize_slug(None, Some(1776912300));
        assert!(out.starts_with("BTC 5min "), "got {out}");
        assert!(out.contains("-"), "got {out}");
    }

    #[test]
    fn humanize_without_anything() {
        assert_eq!(humanize_slug(None, None), "waiting for market");
    }

    #[test]
    fn humanize_falls_back_to_slug_parse() {
        let out = humanize_slug(Some("btc-updown-5m-1776912300"), None);
        assert!(out.starts_with("BTC 5min "), "got {out}");
    }

    #[test]
    fn chronometer_formats_mmss() {
        let (s, f) = format_chronometer(Some(1000), 1111);
        assert_eq!(s, "01:51 / 05:00");
        assert!((f - 111.0 / 300.0).abs() < 1e-9);
    }

    #[test]
    fn chronometer_clamps_past_five_minutes() {
        let (s, f) = format_chronometer(Some(1000), 1400);
        assert_eq!(s, "05:00 / 05:00");
        assert_eq!(f, 1.0);
    }

    #[test]
    fn chronometer_zero_when_t_zero_missing() {
        let (s, f) = format_chronometer(None, 10_000);
        assert_eq!(s, "00:00 / 05:00");
        assert_eq!(f, 0.0);
    }

    #[test]
    fn block_bar_counts_half() {
        assert_eq!(block_bar_counts(10, 0.5), (5, 5));
    }

    #[test]
    fn block_bar_counts_full_and_empty() {
        assert_eq!(block_bar_counts(10, 1.0), (10, 0));
        assert_eq!(block_bar_counts(10, 0.0), (0, 10));
    }

    #[test]
    fn block_bar_counts_clamps_oob() {
        assert_eq!(block_bar_counts(10, 2.0), (10, 0));
        assert_eq!(block_bar_counts(10, -0.5), (0, 10));
    }
}
