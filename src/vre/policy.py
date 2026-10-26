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


DEFAULT_POLICY = Policy()
