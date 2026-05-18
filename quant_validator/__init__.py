"""quant_validator: Stan's fork extensions on top of ai_quant_lab.

This package provides:
- memory: Extended SQLite schema with deployment status, Greek tracking, overrides
- audit: Append-only audit log helpers (JSONL)
- risk_stats: Deterministic risk statistics for the Risk subagent
- stats: Statistical metrics + DSR + walk-forward (mostly re-exports upstream)
- gates: Statistical gate evaluation (re-exports upstream + adds critic-validator gate)
- sandbox: Re-exports upstream sandbox with shape check for tuple returns

Most modules are thin wrappers over ai_quant_lab.* with Stan-specific extensions.
"""

__version__ = "0.1.0"
