"""Local eval graders for pulsewire daily intelligence quality.

These graders intentionally avoid network and LLM calls. They are the first
layer of the eval stack: deterministic checks for source breadth, safety,
summary usefulness proxies, archive delivery integrity, and tracking quality.
"""

from __future__ import annotations

import difflib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


@dataclass(slots=True)
class Check:
    name: str
    passed: bool
    message: str
    severity: str = "fail"  # fail | warn | info | skip
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CaseResult:
    id: str
    suite: str
    passed: bool
    score: float
    max_score: float
    checks: list[Check] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    ungraded: bool = False  # 第三态:没真值没法判 → 不算过也不算败,如实标出(护栏4)

    @property
    def failed_checks(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and c.severity == "fail"]


def _check(
    checks: list[Check],
    name: str,
    passed: bool,
    message: str,
    *,
    severity: str = "fail",
    details: dict[str, Any] | None = None,
) -> None:
    checks.append(Check(name=name, passed=passed, message=message, severity=severity, details=details or {}))


def _score(checks: list[Check], weights: dict[str, float] | None = None) -> tuple[float, float, bool]:
    weights = weights or {}
    max_score = 0.0
    score = 0.0
    hard_passed = True
    for c in checks:
        if c.severity in {"info", "skip"}:
            continue
        w = float(weights.get(c.name, 1.0))
        max_score += w
        if c.passed or c.severity == "warn":
            score += w if c.passed else max(0.0, w * 0.5)
        if not c.passed and c.severity == "fail":
            hard_passed = False
    return round(score, 2), round(max_score, 2), hard_passed


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _latest_archive_path(archive_dir: Path) -> Path | None:
    paths = sorted(p for p in archive_dir.glob("*.json") if p.name != "latest.json")
    if not paths:
        return None
    return paths[-1]


def _archive_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(payload.get("domains"), list):
        for domain in payload["domains"]:
            label = domain.get("key") or domain.get("label") or "unknown"
            for item in domain.get("items") or []:
                row = dict(item)
                row["_domain"] = label
                items.append(row)
    # New daily archives keep the primary domain both at root `items` and inside
    # `domains`; only use root items for older/smaller payloads that lack domains.
    if payload.get("items") and not items:
        for item in payload.get("items") or []:
            row = dict(item)
            row.setdefault("_domain", "primary")
            items.append(row)
    if payload.get("github"):
        for item in payload.get("github") or []:
            row = dict(item)
            row.setdefault("_domain", "github")
            items.append(row)
    return items


def _text_len(text: str) -> int:
    return len((text or "").strip())


def _normalized_text(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "", text)
    return text


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _normalized_text(a), _normalized_text(b)).ratio()


def _domains(payload: dict[str, Any]) -> list[dict[str, Any]]:
    domains = payload.get("domains")
    if isinstance(domains, list):
        return domains
    report = payload.get("report") or {}
    categories = report.get("categories")
    if isinstance(categories, list):
        return [{"key": c.get("name"), "label": c.get("name"), "items": c.get("items") or []} for c in categories]
    return []


