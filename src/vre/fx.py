"""Daily FX rates (units of currency per 1 USD) from the ECB seed, with weekend carry-forward."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from vre.sources import SEEDS_DIR


@lru_cache(maxsize=4)
def load_rates(path: Path = SEEDS_DIR / "fx_usd.parquet") -> pd.DataFrame:
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"]).astype("datetime64[ns]")
    return df.sort_values("date").reset_index(drop=True)


def rate_on(currency: pd.Series, date: pd.Series, rates: pd.DataFrame | None = None) -> pd.Series:
    """Rate for each (currency, date): the latest ECB fixing on or before that date.
    USD is 1.0. Returns NaN when no fixing exists yet for that date."""
    rates = load_rates() if rates is None else rates
    out = pd.Series(np.where(currency.to_numpy() == "USD", 1.0, np.nan), index=currency.index)
    need = currency != "USD"
    if need.any():
        q = pd.DataFrame({
            "currency": currency[need].astype(str).to_numpy(),
            "date": pd.to_datetime(date[need]).astype("datetime64[ns]").to_numpy(),
            "_i": np.flatnonzero(need.to_numpy()),
        }).sort_values("date")
        m = pd.merge_asof(q, rates, on="date", by="currency", direction="backward")
        out.iloc[m["_i"].to_numpy()] = m["per_usd"].to_numpy()
    return out


def to_usd_cents(cents: pd.Series, currency: pd.Series, date: pd.Series) -> pd.Series:
    rate = rate_on(currency, date)
    return (cents.astype("float64") / rate).round().astype("Int64")
