"""Load a vendor's exports (Parquet, CSV or Excel) into typed, validated tables.

Every kept row carries its lineage (_source_file, _source_row) so a finding can be traced
back to the exact line of the file it came from. Rows that cannot be trusted go to a
quarantine table with the reason, never silently away.
"""
from __future__ import annotations

import os
import re
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from pandera.errors import SchemaErrors

from vre.contracts import ALIASES, COLUMNS, DATE, FLOAT, INT, MONEY, NATURAL_KEYS, SCHEMAS, STRING
from vre.normalize import parse_dates, parse_int, parse_money_cents

FORMATS = (".parquet", ".csv", ".xlsx")


def _key(header: str) -> str:
    return re.sub(r"[^a-z0-9#]", "", str(header).lower())


def map_headers(columns: list[str], table: str) -> dict[str, str]:
    """camelCase, 'Title Case', snake_case and known aliases -> canonical column names."""
    canon = {_key(c): c for c in COLUMNS[table]}
    out = {}
    for col in columns:
        k = _key(col)
        if k in canon:
            out[col] = canon[k]
        elif k in ALIASES and ALIASES[k] in COLUMNS[table]:
            out[col] = ALIASES[k]
    return out


@dataclass
class Loaded:
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    quarantine: list[pd.DataFrame] = field(default_factory=list)
    stats: list[dict] = field(default_factory=list)

    def quarantine_frame(self) -> pd.DataFrame:
        cols = ["vendor_code", "table", "_source_file", "_source_row", "reason", "raw"]
        return pd.concat(self.quarantine, ignore_index=True) if self.quarantine else pd.DataFrame(columns=cols)


def _read(path: Path) -> pd.DataFrame:
    if path.suffix == ".parquet":
        return pd.read_parquet(path)
    if path.suffix == ".csv":
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    return pd.read_excel(path, dtype=object, engine="calamine")


def _quarantine(df: pd.DataFrame, mask: pd.Series, reason: pd.Series | str, vendor: str, table: str) -> pd.DataFrame:
    q = df.loc[mask, ["_source_file", "_source_row"]].copy()
    q["vendor_code"], q["table"] = vendor, table
    q["reason"] = reason if isinstance(reason, str) else reason[mask].to_numpy()
    raw = df.loc[mask].drop(columns=["_source_file", "_source_row"]).astype("string").fillna("")
    q["raw"] = raw.agg(" | ".join, axis=1)
    return q


def load_table(path: Path, table: str, vendor: str) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    raw = _read(path)
    # Row 1 of a CSV/Excel file is the header, so data row i sits on line i + 2
    raw["_source_file"] = f"{vendor}/{path.name}"
    raw["_source_row"] = raw.index + (2 if path.suffix != ".parquet" else 1)
    mapping = map_headers([c for c in raw.columns if not c.startswith("_")], table)
    df = raw.rename(columns=mapping)
    missing = [c for c in COLUMNS[table] if c not in df.columns]
    for c in missing:
        df[c] = pd.NA
    q_parts = []

    # Junk rows from glued exports: a repeated header line, a TOTAL/summary line, a blank line
    cols = list(COLUMNS[table])
    first = df[cols[0]].astype("string").str.strip()
    header_values = {str(h).strip() for h in mapping} | set(cols)
    repeated_header = first.isin(header_values)
    summary = first.str.upper().isin(["TOTAL", "TOTALS", "GRAND TOTAL", "SUBTOTAL"])
    blank = df[cols].isna().all(axis=1) | (first.fillna("") == "") & df[cols[1:]].isna().all(axis=1)
    junk = repeated_header | summary | blank
    if junk.any():
        reason = (pd.Series("summary row", index=df.index)
                  .where(summary, "repeated header row").where(~blank, "blank row"))
        q_parts.append(_quarantine(df, junk, reason, vendor, table))
        df = df[~junk]

    # Parse every column into its type; remember which cells failed to parse
    failures: dict[str, pd.Series] = {}
    out = df[["_source_file", "_source_row"]].copy()
    for col, kind in COLUMNS[table].items():
        s = df[col]
        typed = not (pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s))
        if kind == MONEY:
            v = parse_money_cents(s)
        elif kind == DATE:
            v = parse_dates(s).astype("datetime64[ns]")
        elif kind == INT:
            v = parse_int(s)
        elif kind == FLOAT:
            v = pd.to_numeric(s, errors="coerce").astype("float64")
        else:
            v = s.astype("string").str.strip().fillna("").astype(str)
        if kind != STRING:
            present = s.notna() if typed else s.notna() & (s.astype("string").str.strip() != "")
            failed = present & v.isna()
            if failed.any():
                failures[col] = failed
        out[col] = v
    if failures:
        flags = pd.DataFrame(failures)
        unparseable = flags.any(axis=1)
        reason = flags[unparseable].apply(lambda r: "; ".join(f"unparseable {c}" for c in r.index[r]), axis=1)
        q_parts.append(_quarantine(df, unparseable, reason.reindex(df.index).fillna(""), vendor, table))
        out = out[~unparseable]

    out.insert(0, "vendor_code", vendor)
    stats = {"vendor_code": vendor, "table": table, "file": path.name, "source_file": f"{vendor}/{path.name}",
             "rows_read": len(raw), "rows_quarantined": int(sum(len(q) for q in q_parts)),
             "unmapped_headers": [c for c in raw.columns if not c.startswith("_") and c not in mapping]}
    quarantine = pd.concat(q_parts, ignore_index=True) if q_parts else pd.DataFrame()
    return out.reset_index(drop=True), quarantine, stats


