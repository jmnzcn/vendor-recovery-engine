"""Generate a small world, run the engine on it and score it against the ground truth."""
import json

import pandas as pd
import pytest

from vre.evaluate import evaluate
from vre.generate import WorldConfig, generate
from vre.ingest import load_vendor_dir
from vre.reconcile import reconcile
from vre.reports import write_all


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    out = tmp_path_factory.mktemp("world")
    meta = generate(out, WorldConfig.small(vendors=10, po_lines=25_000, seed=123))
    return out, meta


@pytest.fixture(scope="module")
def result(world):
    loaded = load_vendor_dir(world[0] / "observed", workers=1)
    return loaded, reconcile(loaded)


def test_world_has_every_kind_of_problem(world):
    truth = pd.read_parquet(world[0] / "truth" / "truth.parquet")
    assert truth["category"].nunique() == 8
    expected = {"CLAIMABLE", "NOT_CLAIMABLE", "INSUFFICIENT_EVIDENCE", "ALREADY_RECOVERED", "EXPIRED"}
    assert expected <= set(truth["status"])


def test_engine_matches_ground_truth(world, result):
    ev = evaluate(result[1].findings, pd.read_parquet(world[0] / "truth" / "truth.parquet"))
    assert ev["missing_findings"] == 0 and ev["unexpected_findings"] == 0
    assert ev["claimable_precision"] == 1.0
    assert ev["claimable_recall"] == 1.0
    assert ev["false_claim_usd"] == 0
    assert ev["status_accuracy"] == 1.0


def test_generation_is_deterministic(tmp_path):
    a = generate(tmp_path / "a", WorldConfig.small(vendors=4, po_lines=4_000, seed=5))
    b = generate(tmp_path / "b", WorldConfig.small(vendors=4, po_lines=4_000, seed=5))
    ta = pd.read_parquet(tmp_path / "a" / "truth" / "truth.parquet")
    tb = pd.read_parquet(tmp_path / "b" / "truth" / "truth.parquet")
    assert a["rows"] == b["rows"]
    pd.testing.assert_frame_equal(ta, tb)


def test_reports_are_written(world, result, tmp_path):
    loaded, res = result
    vendors = pd.read_csv(world[0] / "vendors.csv")
    summary = write_all(res.findings, vendors, pd.DataFrame(loaded.stats), tmp_path, "2026-07-31",
                        pdf_vendors=list(vendors["vendor_code"][:2]))
    assert summary["recoverable_usd"] > 0
    assert json.loads((tmp_path / "summary.json").read_text())["claims"] == summary["claims"]
    claims = pd.read_csv(tmp_path / "claims_all_vendors.csv")
    assert len(claims) == summary["claims"] and claims["source"].str.contains(r":\d+$").all()
    code = vendors["vendor_code"].iat[0]
    assert (tmp_path / "vendors" / code / f"recovery_{code}.pdf").stat().st_size > 2000
    assert (tmp_path / "vendors" / code / f"recovery_{code}.xlsx").exists()
