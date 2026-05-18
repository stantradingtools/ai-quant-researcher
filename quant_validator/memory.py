"""quant_validator.memory: Extended ResearchMemory for Stan's fork.

Adds to upstream ai_quant_lab.agents.memory.ResearchMemory:
- market_type, deployment_status, size_multiplier columns
- trial_greeks table for per-trial Greek summary stats
- override_log on trial rows for subjective override audit
- portfolio_greeks on-demand aggregation from latest backtest exit positions
- seed_historical for honest n_trials initialization
- CLI for the memory subagent to invoke via Bash

Usage from CLI:
    python -m quant_validator.memory status
    python -m quant_validator.memory recent --limit 10
    python -m quant_validator.memory portfolio_greeks
    python -m quant_validator.memory correlation --new <thesis_id>
    python -m quant_validator.memory seed_historical --count 30 --note "..."
    python -m quant_validator.memory deploy --thesis_id <id> --status paper --size 0.5
    python -m quant_validator.memory apply_override --thesis_id <id> --failure <key> --reason "..."
    python -m quant_validator.memory overrides
    python -m quant_validator.memory record --thesis_id <id> --accepted true --size_multiplier 0.5
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


# Default database location, overrideable via env or CLI flag
DB_PATH = Path("./memory.db")


# ═══════════════════════════════════════════════════════════════
# Schema and migrations

_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS trials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis_id TEXT NOT NULL,
    hypothesis_text TEXT NOT NULL,
    rationale TEXT,
    code TEXT,
    metrics_json TEXT NOT NULL,
    accepted INTEGER NOT NULL,
    rejection_reason TEXT,
    n_trials_at_time INTEGER NOT NULL,
    iteration INTEGER NOT NULL,
    returns_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trials_accepted ON trials(accepted);
CREATE INDEX IF NOT EXISTS idx_trials_iteration ON trials(iteration);
"""

_FORK_COLUMNS = [
    ("market_type", "TEXT"),
    ("deployment_status", "TEXT DEFAULT 'archived'"),
    ("paper_start_date", "TEXT"),
    ("live_start_date", "TEXT"),
    ("size_multiplier", "REAL DEFAULT 1.0"),
    ("override_log_json", "TEXT"),
]

