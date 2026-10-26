"""Command line: vre fetch | seeds | generate | run | evaluate."""
from __future__ import annotations

import json
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pandas as pd
import typer

from vre.policy import Category, Policy, Status

if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Reconcile Amazon Vendor Central deductions and surface recoverable money.")


@contextmanager
def step(label: str):
    typer.echo(f"  {label:<44}", nl=False)
    t0 = time.perf_counter()
    yield
    typer.secho(f"{time.perf_counter() - t0:6.1f} s", fg=typer.colors.BRIGHT_BLACK)


def _usd(v: float) -> str:
    return f"${v:,.2f}"


@app.command()
def fetch() -> None:
    """Download the real public datasets (UCI Online Retail II, ECB FX rates)."""
    from vre.sources import ECB, UCI
    from vre.sources import fetch as do_fetch

    for src in (UCI, ECB):
        with step(f"{src.name}"):
            do_fetch(src)


@app.command()
def seeds() -> None:
    """Distill the raw datasets into the small seeds the generator uses (data/seeds/)."""
    from vre.sources import build_seeds

    with step("Building seeds from UCI + ECB"):
        counts = build_seeds()
    typer.echo(json.dumps(counts))


@app.command()
def generate(out: Path = typer.Argument(Path("data/world")), vendors: int = 170, po_lines: int = 1_200_000,
             seed: int = 7) -> None:
    """Generate the synthetic vendor world (observed exports + ground truth)."""
    from vre.generate import WorldConfig
    from vre.generate import generate as gen

    with step(f"Generating {vendors} vendors, {po_lines:,} PO lines"):
        meta = gen(out, WorldConfig(vendors=vendors, po_lines=po_lines, seed=seed))
    typer.echo(f"  {meta['observed_rows_total']:,} rows across {sum(1 for _ in (out / 'observed').iterdir())} "
               f"vendor folders · {meta['truth_rows']:,} ground-truth findings")


@app.command()
def run(observed: Path = typer.Argument(Path("data/world/observed"), help="Folder with one sub-folder per vendor"),
        out: Path = typer.Option(Path("out"), help="Where reports are written"),
        as_of: str = typer.Option(Policy().as_of, help="Analysis date (YYYY-MM-DD)"),
        pdf_top: int = typer.Option(10, help="Write PDF reports for the N vendors with most to recover (0 = all)"),
        ) -> None:
    """Ingest, reconcile and write the reports."""
    from vre.ingest import load_vendor_dir
    from vre.reconcile import reconcile
    from vre.reports import write_all

    t0 = time.perf_counter()
    policy = Policy(as_of=as_of)
    typer.secho(f"\nvendor-recovery-engine · as of {as_of}", bold=True)
    with step("Ingesting and validating exports"):
        loaded = load_vendor_dir(observed)
    stats = pd.DataFrame(loaded.stats)
    rows = int(stats["rows_read"].sum())
    with step(f"Reconciling {rows:,} rows"):
        result = reconcile(loaded, policy)
    f = result.findings
    vendors_csv = observed.parent / "vendors.csv"
    vendors = pd.read_csv(vendors_csv) if vendors_csv.exists() else pd.DataFrame(columns=["vendor_code", "vendor_name"])
    by_vendor = f.groupby("vendor_code")["recoverable_usd_cents"].sum().sort_values(ascending=False)
    pdf_vendors = None if pdf_top == 0 else list(by_vendor.index[:pdf_top])
    with step("Writing Excel, PDF and dispute files"):
        summary = write_all(f, vendors, stats, out, as_of, pdf_vendors)
        loaded.quarantine_frame().to_csv(out / "quarantine.csv", index=False)
        (out / "quality.json").write_text(json.dumps({
            **result.quality, "rows_read": rows, "rows_quarantined": int(stats["rows_quarantined"].sum()),
            "duplicates_removed": int(stats["duplicates_removed"].sum())}, indent=2), encoding="utf-8")

    typer.echo("")
    typer.secho(f"  {'Category':<28}{'Claims':>8}{'Recoverable':>16}{'Expired':>14}", bold=True)
    for c in Category:
        g = f[f["category"] == c]
        if len(g):
            typer.echo(f"  {c.value:<28}{int((g['status'] == Status.CLAIMABLE).sum()):>8,}"
                       f"{_usd(g['recoverable_usd_cents'].sum() / 100):>16}"
                       f"{_usd(g['expired_usd_cents'].sum() / 100):>14}")
    typer.secho(f"  {'TOTAL':<28}{summary['claims']:>8,}{_usd(summary['recoverable_usd']):>16}"
                f"{_usd(summary['expired_usd']):>14}", bold=True, fg=typer.colors.GREEN)
    st = f["status"].value_counts()
    typer.echo(f"\n  Not claimed: {st.get(Status.NOT_CLAIMABLE, 0):,} valid · "
               f"{st.get(Status.INSUFFICIENT_EVIDENCE, 0):,} insufficient evidence · "
               f"{st.get(Status.NEEDS_REVIEW, 0):,} ambiguous (review) · "
               f"{st.get(Status.ALREADY_RECOVERED, 0):,} already reversed")
    typer.echo(f"  Quarantined {int(stats['rows_quarantined'].sum()):,} rows · "
               f"{int(stats['duplicates_removed'].sum()):,} duplicate export rows merged")
    typer.secho(f"  Reports in {out}/ · {time.perf_counter() - t0:.1f} s total\n", fg=typer.colors.BRIGHT_BLACK)


@app.command()
def evaluate(findings: Path = typer.Argument(Path("out/findings.parquet")),
             truth: Path = typer.Argument(Path("data/world/truth/truth.parquet")),
             out: Path = typer.Option(None, help="Write the scorecard as JSON here")) -> None:
    """Score findings against the generator's ground truth."""
    from vre.evaluate import evaluate as score

    ev = score(pd.read_parquet(findings), pd.read_parquet(truth))
    typer.secho("\nScorecard against ground truth", bold=True)
    ev["verdict_mismatches"] = len(ev["mismatches"])  # exact count: a percentage can round 5 errors to 100.00 %
    for k in ("rows_truth", "missing_findings", "unexpected_findings", "verdict_mismatches"):
        typer.echo(f"  {k:<26}{ev[k]:>12,}")
    for k in ("status_accuracy", "claimable_precision", "claimable_recall", "amount_exact_rate"):
        typer.echo(f"  {k:<26}{ev[k]:>12.2%}")
    for k in ("recoverable_true_usd", "recovered_usd", "false_claim_usd"):
        typer.echo(f"  {k:<26}{_usd(ev[k]):>16}")
    typer.echo(f"  ambiguous routed to review{ev['review_routed']:>8,} of {ev['review_true']:,}\n")
    typer.echo(ev["by_category"][["category", "claimable_true", "precision", "recall", "recovered_usd",
                                   "false_claim_usd"]].to_string(index=False))
    if out:
        scal = {k: v for k, v in ev.items() if not isinstance(v, pd.DataFrame)}
        scal["by_category"] = ev["by_category"].to_dict("records")
        out.write_text(json.dumps(scal, indent=2, default=float), encoding="utf-8")


if __name__ == "__main__":
    app()
