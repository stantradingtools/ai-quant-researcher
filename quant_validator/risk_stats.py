"""quant_validator.risk_stats: deterministic risk statistics.

Run from the Risk subagent via Bash to compute everything the agent needs
before applying its LLM-based judgement. The agent reads the JSON output.

Usage:
    python -m quant_validator.risk_stats theses/<thesis_id>/

Computes:
- position_stats: mean_abs, max_abs, fraction_at_max, concentration_share
- regime_breakdown: low/mid/high vol Sharpe and counts
- tail_metrics: worst_1d/5d/month, max_dd, skew, kurt
- greek_summary (if greeks.csv): mean/max abs Greeks, net signs
- concentration: max_ticker_pct, max_sector_pct, event_window_pct
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


def _safe_sharpe(series: pd.Series, ann: int = 252) -> float:
    std = series.std(ddof=1)
    if std == 0 or np.isnan(std):
        return 0.0
    return float(series.mean() / std * np.sqrt(ann))


def position_stats(positions: pd.Series) -> dict[str, float]:
    positions = positions.dropna()
    if positions.empty:
        return {"mean_abs_position": 0.0, "max_abs_position": 0.0,
                "fraction_at_max": 0.0, "concentration_share": 0.0}
    abs_pos = positions.abs()
    total = abs_pos.sum()
    return {
        "mean_abs_position": float(abs_pos.mean()),
        "max_abs_position": float(abs_pos.max()),
        "fraction_at_max": float((abs_pos >= abs_pos.quantile(0.99)).mean()),
        "concentration_share": float(abs_pos.max() / total) if total > 1e-9 else 0.0,
    }


def regime_breakdown(returns: pd.Series, ann: int = 252) -> dict[str, dict]:
    returns = returns.dropna()
    if returns.empty or len(returns) < 30:
        return {"insufficient_data": True}
    vol = returns.rolling(21, min_periods=21).std()
    valid = vol.dropna()
    if valid.empty:
        return {"insufficient_data": True}
    q33, q66 = valid.quantile([1/3, 2/3])
    buckets = pd.cut(vol, bins=[-np.inf, q33, q66, np.inf],
                     labels=["low_vol", "mid_vol", "high_vol"])
    out = {}
    for label, slice_returns in returns.groupby(buckets, observed=True):
        if slice_returns.empty:
            continue
        out[str(label)] = {
            "mean": float(slice_returns.mean()),
            "sharpe": _safe_sharpe(slice_returns, ann),
            "n": int(len(slice_returns)),
        }
    return out


def tail_metrics(returns: pd.Series) -> dict[str, float]:
    returns = returns.dropna()
    if returns.empty:
        return {}

    worst_1d = float(returns.min())
    worst_5d = float(returns.rolling(5).sum().min()) if len(returns) >= 5 else worst_1d
    worst_21d = float(returns.rolling(21).sum().min()) if len(returns) >= 21 else worst_1d

    equity = (1 + returns).cumprod()
    peak = equity.cummax()
    drawdown = (equity - peak) / peak
    max_dd_pct = float(drawdown.min())

    # Max drawdown duration in days (rough — counts bars below peak)
    below_peak = (drawdown < 0).astype(int)
    if below_peak.any():
        # consecutive runs of "below peak"
        groups = (below_peak.diff() != 0).cumsum()
        runs = below_peak.groupby(groups).sum()
        max_dd_days = int(runs.max())
    else:
        max_dd_days = 0

    skew = float(returns.skew()) if len(returns) > 3 else 0.0
    kurt = float(returns.kurt()) if len(returns) > 4 else 0.0  # pandas .kurt() returns excess kurtosis

    return {
        "worst_1d_return": worst_1d,
        "worst_5d_return": worst_5d,
        "worst_month_return": worst_21d,
        "max_drawdown_pct": max_dd_pct,
        "max_drawdown_duration_days": max_dd_days,
        "skewness": skew,
        "excess_kurtosis": kurt,
    }


def greek_summary(greeks_df: pd.DataFrame) -> dict[str, object]:
    if greeks_df.empty:
        return {"no_greeks": True}

    def _abs(col): return greeks_df[col].abs().dropna() if col in greeks_df.columns else pd.Series(dtype=float)
    def _sign_of(col):
        if col not in greeks_df.columns:
            return "neutral"
        net = greeks_df[col].sum()
        if net > 1.0:
            return "long"
        if net < -1.0:
            return "short"
        return "neutral"

    out = {}
    for g in ["delta", "gamma", "vega", "theta"]:
        abs_series = _abs(g)
        if not abs_series.empty:
            out[f"mean_abs_{g}"] = float(abs_series.mean())
            out[f"max_abs_{g}"] = float(abs_series.max())
    out["net_vega_sign"] = _sign_of("vega")
    out["net_gamma_sign"] = _sign_of("gamma")
    out["net_delta_sign"] = _sign_of("delta")
    return out


def concentration_stats(positions: pd.DataFrame | pd.Series) -> dict[str, object]:
    """Per-ticker and per-sector concentration. Single-asset → degenerate."""
    if isinstance(positions, pd.Series):
        # Single-asset: only one ticker held throughout
        return {
            "n_unique_tickers_held": 1,
            "max_ticker_share_pct": 100.0,
            "max_sector_share_pct": None,
            "single_asset": True,
        }
    # Cross-sectional: positions has columns per ticker
    abs_notional = positions.abs()
    total_per_bar = abs_notional.sum(axis=1)
    pct_per_ticker = abs_notional.div(total_per_bar, axis=0)
    max_share = float(pct_per_ticker.max().max() * 100)
    return {
        "n_unique_tickers_held": int((abs_notional > 1e-9).any(axis=0).sum()),
        "max_ticker_share_pct": max_share,
        "max_sector_share_pct": None,  # TODO: requires sector mapping in features
        "single_asset": False,
    }


def event_window_concentration(positions: pd.Series | pd.DataFrame,
                                event_dates_csv: Path | None = None) -> float:
    """Percentage of position-days within ±3 days of any high-impact event.

    Reads event dates from adapters/event_calendar output. If file not
    available, returns -1.0 to signal "not computed".
    """
    if event_dates_csv is None or not Path(event_dates_csv).exists():
        return -1.0
    events = pd.read_csv(event_dates_csv, parse_dates=["date"])
    if events.empty:
        return 0.0
    # Build set of "in-window" dates
    windows = set()
    for d in events["date"]:
        for offset in range(-3, 4):
            windows.add((d + pd.Timedelta(days=offset)).normalize())
    # Check positions index
    if isinstance(positions, pd.DataFrame):
        in_window = sum(1 for ts in positions.index if ts.normalize() in windows)
        return float(in_window / len(positions) * 100)
    in_window = sum(1 for ts in positions.index if ts.normalize() in windows)
    return float(in_window / len(positions) * 100)


def compute_all(thesis_dir: Path) -> dict[str, object]:
    """Full deterministic risk-stats computation for one thesis."""
    pos_path = thesis_dir / "results" / "positions.csv"
    ret_path = thesis_dir / "results" / "returns.csv"
    greeks_path = thesis_dir / "results" / "greeks.csv"
    events_path = thesis_dir / "data" / "event_calendar.csv"

    if not pos_path.exists() or not ret_path.exists():
        return {"error": f"Missing required files in {thesis_dir}/results/"}

    positions_df = pd.read_csv(pos_path, index_col=0, parse_dates=True)
    if positions_df.shape[1] == 1:
        positions = positions_df.squeeze("columns")
    else:
        positions = positions_df
    returns = pd.read_csv(ret_path, index_col=0, parse_dates=True).squeeze("columns")

    out = {
        "position_stats": position_stats(positions if isinstance(positions, pd.Series)
                                          else positions.sum(axis=1)),
        "regime_breakdown": regime_breakdown(returns),
        "tail_metrics": tail_metrics(returns),
        "concentration": concentration_stats(positions),
    }
    out["event_window_concentration_pct"] = event_window_concentration(positions, events_path)

    if greeks_path.exists():
        greeks_df = pd.read_csv(greeks_path, index_col=0, parse_dates=True)
        out["greek_summary"] = greek_summary(greeks_df)
    else:
        out["greek_summary"] = {"no_greeks_file": True}

    return out


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    if not argv:
        print("Usage: python -m quant_validator.risk_stats <thesis_dir>")
        return 1
    thesis_dir = Path(argv[0])
    result = compute_all(thesis_dir)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
