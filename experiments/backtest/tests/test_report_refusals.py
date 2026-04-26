from experiments.backtest.report import refusal_headers


def test_no_refusals_when_strict_live_full_scope() -> None:
    out = refusal_headers(
        staleness_policy="strict",
        mode_tag="live",
        scope="entries_and_exits",
    )
    assert out == []


def test_staleness_policy_refusal() -> None:
    out = refusal_headers(
        staleness_policy="allow_stale_diagnostic",
        mode_tag="live",
        scope="entries_and_exits",
    )
    assert any("staleness_policy" in s for s in out)


def test_mode_tag_refusal_when_live_dryrun() -> None:
    out = refusal_headers(
        staleness_policy="strict",
        mode_tag="live_dryrun",
        scope="entries_and_exits",
    )
    assert any("PLUMBING-VALIDATION ONLY" in s for s in out)
    assert any("live_dryrun" in s for s in out)


def test_scope_refusal_when_entries_only() -> None:
    out = refusal_headers(
        staleness_policy="strict",
        mode_tag="live",
        scope="entries_only",
    )
    assert any("ENTRIES-SIDE HAIRCUT ONLY" in s for s in out)


def test_all_three_refusals_stack() -> None:
    out = refusal_headers(
        staleness_policy="allow_stale_diagnostic",
        mode_tag="live_dryrun",
        scope="entries_only",
    )
    assert len(out) == 3
