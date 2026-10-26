"""Score the engine against the generator's ground truth.

The engine never reads the truth table; this module compares the two afterwards.
"""
from __future__ import annotations

import pandas as pd

from vre.policy import Status

KEY = ["vendor_code", "ref_type", "ref"]


def evaluate(findings: pd.DataFrame, truth: pd.DataFrame) -> dict:
    f = findings[[*KEY, "category", "status", "recoverable_usd_cents", "expired_usd_cents"]]
    t = truth[[*KEY, "category", "status", "recoverable_usd_cents", "expired_usd_cents"]]
    j = t.merge(f, on=KEY, how="outer", suffixes=("_true", "_pred"), indicator=True)
    j["status_true"] = j["status_true"].fillna("NO_FINDING")
    j["status_pred"] = j["status_pred"].fillna("NO_FINDING")
    for c in ("recoverable_usd_cents_true", "recoverable_usd_cents_pred"):
        j[c] = j[c].fillna(0).astype("int64")

    claim_true = j["status_true"] == Status.CLAIMABLE
    claim_pred = j["status_pred"] == Status.CLAIMABLE
    tp = claim_true & claim_pred
    category_ok = j["category_true"] == j["category_pred"]

    per_cat = []
    for cat, g in j.groupby(j["category_true"].fillna(j["category_pred"])):
        ct, cp = g["status_true"] == Status.CLAIMABLE, g["status_pred"] == Status.CLAIMABLE
        hit = ct & cp
        per_cat.append({
            "category": cat, "truth_rows": int(g["status_true"].ne("NO_FINDING").sum()),
            "claimable_true": int(ct.sum()), "claimable_pred": int(cp.sum()), "true_positives": int(hit.sum()),
            "precision": hit.sum() / cp.sum() if cp.sum() else 1.0,
            "recall": hit.sum() / ct.sum() if ct.sum() else 1.0,
            "recoverable_true_usd": g.loc[ct, "recoverable_usd_cents_true"].sum() / 100,
            "recovered_usd": g.loc[hit, "recoverable_usd_cents_pred"].sum() / 100,
            "false_claim_usd": g.loc[cp & ~ct, "recoverable_usd_cents_pred"].sum() / 100,
            "status_accuracy": float((g["status_true"] == g["status_pred"]).mean()),
        })

    amount_exact = (j.loc[tp, "recoverable_usd_cents_true"] == j.loc[tp, "recoverable_usd_cents_pred"]).mean()
    confusion = pd.crosstab(j["status_true"], j["status_pred"])
    return {
        "rows_truth": int((j["_merge"] != "right_only").sum()),
        "rows_findings": int((j["_merge"] != "left_only").sum()),
        "missing_findings": int((j["_merge"] == "left_only").sum()),
        "unexpected_findings": int((j["_merge"] == "right_only").sum()),
        "status_accuracy": float((j["status_true"] == j["status_pred"]).mean()),
        "category_accuracy": float(category_ok[j["_merge"] == "both"].mean()),
        "claimable_precision": float(tp.sum() / claim_pred.sum()) if claim_pred.sum() else 1.0,
        "claimable_recall": float(tp.sum() / claim_true.sum()) if claim_true.sum() else 1.0,
        "amount_exact_rate": float(amount_exact) if tp.any() else 1.0,
        "recoverable_true_usd": j.loc[claim_true, "recoverable_usd_cents_true"].sum() / 100,
        "recovered_usd": j.loc[tp, "recoverable_usd_cents_pred"].sum() / 100,
        "false_claim_usd": j.loc[claim_pred & ~claim_true, "recoverable_usd_cents_pred"].sum() / 100,
        "review_routed": int(((j["status_true"] == Status.NEEDS_REVIEW)
                              & (j["status_pred"] == Status.NEEDS_REVIEW)).sum()),
        "review_true": int((j["status_true"] == Status.NEEDS_REVIEW).sum()),
        "by_category": pd.DataFrame(per_cat),
        "confusion": confusion,
        "mismatches": j[(j["status_true"] != j["status_pred"]) | (tp & (j["recoverable_usd_cents_true"]
                                                                       != j["recoverable_usd_cents_pred"]))],
    }