_TRIAL_GREEKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS trial_greeks (
    trial_id INTEGER PRIMARY KEY REFERENCES trials(id),
    mean_abs_delta REAL,
    max_abs_delta REAL,
    mean_abs_gamma REAL,
    max_abs_gamma REAL,
    mean_abs_vega REAL,
    max_abs_vega REAL,
    mean_abs_theta REAL,
    net_vega_sign TEXT,
    net_gamma_sign TEXT
);
"""


def _connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Open connection and run all migrations."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row

    # Base schema
    for stmt in _BASE_SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)

    # Fork columns (idempotent — only add if missing)
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(trials)")}
    for col_name, col_type in _FORK_COLUMNS:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE trials ADD COLUMN {col_name} {col_type}")

    # Trial Greeks side table
    for stmt in _TRIAL_GREEKS_SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)

    return conn


# ═══════════════════════════════════════════════════════════════
# Read queries

def status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return high-level memory status for /memory-status command."""
    total = conn.execute("SELECT COUNT(*) AS c FROM trials").fetchone()["c"]
    accepted = conn.execute("SELECT COUNT(*) AS c FROM trials WHERE accepted = 1").fetchone()["c"]
    deployed = conn.execute(
        "SELECT COUNT(*) AS c FROM trials WHERE deployment_status IN ('paper', 'live')"
    ).fetchone()["c"]
    overrides = conn.execute(
        "SELECT COUNT(*) AS c FROM trials WHERE override_log_json IS NOT NULL"
    ).fetchone()["c"]
    last_row = conn.execute(
        "SELECT created_at FROM trials ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last_date = last_row["created_at"] if last_row else None

    return {
        "total_trials": total,
        "accepted_count": accepted,
        "current_dsr_n_trials": total,
        "deployed_strategies": deployed,
        "override_count": overrides,
        "last_trial_date": last_date,
    }


def recent(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    """Return recent N trials in reverse chronological order."""
    rows = conn.execute(
        """
        SELECT hypothesis_id, hypothesis_text, metrics_json, accepted,
               rejection_reason, deployment_status, created_at
        FROM trials ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out = []
    for row in rows:
        metrics = json.loads(row["metrics_json"]) if row["metrics_json"] else {}
        out.append({
            "hypothesis_id": row["hypothesis_id"],
            "hypothesis_text": row["hypothesis_text"][:80],
            "sharpe": metrics.get("sharpe_ratio", 0.0),
            "accepted": bool(row["accepted"]),
            "verdict": "ACCEPT" if row["accepted"] else f"REJECT ({row['rejection_reason'] or 'unspec'})",
            "deployment_status": row["deployment_status"],
            "created_at": row["created_at"],
        })
    return out


def overrides_audit(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every override applied so far, chronologically."""
    rows = conn.execute(
        """
        SELECT hypothesis_id, override_log_json, created_at
        FROM trials WHERE override_log_json IS NOT NULL
        ORDER BY created_at
        """
    ).fetchall()
    out = []
    for row in rows:
        try:
            log = json.loads(row["override_log_json"])
            for entry in log:
                out.append({
                    "hypothesis_id": row["hypothesis_id"],
                    "trial_created_at": row["created_at"],
                    **entry,
                })
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def n_trials(conn: sqlite3.Connection) -> int:
    """Total trials for DSR."""
    return conn.execute("SELECT COUNT(*) AS c FROM trials").fetchone()["c"]


def accepted_returns(conn: sqlite3.Connection) -> list[pd.Series]:
    """All accepted return series for the correlation gate."""
    rows = conn.execute(
        "SELECT hypothesis_id, returns_json FROM trials WHERE accepted = 1 ORDER BY id"
    ).fetchall()
    out = []
    for row in rows:
        payload = row["returns_json"]
        if not payload:
            continue
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or "index" not in data or "values" not in data:
            continue
        try:
            idx = pd.to_datetime(data["index"])
        except (TypeError, ValueError):
            idx = pd.Index(data["index"])
        out.append(pd.Series(data["values"], index=idx, name=row["hypothesis_id"]))
    return out


def correlation_with_survivors(
    conn: sqlite3.Connection, new_thesis_id: str
) -> list[dict[str, Any]]:
    """Pairwise correlation of new thesis's returns with each accepted survivor.

    Reads returns from theses/<new_thesis_id>/results/returns.csv.
    """
    new_path = Path(f"theses/{new_thesis_id}/results/returns.csv")
    if not new_path.exists():
        raise FileNotFoundError(f"Returns file not found: {new_path}")
    new_returns = pd.read_csv(new_path, index_col=0, parse_dates=True).squeeze("columns")

    survivors = accepted_returns(conn)
    out = []
    for s in survivors:
        if s.name == new_thesis_id:
            continue
        aligned_new, aligned_s = new_returns.align(s, join="inner")
        if len(aligned_new) < 30:
            continue
        corr = float(aligned_new.corr(aligned_s))
        out.append({
            "survivor_id": s.name,
            "correlation": corr,
            "abs_correlation": abs(corr),
            "n_overlap_bars": len(aligned_new),
        })
    out.sort(key=lambda r: r["abs_correlation"], reverse=True)
    return out


# ═══════════════════════════════════════════════════════════════
# Portfolio Greeks (on-demand from latest backtest exit positions)

def portfolio_greeks(conn: sqlite3.Connection) -> dict[str, Any]:
    """Aggregate Greeks from latest backtest exit positions across all
    deployed strategies, scaled by each strategy's size_multiplier.
    """
    deployed = conn.execute(
        """
        SELECT hypothesis_id, deployment_status, size_multiplier, market_type
        FROM trials
        WHERE deployment_status IN ('paper', 'live')
        """
    ).fetchall()

    aggregate = {"net_delta": 0.0, "net_gamma": 0.0, "net_vega": 0.0, "net_theta": 0.0}
    per_strategy = []

    for s in deployed:
        if s["market_type"] != "options":
            continue
        greeks_path = Path(f"theses/{s['hypothesis_id']}/results/greeks.csv")
        if not greeks_path.exists():
            continue
        try:
            df = pd.read_csv(greeks_path, index_col=0, parse_dates=True)
        except Exception:
            continue
        if df.empty:
            continue
        last_row = df.iloc[-1]
        size_mult = s["size_multiplier"] or 1.0
        scaled = {
            "delta": float(last_row.get("delta", 0.0) * size_mult),
            "gamma": float(last_row.get("gamma", 0.0) * size_mult),
            "vega": float(last_row.get("vega", 0.0) * size_mult),
            "theta": float(last_row.get("theta", 0.0) * size_mult),
        }
        for k in ["delta", "gamma", "vega", "theta"]:
            aggregate[f"net_{k}"] += scaled[k]
        per_strategy.append({
            "hypothesis_id": s["hypothesis_id"],
            "deployment_status": s["deployment_status"],
            "size_multiplier": size_mult,
            **scaled,
            "as_of": str(last_row.name),
        })

    return {
        "aggregate": aggregate,
        "per_strategy": per_strategy,
        "method": "backtest_exit_positions",
        "note": "Greeks reflect last bar of each strategy's backtest, NOT today's live position.",
    }


# ═══════════════════════════════════════════════════════════════
# Write operations

def record_trial(
    conn: sqlite3.Connection,
    *,
    thesis_id: str,
    accepted: bool,
    size_multiplier: float = 1.0,
    market_type: str | None = None,
    override_log: list[dict] | None = None,
) -> int:
    """Insert a trial row by reading thesis files from theses/<thesis_id>/.

    Reads:
      theses/<thesis_id>/refined.json   for hypothesis_text, rationale
      theses/<thesis_id>/code/signal.py for code source
      theses/<thesis_id>/results/metrics.json  for metrics
      theses/<thesis_id>/results/returns.csv   for returns_json
      theses/<thesis_id>/decision.json  for rejection_reason if any
    """
    base = Path(f"theses/{thesis_id}")
    refined = json.loads((base / "refined.json").read_text())

    code_path = base / "code" / "signal.py"
    code = code_path.read_text() if code_path.exists() else ""

    metrics_path = base / "results" / "metrics.json"
    metrics = json.loads(metrics_path.read_text()) if metrics_path.exists() else {}

    decision_path = base / "decision.json"
    rejection_reason = None
    if decision_path.exists():
        decision = json.loads(decision_path.read_text())
        rejection_reason = decision.get("rejection_reason")

    returns_path = base / "results" / "returns.csv"
    returns_json = ""
    if returns_path.exists() and accepted:
        df = pd.read_csv(returns_path, index_col=0, parse_dates=True)
        series = df.squeeze("columns")
        returns_json = json.dumps({
            "index": [str(t) for t in series.index],
            "values": [float(v) for v in series.tolist()],
        })

    market_type = market_type or refined.get("market_type")
    override_log_json = json.dumps(override_log) if override_log else None
    n_at_time = n_trials(conn)

    cursor = conn.execute(
        """
        INSERT INTO trials (
            hypothesis_id, hypothesis_text, rationale, code,
            metrics_json, accepted, rejection_reason,
            n_trials_at_time, iteration, returns_json, created_at,
            market_type, deployment_status, size_multiplier, override_log_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            refined["hypothesis_id"],
            refined["title"],
            refined.get("rationale", ""),
            code,
            json.dumps(metrics),
            int(accepted),
            rejection_reason,
            n_at_time,
            0,
            returns_json,
            datetime.now(timezone.utc).isoformat(),
            market_type,
            "archived",
            size_multiplier,
            override_log_json,
        ),
    )
    return int(cursor.lastrowid) if cursor.lastrowid else -1


def seed_historical(
    conn: sqlite3.Connection, count: int, note: str = "pre_system_seed"
) -> int:
    """Insert N placeholder rows so DSR's n_trials starts honest.

    These are accepted=0, rejection_reason='pre_system_seed', no returns.
    They count toward n_trials() but not toward correlation gate.
    """
    inserted = 0
    for i in range(count):
        conn.execute(
            """
            INSERT INTO trials (
                hypothesis_id, hypothesis_text, rationale, code,
                metrics_json, accepted, rejection_reason,
                n_trials_at_time, iteration, returns_json, created_at,
                market_type, deployment_status, size_multiplier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"seed_{i+1:03d}",
                f"Pre-system trial seed {i+1}",
                note,
                "",
                "{}",
                0,
                "pre_system_seed",
                inserted,
                0,
                "",
                datetime.now(timezone.utc).isoformat(),
                None,
                "archived",
                1.0,
            ),
        )
        inserted += 1
    return inserted


def deploy(
    conn: sqlite3.Connection,
    thesis_id: str,
    status: str,
    size_multiplier: float,
) -> bool:
    """Update deployment status of an accepted trial."""
    if status not in ("paper", "live", "retired", "archived"):
        raise ValueError(f"Invalid deployment_status: {status}")

    date_field = None
    if status == "paper":
        date_field = "paper_start_date"
    elif status == "live":
        date_field = "live_start_date"

    today = datetime.now(timezone.utc).date().isoformat()
    if date_field:
        conn.execute(
            f"""
            UPDATE trials
            SET deployment_status = ?, size_multiplier = ?, {date_field} = ?
            WHERE hypothesis_id = ? AND accepted = 1
            """,
            (status, size_multiplier, today, thesis_id),
        )
    else:
        conn.execute(
            """
            UPDATE trials
            SET deployment_status = ?, size_multiplier = ?
            WHERE hypothesis_id = ? AND accepted = 1
            """,
            (status, size_multiplier, thesis_id),
        )
    return conn.total_changes > 0


def apply_override(
    conn: sqlite3.Connection,
    thesis_id: str,
    failure_key: str,
    reason: str,
) -> bool:
    """Append an override entry to the trial's override_log_json."""
    row = conn.execute(
        "SELECT id, override_log_json FROM trials WHERE hypothesis_id = ? ORDER BY id DESC LIMIT 1",
        (thesis_id,),
    ).fetchone()
    if not row:
        return False

    existing = json.loads(row["override_log_json"]) if row["override_log_json"] else []
    existing.append({
        "failure": failure_key,
        "reason": reason,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    conn.execute(
        "UPDATE trials SET override_log_json = ?, accepted = 1, rejection_reason = NULL WHERE id = ?",
        (json.dumps(existing), row["id"]),
    )
    return True


# ═══════════════════════════════════════════════════════════════
# CLI

def _print_table(rows: list[dict], cols: list[str]) -> None:
    """Pretty-print a list of dicts as a Markdown-style table."""
    if not rows:
        print("(no rows)")
        return
    widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("| " + " | ".join(c.ljust(widths[c]) for c in cols) + " |")
    print("|" + "|".join("-" * (widths[c] + 2) for c in cols) + "|")
    for r in rows:
        print("| " + " | ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols) + " |")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quant_validator.memory")
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite database path")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")

    p_recent = sub.add_parser("recent")
    p_recent.add_argument("--limit", type=int, default=10)

    sub.add_parser("portfolio_greeks")

    p_corr = sub.add_parser("correlation")
    p_corr.add_argument("--new", required=True, help="thesis_id of new candidate")

    p_seed = sub.add_parser("seed_historical")
    p_seed.add_argument("--count", type=int, required=True)
    p_seed.add_argument("--note", default="pre_system_seed")

    p_deploy = sub.add_parser("deploy")
    p_deploy.add_argument("--thesis_id", required=True)
    p_deploy.add_argument("--status", required=True, choices=["paper", "live", "retired", "archived"])
    p_deploy.add_argument("--size", type=float, required=True)

    p_or = sub.add_parser("apply_override")
    p_or.add_argument("--thesis_id", required=True)
    p_or.add_argument("--failure", required=True)
    p_or.add_argument("--reason", required=True)

    sub.add_parser("overrides")

    p_rec = sub.add_parser("record")
    p_rec.add_argument("--thesis_id", required=True)
    p_rec.add_argument("--accepted", required=True, choices=["true", "false"])
    p_rec.add_argument("--size_multiplier", type=float, default=1.0)

    args = parser.parse_args(argv)
    conn = _connect(Path(args.db))

    try:
        if args.cmd == "status":
            print(json.dumps(status(conn), indent=2))
        elif args.cmd == "recent":
            rows = recent(conn, args.limit)
            _print_table(rows, ["hypothesis_id", "sharpe", "verdict", "deployment_status", "created_at"])
        elif args.cmd == "portfolio_greeks":
            print(json.dumps(portfolio_greeks(conn), indent=2))
        elif args.cmd == "correlation":
            rows = correlation_with_survivors(conn, args.new)
            _print_table(rows, ["survivor_id", "correlation", "abs_correlation", "n_overlap_bars"])
        elif args.cmd == "seed_historical":
            n = seed_historical(conn, args.count, args.note)
            print(f"Seeded {n} historical trial placeholders.")
        elif args.cmd == "deploy":
            ok = deploy(conn, args.thesis_id, args.status, args.size)
            print(f"Deploy {'succeeded' if ok else 'failed (thesis not found or not accepted)'}")
        elif args.cmd == "apply_override":
            ok = apply_override(conn, args.thesis_id, args.failure, args.reason)
            print(f"Override {'applied' if ok else 'failed'}")
        elif args.cmd == "overrides":
            rows = overrides_audit(conn)
            _print_table(rows, ["hypothesis_id", "failure", "reason", "timestamp"])
        elif args.cmd == "record":
            trial_id = record_trial(
                conn,
                thesis_id=args.thesis_id,
                accepted=(args.accepted == "true"),
                size_multiplier=args.size_multiplier,
            )
            print(f"Recorded trial id={trial_id}")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
