"""Dispute letters drafted by Claude and checked by a deterministic verifier.

The model only sees the facts of one finding and writes the letter. Before a draft can be
used, `verify` extracts every amount, identifier and date it mentions and requires each
one to appear in those facts; the reference and the disputed amount must be there too.
A draft that fails is rejected, never edited by hand into shape.

Without Anthropic credentials the same pipeline runs on a deterministic template, so the
verifier, the mutation test and the reports work offline.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from vre.policy import Category

MODEL = "claude-opus-5-5"
SYSTEM = (
    "You write dispute letters that an Amazon vendor sends to Amazon Vendor Central about a deduction or "
    "payment. Write in plain, polite business English, under 160 words, as the vendor. Use only the facts "
    "you are given: every amount, identifier, document number and date in the letter must be copied exactly "
    "from the facts, and the letter must state the reference and the amount disputed. Do not add facts, "
    "attachments or deadlines that are not in the facts. Return only the letter body, no subject line."
)
CATEGORY_TEXT = {
    Category.SHORTAGE: "shortage claim", Category.PRICE_CLAIM: "price claim",
    Category.CHARGEBACK_LATE_SHIPMENT: "late-shipment chargeback", Category.CHARGEBACK_ASN: "ASN chargeback",
    Category.DUPLICATE_DEDUCTION: "duplicate deduction", Category.UNDERPAYMENT: "underpayment",
    Category.UNPAID_INVOICE: "unpaid invoice", Category.FX_VARIANCE: "exchange-rate variance",
}
SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "C$", "MXN": "MX$", "JPY": "¥"}

_MONEY = re.compile(r"(?:MX\$|C\$|US\$|\$|€|£|¥)\s?\d[\d,]*(?:\.\d{1,2})?")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_ID = re.compile(r"\b[A-Z0-9][A-Z0-9-]{5,}\b")


def money(cents: int, currency: str = "USD") -> str:
    return f"{SYMBOLS.get(currency, '$')}{cents / 100:,.2f}"


def facts_for(row: pd.Series, vendor_name: str) -> dict:
    """Everything the letter may say, and nothing else."""
    is_deduction = row["ref_type"] == "deduction"
    disputed = money(int(row["amount_cents"]), row["currency"]) if is_deduction else money(
        int(row["recoverable_usd_cents"]))
    return {
        "vendor": vendor_name, "vendor_code": row["vendor_code"],
        "reference_type": "deduction" if is_deduction else "invoice", "reference": row["ref"],
        "issue": CATEGORY_TEXT.get(Category(row["category"]), row["category"].lower()),
        "amount_disputed": disputed, "recoverable_usd": money(int(row["recoverable_usd_cents"])),
        "dispute_by": row["dispute_deadline"].strftime("%Y-%m-%d"),
        "matched_record": "" if pd.isna(row["matched_record"]) else str(row["matched_record"]),
        "finding": row["reason"], "evidence": row["evidence"] or "",
    }


@dataclass
class Allowed:
    amounts: set[int] = field(default_factory=set)   # cents, any currency
    ids: set[str] = field(default_factory=set)
    dates: set[str] = field(default_factory=set)


def _id_like(token: str) -> bool:
    """Identifier-looking token. Not only tokens with digits: PO and shipment numbers can be all
    letters (QQUOGOZZ), and mutation testing showed an invented one slipping through."""
    return bool(re.search(r"\d", token)) or "-" in token or len(token) >= 8


def _cents(text: str) -> int:
    return round(float(re.sub(r"[^\d.]", "", text)) * 100)


def allowed_from(facts: dict) -> Allowed:
    blob = " ".join(str(v) for v in facts.values())
    return Allowed(
        amounts={_cents(m) for m in _MONEY.findall(blob)},
        ids={m for m in _ID.findall(blob) if _id_like(m)} | {facts["reference"], facts["vendor_code"]},
        dates=set(_DATE.findall(blob)),
    )


def verify(draft: str, facts: dict) -> list[str]:
    """Violations in a draft; an empty list means every checkable fact is grounded."""
    ok = allowed_from(facts)
    problems = []
    for m in _MONEY.findall(draft):
        if _cents(m) not in ok.amounts:
            problems.append(f"amount not in the facts: {m.strip()}")
    for d in _DATE.findall(draft):
        if d not in ok.dates:
            problems.append(f"date not in the facts: {d}")
    stripped = _MONEY.sub(" ", _DATE.sub(" ", draft))
    for t in _ID.findall(stripped):
        if _id_like(t) and t not in ok.ids and not t.isdigit():
            problems.append(f"identifier not in the facts: {t}")
    if facts["reference"] not in draft:
        problems.append("reference missing")
    if _cents(facts["amount_disputed"]) not in {_cents(m) for m in _MONEY.findall(draft)}:
        problems.append("disputed amount missing")
    return problems


def template_draft(facts: dict) -> str:
    """Deterministic letter used when no model is available."""
    evidence = f" Our records show: {facts['evidence']}." if facts["evidence"] else ""
    return (f"Hello Vendor Central team,\n\nWe dispute {facts['reference_type']} {facts['reference']} "
            f"({facts['issue']}) for {facts['amount_disputed']}. {facts['finding']}{evidence}\n\n"
            f"Please review and reverse the {facts['amount_disputed']} within the dispute period "
            f"(by {facts['dispute_by']}).\n\nThank you,\n{facts['vendor']} ({facts['vendor_code']})")


def claude_draft(client, facts: dict) -> tuple[str, dict]:
    """One letter from Claude. Returns (text, usage). Raises on refusal so it is never used."""
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": "Facts:\n" + json.dumps(facts, indent=2)}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"refused: {getattr(response.stop_details, 'category', None)}")
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    usage = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens,
             "model": response.model}
    return text, usage


def mutations(draft: str, facts: dict, rng: np.random.Generator) -> list[tuple[str, str]]:
    """The draft with one plausible error injected at a time: what a model could get wrong."""
    out = []
    amounts = _MONEY.findall(draft)
    if amounts:
        a = amounts[0]
        wrong = money(_cents(a) + int(rng.integers(1, 5000)), "USD").replace("$", re.match(r"\D+", a).group(0), 1)
        out.append(("changed amount", draft.replace(a, wrong, 1)))
    dates = _DATE.findall(draft)
    if dates:
        d = pd.Timestamp(dates[0]) + pd.Timedelta(days=int(rng.integers(1, 30)))
        out.append(("shifted date", draft.replace(dates[0], d.strftime("%Y-%m-%d"), 1)))
    ids = [t for t in _ID.findall(_MONEY.sub(" ", _DATE.sub(" ", draft))) if _id_like(t)
           and t != facts["reference"]]
    if ids:
        t = ids[0]
        fake = t[:-2] + ("ZZ" if not t.endswith("ZZ") else "YY")
        out.append(("invented document number", draft.replace(t, fake, 1)))
    out.append(("invented supporting document",
                draft + f"\nSee also proof of delivery POD-{''.join(rng.choice(list('ABCDEFGH123456789'), 9))}."))
    out.append(("dropped reference", draft.replace(facts["reference"], "the deduction")))
    return out


def run(findings: pd.DataFrame, vendor_names: dict[str, str], n: int = 200, seed: int = 7,
        use_model: bool | None = None) -> tuple[pd.DataFrame, dict]:
    """Draft and verify letters for a stratified sample of claims, then try to fool the verifier.

    use_model=None: use Claude when credentials are available, else the template.
    """
    rng = np.random.default_rng(seed)
    claims = findings[findings["status"] == "CLAIMABLE"]
    per = max(1, n // max(1, claims["category"].nunique()))
    sample = claims.sample(frac=1, random_state=seed).groupby("category").head(per).head(n)

    client, source = None, "template"
    if use_model is not False:
        try:
            import anthropic
            client = anthropic.Anthropic()
            client.models.retrieve(MODEL)  # cheap credential check
            source = "claude"
        except Exception as exc:  # no credentials or no network: stay offline
            if use_model:
                raise RuntimeError(f"Claude unavailable: {exc}") from exc
            client = None

    rows, tokens_in, tokens_out = [], 0, 0
    for _, r in sample.iterrows():
        facts = facts_for(r, vendor_names.get(r["vendor_code"], r["vendor_code"]))
        try:
            if client is not None:
                draft, usage = claude_draft(client, facts)
                tokens_in, tokens_out = tokens_in + usage["input_tokens"], tokens_out + usage["output_tokens"]
            else:
                draft = template_draft(facts)
            problems = verify(draft, facts)
        except Exception as exc:  # refusal or API error: the letter is not used
            draft, problems = "", [f"not drafted: {exc}"]
        muts = mutations(draft, facts, rng) if draft else []
        caught = [kind for kind, text in muts if verify(text, facts)]
        rows.append({"vendor_code": r["vendor_code"], "ref": r["ref"], "category": r["category"],
                     "draft": draft, "passed": not problems, "violations": "; ".join(problems),
                     "mutations": len(muts), "mutations_caught": len(caught),
                     "missed_mutations": "; ".join(k for k, _ in muts if k not in caught)})
    df = pd.DataFrame(rows)
    summary = {
        "source": source, "model": MODEL if source == "claude" else None,
        "drafted": int((df["draft"] != "").sum()), "passed": int(df["passed"].sum()),
        "rejected": int((~df["passed"]).sum()),
        "violations": df.loc[~df["passed"], "violations"].str.split("; ").explode()
        .str.replace(r":.*", "", regex=True).value_counts().to_dict(),
        "mutations": int(df["mutations"].sum()), "mutations_caught": int(df["mutations_caught"].sum()),
        "input_tokens": tokens_in, "output_tokens": tokens_out,
        # Claude Opus 5.5 list price: $4 / $20 per million input / output tokens
        "cost_usd": round(tokens_in * 4 / 1e6 + tokens_out * 20 / 1e6, 4),
    }
    return df, summary
