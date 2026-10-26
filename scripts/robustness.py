"""Robustness curve: degrade the evidence and the references, measure what happens.

Same underlying world at every level (same deductions, same real outcomes); only what the
exports can prove changes: more missing proofs of delivery, missing shipment records,
truncated and mistyped references. Scored against what really happened.

Usage: python scripts/robustness.py [--vendors 30] [--po-lines 100000] [--seeds 2]
Writes docs/site/robustness.json and prints a table.
"""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import time
from pathlib import Path

import pandas as pd

from vre.evaluate import evaluate
from vre.generate import WorldConfig, generate
from vre.ingest import load_vendor_dir
from vre.reconcile import reconcile

ROOT = Path(__file__).resolve().parent.parent
LEVELS = [0, 1, 2, 3, 5, 7, 10]


def run_level(noise: float, seed: int, vendors: int, po_lines: int, tmp: Path) -> dict:
    cfg = WorldConfig(vendors=vendors, po_lines=po_lines, seed=seed, noise=noise)
    world = tmp / f"n{noise}_s{seed}"
    generate(world, cfg)
    findings = reconcile(load_vendor_dir(world / "observed")).findings
    ev = evaluate(findings, pd.read_parquet(world / "truth" / "truth.parquet"))
    shutil.rmtree(world, ignore_errors=True)
    missed = ev["real_missed_by_status_usd"]
    return {
        "noise": noise, "seed": seed,
        "pod_missing_rate": cfg.rate(0.06), "shipment_missing_rate": cfg.rate(0.01),
        "truncated_ref_rate": cfg.rate(0.06, 0.3), "typo_ref_rate": cfg.rate(0.04, 0.3),
        "verdict_mismatches": len(ev["mismatches"]),
        "real_recoverable_usd": ev["real_recoverable_usd"],
        "recovered_usd": ev["recovered_usd"],
        "real_precision": ev["real_precision"], "real_recall_usd": ev["real_recall_usd"],
        "real_false_claim_usd": ev["real_false_claim_usd"],
        "missed_insufficient_usd": missed.get("INSUFFICIENT_EVIDENCE", 0.0),
        "missed_review_usd": missed.get("NEEDS_REVIEW", 0.0),
        "review_rows": int((findings["status"] == "NEEDS_REVIEW").sum()),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendors", type=int, default=30)
    ap.add_argument("--po-lines", type=int, default=100_000)
    ap.add_argument("--seeds", type=int, default=2)
    args = ap.parse_args()

    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for seed in range(1, args.seeds + 1):
            for noise in LEVELS:
                t0 = time.perf_counter()
                r = run_level(noise, 9000 + seed, args.vendors, args.po_lines, Path(tmp))
                rows.append(r)
                print(f"noise {noise:>4} seed {r['seed']}: precision {r['real_precision']:.4f}  "
                      f"recall {r['real_recall_usd']:.4f}  false ${r['real_false_claim_usd']:,.2f}  "
                      f"mismatches {r['verdict_mismatches']}  ({time.perf_counter() - t0:.0f}s)", flush=True)

    df = pd.DataFrame(rows)
    agg = df.groupby("noise").agg(
        pod_missing_rate=("pod_missing_rate", "first"), truncated_ref_rate=("truncated_ref_rate", "first"),
        typo_ref_rate=("typo_ref_rate", "first"), shipment_missing_rate=("shipment_missing_rate", "first"),
        real_precision=("real_precision", "min"), real_recall_usd=("real_recall_usd", "mean"),
        real_false_claim_usd=("real_false_claim_usd", "sum"), verdict_mismatches=("verdict_mismatches", "sum"),
        real_recoverable_usd=("real_recoverable_usd", "sum"), recovered_usd=("recovered_usd", "sum"),
        missed_insufficient_usd=("missed_insufficient_usd", "sum"), missed_review_usd=("missed_review_usd", "sum"),
    ).reset_index()
    print(agg.round(4).to_string(index=False))
    out = {"config": {"vendors": args.vendors, "po_lines": args.po_lines, "seeds": args.seeds},
           "levels": agg.to_dict("records"), "runs": rows}
    dest = ROOT / "docs" / "site" / "robustness.json"
    dest.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"wrote {dest.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
