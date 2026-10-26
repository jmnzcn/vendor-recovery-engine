"""Synthetic Amazon vendor world on top of real data, with known answers.

Real inputs (data/seeds/): product catalog, unit costs, order quantities and cancellation
rates from UCI Online Retail II; daily FX rates from the ECB.

Synthetic on top: 170 vendors, purchase orders, shipments, carrier proofs of delivery,
invoices, remittances and Vendor Central deductions, shaped like the SP-API vendor models.
Discrepancies are injected on purpose and every one is recorded in a ground-truth table,
so the engine can be scored instead of eyeballed.

Two outputs:
  observed/<vendor>/<table>.<parquet|csv|xlsx>  what a vendor exports: mixed formats,
                                                header styles, money and date formats,
                                                junk rows, duplicated export rows, messy IDs.
  truth/truth.parquet                            the expected finding for every deduction
                                                and every payment problem.
"""
from __future__ import annotations

import json
import string
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from rapidfuzz import process
from rapidfuzz.distance import Levenshtein

from vre.fx import load_rates, rate_on
from vre.normalize import canonical_ids
from vre.policy import (
    DEDUCTION_CHARGEBACK,
    DEDUCTION_PRICE,
    DEDUCTION_REVERSAL,
    DEDUCTION_SHORTAGE,
    REASON_ASN_LATE,
    REASON_LATE_SHIPMENT,
    Category,
    Policy,
    Status,
)
from vre.sources import SEEDS_DIR

ALNUM = np.array(list(string.ascii_uppercase + string.digits))
GBP_TO_USD = 1.27          # converts UCI retail prices (GBP) to USD
WHOLESALE_FACTOR = 0.55    # vendor unit cost as a share of the retail price
CASE_PACK = 6              # UCI quantities are retail lines; Amazon POs buy in cases
CARTON_UNITS = 24

NAME_A = ["North", "Blue", "Summit", "Harbor", "Cedar", "Golden", "Prairie", "Silver", "Maple", "Coastal",
          "Granite", "Evergreen", "Sunrise", "Pioneer", "Riverside", "Atlas", "Lakeside", "Redwood",
          "Ironwood", "Bright", "Crescent", "Meridian", "Willow", "Falcon", "Juniper", "Oakline"]
NAME_B = ["Home", "Living", "Craft", "Gift", "Kitchen", "Decor", "Trading", "Goods", "Supply", "Brands",
          "Design", "Outfitters", "Imports", "Essentials", "Works", "Collective"]
NAME_C = ["LLC", "Inc.", "Co.", "Ltd.", "Corp.", "Group LLC", "Trading Co."]


@dataclass
class WorldConfig:
    vendors: int = 170
    po_lines: int = 1_200_000
    seed: int = 7
    start: str = "2025-03-01"
    end: str = "2026-06-30"

    @classmethod
    def small(cls, vendors: int = 12, po_lines: int = 40_000, seed: int = 7) -> WorldConfig:
        return cls(vendors=vendors, po_lines=po_lines, seed=seed)


def _codes(rng: np.random.Generator, n: int, length: int, taken: set[str] | None = None) -> np.ndarray:
    """n unique random uppercase alphanumeric codes."""
    taken = set() if taken is None else taken
    out: list[str] = []
    while len(out) < n:
        k = int((n - len(out)) * 1.2) + 10
        cand = ["".join(r) for r in ALNUM[rng.integers(0, len(ALNUM), size=(k, length))]]
        for c in cand:
            if c not in taken:
                taken.add(c)
                out.append(c)
                if len(out) == n:
                    break
    return np.array(out)


def _unique_ints(rng: np.random.Generator, n: int, low: int, high: int) -> np.ndarray:
    vals = np.unique(rng.integers(low, high, size=int(n * 1.3) + 50))
    while len(vals) < n:
        vals = np.unique(np.concatenate([vals, rng.integers(low, high, size=n)]))
    return rng.permutation(vals)[:n]


def _days(rng: np.random.Generator, low: int, high: int, n: int) -> pd.TimedeltaIndex:
    return pd.to_timedelta(rng.integers(low, high + 1, size=n), unit="D")