def grade_source_profile(case: dict[str, Any]) -> CaseResult:
    """Grade sources.yaml breadth and authority configuration."""
    import sys

    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from pulsewire.config import load_sources

    thresholds = case.get("thresholds") or {}
    sources = load_sources()
    enabled = [s for s in sources if s.enabled]
    by_domain = Counter(s.domain for s in enabled)
    by_category = Counter(s.category for s in enabled)
    enabled_urls = [s.url for s in enabled]
    dup_urls = sorted(url for url, count in Counter(enabled_urls).items() if count > 1)
    high_weight = sum(1 for s in enabled if s.weight >= float(thresholds.get("authority_weight_floor", 0.8)))
    whitelisted = sum(1 for s in enabled if s.whitelisted)

    checks: list[Check] = []
    _check(
        checks,
        "enabled_sources",
        len(enabled) >= int(thresholds.get("min_enabled_sources", 0)),
        f"enabled sources: {len(enabled)}",
        details={"actual": len(enabled)},
    )
    for domain, minimum in (thresholds.get("min_domain_sources") or {}).items():
        _check(
            checks,
            f"domain_{domain}_sources",
            by_domain.get(domain, 0) >= int(minimum),
            f"{domain} enabled sources: {by_domain.get(domain, 0)}",
            details={"actual": by_domain.get(domain, 0), "minimum": minimum},
        )
    _check(
        checks,
        "category_breadth",
        len(by_category) >= int(thresholds.get("min_categories", 0)),
        f"enabled category count: {len(by_category)}",
        details={"actual": len(by_category), "categories": dict(by_category)},
    )
    _check(
        checks,
        "authority_sources",
        high_weight >= int(thresholds.get("min_high_authority_sources", 0)),
        f"high-weight sources: {high_weight}",
        details={"actual": high_weight},
    )
    _check(
        checks,
        "whitelist_sources",
        whitelisted >= int(thresholds.get("min_whitelisted_sources", 0)),
        f"whitelisted sources: {whitelisted}",
        details={"actual": whitelisted},
    )
    _check(
        checks,
        "duplicate_enabled_urls",
        not dup_urls,
        f"duplicate enabled feed URLs: {len(dup_urls)}",
        severity="warn",
        details={"urls": dup_urls[:20]},
    )

    score, max_score, passed = _score(checks)
    return CaseResult(
        id=case["id"],
        suite=case["suite"],
        passed=passed,
        score=score,
        max_score=max_score,
        checks=checks,
        metrics={
            "enabled_sources": len(enabled),
            "by_domain": dict(by_domain),
            "by_category": dict(by_category),
            "high_weight_sources": high_weight,
            "whitelisted_sources": whitelisted,
            "duplicate_enabled_urls": dup_urls,
        },
    )


def grade_verify_item(case: dict[str, Any]) -> CaseResult:
    """Grade deterministic safety behavior through the real verify_item path."""
    import sys

    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from pulsewire.summarize.schema import FactToken, ItemSummary
    from pulsewire.verify.engine import verify_item

    inp = case["input"]
    expected = case.get("expect") or {}
    item = ItemSummary(**inp["item"])
    facts = {
        f["token"]: FactToken(
            token=f["token"],
            item_id=f.get("item_id", item.item_id),
            source_id=f["source_id"],
            label=f.get("label", ""),
            value=f.get("value"),
            unit=f.get("unit", ""),
        )
        for f in inp.get("facts", [])
    }
    result = verify_item(
        item,
        facts,
        source_text=inp.get("source_text", ""),
        corroboration=int(inp.get("corroboration", 1)),
        risk_min_sources=int(inp.get("risk_min_sources", 2)),
    )
    rendered = {
        "headline": result.headline,
        "tldr": result.tldr,
        "insight": result.insight,
    }
    checks: list[Check] = []
    if "status" in expected:
        _check(
            checks,
            "status",
            result.status == expected["status"],
            f"status={result.status}, expected={expected['status']}",
            details={"actual": result.status, "expected": expected["status"]},
        )
    for field_name, needle in (expected.get("contains") or {}).items():
        _check(
            checks,
            f"{field_name}_contains",
            needle in rendered.get(field_name, ""),
            f"{field_name} should contain {needle!r}",
            details={"field": field_name, "needle": needle, "actual": rendered.get(field_name, "")},
        )
    for number in expected.get("suspect_numbers", []):
        _check(
            checks,
            f"suspect_number_{number}",
            number in result.suspect_numbers,
            f"suspect number {number!r}",
            details={"actual": result.suspect_numbers},
        )
    for token in expected.get("unresolved_tokens", []):
        _check(
            checks,
            f"unresolved_{token}",
            token in result.unresolved_tokens,
            f"unresolved token {token!r}",
            details={"actual": result.unresolved_tokens},
        )
    for source_id in expected.get("used_source_ids", []):
        _check(
            checks,
            f"used_source_{source_id}",
            source_id in result.used_source_ids,
            f"used source id {source_id!r}",
            details={"actual": result.used_source_ids},
        )
    for category in expected.get("risky_categories", []):
        matched = any(str(claim).startswith(f"{category}:") for claim in result.risky_claims)
        _check(
            checks,
            f"risky_{category}",
            matched,
            f"risky category {category!r}",
            details={"actual": result.risky_claims},
        )

    score, max_score, passed = _score(checks)
    return CaseResult(
        id=case["id"],
        suite=case["suite"],
        passed=passed,
        score=score,
        max_score=max_score,
        checks=checks,
        metrics={
            "status": result.status,
            "rendered": rendered,
            "used_source_ids": result.used_source_ids,
            "unresolved_tokens": result.unresolved_tokens,
            "suspect_numbers": result.suspect_numbers,
            "risky_claims": result.risky_claims,
        },
    )


