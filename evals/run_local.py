#!/usr/bin/env python3
"""Run pulsewire local evals.

Usage:
  uv run python evals/run_local.py
  uv run python evals/run_local.py --suite safety
  uv run python evals/run_local.py --archive web/archive/daily/2026-06-15.json
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

from graders import (
    case_to_dict,
    grade_latest_archive,
    grade_reference_topics,
    grade_source_profile,
    grade_summary_quality,
    grade_thread_archive,
    grade_verify_item,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "evals" / "cases.jsonl"
DEFAULT_RESULT = ROOT / "evals" / "results" / "latest.json"


def _load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            cases.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{lineno}: invalid JSONL: {exc}") from exc
    return cases


def _run_case(case: dict[str, Any], *, archive_path: Path | None, as_of: date):
    kind = case.get("kind")
    if kind == "source_profile":
        return grade_source_profile(case)
    if kind == "verify_item":
        return grade_verify_item(case)
    if kind == "latest_archive":
        return grade_latest_archive(case, archive_path=archive_path, as_of=as_of)
    if kind == "summary_quality":
        return grade_summary_quality(case, archive_path=archive_path)
    if kind == "thread_archive":
        return grade_thread_archive(case, archive_path=archive_path)
    if kind == "reference_topics":
        return grade_reference_topics(case, archive_path=archive_path)
    raise SystemExit(f"Unknown eval case kind: {kind!r} in {case.get('id')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run pulsewire local evals")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES, help="JSONL eval case file")
    parser.add_argument(
        "--suite",
        action="append",
        help="suite to run; may repeat. Defaults to all suites in cases.jsonl",
    )
    parser.add_argument("--archive", type=Path, help="daily archive JSON to grade; defaults to newest")
    parser.add_argument("--as-of", default=date.today().isoformat(), help="YYYY-MM-DD date for freshness checks")
    parser.add_argument("--result", type=Path, default=DEFAULT_RESULT, help="where to write JSON report")
    parser.add_argument("--no-write", action="store_true", help="print only; do not write result JSON")
    args = parser.parse_args(argv)

    cases = _load_cases(args.cases)
    suites = set(args.suite or [])
    if suites:
        cases = [c for c in cases if c.get("suite") in suites]
    if not cases:
        raise SystemExit("No eval cases selected")

    as_of = date.fromisoformat(args.as_of)
    archive_path = args.archive.resolve() if args.archive else None
    results = [_run_case(case, archive_path=archive_path, as_of=as_of) for case in cases]
    total_score = round(sum(r.score for r in results), 2)
    total_max = round(sum(r.max_score for r in results), 2)
    ungraded = [r for r in results if r.ungraded]
    failed = [r for r in results if not r.passed and not r.ungraded]  # ungraded 既不算过也不算败
    report = {
        "status": "fail" if failed else "pass",
        "score": total_score,
        "max_score": total_max,
        "ungraded": [r.id for r in ungraded],  # 如实声明未判维度,'通过'不许覆盖(护栏4)
        "as_of": as_of.isoformat(),
        "archive": str(archive_path) if archive_path else None,
        "cases": [case_to_dict(r) for r in results],
    }

    ug = f"  ungraded={len(ungraded)}" if ungraded else ""
    print(f"pulsewire eval: {report['status']}  score={total_score}/{total_max}{ug}")
    for r in results:
        icon = "UNGRADED" if r.ungraded else ("PASS" if r.passed else "FAIL")
        print(f"{icon} {r.suite}/{r.id}: {r.score}/{r.max_score}")
        for check in r.checks:
            if check.severity == "info":
                continue
            if not check.passed or check.severity in {"warn", "skip"}:
                marker = check.severity.upper()
                print(f"  - {marker} {check.name}: {check.message}")

    if not args.no_write:
        args.result.parent.mkdir(parents=True, exist_ok=True)
        args.result.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.result}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