class WorldBuilder:
    def __init__(self, cfg: WorldConfig, policy: Policy):
        self.cfg, self.policy = cfg, policy
        self.rng = np.random.default_rng(cfg.seed)
        self.as_of = pd.Timestamp(policy.as_of)
        self.catalog = pd.read_parquet(SEEDS_DIR / "catalog.parquet")
        self.real_lines = pd.read_parquet(SEEDS_DIR / "order_lines.parquet")
        self.rates = load_rates()

    # ------------------------------------------------------------------ vendors
    def vendors(self) -> pd.DataFrame:
        rng, n = self.rng, self.cfg.vendors
        weight = rng.lognormal(0, 1.0, n)
        lines = np.maximum(300, np.round(weight / weight.sum() * self.cfg.po_lines)).astype(int)
        names = [f"{rng.choice(NAME_A)} {rng.choice(NAME_B)} {rng.choice(NAME_C)}" for _ in range(n)]
        currency = rng.choice(["USD", "EUR", "GBP", "CAD", "MXN", "JPY"], n,
                              p=[0.72, 0.08, 0.06, 0.07, 0.04, 0.03])
        fmt = rng.choice(["parquet", "csv", "xlsx"], n, p=[0.55, 0.35, 0.10])
        fmt = np.where((fmt == "xlsx") & (lines > 15_000), "csv", fmt)  # Excel tops out on big vendors
        df = pd.DataFrame({
            "vendor_code": _codes(rng, n, 5),
            "vendor_name": names,
            "currency": currency,
            "po_lines": lines,
            "file_format": fmt,
            "header_style": rng.choice(["camel", "title", "snake"], n),
            "money_style": rng.choice(["plain", "symbol"], n, p=[0.6, 0.4]),
            "date_style": rng.choice(["iso", "us"], n, p=[0.6, 0.4]),
            "invoice_style": rng.choice(["INV", "NUM", "PFX"], n, p=[0.4, 0.35, 0.25]),
            "invoice_prefix": _codes(rng, n, 2),
            "junk_rows": rng.random(n) < 0.4,
        })
        df["invoice_prefix"] = df["invoice_prefix"].str.replace(r"\d", "X", regex=True)
        # Most euro vendors export with European number and date formats: 1.234,50 and 31.12.2025
        eu = (df["currency"] == "EUR") & (df["file_format"] != "parquet") & (rng.random(n) < 0.7)
        df.loc[eu, "money_style"] = "eu"
        df.loc[eu, "date_style"] = "eu"
        return df

    # ------------------------------------------------------------------ orders
    def orders(self, vendors: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        rng = self.rng
        cat = self.catalog.set_index("stock_code")
        by_code = self.real_lines.groupby("stock_code").indices
        codes = np.array(list(by_code))
        start, end = pd.Timestamp(self.cfg.start), pd.Timestamp(self.cfg.end)
        span = (end - start).days

        line_frames, po_frames = [], []
        taken_po: set[str] = set()
        for v in vendors.itertuples():
            assortment = rng.choice(codes, size=min(len(codes), int(rng.integers(60, 400))), replace=False)
            pool = np.concatenate([by_code[c] for c in assortment])
            picks = self.real_lines.iloc[rng.choice(pool, size=v.po_lines)]
            draw = np.clip(rng.geometric(1 / 8, size=v.po_lines // 4 + 10), 1, 40)
            n_po = int(np.searchsorted(np.cumsum(draw), v.po_lines)) + 1
            sizes = draw[:n_po].copy()
            sizes[-1] -= sizes.sum() - v.po_lines
            po_numbers = _codes(rng, n_po, 8, taken_po)
            po_dates = start + _days(rng, 0, span, n_po)
            po = pd.DataFrame({
                "vendor_code": v.vendor_code,
                "purchaseOrderNumber": po_numbers,
                "purchaseOrderDate": po_dates,
                "shipWindowEnd": po_dates + _days(rng, 7, 14, n_po),
            })
            po_frames.append(po)

            asin_map = dict(zip(assortment, "B0" + pd.Series(_codes(rng, len(assortment), 8)), strict=True))
            stock = picks["stock_code"].to_numpy()
            price_gbp = cat.loc[stock, "unit_price_gbp"].to_numpy()
            unit_usd = np.maximum(0.25, price_gbp * GBP_TO_USD * WHOLESALE_FACTOR)
            lines = pd.DataFrame({
                "vendor_code": v.vendor_code,
                "purchaseOrderNumber": np.repeat(po_numbers, sizes),

                "amazonProductIdentifier": [asin_map[c] for c in stock],
                "vendorProductIdentifier": stock,
                "itemDescription": cat.loc[stock, "description"].to_numpy(),
                "orderedQuantity": picks["quantity"].to_numpy() * CASE_PACK,
                "unit_usd": unit_usd,
                "cancel_rate": cat.loc[stock, "cancel_rate"].to_numpy(),
                "currencyCode": v.currency,
            })
            # One line per product on a PO, so an ASIN identifies the line
            lines = lines.drop_duplicates(["purchaseOrderNumber", "vendorProductIdentifier"])
            lines["itemSequenceNumber"] = lines.groupby("purchaseOrderNumber").cumcount() + 1
            line_frames.append(lines)

        pos = pd.concat(po_frames, ignore_index=True)
        lines = pd.concat(line_frames, ignore_index=True)
        lines = lines.merge(pos[["purchaseOrderNumber", "purchaseOrderDate"]], on="purchaseOrderNumber")
        per_usd = rate_on(lines["currencyCode"], lines["purchaseOrderDate"], self.rates)
        lines["netCost"] = np.round(lines["unit_usd"] * per_usd * 100).astype("int64")  # cents
        if (lines["currencyCode"] == "JPY").any():  # yen has no minor unit
            jpy = lines["currencyCode"] == "JPY"
            lines.loc[jpy, "netCost"] = (lines.loc[jpy, "netCost"] / 100).round().astype("int64") * 100
        return pos, lines.drop(columns=["unit_usd", "purchaseOrderDate"])

    # ------------------------------------------------------------------ shipments, POD, invoices
    def fulfilment(self, vendors: pd.DataFrame, pos: pd.DataFrame, lines: pd.DataFrame) -> dict:
        rng = self.rng
        n = len(pos)
        late = rng.random(n) < 0.08
        shipped = np.where(late, pos["shipWindowEnd"] + _days(rng, 1, 10, n),
                           pos["shipWindowEnd"] - _days(rng, 0, 6, n))
        pos = pos.assign(
            late=late,
            shippedDate=pd.to_datetime(shipped),
            asn_late=rng.random(n) < 0.05,
            shipment_observed=rng.random(n) >= 0.01,
            shipmentIdentifier=_codes(rng, n, 10),
            billOfLadingNumber=_codes(rng, n, 12),
        )
        asn_lag = np.where(pos["asn_late"], rng.integers(1, 4, n), 0)
        pos["shipmentConfirmationDate"] = pos["shippedDate"] + pd.to_timedelta(asn_lag, unit="D")
        # Vendor short-ships some lines (real cancellation rates drive the odds); the invoice follows
        backorder = rng.random(len(lines)) < np.clip(lines["cancel_rate"] * 1.5, 0.005, 0.25)
        cut = np.minimum(lines["orderedQuantity"] - 1, rng.integers(1, 4 * CASE_PACK, len(lines)))
        lines["shippedQuantity"] = np.where(backorder & (cut > 0), lines["orderedQuantity"] - cut,
                                            lines["orderedQuantity"])
        units = lines.groupby("purchaseOrderNumber")["shippedQuantity"].sum()
        pos["cartonCount"] = np.ceil(pos["purchaseOrderNumber"].map(units) / CARTON_UNITS).astype(int).clip(lower=1)
        pos["pod_state"] = rng.choice(["full", "short", "missing"], n, p=[0.88, 0.06, 0.06])
        pos["deliveredDate"] = pos["shippedDate"] + _days(rng, 2, 6, n)
        short_by = np.minimum(pos["cartonCount"] - 1, rng.integers(1, 4, n))
        pos["cartonsDelivered"] = np.where((pos["pod_state"] == "short") & (short_by > 0),
                                           pos["cartonCount"] - short_by, pos["cartonCount"])
        pos.loc[(pos["pod_state"] == "short") & (pos["cartonsDelivered"] == pos["cartonCount"]), "pod_state"] = "full"
        pos["podDocumentId"] = "POD-" + pd.Series(_codes(rng, n, 9), index=pos.index)

        # One invoice per PO, numbered in the vendor's own style
        pos = pos.merge(vendors[["vendor_code", "invoice_style", "invoice_prefix", "currency"]], on="vendor_code")
        pos["invoice_num"] = 0
        for idx in pos.groupby("vendor_code").indices.values():
            pos.loc[pos.index[idx], "invoice_num"] = _unique_ints(rng, len(idx), 1, 999_999)
        num = pos["invoice_num"].astype(int)
        pos["invoiceNumber"] = np.select(
            [pos["invoice_style"] == "INV", pos["invoice_style"] == "NUM"],
            ["INV-" + num.map("{:07d}".format), num.map("{:08d}".format)],
            pos["invoice_prefix"] + "-" + num.map("{:06d}".format),
        )
        pos["invoiceDate"] = pos["shippedDate"] + _days(rng, 0, 2, n)
        pos["dueDate"] = pos["invoiceDate"] + pd.to_timedelta(self.policy.payment_terms_days, "D")

        # Some vendors over-bill a line: Amazon's price claim on it is valid
        lines["overbilled"] = rng.random(len(lines)) < 0.01
        uplift = 1 + rng.uniform(0.02, 0.15, len(lines))
        lines["invoiceNetCost"] = np.where(lines["overbilled"], np.round(lines["netCost"] * uplift),
                                           lines["netCost"]).astype("int64")
        lines = lines.merge(pos[["purchaseOrderNumber", "invoiceNumber", "invoiceDate"]], on="purchaseOrderNumber")
        lines["line_total"] = lines["shippedQuantity"] * lines["invoiceNetCost"]
        pos["invoiceAmount"] = pos["purchaseOrderNumber"].map(lines.groupby("purchaseOrderNumber")["line_total"].sum())
        return {"pos": pos, "lines": lines}

    # ------------------------------------------------------------------ deductions
    def deductions(self, pos: pd.DataFrame, lines: pd.DataFrame) -> pd.DataFrame:
        rng = self.rng
        p = pos.set_index("purchaseOrderNumber")
        L = lines.join(p[["pod_state", "shipment_observed", "late", "asn_late"]],
                       on="purchaseOrderNumber")

        # Shortages: Amazon claims missing units. Odds depend on what the POD shows.
        odds = L["pod_state"].map({"full": 0.03, "short": 0.55, "missing": 0.05}).to_numpy()
        s = L[rng.random(len(L)) < odds].copy()
        qty = np.minimum(s["shippedQuantity"], rng.integers(1, 2 * CASE_PACK, len(s)))
        short = pd.DataFrame({
            "vendor_code": s["vendor_code"], "deductionType": DEDUCTION_SHORTAGE, "reasonCode": "",
            "true_ref": s["invoiceNumber"], "ref_kind": "invoice",
            "amazonProductIdentifier": s["amazonProductIdentifier"], "quantity": qty,
            "amount": qty * s["invoiceNetCost"], "currencyCode": s["currencyCode"],
            "deductionDate": s["invoiceDate"] + _days(rng, 15, 60, len(s)),
            "category": Category.SHORTAGE.value,
            "status": np.select(
                [~s["shipment_observed"] | (s["pod_state"] == "missing"), s["pod_state"] == "short"],
                [Status.INSUFFICIENT_EVIDENCE.value, Status.NOT_CLAIMABLE.value], Status.CLAIMABLE.value),
        })

        # Price claims: valid on over-billed lines, unsupported on a few correctly billed ones
        valid = L[L["overbilled"] & (rng.random(len(L)) < 0.9)]
        bogus = L[~L["overbilled"] & (rng.random(len(L)) < 0.01)]
        pct = rng.uniform(0.03, 0.12, len(bogus))
        price = pd.concat([
            pd.DataFrame({"src": valid.index, "amount": (valid["invoiceNetCost"] - valid["netCost"])
                          * valid["shippedQuantity"], "status": Status.NOT_CLAIMABLE.value}),
            pd.DataFrame({"src": bogus.index, "amount": np.round(bogus["line_total"] * pct).astype("int64"),
                          "status": Status.CLAIMABLE.value}),
        ])
        pl = L.loc[price["src"]]
        price = pd.DataFrame({
            "vendor_code": pl["vendor_code"].to_numpy(), "deductionType": DEDUCTION_PRICE, "reasonCode": "",
            "true_ref": pl["invoiceNumber"].to_numpy(), "ref_kind": "invoice",
            "amazonProductIdentifier": pl["amazonProductIdentifier"].to_numpy(),
            "quantity": pl["shippedQuantity"].to_numpy(), "amount": price["amount"].to_numpy(),
            "currencyCode": pl["currencyCode"].to_numpy(),
            "deductionDate": (pl["invoiceDate"] + _days(rng, 20, 75, len(pl))).to_numpy(),
            "category": Category.PRICE_CLAIM.value, "status": price["status"].to_numpy(),
        })

        # Chargebacks at PO level
        po = pos.copy()
        po_value = po["invoiceAmount"]
        cb_frames = []
        for reason, flag, category, valid_rate, bogus_rate in [
            (REASON_LATE_SHIPMENT, po["late"], Category.CHARGEBACK_LATE_SHIPMENT, 0.85, 0.02),
            (REASON_ASN_LATE, po["asn_late"], Category.CHARGEBACK_ASN, 0.85, 0.01),
        ]:
            r = rng.random(len(po))
            hit = np.where(flag, r < valid_rate, r < bogus_rate)
            missing_hit = ~po["shipment_observed"] & (r < 0.3)
            hit = hit | missing_hit
            c = po[hit]
            status = np.select(
                [~c["shipment_observed"], flag[hit]],
                [Status.INSUFFICIENT_EVIDENCE.value, Status.NOT_CLAIMABLE.value], Status.CLAIMABLE.value)
            floor = np.where(c["currency"] == "USD", 2500, 0)
            cb_frames.append(pd.DataFrame({
                "vendor_code": c["vendor_code"], "deductionType": DEDUCTION_CHARGEBACK, "reasonCode": reason,
                "true_ref": c["purchaseOrderNumber"], "ref_kind": "po", "amazonProductIdentifier": "",
                "quantity": 0, "amount": np.maximum(floor, np.round(po_value[hit] * 0.03)).astype("int64"),
                "currencyCode": c["currency"],
                "deductionDate": c["shippedDate"] + _days(rng, 10, 45, len(c)),
                "category": category.value, "status": status,
            }))

        d = pd.concat([short, price, *cb_frames], ignore_index=True)
        d = d[d["amount"] > 0].reset_index(drop=True)
        d["deductionDate"] = pd.to_datetime(d["deductionDate"])
        d = d[d["deductionDate"] <= self.as_of].reset_index(drop=True)
        d["recoverable"] = np.where(d["status"] == Status.CLAIMABLE.value, d["amount"], 0)
        return d

    def messy_refs(self, d: pd.DataFrame, pos: pd.DataFrame) -> pd.DataFrame:
        """Reference numbers as Amazon writes them: variants, truncations, typos, orphans.
        Labels each deduction NEEDS_REVIEW / INSUFFICIENT_EVIDENCE where a careful
        analyst could not resolve the reference."""
        rng = self.rng
        n = len(d)
        ref = d["true_ref"].astype(str).copy()
        kind = d["ref_kind"].to_numpy()
        u = rng.random(n)
        is_inv = kind == "invoice"

        variant = u < 0.20
        lower = variant & (rng.random(n) < 0.35)
        nosep = variant & ~lower & (rng.random(n) < 0.5)
        digits_only = variant & ~lower & ~nosep & is_inv
        spaced = variant & ~lower & ~nosep & ~is_inv
        ref[lower] = ref[lower].str.lower()
        ref[nosep] = ref[nosep].str.replace("-", "", regex=False)
        ref[digits_only] = ref[digits_only].str.replace(r"\D", "", regex=True)
        ref[spaced] = ref[spaced].str[:4] + "-" + ref[spaced].str[4:]

        truncated = is_inv & (u >= 0.20) & (u < 0.26)
        tlen = rng.integers(4, 6, n)
        digits = ref.str.replace(r"\D", "", regex=True)
        ref[truncated] = [s[-k:] for s, k in zip(digits[truncated], tlen[truncated], strict=True)]

        typo = ~is_inv & (u >= 0.20) & (u < 0.24)
        orphan = is_inv & (u >= 0.26) & (u < 0.265)

        status = d["status"].to_numpy().copy()

        # Index of every vendor's invoice digit strings, by suffix length
        inv = pos[["vendor_code", "invoiceNumber"]].copy()
        inv["digits"] = inv["invoiceNumber"].str.replace(r"\D", "", regex=True)
        suffix_count = Counter()
        canon_by_vendor: dict[str, set[str]] = {}
        for vcode, g in inv.groupby("vendor_code"):
            canon_by_vendor[vcode] = set(canonical_ids(g["invoiceNumber"]))
            for k in range(4, 9):
                suffix_count.update((vcode, k, x[-k:]) for x in g["digits"] if len(x) >= k)

        for i in np.flatnonzero(truncated):
            v, r = d.at[i, "vendor_code"], ref.iat[i]
            if canonical_ids(pd.Series([r])).iat[0] in canon_by_vendor[v]:
                ref.iat[i] = d.at[i, "true_ref"]  # would read as a different invoice: keep it clean
            elif suffix_count[(v, len(r), r)] > 1:
                status[i] = Status.NEEDS_REVIEW.value

        po_by_vendor = {v: g.to_numpy() for v, g in pos.groupby("vendor_code")["purchaseOrderNumber"]}
        for i in np.flatnonzero(typo):
            v, true = d.at[i, "vendor_code"], d.at[i, "true_ref"]
            pool = po_by_vendor[v]
            pool_set = set(pool)
            while True:
                j = int(rng.integers(0, len(true)))
                t = true[:j] + str(rng.choice(ALNUM)) + true[j + 1:]
                if t != true and t not in pool_set:
                    break
            ref.iat[i] = t
            near = len(process.extract(t, pool, scorer=Levenshtein.distance, score_cutoff=1, limit=None))
            if near > 1:
                status[i] = Status.NEEDS_REVIEW.value

        for i in np.flatnonzero(orphan):
            v = d.at[i, "vendor_code"]
            style = pos.loc[pos["vendor_code"] == v, "invoiceNumber"].iat[0]
            while True:
                num = int(rng.integers(1, 999_999))
                cand = (f"INV-{num:07d}" if style.startswith("INV-") else
                        f"{num:08d}" if style.isdigit() else f"{style.split('-')[0]}-{num:06d}")
                if canonical_ids(pd.Series([cand])).iat[0] not in canon_by_vendor[v]:
                    break
            ref.iat[i] = cand
            status[i] = Status.INSUFFICIENT_EVIDENCE.value

        d = d.assign(referenceNumber=ref.to_numpy(), status=status)
        d["recoverable"] = np.where(d["status"] == Status.CLAIMABLE.value, d["amount"], 0)
        return d

    def duplicates_and_reversals(self, d: pd.DataFrame) -> pd.DataFrame:
        rng = self.rng
        d = d.copy()
        d["deductionNumber"] = ""
        prefix = d["deductionType"].map({DEDUCTION_SHORTAGE: "SC", DEDUCTION_PRICE: "PC",
                                         DEDUCTION_CHARGEBACK: "CB"})
        d["deductionNumber"] = prefix + pd.Series(_unique_ints(rng, len(d), 10**8, 10**9), index=d.index).astype(str)

        # Amazon applies the same deduction twice
        dup_src = d[rng.random(len(d)) < 0.015]
        dup = dup_src.copy()
        dup["deductionDate"] = dup["deductionDate"] + _days(rng, 5, 40, len(dup))
        dup["deductionNumber"] = prefix.loc[dup.index] + pd.Series(
            _unique_ints(rng, len(dup), 10**8, 10**9) + 10**9, index=dup.index).astype(str).str[1:]
        dup = dup[~dup["deductionNumber"].isin(d["deductionNumber"]) & (dup["deductionDate"] <= self.as_of)]
        dup["category"] = Category.DUPLICATE_DEDUCTION.value
        dup["status"] = Status.CLAIMABLE.value
        dup["recoverable"] = dup["amount"]
        dup["is_duplicate"] = True
        d["is_duplicate"] = False

        # Amazon reverses some unsupported deductions on its own, in full or in part
        cand = d[(d["status"] == Status.CLAIMABLE.value) & ~d.index.isin(dup_src.index)]
        rev_src = cand[rng.random(len(cand)) < 0.15]
        partial = rng.random(len(rev_src)) < 0.2
        rev_amount = np.where(partial, np.round(rev_src["amount"] * rng.uniform(0.3, 0.7, len(rev_src))),
                              rev_src["amount"]).astype("int64")
        rev_date = rev_src["deductionDate"] + _days(rng, 20, 120, len(rev_src))
        seen = rev_date <= self.as_of
        orig_ref = rev_src["deductionNumber"].copy()
        noisy = rng.random(len(orig_ref)) < 0.2
        orig_ref[noisy] = orig_ref[noisy].str.lower().str[:2] + "-" + orig_ref[noisy].str[2:]
        rev = pd.DataFrame({
            "vendor_code": rev_src["vendor_code"], "deductionType": DEDUCTION_REVERSAL, "reasonCode": "",
            "referenceNumber": orig_ref, "amazonProductIdentifier": rev_src["amazonProductIdentifier"],
            "quantity": 0, "amount": rev_amount, "currencyCode": rev_src["currencyCode"],
            "deductionDate": rev_date, "is_duplicate": False,
            "deductionNumber": "RV" + pd.Series(_unique_ints(rng, len(rev_src), 10**8, 10**9),
                                                index=rev_src.index).astype(str),
        })[seen]
        rev_amount_seen = pd.Series(rev_amount, index=rev_src.index)[seen]
        d.loc[rev_amount_seen.index, "recoverable"] = d.loc[rev_amount_seen.index, "amount"] - rev_amount_seen
        full = rev_amount_seen.index[d.loc[rev_amount_seen.index, "recoverable"] <= 0]
        d.loc[full, "status"] = Status.ALREADY_RECOVERED.value
        d.loc[full, "recoverable"] = 0

        out = pd.concat([d, dup], ignore_index=True)
        # Past the dispute window: still worth reporting, no longer recoverable
        age = (self.as_of - out["deductionDate"]).dt.days
        expired = (out["status"] == Status.CLAIMABLE.value) & (age > self.policy.deduction_window_days)
        out.loc[expired, "status"] = Status.EXPIRED.value
        out["expired_amount"] = np.where(expired, out["recoverable"], 0)
        out.loc[expired, "recoverable"] = 0
        return pd.concat([out, rev], ignore_index=True)

    # ------------------------------------------------------------------ payments
    def payments(self, pos: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        rng, pol = self.rng, self.policy
        inv = pos[["vendor_code", "invoiceNumber", "purchaseOrderNumber", "invoiceDate", "dueDate", "invoiceAmount",
                   "currency"]].copy()
        n = len(inv)
        inv["paymentDate"] = inv["dueDate"] + _days(rng, -2, 7, n)
        overdue = inv["dueDate"] + pd.to_timedelta(pol.payment_grace_days, "D") < self.as_of
        unpaid = overdue & (rng.random(n) < 0.008)
        paid = (inv["paymentDate"] <= self.as_of) & ~unpaid
        p = inv[paid].copy()
        m = len(p)
        true_rate = rate_on(p["currency"], p["paymentDate"], self.rates).to_numpy()
        is_fx = (p["currency"] != "USD").to_numpy()
        fx_bad = is_fx & (rng.random(m) < 0.10)
        applied = true_rate * np.where(fx_bad, 1 + rng.uniform(0.015, 0.04, m),
                                       1 + rng.uniform(-0.003, 0.003, m))
        applied = np.where(is_fx, np.round(applied, 6), 1.0)
        due_usd = np.round(p["invoiceAmount"].to_numpy() / applied).astype("int64")
        under = ~fx_bad & (rng.random(m) < 0.012)
        short = np.round(due_usd * rng.uniform(0.01, 0.08, m)).astype("int64")
        jitter = np.where(rng.random(m) < 0.03, rng.integers(-1, 2, m), 0)
        p["amountPaid"] = np.where(under, due_usd - short, due_usd + jitter)
        p["exchangeRate"] = applied
        p["paymentNumber"] = (p["vendor_code"] + "-" + p["paymentDate"].dt.strftime("%G%V")).to_numpy()

        truth = []
        correct_usd = np.round(p["invoiceAmount"].to_numpy() / true_rate).astype("int64")
        fx_gap = correct_usd - due_usd
        fx_rows = p[fx_bad & (fx_gap > pol.payment_tolerance_cents)]
        truth.append(pd.DataFrame({"vendor_code": fx_rows["vendor_code"], "ref": fx_rows["invoiceNumber"],
                                   "category": Category.FX_VARIANCE.value,
                                   "recoverable": fx_gap[fx_bad & (fx_gap > pol.payment_tolerance_cents)],
                                   "dueDate": fx_rows["dueDate"]}))
        gap = (due_usd - p["amountPaid"]).to_numpy()
        um = under & (gap > pol.payment_tolerance_cents)
        truth.append(pd.DataFrame({"vendor_code": p["vendor_code"][um], "ref": p["invoiceNumber"][um],
                                   "category": Category.UNDERPAYMENT.value, "recoverable": gap[um],
                                   "dueDate": p["dueDate"][um]}))
        up = inv[unpaid]
        truth.append(pd.DataFrame({"vendor_code": up["vendor_code"], "ref": up["invoiceNumber"],
                                   "category": Category.UNPAID_INVOICE.value,
                                   "recoverable": np.round(up["invoiceAmount"] / rate_on(
                                       up["currency"], up["dueDate"], self.rates)).astype("int64"),
                                   "dueDate": up["dueDate"]}))
        t = pd.concat(truth, ignore_index=True)
        t["status"] = Status.CLAIMABLE.value
        age = (self.as_of - t["dueDate"]).dt.days
        exp = age > pol.payment_window_days
        t["expired_usd_cents"] = np.where(exp, t["recoverable"], 0)
        t.loc[exp, "status"] = Status.EXPIRED.value
        t.loc[exp, "recoverable"] = 0
        rem = p.rename(columns={"currency": "invoiceCurrency"})
        rem["paymentCurrency"] = "USD"
        return rem, t


# ---------------------------------------------------------------------- writing
TABLE_COLUMNS = {
    "purchase_orders": ["purchaseOrderNumber", "purchaseOrderDate", "shipWindowEnd", "itemSequenceNumber",
                        "amazonProductIdentifier", "vendorProductIdentifier", "itemDescription",
                        "orderedQuantity", "netCost", "currencyCode"],
    "shipments": ["shipmentIdentifier", "purchaseOrderNumber", "shippedDate", "shipmentConfirmationDate",
                  "billOfLadingNumber", "cartonCount"],
    "proof_of_delivery": ["billOfLadingNumber", "carrier", "deliveredDate", "cartonsDelivered", "podDocumentId"],
    "invoices": ["invoiceNumber", "invoiceDate", "invoiceType", "purchaseOrderNumber", "itemSequenceNumber",
                 "amazonProductIdentifier", "invoicedQuantity", "netCost", "currencyCode"],
    "remittances": ["paymentNumber", "paymentDate", "invoiceNumber", "invoiceDate", "invoiceAmount",
                    "invoiceCurrency", "amountPaid", "paymentCurrency", "exchangeRate"],
    "deductions": ["deductionNumber", "deductionDate", "deductionType", "reasonCode", "referenceNumber",
                   "amazonProductIdentifier", "quantity", "amount", "currencyCode"],
}
MONEY_COLUMNS = {"netCost", "invoiceAmount", "amountPaid", "amount"}
DATE_COLUMNS = {"purchaseOrderDate", "shipWindowEnd", "shippedDate", "shipmentConfirmationDate",
                "deliveredDate", "invoiceDate", "paymentDate", "deductionDate"}


HUMAN_ALIASES = {"purchaseOrderNumber": "PO #", "invoiceNumber": "Invoice #", "amazonProductIdentifier": "ASIN"}


def _header(col: str, style: str) -> str:
    if style == "title" and col in HUMAN_ALIASES:
        return HUMAN_ALIASES[col]
    words = pd.Series([col]).str.replace(r"([a-z])([A-Z])", r"\1 \2", regex=True).iat[0].split()
    if style == "title":
        return " ".join(w[:1].upper() + w[1:] for w in words)
    if style == "snake":
        return "_".join(w.lower() for w in words)
    return col


def _write_table(df: pd.DataFrame, table: str, vendor: pd.Series, out_dir: Path, rng: np.random.Generator) -> None:
    df = df[TABLE_COLUMNS[table]].copy()
    for c in MONEY_COLUMNS & set(df.columns):
        df[c] = df[c].astype("int64") / 100
    fmt = vendor["file_format"]
    if fmt != "parquet":
        for c in MONEY_COLUMNS & set(df.columns):
            if vendor["money_style"] == "symbol":
                df[c] = df[c].map(lambda x: f"${x:,.2f}")
            elif vendor["money_style"] == "eu":
                df[c] = df[c].map(lambda x: f"{x:,.2f}".replace(",", "_").replace(".", ",").replace("_", "."))
            else:
                df[c] = df[c].map("{:.2f}".format)
        if fmt == "csv":
            for c in DATE_COLUMNS & set(df.columns):
                df[c] = pd.to_datetime(df[c]).dt.strftime(
                    {"us": "%m/%d/%Y", "eu": "%d.%m.%Y"}.get(vendor["date_style"], "%Y-%m-%d"))
    df.columns = [_header(c, vendor["header_style"]) for c in df.columns]
    if fmt != "parquet" and vendor["junk_rows"] and len(df) > 20:
        # Exports glued together (repeated header) and a TOTAL row at the bottom
        cut = int(rng.integers(5, len(df) - 5))
        header = pd.DataFrame([df.columns], columns=df.columns)
        total = pd.DataFrame([["TOTAL"] + [""] * (df.shape[1] - 1)], columns=df.columns)
        df = pd.concat([df.iloc[:cut].astype(str), header, df.iloc[cut:].astype(str), total], ignore_index=True)
    path = out_dir / f"{table}.{fmt}"
    if fmt == "parquet":
        df.to_parquet(path, index=False)
    elif fmt == "csv":
        df.to_csv(path, index=False)
    else:
        df.to_excel(path, index=False, engine="openpyxl")


def generate(out: Path, cfg: WorldConfig | None = None, policy: Policy | None = None) -> dict:
    cfg = cfg or WorldConfig()
    policy = policy or Policy()
    b = WorldBuilder(cfg, policy)
    rng = b.rng

    vendors = b.vendors()
    pos, lines = b.orders(vendors)
    ful = b.fulfilment(vendors, pos, lines)
    pos, lines = ful["pos"], ful["lines"]
    ded = b.deductions(pos, lines)
    ded = b.messy_refs(ded, pos)
    ded = b.duplicates_and_reversals(ded)
    rem, pay_truth = b.payments(pos)

    observed = out / "observed"
    truth_dir = out / "truth"
    observed.mkdir(parents=True, exist_ok=True)
    truth_dir.mkdir(parents=True, exist_ok=True)

    # Exact duplicate export rows (not real duplicates: same deduction / payment line twice)
    ded_obs = pd.concat([ded, ded[(ded["deductionType"] != DEDUCTION_REVERSAL) & (rng.random(len(ded)) < 0.01)]])
    rem_obs = pd.concat([rem, rem[rng.random(len(rem)) < 0.005]])
    # Remittances quote invoice numbers loosely too
    rem_obs = rem_obs.copy()
    loose = rng.random(len(rem_obs)) < 0.15
    rem_obs.loc[loose, "invoiceNumber"] = rem_obs.loc[loose, "invoiceNumber"].str.lower().str.replace("-", " ")
    # ...and sometimes quote the PO number where the invoice number should be
    quote_po = ~loose & (rng.random(len(rem_obs)) < 0.02)
    rem_obs.loc[quote_po, "invoiceNumber"] = rem_obs.loc[quote_po, "purchaseOrderNumber"]
    # Deductions still pending in Vendor Central: no number, no amount yet (not real deductions)
    pending = ded[(ded["deductionType"] != DEDUCTION_REVERSAL) & (rng.random(len(ded)) < 0.003)].copy()
    pending["deductionNumber"], pending["amount"] = "", 0
    ded_obs = pd.concat([ded_obs, pending])

    ship_lines = pos[pos["shipment_observed"]]
    pod = pos[pos["shipment_observed"] & (pos["pod_state"] != "missing")].assign(
        carrier=lambda x: rng.choice(["UPS Freight", "FedEx Freight", "XPO", "Estes", "Old Dominion"], len(x)))
    po_lines = lines.merge(pos[["purchaseOrderNumber", "purchaseOrderDate", "shipWindowEnd"]],
                           on="purchaseOrderNumber")
    inv_lines = lines.assign(invoiceType="Invoice", invoicedQuantity=lines["shippedQuantity"],
                             netCost=lines["invoiceNetCost"])

    by = {
        "purchase_orders": po_lines.groupby("vendor_code"),
        "shipments": ship_lines.groupby("vendor_code"),
        "proof_of_delivery": pod.groupby("vendor_code"),
        "invoices": inv_lines.groupby("vendor_code"),
        "remittances": rem_obs.groupby("vendor_code"),
        "deductions": ded_obs.sample(frac=1, random_state=cfg.seed).groupby("vendor_code"),
    }
    rows = Counter()
    for v in vendors.itertuples(index=False):
        vdir = observed / v.vendor_code
        vdir.mkdir(exist_ok=True)
        vs = pd.Series(v._asdict())
        for table, groups in by.items():
            try:
                df = groups.get_group(v.vendor_code)
            except KeyError:
                df = pd.DataFrame(columns=TABLE_COLUMNS[table])
            _write_table(df, table, vs, vdir, rng)
            rows[table] += len(df)

    vendors.drop(columns=["po_lines"]).to_csv(out / "vendors.csv", index=False)

    # Ground truth: one row per deduction (except reversals) and per payment problem
    td = ded[ded["deductionType"] != DEDUCTION_REVERSAL]
    usd = lambda cents, cur, date: (cents / rate_on(cur, date, b.rates)).round().astype("int64")  # noqa: E731
    truth = pd.concat([
        pd.DataFrame({
            "vendor_code": td["vendor_code"], "ref_type": "deduction", "ref": td["deductionNumber"],
            "category": td["category"], "status": td["status"],
            "recoverable_usd_cents": usd(td["recoverable"], td["currencyCode"], td["deductionDate"]),
            "expired_usd_cents": usd(td["expired_amount"], td["currencyCode"], td["deductionDate"]),
        }),
        pd.DataFrame({
            "vendor_code": pay_truth["vendor_code"], "ref_type": "invoice", "ref": pay_truth["ref"],
            "category": pay_truth["category"], "status": pay_truth["status"],
            "recoverable_usd_cents": pay_truth["recoverable"], "expired_usd_cents": pay_truth["expired_usd_cents"],
        }),
    ], ignore_index=True)
    truth.to_parquet(truth_dir / "truth.parquet", index=False)

    meta = {"config": asdict(cfg), "policy": asdict(policy), "rows": dict(rows),
            "observed_rows_total": int(sum(rows.values())), "truth_rows": len(truth)}
    (out / "world.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    return meta
