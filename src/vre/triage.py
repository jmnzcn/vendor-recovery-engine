"""Work queue: which claims to file first.

Expected value = recoverable amount x assumed win rate (by category, discounted when the
reference was matched by suffix or typo). Claims whose dispute window closes within
`urgent_days` come first; within each group, highest expected value first.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from vre.policy import MATCH_CONFIDENCE, WIN_RATE, Policy, Status


def prioritize(findings: pd.DataFrame, policy: Policy) -> pd.DataFrame:
    f = findings.copy()
    as_of = pd.Timestamp(policy.as_of)
    claim = f["status"] == Status.CLAIMABLE
    f["days_left"] = np.where(claim, (f["dispute_deadline"] - as_of).dt.days, pd.NA)
    f["days_left"] = f["days_left"].astype("Int64")
    win = f["category"].map(WIN_RATE).fillna(0.5) * f["match_tier"].map(MATCH_CONFIDENCE).fillna(1.0)
    f["win_rate"] = np.where(claim, win.round(3), 0.0)
    f["expected_value_usd_cents"] = np.where(claim, (f["recoverable_usd_cents"] * win).round(), 0).astype("int64")
    f["urgent"] = claim & (f["days_left"] <= policy.urgent_days).fillna(False)

    f["priority_rank"] = pd.array([pd.NA] * len(f), dtype="Int64")
    q = f[claim].sort_values(["urgent", "expected_value_usd_cents"], ascending=[False, False])
    f.loc[q.index, "priority_rank"] = np.arange(1, len(q) + 1)
    return f


def queue_summary(findings: pd.DataFrame) -> dict:
    claim = findings["status"] == Status.CLAIMABLE
    urgent = findings["urgent"]
    return {
        "urgent_claims": int(urgent.sum()),
        "urgent_usd": round(findings.loc[urgent, "recoverable_usd_cents"].sum() / 100, 2),
        "expected_value_usd": round(findings.loc[claim, "expected_value_usd_cents"].sum() / 100, 2),
    }
