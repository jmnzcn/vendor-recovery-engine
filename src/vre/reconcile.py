"""Reconciliation: turn validated tables into findings with a status, an amount and a reason.

Every deduction gets exactly one finding, and so does every invoice with a payment
problem. A finding is only CLAIMABLE when the vendor's own records prove it; otherwise
it says why not (valid deduction, missing evidence, ambiguous reference, already
reversed, past the dispute window).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from vre.fx import rate_on
from vre.ingest import Loaded
from vre.match import AMBIGUOUS, UNMATCHED, resolve
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

FINDING_COLUMNS = [
    "vendor_code", "ref_type", "ref", "category", "status", "recoverable_usd_cents", "expired_usd_cents",
    "amount_cents", "currency", "event_date", "dispute_deadline", "match_tier", "matched_record",
    "reason", "evidence", "source_file", "source_row",
]


@dataclass
class Result:
    findings: pd.DataFrame
    quality: dict


def _money(cents: int | float, currency: str = "USD") -> str:
    sym = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "C$", "MXN": "MX$", "JPY": "¥"}.get(currency, "")
    return f"{sym}{cents / 100:,.2f}"


def _key(df: pd.DataFrame, cols: list[str]) -> pd.MultiIndex:
    return pd.MultiIndex.from_frame(df[cols])


def _line_key(s: pd.DataFrame) -> pd.MultiIndex:
    """(vendor, matched invoice, ASIN): identifies the invoice line a deduction is about."""
    return pd.MultiIndex.from_arrays([s["vendor_code"], s["matched_record"].astype(str), s["amazonProductIdentifier"]])


class Reconciler:
    def __init__(self, loaded: Loaded, policy: Policy):
        self.t, self.policy = loaded.tables, policy
        self.as_of = pd.Timestamp(policy.as_of)
        self.quality: dict = {}
        self._index()

    # ------------------------------------------------------------------ evidence indexes
    def _index(self) -> None:
        t = self.t
        inv = t["invoices"]
        self.inv_lines = inv.set_index(["vendor_code", "invoiceNumber", "amazonProductIdentifier"]).sort_index()
        inv_tot = inv.assign(total=inv["invoicedQuantity"].astype("int64") * inv["netCost"].astype("int64"))
        self.inv_head = inv_tot.groupby(["vendor_code", "invoiceNumber"]).agg(
            invoiceDate=("invoiceDate", "first"), purchaseOrderNumber=("purchaseOrderNumber", "first"),
            total=("total", "sum"), currency=("currencyCode", "first"),
            inv_source_file=("_source_file", "first"), inv_source_row=("_source_row", "first"))
        po = t["purchase_orders"].assign(po_canon=lambda x: canonical_ids(x["purchaseOrderNumber"]))
        self.po_lines = po.set_index(["vendor_code", "po_canon", "amazonProductIdentifier"]).sort_index()
        self.po_head = po.groupby(["vendor_code", "po_canon"]).agg(
            purchaseOrderNumber=("purchaseOrderNumber", "first"), shipWindowEnd=("shipWindowEnd", "max"))
        sh = t["shipments"].assign(po_canon=lambda x: canonical_ids(x["purchaseOrderNumber"]))
        self.ship = sh.drop_duplicates(["vendor_code", "po_canon"]).set_index(["vendor_code", "po_canon"])
        self.pod = t["proof_of_delivery"].drop_duplicates(["vendor_code", "billOfLadingNumber"]).set_index(
            ["vendor_code", "billOfLadingNumber"])

    # ------------------------------------------------------------------ deductions
    def deductions(self) -> pd.DataFrame:
        p = self.policy
        d = self.t["deductions"].copy()
        d["ded_canon"] = canonical_ids(d["deductionNumber"])
        is_rev = d["deductionType"] == DEDUCTION_REVERSAL
        rev, d = d[is_rev].copy(), d[~is_rev].copy()

        # Reversals: Amazon paying back a deduction, quoting its number loosely
        rev = rev.assign(ref=rev["referenceNumber"])
        rm = resolve(rev, d, id_col="deductionNumber")
        rev["original"] = rm["matched"]
        self.quality["reversals_unmatched"] = int(rev["original"].isna().sum())
        reversed_amt = rev.dropna(subset=["original"]).groupby(["vendor_code", "original"])["amount"].sum()

        # Duplicates: the same deduction applied again within the window
        d["ref_canon"] = canonical_ids(d["referenceNumber"])
        dup_key = ["vendor_code", "deductionType", "reasonCode", "ref_canon", "amazonProductIdentifier",
                   "quantity", "amount"]
        d = d.sort_values(["deductionDate", "deductionNumber"])
        first_date = d.groupby(dup_key, dropna=False)["deductionDate"].transform("first")
        first_no = d.groupby(dup_key, dropna=False)["deductionNumber"].transform("first")
        is_dup = (d["deductionNumber"] != first_no) & (
            (d["deductionDate"] - first_date).dt.days <= p.duplicate_window_days)

        d["category"] = np.select(
            [d["deductionType"] == DEDUCTION_SHORTAGE, d["deductionType"] == DEDUCTION_PRICE,
             (d["deductionType"] == DEDUCTION_CHARGEBACK) & (d["reasonCode"] == REASON_LATE_SHIPMENT),
             (d["deductionType"] == DEDUCTION_CHARGEBACK) & (d["reasonCode"] == REASON_ASN_LATE)],
            [Category.SHORTAGE, Category.PRICE_CLAIM, Category.CHARGEBACK_LATE_SHIPMENT, Category.CHARGEBACK_ASN],
            "UNKNOWN")
        d["status"], d["reason"], d["evidence"] = "", "", ""
        d["match_tier"], d["matched_record"] = "", pd.Series(pd.NA, index=d.index, dtype="string")

        dup = d[is_dup]
        d.loc[dup.index, "category"] = Category.DUPLICATE_DEDUCTION
        d.loc[dup.index, "status"] = Status.CLAIMABLE
        d.loc[dup.index, "matched_record"] = first_no[is_dup]
        d.loc[dup.index, "match_tier"] = "duplicate"
        d.loc[dup.index, "reason"] = ("Same deduction applied again: " + first_no[is_dup]
                                      + " has the same reference, ASIN, quantity and amount.")
        d.loc[dup.index, "evidence"] = ("Original deduction " + first_no[is_dup] + " dated "
                                        + first_date[is_dup].dt.strftime("%Y-%m-%d"))

        own = d[~is_dup]
        inv_refs = own[own["deductionType"].isin([DEDUCTION_SHORTAGE, DEDUCTION_PRICE])]
        po_refs = own[own["deductionType"] == DEDUCTION_CHARGEBACK]
        mi = resolve(inv_refs.assign(ref=inv_refs["referenceNumber"]), self.t["invoices"], id_col="invoiceNumber",
                     suffix=True, min_suffix=p.min_suffix_digits)
        mp = resolve(po_refs.assign(ref=po_refs["referenceNumber"]), self.t["purchase_orders"],
                     id_col="purchaseOrderNumber", fuzzy=True)
        m = pd.concat([mi, mp])
        d.loc[m.index, "matched_record"] = m["matched"]
        d.loc[m.index, "match_tier"] = m["tier"]

        amb = m.index[m["tier"] == AMBIGUOUS]
        d.loc[amb, "status"] = Status.NEEDS_REVIEW
        d.loc[amb, "reason"] = ("Reference " + d.loc[amb, "referenceNumber"]
                                + " matches more than one record; not matched automatically.")
        un = m.index[m["tier"] == UNMATCHED]
        d.loc[un, "status"] = Status.INSUFFICIENT_EVIDENCE
        d.loc[un, "reason"] = "Reference " + d.loc[un, "referenceNumber"] + " not found in the invoice or PO records."

        ok = d.index.isin(m.index[~m["tier"].isin([AMBIGUOUS, UNMATCHED])])
        self._shortages(d, ok & (d["deductionType"] == DEDUCTION_SHORTAGE).to_numpy())
        self._price_claims(d, ok & (d["deductionType"] == DEDUCTION_PRICE).to_numpy())
        self._chargebacks(d, ok & (d["deductionType"] == DEDUCTION_CHARGEBACK).to_numpy())

        # Reversals net against what is still recoverable
        d["amount_cents"] = d["amount"].astype("int64")
        back = reversed_amt.reindex(_key(d, ["vendor_code", "deductionNumber"])).fillna(0).to_numpy().astype("int64")
        d["reversed"] = back
        claim = d["status"] == Status.CLAIMABLE
        d["recoverable"] = np.where(claim, np.maximum(d["amount_cents"] - back, 0), 0)
        full = claim & (back > 0) & (d["recoverable"] == 0)
        d.loc[full, "status"] = Status.ALREADY_RECOVERED
        d.loc[full, "reason"] = "Amazon already reversed this deduction in full."
        part = claim & (back > 0) & (d["recoverable"] > 0)
        d.loc[part, "evidence"] = d.loc[part, "evidence"] + "; partially reversed: " + [
            _money(b, c) for b, c in zip(d.loc[part, "reversed"], d.loc[part, "currencyCode"], strict=True)]

        d["event_date"] = d["deductionDate"]
        d["dispute_deadline"] = d["deductionDate"] + pd.to_timedelta(p.deduction_window_days, "D")
        out = d.rename(columns={"deductionNumber": "ref", "currencyCode": "currency",
                                "_source_file": "source_file", "_source_row": "source_row"})
        out["ref_type"] = "deduction"
        out["recoverable_usd_cents"] = self._usd(out["recoverable"], out["currency"], out["event_date"])
        return self._expire(out)

    def _shortages(self, d: pd.DataFrame, mask: np.ndarray) -> None:
        s = d[mask]
        if s.empty:
            return
        key = _line_key(s)
        line = self.inv_lines.reindex(key)
        po_canon = canonical_ids(pd.Series(line["purchaseOrderNumber"].to_numpy(), index=s.index).fillna(""))
        ship = self.ship.reindex(pd.MultiIndex.from_arrays([s["vendor_code"], po_canon]))
        pod = self.pod.reindex(pd.MultiIndex.from_arrays([s["vendor_code"], ship["billOfLadingNumber"].fillna("")]))
        no_line = line["invoicedQuantity"].isna().to_numpy()
        no_ship = ship["shipmentIdentifier"].isna().to_numpy()
        no_pod = pod["podDocumentId"].isna().to_numpy()
        cartons = ship["cartonCount"].to_numpy(dtype="float")
        delivered = pod["cartonsDelivered"].to_numpy(dtype="float")
        full = delivered >= cartons
        status = np.select([no_line, no_ship, no_pod, full],
                           [Status.INSUFFICIENT_EVIDENCE, Status.INSUFFICIENT_EVIDENCE, Status.INSUFFICIENT_EVIDENCE,
                            Status.CLAIMABLE], Status.NOT_CLAIMABLE)
        inv_no = s["matched_record"].astype(str).to_numpy()
        reason = np.select(
            [no_line, no_ship, no_pod, full],
            ["ASIN not on invoice " + inv_no,
             "No shipment record for the PO on invoice " + inv_no + "; delivery cannot be proven.",
             "No carrier proof of delivery for the shipment; delivery cannot be proven.",
             "Carrier proof of delivery shows every carton delivered; the shortage is not supported."],
            "Proof of delivery shows cartons missing at delivery; the shortage is valid.")
        evidence = np.where(no_pod | no_ship | no_line, "",
                            "POD " + pod["podDocumentId"].fillna("").to_numpy() + ": "
                            + pd.Series(delivered).fillna(0).astype(int).astype(str).to_numpy() + " of "
                            + pd.Series(cartons).fillna(0).astype(int).astype(str).to_numpy()
                            + " cartons delivered " + pod["deliveredDate"].dt.strftime("%Y-%m-%d").fillna("").to_numpy()
                            + "; BOL " + ship["billOfLadingNumber"].fillna("").to_numpy())
        d.loc[s.index, "status"], d.loc[s.index, "reason"], d.loc[s.index, "evidence"] = status, reason, evidence

    def _price_claims(self, d: pd.DataFrame, mask: np.ndarray) -> None:
        s = d[mask]
        if s.empty:
            return
        key = _line_key(s)
        line = self.inv_lines.reindex(key)
        po_canon = canonical_ids(pd.Series(line["purchaseOrderNumber"].to_numpy(), index=s.index).fillna(""))
        po_key = pd.MultiIndex.from_arrays([s["vendor_code"], po_canon, s["amazonProductIdentifier"]])
        po = self.po_lines.reindex(po_key)
        inv_cost = line["netCost"].to_numpy(dtype="float")
        po_cost = po["netCost"].to_numpy(dtype="float")
        missing = np.isnan(inv_cost) | np.isnan(po_cost)
        over = inv_cost > po_cost
        cur = s["currencyCode"].to_numpy()
        fmt = lambda a: np.array([_money(x, c) if not np.isnan(x) else "?" for x, c in zip(a, cur, strict=True)])  # noqa: E731
        status = np.select([missing, over], [Status.INSUFFICIENT_EVIDENCE, Status.NOT_CLAIMABLE], Status.CLAIMABLE)
        reason = np.select(
            [missing, over],
            ["Invoice or PO line for this ASIN not found; the agreed cost cannot be checked.",
             "Invoiced above the PO cost; Amazon's price claim is valid."],
            "Invoiced at the agreed PO cost; the price claim is not supported.")
        evidence = np.where(missing, "", "Invoice unit cost " + fmt(inv_cost) + " vs PO unit cost " + fmt(po_cost)
                            + " (PO " + line["purchaseOrderNumber"].fillna("").to_numpy() + ")")
        d.loc[s.index, "status"], d.loc[s.index, "reason"], d.loc[s.index, "evidence"] = status, reason, evidence

    def _chargebacks(self, d: pd.DataFrame, mask: np.ndarray) -> None:
        s = d[mask]
        if s.empty:
            return
        canon = canonical_ids(s["matched_record"].astype("string").fillna(""))
        key = pd.MultiIndex.from_arrays([s["vendor_code"], canon])
        ship = self.ship.reindex(key)
        head = self.po_head.reindex(key)
        no_ship = ship["shipmentIdentifier"].isna().to_numpy()
        late_reason = (s["reasonCode"] == REASON_LATE_SHIPMENT).to_numpy()
        shipped, window = ship["shippedDate"], head["shipWindowEnd"]
        asn = ship["shipmentConfirmationDate"]
        compliant = np.where(late_reason, (shipped <= window).to_numpy(), (asn <= shipped).to_numpy())
        status = np.select([no_ship, compliant], [Status.INSUFFICIENT_EVIDENCE, Status.CLAIMABLE], Status.NOT_CLAIMABLE)
        f = lambda x: x.dt.strftime("%Y-%m-%d").fillna("").to_numpy()  # noqa: E731
        reason = np.select(
            [no_ship, compliant & late_reason, compliant],
            ["No shipment record for the PO; compliance cannot be proven.",
             "Shipped inside the PO ship window; the late-shipment chargeback is not supported.",
             "ASN was sent no later than the ship date; the ASN chargeback is not supported."],
            np.where(late_reason, "Shipped after the PO ship window; the chargeback is valid.",
                     "ASN was sent after the ship date; the chargeback is valid."))
        evidence = np.where(no_ship, "", "Shipment " + ship["shipmentIdentifier"].fillna("").to_numpy()
                            + " shipped " + f(shipped) + np.where(late_reason, ", ship window ends " + f(window),
                                                                  ", ASN sent " + f(asn)))
        d.loc[s.index, "status"], d.loc[s.index, "reason"], d.loc[s.index, "evidence"] = status, reason, evidence

    # ------------------------------------------------------------------ payments
    def payments(self) -> pd.DataFrame:
        p = self.policy
        r = self.t["remittances"].copy()
        m = resolve(r.assign(ref=r["invoiceNumber"]), self.t["invoices"], id_col="invoiceNumber",
                    suffix=True, min_suffix=p.min_suffix_digits)
        r["invoice"] = m["matched"]
        # Remittances sometimes quote the PO number instead of the invoice number: accept it
        # only when that PO has exactly one invoice
        miss = r["invoice"].isna()
        if miss.any():
            heads = self.inv_head.reset_index()
            mp = resolve(r[miss].assign(ref=r.loc[miss, "invoiceNumber"]), heads, id_col="purchaseOrderNumber")
            per_po = heads.groupby(["vendor_code", "purchaseOrderNumber"])["invoiceNumber"].agg(["size", "first"])
            k = pd.MultiIndex.from_arrays([r.loc[miss, "vendor_code"], mp["matched"].fillna("")])
            hit = per_po.reindex(k)
            one = (hit["size"] == 1).to_numpy()
            r.loc[r.index[miss][one], "invoice"] = hit["first"].to_numpy()[one]
            self.quality["remittance_lines_matched_by_po"] = int(one.sum())
        self.quality["remittance_lines_unmatched"] = int(r["invoice"].isna().sum())
        r = r.dropna(subset=["invoice"])
        # Exported twice with the invoice number spelled differently: still one payment
        before = len(r)
        r = r.drop_duplicates(["vendor_code", "paymentNumber", "invoice", "amountPaid"])
        self.quality["remittance_duplicates_after_matching"] = before - len(r)

        pay = r.groupby(["vendor_code", "invoice"]).agg(
            paid=("amountPaid", "sum"), rate=("exchangeRate", "first"), paymentDate=("paymentDate", "first"),
            paymentNumber=("paymentNumber", "first"), source_file=("_source_file", "first"),
            source_row=("_source_row", "first"))
        pay.index.names = ["vendor_code", "invoiceNumber"]
        head = self.inv_head.copy()
        head["dueDate"] = head["invoiceDate"] + pd.to_timedelta(p.payment_terms_days, "D")
        j = head.join(pay, how="left")
        j = j.reset_index().rename(columns={"invoiceNumber": "ref"})
        rows = []

        paid = j[j["paid"].notna()].copy()
        due_applied = (paid["total"] / paid["rate"]).round()
        gap = due_applied - paid["paid"].astype("int64")
        ecb = rate_on(paid["currency"], paid["paymentDate"])
        deviation = paid["rate"] / ecb - 1
        under = gap > p.payment_tolerance_cents
        u = paid[under].assign(category=Category.UNDERPAYMENT, recoverable=gap[under].astype("int64"))
        u["reason"] = "Paid less than the invoice amount with no deduction explaining the difference."
        u["evidence"] = ("Payment " + u["paymentNumber"] + " on " + u["paymentDate"].dt.strftime("%Y-%m-%d")
                         + ": paid " + u["paid"].map(_money) + " of " + due_applied[under].map(_money) + " due")
        rows.append(u)

        fx_gap = (paid["total"] / ecb).round() - due_applied
        fx = (paid["currency"] != "USD") & (deviation > p.fx_tolerance) & (fx_gap > p.payment_tolerance_cents)
        f = paid[fx].assign(category=Category.FX_VARIANCE, recoverable=fx_gap[fx].astype("int64"))
        f["reason"] = "Exchange rate applied is worse than the ECB reference rate beyond the allowed spread."
        f["evidence"] = ("Applied " + f["rate"].map("{:.6f}".format) + " " + f["currency"] + "/USD vs ECB "
                         + ecb[fx].map("{:.6f}".format) + " on " + f["paymentDate"].dt.strftime("%Y-%m-%d")
                         + " (" + (deviation[fx] * 100).map("{:+.2f}%".format) + ")")
        rows.append(f)

        unpaid = j[j["paid"].isna() & (j["dueDate"] + pd.to_timedelta(p.payment_grace_days, "D") < self.as_of)].copy()
        unpaid["category"] = Category.UNPAID_INVOICE
        unpaid_rate = rate_on(unpaid["currency"], unpaid["dueDate"])
        unpaid["recoverable"] = (unpaid["total"] / unpaid_rate).round().astype("int64")
        unpaid["reason"] = "Invoice past due with no payment in any remittance."
        unpaid["evidence"] = "Due " + unpaid["dueDate"].dt.strftime("%Y-%m-%d") + ", invoice total " + [
            _money(t, c) for t, c in zip(unpaid["total"], unpaid["currency"], strict=True)]
        # The evidence is an absence: point at the invoice that was never paid
        unpaid["source_file"], unpaid["source_row"] = unpaid["inv_source_file"], unpaid["inv_source_row"]
        rows.append(unpaid)

        out = pd.concat(rows, ignore_index=True)
        out["ref_type"] = "invoice"
        out["status"] = Status.CLAIMABLE
        out["amount_cents"] = out["total"].astype("int64")
        out["recoverable_usd_cents"] = out["recoverable"].astype("Int64")
        out["event_date"] = out["dueDate"]
        out["dispute_deadline"] = out["dueDate"] + pd.to_timedelta(p.payment_window_days, "D")
        out["match_tier"], out["matched_record"] = "", out["ref"]
        return self._expire(out)

    # ------------------------------------------------------------------ helpers
    def _usd(self, cents: pd.Series, currency: pd.Series, date: pd.Series) -> pd.Series:
        return (cents.astype("float64") / rate_on(currency, date)).round().astype("Int64")

    def _expire(self, df: pd.DataFrame) -> pd.DataFrame:
        expired = (df["status"] == Status.CLAIMABLE) & (df["dispute_deadline"] < self.as_of)
        df["expired_usd_cents"] = np.where(expired, df["recoverable_usd_cents"], 0)
        df.loc[expired, "recoverable_usd_cents"] = 0
        df.loc[expired, "reason"] = "Past the dispute window (" + df.loc[expired, "dispute_deadline"].dt.strftime(
            "%Y-%m-%d") + "). " + df.loc[expired, "reason"]
        df.loc[expired, "status"] = Status.EXPIRED
        df.loc[df["status"] != Status.CLAIMABLE, "recoverable_usd_cents"] = 0
        return df

    def run(self) -> Result:
        findings = pd.concat([self.deductions(), self.payments()], ignore_index=True)
        findings = findings[FINDING_COLUMNS].copy()
        for c in ("category", "status"):
            findings[c] = findings[c].astype(str)
        findings["recoverable_usd_cents"] = findings["recoverable_usd_cents"].fillna(0).astype("int64")
        findings["expired_usd_cents"] = findings["expired_usd_cents"].fillna(0).astype("int64")
        return Result(findings=findings.sort_values(["vendor_code", "ref_type", "ref"]).reset_index(drop=True),
                      quality=self.quality)


def reconcile(loaded: Loaded, policy: Policy | None = None) -> Result:
    return Reconciler(loaded, policy or Policy()).run()