def grade_latest_archive(case: dict[str, Any], *, archive_path: Path | None, as_of: date) -> CaseResult:
    """Grade the latest delivered daily archive as a product artifact."""
    thresholds = case.get("thresholds") or {}
    archive_dir = ROOT / "web" / "archive" / "daily"
    target = archive_path or _latest_archive_path(archive_dir)
    checks: list[Check] = []
    if target is None or not target.exists():
        _check(checks, "archive_exists", False, "no daily archive json found")
        score, max_score, passed = _score(checks)
        return CaseResult(case["id"], case["suite"], passed, score, max_score, checks)

    payload = _read_json(target)
    archive_date = date.fromisoformat(payload.get("date") or target.stem)
    age_days = (as_of - archive_date).days
    domains = _domains(payload)
    items = _archive_items(payload)
    primary_key = thresholds.get("primary_domain_key", "ai")
    primary = next((d for d in domains if d.get("key") == primary_key), domains[0] if domains else {})
    primary_items = primary.get("items") or []
    github_items = payload.get("github") or []
    threads = payload.get("threads") or []

    _check(
        checks,
        "archive_fresh",
        0 <= age_days <= int(thresholds.get("max_archive_age_days", 2)),
        f"latest archive date {archive_date.isoformat()} is {age_days} days from as_of",
        details={"archive": str(target), "date": archive_date.isoformat(), "as_of": as_of.isoformat()},
    )
    _check(
        checks,
        "primary_not_empty",
        len(primary_items) >= int(thresholds.get("min_primary_items", 1)),
        f"primary domain items: {len(primary_items)}",
        details={"actual": len(primary_items)},
    )
    _check(
        checks,
        "domain_count",
        len([d for d in domains if d.get("items")]) >= int(thresholds.get("min_nonempty_domains", 1)),
        f"non-empty domains: {len([d for d in domains if d.get('items')])}",
        details={"domains": [(d.get("key"), len(d.get("items") or [])) for d in domains]},
    )
    _check(
        checks,
        "github_board",
        len(github_items) >= int(thresholds.get("min_github_items", 0)),
        f"github board items: {len(github_items)}",
        details={"actual": len(github_items)},
    )
    _check(
        checks,
        "thread_presence",
        len(threads) >= int(thresholds.get("min_threads", 0)),
        f"visible tracked threads: {len(threads)}",
        details={"actual": len(threads)},
    )
    missing_fields = [
        item.get("id") or item.get("url") or item.get("headline") or item.get("title")
        for item in items
        if not (item.get("headline") or item.get("title_zh") or item.get("title"))
        or not item.get("insight")
        or not item.get("url")
    ]
    _check(
        checks,
        "item_integrity",
        not missing_fields,
        f"items missing headline/insight/url: {len(missing_fields)}",
        details={"examples": missing_fields[:10]},
    )

    score, max_score, passed = _score(checks)
    return CaseResult(
        id=case["id"],
        suite=case["suite"],
        passed=passed,
        score=score,
        max_score=max_score,
        checks=checks,
        metrics={
            "archive": str(target),
            "archive_date": archive_date.isoformat(),
            "age_days": age_days,
            "domains": [(d.get("key"), d.get("label"), len(d.get("items") or [])) for d in domains],
            "items": len(items),
            "github_items": len(github_items),
            "threads": len(threads),
        },
    )


