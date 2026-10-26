import pandas as pd
import pytest
from hypothesis import given
from hypothesis import strategies as st

from vre.normalize import canonical_id, canonical_ids, parse_dates, parse_int, parse_money_cents


@pytest.mark.parametrize(("raw", "canon"), [
    ("INV-0012345", "INV12345"), ("inv-0012345 ", "INV12345"), ("INV 0012345", "INV12345"),
    ("00123456", "123456"), ("NW-04512", "NW4512"), ("4X7K-2P9Q", "4X7K2P9Q"), ("", ""), (None, ""),
])
def test_canonical_id(raw, canon):
    assert canonical_id(raw) == canon


def test_canonical_ids_matches_scalar_version():
    raw = ["INV-0012345", "nw 04512", "000042", "B4N8-T2LW", "AB000123", "x"]
    assert canonical_ids(pd.Series(raw)).tolist() == [canonical_id(r) for r in raw]


@given(prefix=st.sampled_from(["INV", "NW", "AB", ""]), number=st.integers(1, 9_999_999),
       width=st.integers(1, 9), sep=st.sampled_from(["-", " ", "", "_"]), lower=st.booleans())
def test_canonical_id_ignores_case_separators_and_leading_zeros(prefix, number, width, sep, lower):
    clean = f"{prefix}{number}"
    messy = f"{prefix}{sep}{number:0{width}d}"
    messy = messy.lower() if lower else messy
    assert canonical_id(messy) == canonical_id(clean)


@pytest.mark.parametrize(("raw", "cents"), [
    (["$1,234.50", "12.30", "(5.00)", "7"], [123450, 1230, -500, 700]),
    (["1.234,50", "12,30", "(5,00)"], [123450, 1230, -500]),
    (["€99,99", "1.000.000,01"], [9999, 100000001]),
])
def test_parse_money(raw, cents):
    assert parse_money_cents(pd.Series(raw)).tolist() == cents


def test_parse_money_numeric_and_garbage():
    assert parse_money_cents(pd.Series([1.005, 2.5])).tolist() == [100, 250]
    out = parse_money_cents(pd.Series(["12.00", "N/A", "pending"]))
    assert out.iloc[0] == 1200 and out.iloc[1:].isna().all()


@given(st.integers(-10**9, 10**9))
def test_money_roundtrip_us_and_eu(cents):
    us = f"{cents / 100:,.2f}"
    eu = us.replace(",", "_").replace(".", ",").replace("_", ".")
    assert parse_money_cents(pd.Series([us, "0.00"])).iloc[0] == cents
    assert parse_money_cents(pd.Series([eu, "0,00"])).iloc[0] == cents


def test_parse_dates_formats():
    got = parse_dates(pd.Series(["2026-03-16", "03/16/2026", "16.03.2026", "2026-03-16 00:00:00", "soon"]))
    assert got.iloc[:4].eq(pd.Timestamp("2026-03-16")).all() and pd.isna(got.iloc[4])


def test_parse_int_rejects_fractions():
    assert parse_int(pd.Series(["12", "1,200", "3.5", "x"])).tolist()[:2] == [12, 1200]
    assert parse_int(pd.Series(["3.5", "x"])).isna().all()
