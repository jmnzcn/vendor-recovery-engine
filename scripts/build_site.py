"""Build docs/site/data.json for the results page (docs/index.html) from a finished run.

Usage: python scripts/build_site.py [out_dir] [world_dir]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
TOP_CLAIMS_PER_VENDOR = 15


def main(out: Path, world: Path) -> None:
    f = pd.read_parquet(out / "findings.parquet")
    meta = json.loads((world / "world.json").read_text(encoding="utf-8"))
    score = json.loads((out / "scorecard.json").read_text(encoding="utf-8"))
    quality = json.loads((out / "quality.json").read_text(encoding="utf-8"))
    vendors = pd.read_csv(world / "vendors.csv").set_index("vendor_code")

    by_cat = f.groupby("category").agg(
        findings=("ref", "size"), claims=("status", lambda s: int((s == "CLAIMABLE").sum())),
        recoverable=("recoverable_usd_cents", "sum"), expired=("expired_usd_cents", "sum")).reset_index()
    by_status = f.groupby(["category", "status"]).size().rename("n").reset_index()

    per_vendor = []
    for code, g in f.groupby("vendor_code"):
        # The two largest claims of each category, so the sample shows every kind of evidence
        c = g[g["status"] == "CLAIMABLE"]
        claims = (c.sort_values("recoverable_usd_cents", ascending=False).groupby("category").head(2)
                  .nlargest(TOP_CLAIMS_PER_VENDOR, "recoverable_usd_cents"))
        per_vendor.append({
            "code": code, "name": vendors.at[code, "vendor_name"], "currency": vendors.at[code, "currency"],
            "format": vendors.at[code, "file_format"],
            "findings": len(g), "claims": int((g["status"] == "CLAIMABLE").sum()),
            "recoverable": int(g["recoverable_usd_cents"].sum()), "expired": int(g["expired_usd_cents"].sum()),
            "status": g["status"].value_counts().to_dict(),
            "top": [[r.ref, r.category, int(r.recoverable_usd_cents), r.dispute_deadline.strftime("%Y-%m-%d"),
                     r.match_tier, r.reason, r.evidence, f"{r.source_file}:{int(r.source_row)}"]
                    for r in claims.itertuples()],
        })
    per_vendor.sort(key=lambda v: -v["recoverable"])

    # Recoverable money by the month its dispute window closes
    claims = f[f["status"] == "CLAIMABLE"]
    by_month = (claims.assign(month=claims["dispute_deadline"].dt.strftime("%Y-%m"))
                .groupby("month").agg(claims=("ref", "size"), recoverable=("recoverable_usd_cents", "sum"),
                                       urgent=("urgent", "sum")).reset_index())
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))

    drafts = {}
    if (out / "drafts" / "summary.json").exists():
        drafts = json.loads((out / "drafts" / "summary.json").read_text(encoding="utf-8"))
        letters = pd.read_json(out / "drafts" / "drafts.jsonl", lines=True)
        sample = letters[letters["category"].isin(["SHORTAGE", "FX_VARIANCE"]) & letters["passed"]]
        drafts["samples"] = sample.groupby("category").head(1)[["ref", "category", "draft"]].to_dict("records")
    robustness = {}
    if (ROOT / "docs" / "site" / "robustness.json").exists():
        robustness = json.loads((ROOT / "docs" / "site" / "robustness.json").read_text(encoding="utf-8"))
        robustness.pop("runs", None)

    data = {
        "as_of": meta["policy"]["as_of"], "seed": meta["config"]["seed"],
        "rows": meta["observed_rows_total"], "rows_by_table": meta["rows"],
        "vendors": len(per_vendor), "findings": len(f),
        "recoverable": int(f["recoverable_usd_cents"].sum()), "expired": int(f["expired_usd_cents"].sum()),
        "claims": int((f["status"] == "CLAIMABLE").sum()),
        "by_category": by_cat.to_dict("records"), "by_status": by_status.to_dict("records"),
        "scorecard": {k: score[k] for k in ("rows_truth", "status_accuracy", "claimable_precision",
                                            "claimable_recall", "amount_exact_rate", "false_claim_usd",
                                            "recovered_usd", "review_routed", "review_true")},
        "scorecard_by_category": score["by_category"],
        "quality": quality,
        "urgent_usd": summary["urgent_usd"], "urgent_claims": summary["urgent_claims"],
        "expected_value_usd": summary["expected_value_usd"],
        "by_deadline_month": by_month.to_dict("records"),
        "drafts": drafts, "robustness": robustness,
        "vendors_list": per_vendor,
    }
    dest = ROOT / "docs" / "site" / "data.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, separators=(",", ":"), default=str), encoding="utf-8")
    print(f"{dest.relative_to(ROOT)}: {dest.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    args = [Path(a) for a in sys.argv[1:]]
    main(args[0] if args else ROOT / "out", args[1] if len(args) > 1 else ROOT / "data" / "world")
