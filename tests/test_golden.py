"""Conformance: a hand-built dataset where every rule has a case and an expected answer.

tests/golden/expected_findings.csv is written by hand, not generated. If a change to the
engine alters any of these answers, this test fails and the change has to justify it.
"""
from pathlib import Path

import pandas as pd
import pytest

from vre.ingest import load_vendor_dir
from vre.reconcile import reconcile

GOLDEN = Path(__file__).parent / "golden"
KEY = ["vendor_code", "ref_type", "ref"]


@pytest.fixture(scope="module")
def run():
    loaded = load_vendor_dir(GOLDEN / "observed", workers=1)
    return loaded, reconcile(loaded)


@pytest.fixture(scope="module")
def expected():
    return pd.read_csv(GOLDEN / "expected_findings.csv", keep_default_na=False)


def test_every_expected_finding_matches(run, expected):
    findings = run[1].findings
    got = expected.merge(findings, on=KEY, how="left", suffixes=("", "_got"))
    for col in ("category", "status", "recoverable_usd_cents", "expired_usd_cents", "match_tier"):
        diff = got[got[col].astype(str) != got[f"{col}_got"].astype(str)]
        assert diff.empty, f"{col} differs:\n{diff[[*KEY, col, f'{col}_got', 'note']]}"


def test_no_unexpected_findings(run, expected):
    extra = run[1].findings.merge(expected[KEY], on=KEY, how="left", indicator=True)
    assert (extra["_merge"] == "both").all(), extra[extra["_merge"] != "both"][KEY]


def test_quarantine_points_at_the_exact_file_line(run):
    q = run[0].quarantine_frame().set_index("_source_file")
    assert q.loc["G0001/remittances.csv", "_source_row"] == 8          # TOTAL line
    assert q.loc["G0001/remittances.csv", "reason"] == "summary row"
    assert q.loc["G0001/deductions.csv", "_source_row"] == 17          # pending row, no number yet
    assert "deductionNumber" in q.loc["G0001/deductions.csv", "reason"]


def test_export_duplicates_are_merged_not_flagged(run):
    loaded, result = run
    d = loaded.tables["deductions"]
    assert (d["deductionNumber"] == "SC100000001").sum() == 1
    assert result.quality["remittance_duplicates_after_matching"] == 1  # 'inv 0001001' == 'INV-0001001'
    assert result.quality["remittance_lines_matched_by_po"] == 1        # paid quoting the PO number


def test_european_formats_are_parsed(run):
    inv = run[0].tables["invoices"].set_index("invoiceNumber")
    assert inv.loc["AB-000123", "netCost"] == 1000                      # "10,00" -> 1000 cents
    rem = run[0].tables["remittances"].set_index("invoiceNumber")
    assert rem.loc["AB-000123", "amountPaid"] == 111437                 # "1.114,37"
    assert rem.loc["AB-000123", "paymentDate"] == pd.Timestamp("2026-03-16")


def test_every_claim_is_traceable_to_a_source_row(run):
    claims = run[1].findings.query("status == 'CLAIMABLE'")
    assert claims["source_file"].notna().all() and claims["source_row"].notna().all()
    assert claims["reason"].str.len().gt(20).all()
