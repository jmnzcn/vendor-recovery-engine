"""Parsing and normalization of the values that break reconciliations: IDs, money, dates.

Money is kept as integer cents end to end; floats never touch an amount.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

_NON_ALNUM = re.compile(r"[^A-Z0-9]")


def canonical_id(value: object) -> str:
    """Canonical form of an invoice, PO or deduction number.

    'inv-0012345 ' -> 'INV12345', 'NW 04512' -> 'NW4512', '000123' -> '123'.
    Letters are kept (they carry meaning); separators, case and the leading zeros of
    the numeric part are not.
    """
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return ""
    s = _NON_ALNUM.sub("", str(value).upper())
    m = re.fullmatch(r"([A-Z]*)0*(\d+)", s)
    if m:
        return m.group(1) + m.group(2)
    return s


def canonical_ids(values: pd.Series) -> pd.Series:
    s = values.astype("string").fillna("").str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)
    parts = s.str.extract(r"^([A-Z]*)0*(\d+)$")
    out = parts[0].fillna("") + parts[1].fillna("")
    return out.where(parts[1].notna(), s).astype("string")


def digits_of(values: pd.Series) -> pd.Series:
    """Numeric part of an ID, without leading zeros ('NW-04512' -> '4512')."""
    d = values.astype("string").fillna("").str.replace(r"\D", "", regex=True).str.lstrip("0")
    return d.astype("string")


_MONEY_CLEAN = re.compile(r"[\s$€£¥,]")


def uses_decimal_comma(values: pd.Series) -> bool:
    """True when a text column is written European style: '1.234,50'.

    Decided per column on the whole file, never per value: '1,234' alone could be either.
    """
    s = values.dropna().astype("string").str.strip().str.strip("()")
    comma = s.str.contains(r"\d,\d{1,2}$", regex=True, na=False).sum()
    dot = s.str.contains(r"\d\.\d{1,2}$", regex=True, na=False).sum()
    return bool(comma > dot)


def parse_money_cents(values: pd.Series) -> pd.Series:
    """'$1,234.50' -> 123450, '1.234,50' -> 123450, '(12.30)' -> -1230, 7.5 -> 750.
    Unparseable -> <NA>."""
    if pd.api.types.is_numeric_dtype(values):
        return (pd.to_numeric(values, errors="coerce") * 100).round().astype("Int64")
    s = values.astype("string").str.strip()
    neg = s.str.startswith("(", na=False) & s.str.endswith(")", na=False)
    s = s.str.strip("()")
    if uses_decimal_comma(s):
        s = s.str.replace(".", "", regex=False).str.replace(",", ".", regex=False)
    s = s.str.replace(_MONEY_CLEAN, "", regex=True)
    num = pd.to_numeric(s, errors="coerce")
    num = num.where(~neg, -num)
    return (num * 100).round().astype("Int64")


def parse_dates(values: pd.Series) -> pd.Series:
    """ISO, US 'MM/DD/YYYY', European 'DD.MM.YYYY' and real datetimes, to datetime64 (time dropped)."""
    if pd.api.types.is_datetime64_any_dtype(values):
        return pd.to_datetime(values).dt.normalize()
    s = values.astype("string").str.strip()
    iso = pd.to_datetime(s, format="%Y-%m-%d", errors="coerce")
    iso_ts = pd.to_datetime(s, format="%Y-%m-%d %H:%M:%S", errors="coerce")
    us = pd.to_datetime(s, format="%m/%d/%Y", errors="coerce")
    eu = pd.to_datetime(s, format="%d.%m.%Y", errors="coerce")
    return iso.fillna(iso_ts).fillna(us).fillna(eu).dt.normalize()


def parse_int(values: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(values):
        num = pd.to_numeric(values, errors="coerce")
    else:
        num = pd.to_numeric(values.astype("string").str.replace(",", "", regex=False).str.strip(),
                            errors="coerce")
    whole = num.where(num == num.round())
    return whole.astype("Int64")