def check_contract(df: pd.DataFrame, table: str) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """Validate a whole table at once (all vendors): bad rows to quarantine, then merge
    records exported twice. Returns (kept, quarantined, column-level issues)."""
    issues: list[str] = []
    quarantined = pd.DataFrame()
    if len(df):
        try:
            SCHEMAS[table].validate(df, lazy=True)
        except SchemaErrors as err:
            fc = err.failure_cases
            # Column-level problems have no row index: report them, row problems quarantine the row
            issues = sorted({f"{c}: {k}" for c, k, i in zip(fc["column"], fc["check"], fc["index"], strict=True)
                             if pd.isna(i)})
            fc = fc.dropna(subset=["index"])
            if len(fc):
                fc = fc.assign(msg=fc["column"].astype(str) + ": " + fc["check"].astype(str))
                reasons = fc.groupby("index")["msg"].agg(lambda m: "; ".join(sorted(set(m))))
                mask = df.index.isin(reasons.index)
                q = df.loc[mask, ["vendor_code", "_source_file", "_source_row"]].copy()
                q["table"] = table
                q["reason"] = reasons.reindex(q.index).to_numpy()
                q["raw"] = df.loc[mask, list(COLUMNS[table])].astype("string").fillna("").agg(" | ".join, axis=1)
                quarantined = q
                df = df[~mask]
    kept = df.drop_duplicates(subset=["vendor_code", *NATURAL_KEYS[table]], keep="first")
    return kept.reset_index(drop=True), quarantined, issues


def _load_task(task: tuple[Path, str, str]) -> tuple[str, pd.DataFrame, pd.DataFrame, dict]:
    path, table, vendor = task
    return (table, *load_table(path, table, vendor))


def load_vendor_dir(root: Path, workers: int | None = None) -> Loaded:
    """Load observed/<vendor>/<table>.<ext> for every vendor under root.

    Files are parsed in parallel (each file needs its own format detection); contract
    checks and de-duplication then run once per table over all vendors."""
    loaded = Loaded()
    parts: dict[str, list[pd.DataFrame]] = {t: [] for t in COLUMNS}
    tasks = [(vdir / f"{table}{ext}", table, vdir.name)
             for vdir in sorted(p for p in root.iterdir() if p.is_dir())
             for table in COLUMNS for ext in FORMATS if (vdir / f"{table}{ext}").exists()]
    workers = workers if workers is not None else min(8, os.cpu_count() or 1)
    if workers > 1 and len(tasks) > 12:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_load_task, tasks, chunksize=8))
    else:
        results = [_load_task(t) for t in tasks]
    for table, df, q, st in results:
        parts[table].append(df)
        if len(q):
            loaded.quarantine.append(q)
        loaded.stats.append(st)
    for table, frames in parts.items():
        frames = [f for f in frames if len(f)]
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
            columns=["vendor_code", "_source_file", "_source_row", *COLUMNS[table]])
        before = df.groupby("_source_file").size()
        kept, q, issues = check_contract(df, table)
        if len(q):
            loaded.quarantine.append(q)
        failed = q.groupby("_source_file").size() if len(q) else pd.Series(dtype="int64")
        after = kept.groupby("_source_file").size()
        for st in loaded.stats:
            if st["table"] == table:
                f = st["source_file"]
                st["rows_quarantined"] += int(failed.get(f, 0))
                st["rows_kept"] = int(after.get(f, 0))
                st["duplicates_removed"] = int(before.get(f, 0) - failed.get(f, 0) - after.get(f, 0))
                st["schema_issues"] = issues
        loaded.tables[table] = kept
    return loaded