def grade_summary_quality(case: dict[str, Any], *, archive_path: Path | None) -> CaseResult:
    """Grade summary readability and safety proxies in the delivered archive."""
    thresholds = case.get("thresholds") or {}
    archive_dir = ROOT / "web" / "archive" / "daily"
    target = archive_path or _latest_archive_path(archive_dir)
    checks: list[Check] = []
    if target is None or not target.exists():
        _check(checks, "archive_exists", False, "no daily archive json found")
        score, max_score, passed = _score(checks)
        return CaseResult(case["id"], case["suite"], passed, score, max_score, checks)

    payload = _read_json(target)
    items = _archive_items(payload)
    min_headline = int(thresholds.get("min_headline_chars", 8))
    min_tldr = int(thresholds.get("min_tldr_chars", 20))
    min_insight = int(thresholds.get("min_insight_chars", 260))
    max_short_rate = float(thresholds.get("max_short_item_rate", 0.05))
    min_explainer_rate = float(thresholds.get("min_explainer_phrase_rate", 0.55))
    duplicate_warn = float(thresholds.get("duplicate_similarity_warn", 0.50))
    duplicate_fail = float(thresholds.get("duplicate_similarity_fail", 0.72))

    short_items: list[dict[str, Any]] = []
    explainer_hits = 0
    unsafe_waiting: list[dict[str, Any]] = []
    risky_unmarked: list[dict[str, Any]] = []
    duplicate_pairs: list[dict[str, Any]] = []
    explainer_re = re.compile(r"为什么|意味着|影响|对.+来说|接下来|值得关注|不过|但|风险|争议|背景")
    risky_re = re.compile(r"IPO|上市|估值|翻[倍番]|倍增|治愈|根治|突破性疗法|伤亡|击毙|制裁|封锁|停火|反超|碾压")
    hedge_re = re.compile(r"据|称|宣称|报道|有消息|如果属实|待观察|待核实|目前|尚未|可能|或许")

    for item in items:
        headline = item.get("headline") or item.get("title_zh") or item.get("title") or ""
        tldr = item.get("tldr") or ""
        insight = item.get("insight") or ""
        body = f"{headline}\n{tldr}\n{insight}"
        if _text_len(headline) < min_headline or _text_len(tldr) < min_tldr or _text_len(insight) < min_insight:
            short_items.append({"headline": headline, "domain": item.get("_domain")})
        if explainer_re.search(insight):
            explainer_hits += 1
        if "[待核实]" in body and not item.get("needs_review", False):
            unsafe_waiting.append({"headline": headline, "domain": item.get("_domain")})
        if risky_re.search(body) and not hedge_re.search(body) and not item.get("needs_review", False):
            risky_unmarked.append({"headline": headline, "domain": item.get("_domain")})

    by_domain: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_domain.setdefault(str(item.get("_domain") or "unknown"), []).append(item)
    for domain, domain_items in by_domain.items():
        for i, a in enumerate(domain_items):
            for b in domain_items[i + 1:]:
                ta = f"{a.get('headline') or a.get('title_zh') or a.get('title') or ''} {a.get('tldr') or ''}"
                tb = f"{b.get('headline') or b.get('title_zh') or b.get('title') or ''} {b.get('tldr') or ''}"
                sim = _similarity(ta, tb)
                if sim >= duplicate_warn:
                    duplicate_pairs.append({
                        "domain": domain,
                        "similarity": round(sim, 3),
                        "a": a.get("headline") or a.get("title_zh") or a.get("title"),
                        "b": b.get("headline") or b.get("title_zh") or b.get("title"),
                    })

    short_rate = len(short_items) / len(items) if items else 1.0
    explainer_rate = explainer_hits / len(items) if items else 0.0
    hard_dupes = [p for p in duplicate_pairs if p["similarity"] >= duplicate_fail]
    warn_dupes = [p for p in duplicate_pairs if p["similarity"] < duplicate_fail]

    _check(
        checks,
        "summary_lengths",
        short_rate <= max_short_rate,
        f"short item rate: {short_rate:.1%}",
        details={"short_items": short_items[:20], "rate": short_rate},
    )
    _check(
        checks,
        "explainer_language",
        explainer_rate >= min_explainer_rate,
        f"explainer phrase rate: {explainer_rate:.1%}",
        details={"rate": explainer_rate, "hits": explainer_hits, "items": len(items)},
    )
    _check(
        checks,
        "waiting_marker_integrity",
        not unsafe_waiting,
        "items with [待核实] must carry needs_review=true",
        details={"items": unsafe_waiting[:20]},
    )
    _check(
        checks,
        "risky_claim_marking",
        not risky_unmarked,
        "risky claims should be hedged or marked needs_review",
        severity="warn",
        details={"items": risky_unmarked[:20]},
    )
    _check(
        checks,
        "duplicate_story_hard",
        not hard_dupes,
        f"hard duplicate story pairs: {len(hard_dupes)}",
        details={"pairs": hard_dupes[:20]},
    )
    _check(
        checks,
        "duplicate_story_warn",
        not warn_dupes,
        f"possible duplicate story pairs: {len(warn_dupes)}",
        severity="warn",
        details={"pairs": warn_dupes[:20]},
    )

    score, max_score, passed = _score(checks, weights={"summary_lengths": 2, "risky_claim_marking": 2})
    return CaseResult(
        id=case["id"],
        suite=case["suite"],
        passed=passed,
        score=score,
        max_score=max_score,
        checks=checks,
        metrics={
            "archive": str(target),
            "items": len(items),
            "short_rate": short_rate,
            "explainer_rate": explainer_rate,
            "possible_duplicates": len(duplicate_pairs),
            "hard_duplicates": len(hard_dupes),
            "needs_review_items": sum(1 for item in items if item.get("needs_review")),
        },
    )


