"""Tiered reference matching that refuses to guess.

Amazon quotes invoice, PO and deduction numbers loosely: different case, separators,
dropped prefixes and leading zeros, truncated to the last digits, or with a typo.
Each reference is resolved by the first tier that gives exactly one candidate:

  exact      the reference is the record number as written
  canonical  equal after normalization (case, separators, leading zeros)
  suffix     digits-only reference that ends exactly one record's digits (>= 4 digits)
  fuzzy      PO numbers only: exactly one PO within one edit (Levenshtein distance 1)

Two or more candidates on a tier -> AMBIGUOUS (sent to review, never auto-matched).
No candidate on any tier -> UNMATCHED.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from rapidfuzz import process
from rapidfuzz.distance import Levenshtein

from vre.normalize import canonical_ids

EXACT, CANONICAL, SUFFIX, FUZZY = "exact", "canonical", "suffix", "fuzzy"
AMBIGUOUS, UNMATCHED = "ambiguous", "unmatched"


def resolve(refs: pd.DataFrame, records: pd.DataFrame, *, id_col: str, suffix: bool = False,
            fuzzy: bool = False, min_suffix: int = 4) -> pd.DataFrame:
    """Resolve refs[vendor_code, ref] against records[vendor_code, id_col].

    Returns a frame aligned with `refs` holding `matched` (the record id or <NA>) and
    `tier` (exact / canonical / suffix / fuzzy / ambiguous / unmatched).
    """
    out = pd.DataFrame(index=refs.index)
    out["matched"] = pd.Series(pd.NA, index=refs.index, dtype="string")
    out["tier"] = UNMATCHED
    rec = records[["vendor_code", id_col]].drop_duplicates().rename(columns={id_col: "rid"})
    rec["canon"] = canonical_ids(rec["rid"])
    q = refs[["vendor_code", "ref"]].copy()
    q["ref"] = q["ref"].astype("string").fillna("").str.strip()
    q["canon"] = canonical_ids(q["ref"])
    todo = q["ref"] != ""

    # exact
    exact = rec.set_index(["vendor_code", "rid"]).index
    hit = todo & pd.MultiIndex.from_frame(q[["vendor_code", "ref"]]).isin(exact)
    out.loc[hit, "matched"] = q.loc[hit, "ref"]
    out.loc[hit, "tier"] = EXACT
    todo &= ~hit

    # canonical
    groups = rec.groupby(["vendor_code", "canon"])["rid"]
    canon_n = groups.size()
    canon_one = groups.first()
    key = pd.MultiIndex.from_frame(q[["vendor_code", "canon"]])
    n = pd.Series(canon_n.reindex(key).to_numpy(), index=q.index).fillna(0)
    one = todo & (n == 1)
    out.loc[one, "matched"] = canon_one.reindex(key).to_numpy()[one.to_numpy()]
    out.loc[one, "tier"] = CANONICAL
    many = todo & (n > 1)
    out.loc[many, "tier"] = AMBIGUOUS
    todo &= ~(one | many)

    if suffix and todo.any():
        rd = q["ref"].str.replace(r"[^0-9A-Za-z]", "", regex=True)
        digits_only = rd.str.fullmatch(r"\d+", na=False) & (rd.str.len() >= min_suffix)
        cand = todo & digits_only
        if cand.any():
            rec["digits"] = rec["rid"].astype("string").str.replace(r"\D", "", regex=True)
            lens = sorted(rd[cand].str.len().unique())
            for k in lens:
                sub = rec[rec["digits"].str.len() >= k]
                g = sub.assign(suf=sub["digits"].str[-k:]).groupby(["vendor_code", "suf"])["rid"]
                cnt, first = g.size(), g.first()
                mask = cand & (rd.str.len() == k)
                kk = pd.MultiIndex.from_arrays([q.loc[mask, "vendor_code"], rd[mask]])
                c = pd.Series(cnt.reindex(kk).to_numpy(), index=q.index[mask]).fillna(0)
                single = c.index[c == 1]
                out.loc[single, "matched"] = first.reindex(kk).to_numpy()[(c == 1).to_numpy()]
                out.loc[single, "tier"] = SUFFIX
                out.loc[c.index[c > 1], "tier"] = AMBIGUOUS
            todo &= ~out["tier"].isin([SUFFIX, AMBIGUOUS])

    if fuzzy and todo.any():
        # Typos are about characters, so compare the raw characters (case and separators aside).
        # Not the canonical form: dropping leading zeros turns 'MJPYNQ05' into 'MJPYNQ5' and
        # puts a one-character typo two edits away.
        rec["chars"] = rec["rid"].astype("string").str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)
        q["chars"] = q["ref"].str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)
        pools = {v: g for v, g in rec.groupby("vendor_code")}
        for i in np.flatnonzero(todo.to_numpy()):
            idx = q.index[i]
            pool = pools.get(q.at[idx, "vendor_code"])
            if pool is None or not q.at[idx, "chars"]:
                continue
            hits = process.extract(q.at[idx, "chars"], pool["chars"].tolist(), scorer=Levenshtein.distance,
                                   score_cutoff=1, limit=None)
            if len(hits) == 1:
                out.at[idx, "matched"] = pool["rid"].iat[hits[0][2]]
                out.at[idx, "tier"] = FUZZY
            elif len(hits) > 1:
                out.at[idx, "tier"] = AMBIGUOUS
    return out
