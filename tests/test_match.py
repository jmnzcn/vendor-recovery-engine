import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

from vre.match import AMBIGUOUS, CANONICAL, EXACT, FUZZY, SUFFIX, UNMATCHED, resolve

INVOICES = pd.DataFrame({"vendor_code": "V1", "invoiceNumber": [
    "INV-0001001", "INV-0011001", "INV-0002001", "INV-0123456"]})
POS = pd.DataFrame({"vendor_code": "V1", "purchaseOrderNumber": ["7KX2M9QA", "B4N8T2LW", "7KX2M9ZZ"]})


def _resolve(refs, records, **kw):
    q = pd.DataFrame({"vendor_code": "V1", "ref": refs})
    return resolve(q, records, **kw)


def test_invoice_tiers():
    r = _resolve(["INV-0002001", "inv 0002001", "0123456", "123456", "1001", "2001", "INV-0999999", "AB-1"],
                 INVOICES, id_col="invoiceNumber", suffix=True)
    assert r["tier"].tolist() == [EXACT, CANONICAL, SUFFIX, SUFFIX, AMBIGUOUS, SUFFIX, UNMATCHED, UNMATCHED]
    assert r["matched"].tolist()[:4] == ["INV-0002001", "INV-0002001", "INV-0123456", "INV-0123456"]
    assert pd.isna(r["matched"].iloc[4])                       # ambiguous is never auto-matched


def test_short_references_are_not_suffix_matched():
    r = _resolve(["456"], INVOICES, id_col="invoiceNumber", suffix=True, min_suffix=4)
    assert r["tier"].iloc[0] == UNMATCHED


def test_po_fuzzy_needs_a_unique_neighbour():
    r = _resolve(["B4N8T2LX", "7KX2M9QZ", "ZZZZZZZZ"], POS, id_col="purchaseOrderNumber", fuzzy=True)
    assert r["tier"].tolist() == [FUZZY, AMBIGUOUS, UNMATCHED]   # 7KX2M9QZ is one edit from two POs
    assert r["matched"].iloc[0] == "B4N8T2LW"


def test_vendors_do_not_leak_into_each_other():
    other = pd.DataFrame({"vendor_code": "V2", "invoiceNumber": ["INV-0002001"]})
    q = pd.DataFrame({"vendor_code": ["V2", "V3"], "ref": ["INV-0002001", "INV-0002001"]})
    r = resolve(q, pd.concat([INVOICES, other]), id_col="invoiceNumber", suffix=True)
    assert r["tier"].tolist() == [EXACT, UNMATCHED]


@settings(max_examples=60, deadline=None)
@given(numbers=st.lists(st.integers(1, 999_999), min_size=2, max_size=40, unique=True), k=st.integers(4, 6),
       pick=st.integers(0, 39))
def test_truncated_reference_is_matched_only_when_unique(numbers, k, pick):
    recs = pd.DataFrame({"vendor_code": "V1", "invoiceNumber": [f"INV-{n:07d}" for n in numbers]})
    target = recs["invoiceNumber"].iloc[pick % len(numbers)]
    ref = target.replace("-", "")[-k:]
    holders = recs["invoiceNumber"].str.replace(r"\D", "", regex=True).str.endswith(ref).sum()
    r = _resolve([ref], recs, id_col="invoiceNumber", suffix=True).iloc[0]
    if r["tier"] in (EXACT, CANONICAL):      # the truncated text happens to be a full invoice number
        return
    if holders == 1:
        assert (r["tier"], r["matched"]) == (SUFFIX, target)
    else:
        assert r["tier"] == AMBIGUOUS and pd.isna(r["matched"])


def test_fuzzy_compares_characters_not_the_canonical_form():
    # Canonical form strips the zero in 'MJPYNQ05' ('MJPYNQ5'), which would put this one-character
    # typo two edits away. Found on the holdout run: 5 chargebacks were left unmatched.
    pos = pd.DataFrame({"vendor_code": "V1", "purchaseOrderNumber": ["MJPYNQ05", "ABCDEFGH"]})
    r = _resolve(["MJPYNQ0V", "mjpynq-0v"], pos, id_col="purchaseOrderNumber", fuzzy=True)
    assert r["tier"].tolist() == [FUZZY, FUZZY]
    assert r["matched"].tolist() == ["MJPYNQ05", "MJPYNQ05"]