def grade_thread_archive(case: dict[str, Any], *, archive_path: Path | None) -> CaseResult:
    """Grade visible long-horizon tracking data in the delivered archive."""
    thresholds = case.get("thresholds") or {}
    archive_dir = ROOT / "web" / "archive" / "daily"
    target = archive_path or _latest_archive_path(archive_dir)
    checks: list[Check] = []
    if target is None or not target.exists():
        _check(checks, "archive_exists", False, "no daily archive json found")
        score, max_score, passed = _score(checks)
        return CaseResult(case["id"], case["suite"], passed, score, max_score, checks)
    payload = _read_json(target)
    threads = payload.get("threads") or []
    min_threads = int(thresholds.get("min_visible_threads", 1))
    min_days = int(thresholds.get("min_thread_days", 2))
    min_timeline_points = int(thresholds.get("min_timeline_points", 2))
    stale_or_broken: list[dict[str, Any]] = []
    date_order_bad: list[dict[str, Any]] = []
    thin_threads: list[dict[str, Any]] = []
    domains = Counter()

    for thread in threads:
        timeline = thread.get("timeline") or []
        domains[str(thread.get("domain") or "unknown")] += 1
        dates = [p.get("date") for p in timeline if p.get("date")]
        unique_dates = sorted(set(dates), reverse=True)
        if thread.get("days", 0) < min_days or len(unique_dates) < min_days:
            thin_threads.append({"thread": thread.get("name"), "days": thread.get("days"), "dates": unique_dates})
        if len(timeline) < min_timeline_points:
            stale_or_broken.append({"thread": thread.get("name"), "points": len(timeline)})
        if dates != sorted(dates, reverse=True):
            date_order_bad.append({"thread": thread.get("name"), "dates": dates[:10]})

    _check(
        checks,
        "visible_threads",
        len(threads) >= min_threads,
        f"visible threads: {len(threads)}",
        details={"actual": len(threads)},
    )
    _check(
        checks,
        "thread_depth",
        not thin_threads,
        f"threads below {min_days} days: {len(thin_threads)}",
        details={"threads": thin_threads[:20]},
    )
    _check(
        checks,
        "timeline_points",
        not stale_or_broken,
        f"threads below {min_timeline_points} timeline points: {len(stale_or_broken)}",
        details={"threads": stale_or_broken[:20]},
    )
    _check(
        checks,
        "timeline_order",
        not date_order_bad,
        "thread timelines should be newest first",
        details={"threads": date_order_bad[:20]},
    )
    _check(
        checks,
        "thread_domain_breadth",
        len(domains) >= int(thresholds.get("min_thread_domains", 1)),
        f"thread domains: {dict(domains)}",
        severity="warn",
        details={"domains": dict(domains)},
    )

    score, max_score, passed = _score(checks)
    return CaseResult(
        id=case["id"],
        suite=case["suite"],
        passed=passed,
        score=score,
        max_score=max_score,
        checks=checks,
        metrics={"archive": str(target), "threads": len(threads), "domains": dict(domains)},
    )


