# Evaluation log

Every change to the engine is scored against the generator's ground truth. This log keeps
the runs where the evaluation caught a real defect, with the numbers before and after.

| # | World | What was added to the world | Before the fix | Defect found | Fix | After |
|---|---|---|---|---|---|---|
| 1 | 30 vendors, 80k PO lines, seed 7 | Remittance lines that quote the PO number instead of the invoice number (2 %) | Claimable precision **94.95 %**; **154 false "unpaid invoice" claims worth $88,756.79**; 5 FX/underpayment problems hidden behind them | A payment quoting the PO was never linked to its invoice, so a paid invoice looked unpaid | Match unresolved remittance references against PO numbers, accepted only when the PO has exactly one invoice | Precision 100 %, recall 100 %, $0 false claims |
| 2 | 80 vendors, 200k PO lines, seed 11 | Euro vendors exporting `1.234,50` amounts and `31.12.2025` dates | Recall **98.01 %**: two vendors fully quarantined, 336 findings missing, **$2,669.09 not found**. Worse, `1.234,50` parsed silently as `1.2345` | Date format not recognised; decimal comma read as a thousands separator with no error | Detect the decimal separator per column on the whole file; add `DD.MM.YYYY` | Recall 100 %, all rows ingested |
| 3 | **Holdout**: 170 vendors, 1.2M PO lines, seed 2026 (never used during development) | Nothing new: first run of the finished engine | Claimable precision and recall 100 %, but **5 verdicts wrong** out of 112,330 (the CLI printed "100.00 %" because of rounding) | Fuzzy PO matching compared canonical forms, and canonicalization drops leading zeros of a trailing number: `MJPYNQ05` became `MJPYNQ5`, so the one-typo reference `MJPYNQ0V` sat two edits away and 5 chargebacks were marked *insufficient evidence* instead of being judged | Fuzzy tier compares raw characters (case and separators aside); the scorecard now prints the exact mismatch count, not just a rounded rate | 112,330 / 112,330 verdicts match |

A 100 % score on the generated benchmark means the rules are implemented consistently on
messy inputs; it does not mean real data would score the same. Real exports will bring
quirks the generator does not have yet; each one becomes a new row here.
