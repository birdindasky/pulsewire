"""LLM 错误分类 + 永久错熔断(f01)—— 2026-07-02 E1 回归。

核心不变量:余额烧干 / 凭证失效这类「永久错」必须穿透判官熔断整跑,绝不被吞成
fail-safe 票出毒日报;瞬时错(超时 / 限流 / 偶发5xx)照旧 fail-safe / 重试,零回归。
"""

from __future__ import annotations

import pytest

from pulsewire.config import get_settings
from pulsewire.llm_errors import PermanentLLMError, classify_llm_error


class _Err(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        if status is not None:
            self.status_code = status


# ---------------- 分类器 ---------------- #
def test_insufficient_balance_is_permanent():
    # E1 原文:litellm 把 DeepSeek 余额不足包成 BadRequestError(status 400),靠文案识别
    e = _Err('DeepseekException - {"error":{"message":"Insufficient Balance",'
             '"code":"invalid_request_error"}}', status=400)
    assert isinstance(classify_llm_error(e), PermanentLLMError)


def test_auth_billing_status_is_permanent():
    for s in (401, 402, 403):
        assert isinstance(classify_llm_error(_Err("nope", status=s)), PermanentLLMError)


def test_quota_and_key_markers_are_permanent():
    assert isinstance(classify_llm_error(_Err("You exceeded your current quota")), PermanentLLMError)
    assert isinstance(classify_llm_error(_Err("Incorrect API key provided")), PermanentLLMError)
    assert isinstance(classify_llm_error(_Err("insufficient_quota")), PermanentLLMError)


def test_transient_errors_are_none():
    # 超时 / 限流 / 5xx / 网络抖 → None(照旧重试 / fail-safe,零回归)
    assert classify_llm_error(_Err("Read timed out")) is None
    assert classify_llm_error(_Err("rate limit exceeded", status=429)) is None
    assert classify_llm_error(_Err("internal server error", status=500)) is None
    assert classify_llm_error(_Err("Server disconnected without sending a response")) is None


def test_generic_400_without_marker_is_transient():
    # 普通坏请求(非余额/配额/凭证)不误判成永久,免得偶发坏请求熔断整跑
    assert classify_llm_error(_Err("bad request: malformed json", status=400)) is None


def test_already_permanent_passthrough():
    p = PermanentLLMError("x")
    assert classify_llm_error(p) is p


# ---------------- 判官穿透 vs fail-safe ---------------- #
def test_magnitude_judge_propagates_permanent(monkeypatch):
    """余额尽:水货闸绝不把 PermanentLLMError 吞成 KEEP 票,而是穿透熔断。"""
    from pulsewire.events import magnitude_judge as mj

    def _boom(*a, **k):
        raise PermanentLLMError("Insufficient Balance")

    monkeypatch.setattr(mj, "judge_is_water", _boom)
    judge = mj.make_water_judge(get_settings())
    with pytest.raises(PermanentLLMError):
        judge({"headline": "某AI新品发布", "snippet": "正文"})


def test_magnitude_judge_transient_fails_safe(monkeypatch):
    """瞬时错(超时)照旧 fail-safe KEEP(返 False 不抛)——零回归。"""
    from pulsewire.events import magnitude_judge as mj

    def _timeout(*a, **k):
        raise RuntimeError("Read timed out")

    monkeypatch.setattr(mj, "judge_is_water", _timeout)
    judge = mj.make_water_judge(get_settings())
    assert judge({"headline": "某AI新品发布", "snippet": "正文"}) is False


def test_worthiness_judge_propagates_permanent(monkeypatch):
    """够格闸同样穿透永久错(覆盖第二道判官,证明模式一致)。"""
    from pulsewire.events import worthiness_judge as wj

    def _boom(*a, **k):
        raise PermanentLLMError("Insufficient Balance")

    monkeypatch.setattr(wj, "judge_is_worthy", _boom)
    judge = wj.make_worthiness_judge(get_settings())
    with pytest.raises(PermanentLLMError):
        judge({"headline": "某AI新品发布", "snippet": "正文"})


# ---------------- 写稿不重试永久错 ---------------- #
def test_summarize_does_not_retry_permanent(monkeypatch):
    """_complete_validated 遇 PermanentLLMError 立即冒泡,不追加提示傻重试(E1 核心病)。"""
    from pulsewire.summarize import engine as se

    calls = [0]

    def _boom(system, user, settings, **k):
        calls[0] += 1
        raise PermanentLLMError("Insufficient Balance")

    monkeypatch.setattr(se, "complete", _boom)
    with pytest.raises(PermanentLLMError):
        se._complete_validated("sys", "usr", get_settings())
    assert calls[0] == 1  # 只调一次,零重试


def test_permanent_llm_error_not_runtimeerror():
    """回归守卫:PermanentLLMError 必须继承 Exception 而非 RuntimeError,否则会被
    summarize/engine.py _do_chunk 的 `except RuntimeError` 静默吞回 → 写稿丢块发毒稿
    (2026-07-03 独立考官逮到的假绿灯根因)。"""
    e = PermanentLLMError("x")
    assert isinstance(e, Exception)
    assert not isinstance(e, RuntimeError)


@pytest.mark.asyncio
async def test_run_summarize_propagates_permanent(monkeypatch):
    """真实路径穿透(堵假绿灯):余额尽时 run_summarize 必须冒泡 PermanentLLMError 熔断,
    而不是被 _do_chunk 的 except RuntimeError 吞成 RuntimeError 静默丢块照发。

    旧测只孤立测 _complete_validated(它确实 re-raise),没走 run_summarize→_do_chunk 真实
    调用链,于是绿灯掩盖了写稿阶段的漏网。本测试补这条真实路径。"""
    from datetime import datetime, timezone

    from sqlalchemy import delete
    from sqlalchemy.exc import InterfaceError, OperationalError
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from pulsewire.store import upsert_item, upsert_ranking
    from pulsewire.store.tables import Item, Ranking, Summary
    from pulsewire.summarize import engine as se

    settings = get_settings()
    engine = create_async_engine(settings.database.async_dsn, poolclass=NullPool)
    try:
        conn = await engine.connect()
        await conn.close()
    except (OperationalError, InterfaceError, ConnectionError, OSError):
        await engine.dispose()
        pytest.skip("数据库不可用,跳过")
    sm = async_sessionmaker(engine, expire_on_commit=False)

    def _boom(*a, **k):
        raise PermanentLLMError("Insufficient Balance")

    monkeypatch.setattr(se, "complete", _boom)

    ik = "int_permf01"
    now = datetime.now(timezone.utc)
    iid = None
    try:
        async with sm() as s:
            async with s.begin():
                iid = await upsert_item(
                    s, source="permf01", url="https://permf01.example/1",
                    title="某AI新品发布 芯片", content="正文内容。", published_at=now)
                await upsert_ranking(
                    s, interest_key=ik, interest="科技", tags=None, item_id=iid,
                    cluster_id=None, recall_score=0.5, rule_score=0.5, rerank_score=0.5,
                    final_score=0.9, rank=1, provider="rule")
        with pytest.raises(PermanentLLMError):  # 关键:冒泡的是永久错,不是 RuntimeError
            await se.run_summarize(settings, interest_key=ik, run_id="daily_permf01", sessionmaker=sm)
    finally:
        async with sm() as s:
            async with s.begin():
                await s.execute(delete(Ranking).where(Ranking.interest_key == ik))
                await s.execute(delete(Summary).where(Summary.interest_key == ik))
                if iid:
                    await s.execute(delete(Item).where(Item.item_id == iid))
        await engine.dispose()