def grade_reference_topics(case: dict[str, Any], *, archive_path: Path | None) -> CaseResult:
    """Grade manually supplied hot-topic recall.

    The eval does not invent a truth set. If the case has no topics, it is
    skipped and reports why.
    """
    topics = case.get("topics") or []
    checks: list[Check] = []
    if not topics:
        _check(
            checks,
            "reference_topics_supplied",
            False,
            "未提供热点真值(人工/记分牌)→ hot_news 维【未判 ungraded】:不计为通过、也不算硬失败"
            "(护栏4:未考维如实声明,'通过'绝不许覆盖它;真值由记分牌独立抓填后才真判)",
            severity="skip",
        )
        return CaseResult(
            case["id"], case["suite"], False, 0.0, 0.0, checks,
            {"skipped": True, "ungraded": True}, ungraded=True,
        )
    archive_dir = ROOT / "web" / "archive" / "daily"
    target = archive_path or _latest_archive_path(archive_dir)
    if target is None or not target.exists():
        _check(checks, "archive_exists", False, "no daily archive json found")
        score, max_score, passed = _score(checks)
        return CaseResult(case["id"], case["suite"], passed, score, max_score, checks)
    payload = _read_json(target)
    haystack = "\n".join(
        f"{item.get('headline') or item.get('title_zh') or item.get('title') or ''}\n"
        f"{item.get('tldr') or ''}\n{item.get('insight') or ''}"
        for item in _archive_items(payload)
    ).lower()
    missed: list[str] = []
    for topic in topics:
        keywords = [str(k).lower() for k in topic.get("keywords", [])]
        hit = any(k in haystack for k in keywords)
        _check(
            checks,
            f"topic_{topic.get('id') or topic.get('name')}",
            hit,
            f"reference topic: {topic.get('name')}",
            details={"keywords": keywords},
        )
        if not hit:
            missed.append(topic.get("name") or topic.get("id") or str(keywords))
    score, max_score, passed = _score(checks, weights={c.name: 2.0 for c in checks})
    return CaseResult(
        id=case["id"],
        suite=case["suite"],
        passed=passed,
        score=score,
        max_score=max_score,
        checks=checks,
        metrics={"archive": str(target), "topics": len(topics), "missed": missed},
    )


def case_to_dict(result: CaseResult) -> dict[str, Any]:
    return {
        "id": result.id,
        "suite": result.suite,
        "passed": result.passed,
        "ungraded": result.ungraded,
        "score": result.score,
        "max_score": result.max_score,
        "metrics": result.metrics,
        "checks": [
            {
                "name": c.name,
                "passed": c.passed,
                "severity": c.severity,
                "message": c.message,
                "details": c.details,
            }
            for c in result.checks
        ],
    }
