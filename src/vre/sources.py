"""Real public data: download, verify and distill into small committed seeds.

Two real sources feed the generator:

* UCI Online Retail II (CC BY 4.0): ~1.07M real wholesale/retail transaction lines.
  Used for the product catalog, unit costs, order quantities and cancellation rates.
* ECB euro foreign exchange reference rates: real daily rates, used to convert
  non-USD invoices and to check the exchange rate applied on each payment.

The raw files are large and stay out of Git (data/raw/). `build_seeds` turns them into
small Parquet files under data/seeds/ that are committed, so tests and the generator
run without network access.
"""
from __future__ import annotations

import hashlib
import io
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = ROOT / "data" / "raw"
SEEDS_DIR = ROOT / "data" / "seeds"

# Currencies the generated vendors invoice in (besides USD)
FX_CURRENCIES = ["EUR", "GBP", "CAD", "MXN", "JPY"]


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    filename: str
    sha256: str | None  # None: the file is updated daily upstream, so it cannot be pinned
    license: str


UCI = Source(
    name="UCI Online Retail II",
    url="https://archive.ics.uci.edu/static/public/502/online+retail+ii.zip",
    filename="uci/online_retail_ii.zip",
    sha256="572e36277c2390fbfde10664750731e0a86f55e33470d91919085f0408e67bfb",
    license="CC BY 4.0 (Chen, D. 2019, UCI Machine Learning Repository, doi:10.24432/C5CG6D)",
)
ECB = Source(
    name="ECB euro foreign exchange reference rates",
    url="https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip",
    filename="ecb/eurofxref-hist.zip",
    sha256=None,
    license="ECB statistics, free reuse with attribution",
)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(source: Source, raw_dir: Path = RAW_DIR, force: bool = False) -> Path:
    """Download a source once and verify its checksum when it is pinned."""
    dest = raw_dir / source.filename
    if dest.exists() and not force:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(source.url, timeout=120) as resp, tmp.open("wb") as out:
        while chunk := resp.read(1 << 20):
            out.write(chunk)
    if source.sha256 and (digest := _sha256(tmp)) != source.sha256:
        tmp.unlink()
        raise ValueError(f"{source.name}: checksum mismatch ({digest}); upstream file changed")
    tmp.replace(dest)
    return dest


def load_uci(raw_dir: Path = RAW_DIR) -> pd.DataFrame:
    """Read both yearly sheets. Invoice and StockCode must be read as text:
    cancellations look like 'C489449' and break numeric inference."""
    cache = raw_dir / "uci" / "online_retail_ii.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    with zipfile.ZipFile(fetch(UCI, raw_dir)) as z:
        data = io.BytesIO(z.read("online_retail_II.xlsx"))
    sheets = pd.read_excel(
        data, sheet_name=None, engine="openpyxl",
        dtype={"Invoice": str, "StockCode": str, "Description": str, "Country": str},
    )
    df = pd.concat(sheets.values(), ignore_index=True)
    df.to_parquet(cache, index=False)
    return df


def load_ecb(raw_dir: Path = RAW_DIR) -> pd.DataFrame:
    with zipfile.ZipFile(fetch(ECB, raw_dir)) as z:
        return pd.read_csv(io.BytesIO(z.read("eurofxref-hist.csv")), na_values=["N/A"])


def build_catalog(uci: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Catalog and real order lines from UCI.

    Keeps product lines only: drops cancellations, postage, manual adjustments and bank
    charges (StockCodes that are not five digits plus an optional letter suffix).
    """
    code_ok = uci["StockCode"].str.match(r"^\d{5}[A-Za-z]{0,2}$", na=False)
    is_cancel = uci["Invoice"].str.startswith("C", na=False)
    sales = uci[code_ok & ~is_cancel & (uci["Quantity"] > 0) & (uci["Price"] > 0)]
    cancels = uci[code_ok & is_cancel]

    sales = sales.assign(StockCode=sales["StockCode"].str.upper())
    cancels = cancels.assign(StockCode=cancels["StockCode"].str.upper())
    cancel_qty = cancels.groupby("StockCode")["Quantity"].sum().abs()

    catalog = (
        sales.groupby("StockCode")
        .agg(
            description=("Description", lambda s: s.mode().iat[0] if not s.mode().empty else ""),
            unit_price_gbp=("Price", "median"),
            lines=("Quantity", "size"),
            sold_qty=("Quantity", "sum"),
        )
        .reset_index()
        .rename(columns={"StockCode": "stock_code"})
    )
    catalog["cancel_rate"] = (
        catalog["stock_code"].map(cancel_qty).fillna(0) / catalog["sold_qty"]
    ).clip(0, 0.5).round(4)
    catalog = catalog[catalog["lines"] >= 5].drop(columns="sold_qty").reset_index(drop=True)
    catalog["description"] = catalog["description"].fillna("").str.strip().str.title()

    # Real order lines (item + quantity) to sample PO lines from; outliers capped at p99.9
    cap = sales["Quantity"].quantile(0.999)
    lines = sales.loc[sales["StockCode"].isin(catalog["stock_code"]), ["StockCode", "Quantity"]]
    lines = lines.rename(columns={"StockCode": "stock_code", "Quantity": "quantity"})
    lines["quantity"] = lines["quantity"].clip(upper=cap).astype("int32")
    return catalog, lines.reset_index(drop=True)


def build_fx(ecb: pd.DataFrame, start: str = "2024-01-01") -> pd.DataFrame:
    """Daily rates as units of currency per 1 USD, derived from ECB EUR cross rates."""
    df = ecb[["Date", "USD", *[c for c in FX_CURRENCIES if c != "EUR"]]].copy()
    df["Date"] = pd.to_datetime(df["Date"])
    df = df[df["Date"] >= start].sort_values("Date")
    out = pd.DataFrame({"date": df["Date"], "EUR": 1 / df["USD"]})
    for c in FX_CURRENCIES:
        if c != "EUR":
            out[c] = df[c] / df["USD"]
    long = out.melt(id_vars="date", var_name="currency", value_name="per_usd").dropna()
    return long.sort_values(["currency", "date"]).reset_index(drop=True)


def build_seeds(raw_dir: Path = RAW_DIR, seeds_dir: Path = SEEDS_DIR) -> dict[str, int]:
    seeds_dir.mkdir(parents=True, exist_ok=True)
    catalog, lines = build_catalog(load_uci(raw_dir))
    fx = build_fx(load_ecb(raw_dir))
    catalog.to_parquet(seeds_dir / "catalog.parquet", index=False)
    lines.to_parquet(seeds_dir / "order_lines.parquet", index=False)
    fx.to_parquet(seeds_dir / "fx_usd.parquet", index=False)
    return {"catalog": len(catalog), "order_lines": len(lines), "fx_rates": len(fx)}
