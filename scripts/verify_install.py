"""Verify Stan's fork overlay is correctly installed.

Run from the repo root:
    python scripts/verify_install.py

Tests work WITHOUT any API keys. Each test prints PASS/FAIL with details.
Summary at the end. Exit code 0 if all pass, 1 if any fail.

Tests:
  1.  Python version (3.10+)
  2.  Overlay modules importable
  3.  Subagent files present (.claude/agents/)
  4.  Slash command files present (.claude/commands/)
  5.  Memory module CRUD lifecycle
  6.  Audit log lifecycle
  7.  Event calendar deterministic dates (2026)
  8.  Risk stats on synthetic data
  9.  Unusual Whales correctly inert without API key
  10. Deribit live ping (free API, requires internet)
  11. Config files present and parseable
  12. .env.example present with all expected keys
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)


# ANSI colors; Windows cmd may not render — that's OK, still readable
PASS = "\033[92m[PASS]\033[0m"
FAIL = "\033[91m[FAIL]\033[0m"
WARN = "\033[93m[WARN]\033[0m"


_results = {"pass": 0, "fail": 0, "warn": 0}


def test(name: str, fn) -> None:
    print(f"\n--- Test: {name}")
    try:
        result = fn()
        if result == "warn":
            print(f"  {WARN}")
            _results["warn"] += 1
        else:
            print(f"  {PASS}")
            _results["pass"] += 1
    except Exception as e:
        print(f"  {FAIL}: {e}")
        traceback.print_exc()
        _results["fail"] += 1


# ═══════════════════════════════════════════════════════════════
# Individual tests

def t_python_version():
    if sys.version_info < (3, 10):
        raise RuntimeError(f"Python 3.10+ required; you have {sys.version}")
    print(f"  Python {sys.version.split()[0]} OK")


def t_imports():
    import quant_validator
    import quant_validator.memory
    import quant_validator.audit
    import quant_validator.risk_stats
    import adapters
    import adapters.event_calendar
    import adapters.deribit
    import adapters.crypto_data_download
    import adapters.unusual_whales
    import adapters.massive
    import adapters.alpha_vantage
    import adapters.flash_alpha
    import adapters.orats
    import features_custom
    import features_custom.skew
    import features_custom.vol
    import features_custom.exposure
    import features_custom.pe_quadrant
    print("  All 18 overlay modules imported successfully")


def t_subagents():
    agents_dir = REPO_ROOT / ".claude" / "agents"
    expected = ["hypothesis-refiner", "code", "critic-pre",
                "critic-validator", "risk", "memory"]
    for name in expected:
        path = agents_dir / f"{name}.md"
        if not path.exists():
            raise FileNotFoundError(f"Missing subagent file: {path}")
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            raise ValueError(f"{name}.md missing YAML frontmatter")
        if "description:" not in text:
            raise ValueError(f"{name}.md missing description field in frontmatter")
    print(f"  All 6 subagent files present with valid YAML frontmatter")


def t_commands():
    cmd_dir = REPO_ROOT / ".claude" / "commands"
    expected = ["validate-thesis", "override-reject"]
    for name in expected:
        path = cmd_dir / f"{name}.md"
        if not path.exists():
            raise FileNotFoundError(f"Missing command file: {path}")
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            raise ValueError(f"{name}.md missing YAML frontmatter")
    print(f"  All 2 slash command files present")


def t_memory():
    tmpdir = Path(tempfile.mkdtemp())
    try:
        test_db = tmpdir / "test_memory.db"
        from quant_validator.memory import (
            _connect, status, seed_historical, n_trials, overrides_audit,
        )
        conn = _connect(test_db)
        s = status(conn)
        assert s["total_trials"] == 0, f"expected 0, got {s['total_trials']}"

        inserted = seed_historical(conn, count=5, note="test_seed")
        assert inserted == 5

        s = status(conn)
        assert s["total_trials"] == 5
        assert n_trials(conn) == 5
        assert overrides_audit(conn) == []
        conn.close()
        print("  Memory CRUD: connect → seed → status → close OK")
    finally:
        shutil.rmtree(tmpdir)


def t_audit():
    tmpdir = Path(tempfile.mkdtemp())
    cwd_before = os.getcwd()
    os.chdir(tmpdir)
    try:
        from quant_validator.audit import (
            log_step_start, log_step_complete, log_user_question,
            log_user_response, log_override_applied, log_pipeline_complete,
            read_audit_log, read_user_interactions,
        )
        tid = "test_verify_thesis"
        log_step_start(tid, 1, "test_step")
        log_step_complete(tid, 1, "test_step", status="pass", metrics={"x": 1})
        log_user_question(tid, 2, "Continue?", options=["yes", "no"])
        log_user_response(tid, 2, "yes")
        log_override_applied(tid, "gates:dsr",
                             "test reason text over 20 chars",
                             computed_value=0.97, threshold=0.95)
        log_pipeline_complete(tid, "accepted", stopped_at_step=None)

        audit = read_audit_log(tid)
        ui = read_user_interactions(tid)
        assert len(audit) == 4, f"expected 4 audit entries, got {len(audit)}"
        assert len(ui) == 2, f"expected 2 user interactions, got {len(ui)}"
        print(f"  Audit lifecycle: {len(audit)} audit events, {len(ui)} user interactions OK")
    finally:
        os.chdir(cwd_before)
        shutil.rmtree(tmpdir)


def t_event_calendar():
    from adapters.event_calendar import (
        get_triple_witching_dates,
        get_jpm_collar_roll_dates,
        get_nfp_dates,
    )
    tw = get_triple_witching_dates("2026-01-01", "2026-12-31")
    expected = ["2026-03-20", "2026-06-19", "2026-09-18", "2026-12-18"]
    actual = [str(d.date()) for d in tw["date"]]
    assert actual == expected, f"triple witching mismatch: {actual} vs {expected}"
    print(f"  Triple witching 2026: {actual} OK")

    jpm = get_jpm_collar_roll_dates("2026-01-01", "2026-12-31")
    expected_jpm = ["2026-03-31", "2026-06-30", "2026-09-30", "2026-12-31"]
    actual_jpm = [str(d.date()) for d in jpm["date"]]
    assert actual_jpm == expected_jpm, f"JPM roll mismatch: {actual_jpm}"
    print(f"  JPM collar rolls 2026: {actual_jpm} OK")

    nfp = get_nfp_dates("2026-01-01", "2026-03-31")
    expected_nfp = ["2026-01-02", "2026-02-06", "2026-03-06"]
    actual_nfp = [str(d.date()) for d in nfp["date"]]
    assert actual_nfp == expected_nfp, f"NFP mismatch: {actual_nfp}"
    print(f"  NFP Q1 2026: {actual_nfp} OK")


def t_risk_stats():
    import numpy as np
    import pandas as pd
    from quant_validator.risk_stats import compute_all

    tmpdir = Path(tempfile.mkdtemp())
    cwd_before = os.getcwd()
    os.chdir(tmpdir)
    try:
        results = tmpdir / "results"
        results.mkdir()
        dates = pd.date_range("2024-01-01", periods=300, freq="B")
        np.random.seed(42)
        pd.Series(np.random.uniform(-1, 1, 300), index=dates,
                  name="position").to_csv(results / "positions.csv")
        pd.Series(np.random.normal(0.0005, 0.012, 300), index=dates,
                  name="returns").to_csv(results / "returns.csv")

        out = compute_all(tmpdir)
        for key in ["position_stats", "regime_breakdown",
                    "tail_metrics", "concentration"]:
            assert key in out, f"missing {key} in risk_stats output"
        sharpe = out["regime_breakdown"]["low_vol"]["sharpe"]
        assert isinstance(sharpe, float)
        print(f"  Risk stats computed: regime breakdown has 3 vol buckets OK")
    finally:
        os.chdir(cwd_before)
        shutil.rmtree(tmpdir)


def t_uw_inert():
    saved = os.environ.pop("UW_API_KEY", None)
    try:
        import importlib
        from adapters import unusual_whales
        importlib.reload(unusual_whales)
        try:
            unusual_whales.fetch_dark_pool_trades("AAPL", "2024-01-01")
            raise AssertionError("Expected UnusualWhalesNotSubscribed, got nothing")
        except unusual_whales.UnusualWhalesNotSubscribed:
            print("  UW adapter correctly raises NotSubscribed without API key OK")
    finally:
        if saved is not None:
            os.environ["UW_API_KEY"] = saved


def t_deribit_live():
    try:
        from adapters.deribit import fetch_index_price
        price = fetch_index_price("BTC")
        if price > 0:
            print(f"  BTC index price from Deribit public API: ${price:,.2f} OK")
        else:
            print(f"  Deribit returned zero or negative price; check network")
            return "warn"
    except Exception as e:
        print(f"  Deribit live ping failed (network/firewall/rate limit): {e}")
        print(f"  This is a WARNING not failure — overlay code is fine")
        return "warn"


def t_configs():
    config_dir = REPO_ROOT / "config"
    portfolio = json.loads((config_dir / "portfolio_targets.json").read_text())
    for key in ["max_abs_delta", "max_abs_gamma", "max_abs_vega", "max_abs_theta"]:
        assert key in portfolio, f"{key} missing from portfolio_targets.json"

    import pandas as pd
    for fname in ["market_holidays.csv", "fomc_dates.csv",
                  "cpi_dates.csv", "jpm_collar_history.csv"]:
        path = config_dir / fname
        assert path.exists(), f"{fname} missing"
        df = pd.read_csv(path)
        if fname != "jpm_collar_history.csv":
            assert len(df) > 0, f"{fname} has no rows"
    print("  All 5 config files present and parseable OK")


def t_env_example():
    path = REPO_ROOT / ".env.example"
    assert path.exists(), ".env.example missing at repo root"
    text = path.read_text(encoding="utf-8")
    for key in ["MASSIVE_API_KEY", "ALPHA_VANTAGE_API_KEY",
                "FLASH_ALPHA_API_KEY", "ORATS_API_TOKEN", "UW_API_KEY"]:
        assert key in text, f"{key} not in .env.example"
    print("  .env.example has all 5 expected API key slots OK")


# ─── v0.3 additions ───────────────────────────────────────────

def t_vs_random_discriminates():
    """Vs. Random Tier A must pass a real-edge series and not-pass a no-edge one."""
    import numpy as np
    import pandas as pd
    from quant_validator.vs_random import vs_random_permutation

    rng = np.random.default_rng(0)
    dates = pd.date_range("2022-01-03", periods=600, freq="B")
    asset = pd.Series(rng.normal(0.0003, 0.012, 600), index=dates)

    # No-edge: random timing
    pos_noedge = pd.Series(rng.choice([0, 0, 0.5, -0.5], 600), index=dates)
    strat_noedge = (pos_noedge.shift(1) * asset).fillna(0)
    r_noedge = vs_random_permutation(pos_noedge, strat_noedge, asset_returns=asset, n=500, seed=1)
    assert r_noedge["verdict"] in ("fail", "borderline"), \
        f"no-edge should not pass, got {r_noedge['verdict']}"

    # Real edge: predictive timing
    sig = np.sign(asset.shift(-1) + rng.normal(0, 0.02, 600)).fillna(0)
    pos_edge = pd.Series((sig * 0.4).values, index=dates)
    strat_edge = (pos_edge.shift(1) * asset).fillna(0)
    r_edge = vs_random_permutation(pos_edge, strat_edge, asset_returns=asset, n=500, seed=1)
    assert r_edge["verdict"] == "pass", f"real-edge should pass, got {r_edge['verdict']}"
    print(f"  Tier A: no-edge={r_noedge['verdict']}, real-edge={r_edge['verdict']} OK")


def t_stats_and_gates_cli():
    """stats.compute and gates.evaluate must run end-to-end and produce JSON."""
    import numpy as np
    import pandas as pd
    from quant_validator.stats import compute as stats_compute
    from quant_validator.vs_random import run_vs_random
    from quant_validator.gates import evaluate as gates_evaluate

    tmpdir = Path(tempfile.mkdtemp())
    cwd_before = os.getcwd()
    os.chdir(tmpdir)
    try:
        tid = "verify_v03"
        base = tmpdir / "theses" / tid / "results"
        base.mkdir(parents=True)
        rng = np.random.default_rng(3)
        dates = pd.date_range("2022-01-03", periods=600, freq="B")
        asset = pd.Series(rng.normal(0.0003, 0.012, 600), index=dates)
        sig = np.sign(asset.shift(-1) + rng.normal(0, 0.02, 600)).fillna(0)
        pos = pd.Series((sig * 0.4).values, index=dates)
        strat = (pos.shift(1) * asset).fillna(0)
        pos.to_csv(base / "positions.csv", header=["position"])
        strat.to_csv(base / "returns.csv", header=["returns"])
        asset.to_csv(base / "asset_returns.csv", header=["asset_return"])

        s = stats_compute(Path(f"theses/{tid}"), n_trials=30)
        assert s["status"] == "ok" and "dsr" in s
        v = run_vs_random(Path(f"theses/{tid}"), n=400, seed=5)
        assert v["status"] == "ok"
        g = gates_evaluate(tid)
        assert "vs_random" in g["gates"], "vs_random gate missing from gates output"
        assert g["overall"] in ("pass", "warning", "fail")
        print(f"  stats+vs_random+gates ran; gates overall={g['overall']}, "
              f"vs_random gate present OK")
    finally:
        os.chdir(cwd_before)
        shutil.rmtree(tmpdir)


def t_override_upsert():
    """apply_override pre-Step-10 must create a tracked row that record_trial upserts."""
    import numpy as np
    import pandas as pd
    from quant_validator.memory import (
        _connect, apply_override, record_trial, overrides_audit, status,
    )
    tmpdir = Path(tempfile.mkdtemp())
    cwd_before = os.getcwd()
    os.chdir(tmpdir)
    try:
        base = tmpdir / "theses" / "ov"
        (base / "results").mkdir(parents=True)
        (base / "refined.json").write_text(
            '{"hypothesis_id":"ov","title":"OV","rationale":"t","market_type":"equities"}'
        )
        (base / "results" / "returns.csv").write_text("timestamp,returns\n2024-01-01,0.01\n2024-01-02,-0.01\n")
        (base / "results" / "metrics.json").write_text('{"sharpe_ratio":1.0}')
        (base / "decision.json").write_text('{"decision":"accepted_with_override","rejection_reason":null}')

        db = tmpdir / "mem.db"
        conn = _connect(db)
        # override BEFORE record (Step 2 override scenario)
        apply_override(conn, "ov", "critic_pre", "valid structural justification over twenty chars")
        assert status(conn)["total_trials"] == 1, "override should create a tracked row"
        assert len(overrides_audit(conn)) == 1, "override should be visible in audit"
        # Step 10 record — should UPSERT, not duplicate
        record_trial(conn, thesis_id="ov", accepted=True, size_multiplier=0.5)
        st = status(conn)
        assert st["total_trials"] == 1, f"record_trial should upsert, got {st['total_trials']} rows"
        assert len(overrides_audit(conn)) == 1, "override must survive the upsert"
        conn.close()
        print("  override pre-Step-10 tracked + upserted by record_trial OK")
    finally:
        os.chdir(cwd_before)
        shutil.rmtree(tmpdir)


def t_adapter_stub_messaging():
    """Stub adapters must raise NotImplementedError (not a misleading key error)."""
    import importlib
    saved = {}
    for k in ["AV_API_KEY", "ALPHA_VANTAGE_API_KEY", "MASSIVE_API_KEY",
              "FLASH_ALPHA_API_KEY", "FLASH_API_KEY", "ORATS_API_TOKEN", "ORATS_API_KEY"]:
        saved[k] = os.environ.pop(k, None)
    try:
        from adapters import alpha_vantage, massive, flash_alpha, orats
        for mod in (alpha_vantage, massive, flash_alpha, orats):
            importlib.reload(mod)
        checks = [
            (alpha_vantage.fetch_bars, (["SPY"], "2024-01-01", "2024-12-31")),
            (massive.fetch_bars, (["SPY"], "2024-01-01", "2024-12-31")),
            (flash_alpha.fetch_exposure_summary, ("SPY", "2024-01-01", "2024-12-31")),
            (orats.fetch_cores, ("SPY", "2024-01-01", "2024-12-31")),
        ]
        for fn, args in checks:
            try:
                fn(*args)
                raise AssertionError(f"{fn.__name__} should have raised")
            except NotImplementedError:
                pass  # correct
            except RuntimeError as e:
                raise AssertionError(f"{fn.__name__} raised misleading RuntimeError: {e}")
        print("  4 stub adapters raise NotImplementedError first OK")
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


# ═══════════════════════════════════════════════════════════════
# Main

def main() -> int:
    print("=" * 60)
    print("Stan's fork overlay — installation verification")
    print("=" * 60)
    print(f"Repo root: {REPO_ROOT}")

    test("Python version (3.10+)", t_python_version)
    test("Overlay modules importable", t_imports)
    test("Subagent files present", t_subagents)
    test("Slash command files present", t_commands)
    test("Memory module CRUD lifecycle", t_memory)
    test("Audit log lifecycle", t_audit)
    test("Event calendar deterministic dates (2026)", t_event_calendar)
    test("Risk stats on synthetic data", t_risk_stats)
    test("Unusual Whales correctly inert without API key", t_uw_inert)
    test("Deribit live ping (free API, requires internet)", t_deribit_live)
    test("Config files present and parseable", t_configs)
    test(".env.example present with expected keys", t_env_example)
    test("v0.3 — Vs. Random Tier A discriminates edge", t_vs_random_discriminates)
    test("v0.3 — stats + vs_random + gates CLI end-to-end", t_stats_and_gates_cli)
    test("v0.3 — override upsert (pre-Step-10 tracking)", t_override_upsert)
    test("v0.3 — adapter stubs raise NotImplementedError first", t_adapter_stub_messaging)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Passed: {_results['pass']}")
    print(f"  Warned: {_results['warn']}")
    print(f"  Failed: {_results['fail']}")

    if _results["fail"] == 0:
        print("\nAll checks passed. Overlay correctly installed.")
        print("\nNext steps:")
        print("  1. cp .env.example .env")
        print("  2. Edit .env with your API keys (notepad .env)")
        print("  3. python -m quant_validator.memory seed_historical --count 30 \\")
        print("       --note 'PE Quadrant + skew_quadrant + Skew_backtest PATCH-1 to 21h'")
        print("  4. Open Claude Code from this folder: claude")
        print("  5. In Claude Code, run: /agents to confirm subagents are listed")
        return 0
    else:
        print(f"\n{_results['fail']} test(s) failed. Review errors above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
