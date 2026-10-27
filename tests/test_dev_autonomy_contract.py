from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / ".pi" / "extensions" / "nrg-laboratory" / "index.ts"
POLICY = ROOT / "config" / "dev_transaction_policy.json"


def _tool_block(name: str) -> str:
    text = INDEX.read_text(encoding="utf-8")
    marker = f'name: "{name}"'
    start = text.index(marker)
    end = text.find("pi.registerTool({", start + len(marker))
    return text[start:end if end != -1 else len(text)]


def test_standing_grant_is_session_bound_and_expiring():
    text = INDEX.read_text(encoding="utf-8")
    assert "sessionManager?.getSessionId?.()" in text
    assert "DEV_GRANT_TTL_MS" in text
    assert "requireDevStanding(ctx, params.transaction_id)" in text
    assert "grantDevStanding(ctx, plan)" in text
    assert "revokeDevStanding(ctx, params.transaction_id)" in text


def test_candidate_mutation_and_named_tests_require_grant_not_prompt():
    for name in ("nrg_dev_write", "nrg_dev_edit", "nrg_dev_test"):
        block = _tool_block(name)
        assert "requireDevStanding(ctx, params.transaction_id)" in block
        assert "ctx.ui.confirm" not in block
        assert "requires interactive confirmation" not in block


def test_transaction_start_commit_discard_still_prompt():
    start = _tool_block("nrg_dev_transaction_start")
    assert "ctx.ui.confirm" in start
    assert "grantDevStanding(ctx, plan)" in start
    for name in ("nrg_dev_commit", "nrg_dev_discard"):
        block = _tool_block(name)
        assert "ctx.ui.confirm" in block
        assert "revokeDevStanding(ctx, params.transaction_id)" in block


def test_advanced_patch_still_prompts():
    assert "ctx.ui.confirm" in _tool_block("nrg_dev_apply_patch")


def test_policy_documents_standing_candidate_grant():
    text = POLICY.read_text(encoding="utf-8")
    assert "transaction-start confirmation authorizes" in text
    assert "session-bound" in text
    assert "time-limited" in text
    assert "no active grant" in text
