from pathlib import Path

import numpy as np
import pytest

from vre.disputes import facts_for, mutations, template_draft, verify
from vre.ingest import load_vendor_dir
from vre.policy import Policy
from vre.reconcile import reconcile

GOLDEN = Path(__file__).parent / "golden" / "observed"


@pytest.fixture(scope="module")
def findings():
    # As of 2027-04-01 the unpaid invoice (deadline 2027-04-11) is 10 days from its deadline
    return reconcile(load_vendor_dir(GOLDEN, workers=1), Policy(as_of="2027-04-01")).findings


def test_urgent_claims_come_first(findings):
    q = findings.dropna(subset=["priority_rank"]).sort_values("priority_rank")
    assert q["urgent"].tolist() == sorted(q["urgent"].tolist(), reverse=True)
    assert q.iloc[0]["ref"] == "INV-0005001" and q.iloc[0]["days_left"] == 10
    urgent = q[q["urgent"]]
    assert (urgent["expected_value_usd_cents"].diff().dropna() <= 0).all()


def test_only_claims_are_ranked_and_valued(findings):
    other = findings[findings["status"] != "CLAIMABLE"]
    assert other["priority_rank"].isna().all() and (other["expected_value_usd_cents"] == 0).all()


def test_weaker_matches_lower_the_expected_value(findings):
    claim = findings.set_index("ref")
    exact = claim.loc["SC100000001"]                       # shortage, exact match
    assert exact["expected_value_usd_cents"] == round(exact["recoverable_usd_cents"] * 0.85)
    suffix = claim.loc["PC100000001"]                      # price claim, matched on digits suffix
    assert suffix["win_rate"] == pytest.approx(0.80 * 0.85)


@pytest.fixture(scope="module")
def letters(findings):
    claims = findings[findings["status"] == "CLAIMABLE"]
    return [(facts_for(r, "Golden Fixture Supply Co."), r) for _, r in claims.iterrows()]


def test_template_letters_pass_verification(letters):
    for facts, _ in letters:
        draft = template_draft(facts)
        assert verify(draft, facts) == [], (facts["reference"], verify(draft, facts))


def test_every_injected_error_is_caught(letters):
    rng = np.random.default_rng(0)
    for facts, _ in letters:
        for kind, text in mutations(template_draft(facts), facts, rng):
            assert verify(text, facts), f"{kind} not caught for {facts['reference']}"


def test_invented_all_letter_document_number_is_caught(letters):
    # Regression: the verifier used to check only identifiers containing digits
    facts, _ = letters[0]
    draft = template_draft(facts) + " Shipment QQUOGOZZ confirms it."
    assert any("QQUOGOZZ" in p for p in verify(draft, facts))


def test_letter_must_state_reference_and_amount(letters):
    facts, _ = letters[0]
    assert "reference missing" in verify("We dispute this deduction.", facts)
    assert "disputed amount missing" in verify(f"We dispute {facts['reference']}.", facts)


def test_letters_are_never_built_from_non_claims(findings):
    valid = findings[findings["status"] == "NOT_CLAIMABLE"].iloc[0]
    facts = facts_for(valid, "x")
    # A letter quoting an amount from another finding is rejected
    assert verify(template_draft(facts).replace(facts["amount_disputed"], "$999.99"), facts)
