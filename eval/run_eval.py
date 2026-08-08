"""Golden-set evaluation (policy.md §8–§9, §12 phases 2–4).

Reports:
  - retrieval recall@8  (must_cite_doc found in top retrieved sources)
  - per-class precision / recall on verdicts
  - precision on `supported` (the optimisation target)

Usage:
  # Phase 2 — retrieval only (no Claude verify; still uses Claude for query gen + route/claims)
  python -m eval.run_eval --retrieval-only

  # Phases 3–4 — full verify path
  python -m eval.run_eval

  # Subset while iterating
  python -m eval.run_eval --limit 5 --ids T01,F01,N01

Privacy: golden.csv is consented/scripted eval material only (policy.md §11).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.pipeline import process_message  # noqa: E402
from pipeline.retrieve import retrieve_for_claim  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "golden.csv"
VERDICTS = ("supported", "refuted", "insufficient")


@dataclass
class Row:
    id: str
    transcript_en: str
    audio_path: str
    claims_expected: list[str]
    verdict_expected: str
    must_cite_doc: str
    provenance: str


def load_golden(path: Path = GOLDEN) -> list[Row]:
    rows: list[Row] = []
    with path.open(encoding="utf-8") as f:
        for raw in csv.DictReader(f):
            claims_raw = (raw.get("claims_expected") or "").strip()
            try:
                claims = json.loads(claims_raw) if claims_raw else []
            except json.JSONDecodeError:
                claims = [claims_raw] if claims_raw else []
            rows.append(
                Row(
                    id=raw["id"],
                    transcript_en=raw["transcript_en"],
                    audio_path=raw.get("audio_path") or "",
                    claims_expected=claims,
                    verdict_expected=(raw.get("verdict_expected") or "insufficient").strip(),
                    must_cite_doc=(raw.get("must_cite_doc") or "").strip(),
                    provenance=raw.get("provenance") or "",
                )
            )
    return rows


def _doc_hit(must_cite: str, sources: list[dict]) -> bool:
    if not must_cite:
        return True  # nothing required
    needle = must_cite.lower()
    for s in sources:
        blob = " ".join(
            [
                str(s.get("source_url") or ""),
                str(s.get("title") or ""),
                str(s.get("source_name") or ""),
            ]
        ).lower()
        if needle in blob:
            return True
    return False


def precision_recall(y_true: list[str], y_pred: list[str]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for label in VERDICTS:
        tp = sum(1 for t, p in zip(y_true, y_pred) if t == label and p == label)
        fp = sum(1 for t, p in zip(y_true, y_pred) if t != label and p == label)
        fn = sum(1 for t, p in zip(y_true, y_pred) if t == label and p != label)
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        out[label] = {"precision": round(prec, 4), "recall": round(rec, 4), "tp": tp, "fp": fp, "fn": fn}
    return out


def confusion(y_true: list[str], y_pred: list[str]) -> dict[str, Counter]:
    matrix: dict[str, Counter] = {t: Counter() for t in VERDICTS}
    for t, p in zip(y_true, y_pred):
        t = t if t in VERDICTS else "insufficient"
        p = p if p in VERDICTS else "insufficient"
        matrix[t][p] += 1
    return matrix


def evaluate(
    rows: list[Row],
    *,
    retrieval_only: bool = False,
) -> dict:
    y_true: list[str] = []
    y_pred: list[str] = []
    retrieval_hits = 0
    retrieval_needed = 0
    details = []

    for row in rows:
        expected = row.verdict_expected if row.verdict_expected in VERDICTS else "insufficient"

        if retrieval_only:
            # Retrieve against expected claim text, or the transcript if no claims listed.
            query_text = row.claims_expected[0] if row.claims_expected else row.transcript_en
            sources: list[dict] = []
            hit = True
            if query_text.strip() and not (expected == "insufficient" and not row.claims_expected):
                rr = retrieve_for_claim(query_text)
                sources = [
                    {
                        "source_url": s.source_url,
                        "title": s.title,
                        "source_name": s.source_name,
                    }
                    for s in rr.sources
                ]
                hit = _doc_hit(row.must_cite_doc, sources) if row.must_cite_doc else True
            if row.must_cite_doc:
                retrieval_needed += 1
                if hit:
                    retrieval_hits += 1
            details.append(
                {
                    "id": row.id,
                    "retrieval_hit": hit if row.must_cite_doc else None,
                    "n_sources": len(sources),
                    "mode": "retrieval_only",
                }
            )
            continue

        # Full pipeline (stages 2–9; compose on but unused for metrics).
        result = process_message(
            row.transcript_en,
            text_en=row.transcript_en,
            source_language="en",
            with_compose=True,
        )
        d = result.to_dict()
        claims = d.get("claims") or []

        if not claims:
            pred = "insufficient"
            sources = []
            hit = True if not row.must_cite_doc else False
        else:
            # Primary claim = first extracted claim (policy.md: evaluate per claim).
            primary = claims[0]
            pred = primary.get("verdict") or "insufficient"
            sources = primary.get("sources") or []
            hit = _doc_hit(row.must_cite_doc, sources) if row.must_cite_doc else True

        if row.must_cite_doc:
            retrieval_needed += 1
            if hit:
                retrieval_hits += 1

        y_true.append(expected)
        y_pred.append(pred if pred in VERDICTS else "insufficient")
        details.append(
            {
                "id": row.id,
                "expected": expected,
                "predicted": pred,
                "retrieval_hit": hit if row.must_cite_doc else None,
                "n_claims": len(claims),
                "gates": (claims[0].get("gates_triggered") if claims else None),
            }
        )

    report: dict = {
        "n": len(rows),
        "retrieval_recall_at_8": (
            round(retrieval_hits / retrieval_needed, 4) if retrieval_needed else None
        ),
        "retrieval_hits": retrieval_hits,
        "retrieval_needed": retrieval_needed,
        "details": details,
    }

    if not retrieval_only and y_true:
        pr = precision_recall(y_true, y_pred)
        report["per_class"] = pr
        report["precision_supported"] = pr["supported"]["precision"]
        report["target_precision_supported"] = 0.98
        report["meets_precision_target"] = pr["supported"]["precision"] >= 0.98
        conf = confusion(y_true, y_pred)
        report["confusion"] = {k: dict(v) for k, v in conf.items()}
        report["pred_counts"] = dict(Counter(y_pred))
        report["true_counts"] = dict(Counter(y_true))

    return report


def _print_report(report: dict) -> None:
    print(f"cases: {report['n']}")
    rr = report.get("retrieval_recall_at_8")
    if rr is not None:
        print(
            f"retrieval recall@8: {rr} "
            f"({report['retrieval_hits']}/{report['retrieval_needed']})"
        )
    if "per_class" in report:
        print("\nper-class precision / recall:")
        for label, m in report["per_class"].items():
            print(
                f"  {label:12}  P={m['precision']:.3f}  R={m['recall']:.3f}  "
                f"(tp={m['tp']} fp={m['fp']} fn={m['fn']})"
            )
        print(
            f"\nprecision on supported: {report['precision_supported']:.3f} "
            f"(target ≥ {report['target_precision_supported']}) "
            f"{'PASS' if report['meets_precision_target'] else 'FAIL'}"
        )
        print("\nconfusion (rows=true, cols=pred):")
        header = "true\\pred".ljust(14) + "".join(v.rjust(14) for v in VERDICTS)
        print(header)
        for t in VERDICTS:
            row = t.ljust(14)
            for p in VERDICTS:
                row += str(report["confusion"].get(t, {}).get(p, 0)).rjust(14)
            print(row)


def main() -> int:
    p = argparse.ArgumentParser(description="Run ECHO golden-set evaluation.")
    p.add_argument("--golden", type=Path, default=GOLDEN)
    p.add_argument("--retrieval-only", action="store_true", help="Phase 2: measure recall@8 only")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--ids", type=str, default="", help="Comma-separated case ids")
    p.add_argument("--json-out", type=Path, default=None)
    args = p.parse_args()

    rows = load_golden(args.golden)
    if args.ids:
        keep = {x.strip() for x in args.ids.split(",") if x.strip()}
        rows = [r for r in rows if r.id in keep]
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]

    if not rows:
        print("No golden rows selected.", file=sys.stderr)
        return 1

    report = evaluate(rows, retrieval_only=args.retrieval_only)
    _print_report(report)

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
