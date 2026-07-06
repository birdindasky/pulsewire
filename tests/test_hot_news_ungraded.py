"""hot_news 空真值 = 未判(ungraded),绝不假'通过'(堵 graders.py 元凶空壳;护栏4)。

回归:Phase 2 前 grade_reference_topics 在 topics 为空时硬返 passed=True,
让"考卷上最关键的题不存在"却报通过,掩盖选题/热点从未被考。
"""
from __future__ import annotations

import sys
from pulsewire.config import PROJECT_ROOT

sys.path.insert(0, str(PROJECT_ROOT / "evals"))
from graders import CaseResult, case_to_dict, grade_reference_topics  # noqa: E402


def test_empty_topics_is_ungraded_not_passed():
    case = {"id": "hot_news_v0", "suite": "hot_news", "topics": []}
    r = grade_reference_topics(case, archive_path=None)
    assert r.ungraded is True, "空真值必须标 ungraded"
    assert r.passed is False, "空真值绝不能算 passed(否则掩盖未考)"
    # 如实声明进了报告
    d = case_to_dict(r)
    assert d["ungraded"] is True
    assert any("ungraded" in (c["message"]) or c["name"] == "reference_topics_supplied" for c in d["checks"])


def test_caseresult_default_is_graded():
    r = CaseResult("x", "s", True, 1.0, 1.0)
    assert r.ungraded is False, "正常判分的默认非 ungraded"


def test_ungraded_excluded_from_pass_and_fail_aggregation():
    """ungraded 既不进 failed(不拖垮套件),也不算 pass(不掺水)。"""
    graded_pass = CaseResult("a", "s", True, 1.0, 1.0)
    graded_fail = CaseResult("b", "s", False, 0.0, 1.0)
    ungraded = CaseResult("c", "hot_news", False, 0.0, 0.0, ungraded=True)
    results = [graded_pass, graded_fail, ungraded]
    failed = [r for r in results if not r.passed and not r.ungraded]
    ug = [r for r in results if r.ungraded]
    assert [r.id for r in failed] == ["b"], "ungraded 不该进 failed"
    assert [r.id for r in ug] == ["c"]
    assert graded_pass not in failed and graded_pass not in ug
