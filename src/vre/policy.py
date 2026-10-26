"""Recovery policy: the business rules a reviewer would apply, in one place.

The generator uses these rules to label its ground truth and the engine uses them to
decide. They are rules about *what counts as recoverable*, not about how to find it:
the engine still has to rebuild every fact from messy files.

Dispute windows and tolerances are configurable assumptions, not Amazon policy;
check the current Vendor Central terms before relying on them.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Category(StrEnum):
    SHORTAGE = "SHORTAGE"
    PRICE_CLAIM = "PRICE_CLAIM"
    CHARGEBACK_LATE_SHIPMENT = "CHARGEBACK_LATE_SHIPMENT"
    CHARGEBACK_ASN = "CHARGEBACK_ASN"
    DUPLICATE_DEDUCTION = "DUPLICATE_DEDUCTION"
    UNDERPAYMENT = "UNDERPAYMENT"
    UNPAID_INVOICE = "UNPAID_INVOICE"
    FX_VARIANCE = "FX_VARIANCE"


class Status(StrEnum):
    CLAIMABLE = "CLAIMABLE"                      # evidence supports a dispute
    NOT_CLAIMABLE = "NOT_CLAIMABLE"              # the deduction is valid
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"  # cannot prove either way
    NEEDS_REVIEW = "NEEDS_REVIEW"                # reference matches more than one record
    ALREADY_RECOVERED = "ALREADY_RECOVERED"      # Amazon reversed it in full
    EXPIRED = "EXPIRED"                          # would be claimable, but past the dispute window


# Deduction types as they appear in the deductions report, and chargeback reason codes
DEDUCTION_SHORTAGE = "SHORTAGE"
DEDUCTION_PRICE = "PRICE_CLAIM"
DEDUCTION_CHARGEBACK = "CHARGEBACK"
DEDUCTION_REVERSAL = "REVERSAL"
REASON_LATE_SHIPMENT = "LATE_SHIPMENT"
REASON_ASN_LATE = "ASN_LATE"


@dataclass(frozen=True)
class Policy:
    as_of: str = "2026-07-31"
    deduction_window_days: int = 365   # from deduction date
    payment_window_days: int = 365     # from invoice due date
    payment_terms_days: int = 60
    payment_grace_days: int = 30       # unpaid only after due date + grace
    fx_tolerance: float = 0.005        # bank spread allowed on the applied rate
    payment_tolerance_cents: int = 100  # ignore rounding differences up to $1.00
    duplicate_window_days: int = 120   # same deduction repeated within this window
    min_suffix_digits: int = 4         # shortest truncated reference worth matching
    urgent_days: int = 30              # claims whose window closes this soon go to the top of the queue


# Assumed chance of winning a dispute, by category and by how the reference was matched.
# These are priors to rank the work queue, not measured rates: replace them with the
# vendor's own dispute history as soon as there is one.
WIN_RATE = {
    "SHORTAGE": 0.85, "PRICE_CLAIM": 0.80, "CHARGEBACK_LATE_SHIPMENT": 0.75, "CHARGEBACK_ASN": 0.70,
    "DUPLICATE_DEDUCTION": 0.95, "UNDERPAYMENT": 0.90, "UNPAID_INVOICE": 0.95, "FX_VARIANCE": 0.65,
}
MATCH_CONFIDENCE = {"suffix": 0.85, "fuzzy": 0.80}  # weaker links between claim and record


DEFAULT_POLICY = Policy()
