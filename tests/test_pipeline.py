"""流水线编排器测试:检查点续跑 / 同日幂等 / force 重跑 / 失败告警。

纯逻辑测试无需数据库;编排测试需数据库(连不上自动跳过),并注入假阶段
(不真跑 fetch/LLM),用唯一 run_id 且测试后清理 runs 行。
"""

from __future__ import annotations

import types
import uuid

import pytest
from sqlalchemy.exc import InterfaceError, OperationalError

from pulsewire.config import get_settings
from pulsewire.pipeline import (
    STAGE_NAMES,
    DomainSpec,
    _build_domains,
    _for_domains,
    _start_index,
    default_run_id,
    run_pipeline,
)
from pulsewire.store import get_run, get_sessionmaker
from pulsewire.store.tables import Run


# --------------------------- 纯逻辑(无 DB) --------------------------- #
def test_start_index_resume_points():
    assert _start_index(None) == 0           # 全新:从头
    assert _start_index("fetch") == 1        # 完成 fetch → 从 dedup
    assert _start_index("enrich") == STAGE_NAMES.index("enrich") + 1
    assert _start_index("deliver") == len(STAGE_NAMES)  # 全完成 → 没有剩余阶段
    assert _start_index("bogus") == 0        # 未知阶段 → 保守从头


def test_default_run_id_shape():
    rid = default_run_id(get_settings(), "daily")
    assert rid.startswith("daily_")
    assert len(rid) == len("daily_") + 8     # daily_YYYYMMDD


def test_build_domains_from_config():
    """config.run.domains → 每领域一个 DomainSpec,domain=key(rank 据此过滤),首个 required=主报。"""
    doms = _build_domains(get_settings(), explicit_interest=None, explicit_tags=None)
    keys = [d.key for d in doms]
    assert keys == ["ai", "bio", "geo"]
    assert all(d.domain == d.key for d in doms)        # 多领域 = 按 key 过滤
    assert doms[0].required is True                    # AI 主报
    assert doms[1].required is False and doms[2].required is False
    assert len({d.interest_key for d in doms}) == 3    # 三领域 interest_key 各不同


def test_build_domains_carries_per_domain_freshness_window():
    """按域新鲜窗必须从 DomainCfg 带进 DomainSpec(回归:2026-06-29 逮到漏带 → run_event_rank
    收的是 DomainSpec、漏带使 AI 板放宽在生产静默失效退回全局 48h,今早只出 2 条)。"""
    s = get_settings()
    cfg_by_key = {d.key: d for d in s.run.domains if d.enabled}
    doms = _build_domains(s, explicit_interest=None, explicit_tags=None)
    for d in doms:
        assert d.freshness_window_hours == cfg_by_key[d.key].freshness_window_hours, \
            f"{d.key} 的 freshness_window_hours 没从 DomainCfg 带过来"
    # 且 AI 板当前确实配了放宽窗(>48h);改回窄窗需有意更新此断言
    ai = next(d for d in doms if d.key == "ai")
    assert ai.freshness_window_hours is not None and ai.freshness_window_hours > 48


def test_build_domains_custom_single_no_filter():
    """CLI 显式单兴趣 → 单 custom 领域,domain=None(不按领域过滤,保持旧单兴趣行为)。"""
    doms = _build_domains(get_settings(), explicit_interest="某自定义兴趣", explicit_tags=["t"])
    assert len(doms) == 1
    assert doms[0].domain is None and doms[0].required is True
    assert doms[0].interest == "某自定义兴趣"


@pytest.mark.asyncio
async def test_for_domains_isolates_nonrequired_failure(monkeypatch):
    """次领域(required=False)失败 → 告警 + 跳过,不拖垮主报;主领域成功照常返回。"""
    alerts: list[dict] = []

    async def _rec_alert(settings, **kw):
        alerts.append(kw)

    monkeypatch.setattr("pulsewire.pipeline.alert_failure", _rec_alert)

    def _dom(key, required):
        return DomainSpec(key=key, label=key, interest=key, tags=[],
                          interest_key=f"int_{key}", required=required, domain=key)

    ctx = types.SimpleNamespace(run_id="t", domains=[_dom("ai", True), _dom("bio", False)])

    async def _runner(d):
        if d.key == "bio":
            raise RuntimeError("boom@bio")
        return {"kept": 3}

    out = await _for_domains(ctx, get_settings(), "rank", _runner)
    by = {x["domain"]: x for x in out["domains"]}
    assert by["ai"]["kept"] == 3                        # 主报正常
    assert by["bio"]["status"] == "skipped"             # 次领域跳过不抛
    assert len(alerts) == 1 and alerts[0]["stage"] == "rank:bio"  # 失败有告警(不静默)


@pytest.mark.asyncio
async def test_for_domains_required_failure_raises(monkeypatch):
    """主领域(required=True)失败 → 冒泡(整跑失败),绝不静默产空主报。"""
    monkeypatch.setattr("pulsewire.pipeline.alert_failure", lambda *a, **k: None)

    ctx = types.SimpleNamespace(
        run_id="t",
        domains=[DomainSpec(key="ai", label="ai", interest="ai", tags=[],
                            interest_key="int_ai", required=True, domain="ai")],
    )

    async def _runner(d):
        raise RuntimeError("boom@ai")

    with pytest.raises(RuntimeError, match="boom@ai"):
        await _for_domains(ctx, get_settings(), "rank", _runner)


@pytest.mark.asyncio
async def test_stage_rank_required_empty_raises(monkeypatch):
    """主报领域 rank 后 0 条入选 → 在 rank 站就抛(别拖到 deliver '无内容'才炸,失败点偏晚)。"""
    from pulsewire import pipeline

    async def _fake_rank(settings, **k):
        return {"kept": 0, "recalled": 12, "domain_dropped": 12}

    monkeypatch.setattr("pulsewire.rank.run_rank", _fake_rank)
    monkeypatch.setattr("pulsewire.pipeline.alert_failure", lambda *a, **k: None)
    # 本测试验 legacy 分支的"required 空→抛"语义(已 mock legacy run_rank)。live config 2026-06-19 已切 events,
    # 显式钉回 legacy 免 _stage_rank 改走 events 真跑(DB/LLM)挂死;monkeypatch 自动还原,不泄漏。
    monkeypatch.setattr(get_settings().rank, "engine", "legacy")

    ctx = types.SimpleNamespace(
        run_id="t", interest_key="int_ai",
        domains=[DomainSpec(key="ai", label="ai", interest="ai", tags=[],
                            interest_key="int_ai", required=True, domain="ai")],
    )
    with pytest.raises(RuntimeError, match="0 条入选"):
        await pipeline._stage_rank(get_settings(), ctx)


@pytest.mark.asyncio
async def test_stage_rank_nonrequired_empty_does_not_raise(monkeypatch):
    """次领域 rank 后 0 条 → 不抛(下游 _domains_with_rankings 自会跳过它),只是 kept=0。"""
    from pulsewire import pipeline

    async def _fake_rank(settings, *, interest, tags, limit, domain, run_id):
        return {"kept": 3 if domain == "ai" else 0}

    monkeypatch.setattr("pulsewire.rank.run_rank", _fake_rank)
    monkeypatch.setattr("pulsewire.pipeline.alert_failure", lambda *a, **k: None)
    monkeypatch.setattr(get_settings().rank, "engine", "legacy")  # 同上:验 legacy 分支语义,钉回免走 events 真跑

    def _dom(key, required):
        return DomainSpec(key=key, label=key, interest=key, tags=[],
                          interest_key=f"int_{key}", required=required, domain=key)

    ctx = types.SimpleNamespace(
        run_id="t", interest_key="int_ai",
        domains=[_dom("ai", True), _dom("bio", False)],
    )
    out = await pipeline._stage_rank(get_settings(), ctx)
    by = {x["domain"]: x for x in out["domains"]}
    assert by["ai"]["kept"] == 3
    assert by["bio"]["kept"] == 0  # 次领域空:正常返回,不抛不告警


# --------------------------- 编排(需 DB) --------------------------- #
def _fake_stage(name: str, ran: list[str], fail: bool = False):
    async def runner(settings, ctx):
        ran.append(name)
        if fail:
            raise RuntimeError(f"boom@{name}")
        return {"items": 1}
    return runner


def _stages(ran: list[str], fail_at: str | None = None):
    return [(n, _fake_stage(n, ran, fail=(n == fail_at))) for n in STAGE_NAMES]


async def _db_reachable() -> bool:
    sm = get_sessionmaker()
    try:
        async with sm() as s:
            await s.connection()
        return True
    except (OperationalError, InterfaceError, ConnectionError, OSError):
        return False


async def _cleanup(run_id: str) -> None:
    sm = get_sessionmaker()
    async with sm() as s:
        async with s.begin():
            row = await s.get(Run, run_id)
            if row is not None:
                await s.delete(row)


@pytest.mark.asyncio
async def test_checkpoint_resume_idempotent_force(monkeypatch):
    if not await _db_reachable():
        pytest.skip("数据库不可用,跳过编排测试")
    # 告警走 no-op,避免任何真实飞书/微信网络请求
    async def _noop_alert(*a, **k):
        return {}
    monkeypatch.setattr("pulsewire.pipeline.alert_failure", _noop_alert)

    settings = get_settings()
    run_id = "test_" + uuid.uuid4().hex[:12]
    sm = get_sessionmaker()
    ran: list[str] = []
    try:
        # 1) rank 站故障 → 冒泡;只跑到 enrich;检查点 stage=enrich,status=failed
        with pytest.raises(RuntimeError):
            await run_pipeline(settings, run_id=run_id, stages=_stages(ran, fail_at="rank"))
        # rank 被尝试但抛错;检查点只认最后"完成"的 enrich
        assert ran == ["fetch", "dedup", "enrich", "rank"]
        async with sm() as s:
            row = await get_run(s, run_id)
        assert row.status == "failed"
        assert row.stage == "enrich"

        # 2) 同 run_id 续跑 → 从 rank 接着跑,不重复前三站
        ran.clear()
        res = await run_pipeline(settings, run_id=run_id, stages=_stages(ran))
        assert ran == ["rank", "transcript", "summarize", "github_board", "threads", "render", "deliver"]
        assert res["status"] == "succeeded" and res["skipped"] is False
        async with sm() as s:
            row = await get_run(s, run_id)
        assert row.status == "succeeded" and row.stage == "deliver"

        # 3) 同 run_id 再跑 → 已成功,直接跳过,一站不跑
        ran.clear()
        res2 = await run_pipeline(settings, run_id=run_id, stages=_stages(ran))
        assert res2["skipped"] is True and ran == []

        # 4) --force → 无视已成功,全部重跑(各站幂等)
        ran.clear()
        res3 = await run_pipeline(settings, run_id=run_id, stages=_stages(ran), force=True)
        assert ran == STAGE_NAMES and res3["status"] == "succeeded"
    finally:
        await _cleanup(run_id)


@pytest.mark.asyncio
async def test_alert_fires_even_when_finish_run_raises(monkeypatch):
    """2026-06-15 静默开天窗回归:阶段失败时若写库(finish_run)也挂了(postgres 断连),
    告警仍须发出、且冒泡的是原始阶段异常(RuntimeError)而非写库异常(OSError)。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    settings = get_settings()
    # 自建 NullPool engine 注入 run_pipeline:避免与全局 sessionmaker 的连接池跨事件循环复用
    # (同 conftest.db_session 的理由);否则同文件第二个 DB 测试会撞 'Event loop is closed'。
    engine = create_async_engine(settings.database.async_dsn, poolclass=NullPool)
    try:
        conn = await engine.connect()
        await conn.close()
    except (OperationalError, InterfaceError, ConnectionError, OSError):
        await engine.dispose()
        pytest.skip("数据库不可用,跳过编排测试")
    sm = async_sessionmaker(engine, expire_on_commit=False)

    alerts: list[dict] = []

    async def _rec_alert(_settings, **kw):
        alerts.append(kw)
        return {}

    async def _boom_finish(*a, **k):
        raise OSError("Connect call failed ... :5432")  # 模拟运行中 postgres 断连

    monkeypatch.setattr("pulsewire.pipeline.alert_failure", _rec_alert)
    monkeypatch.setattr("pulsewire.pipeline.finish_run", _boom_finish)

    run_id = "test_" + uuid.uuid4().hex[:12]
    ran: list[str] = []
    try:
        # rank 站失败 + finish_run 写库也挂 → 必须冒泡原始 RuntimeError,不是 OSError
        with pytest.raises(RuntimeError, match="boom@rank"):
            await run_pipeline(
                settings, run_id=run_id, stages=_stages(ran, fail_at="rank"), sessionmaker=sm
            )
        # 关键断言:告警照发(不被写库二次异常吞掉),且指向真正失败的 rank 站
        assert len(alerts) == 1
        assert alerts[0]["stage"] == "rank"
    finally:
        async with sm() as s:
            async with s.begin():
                row = await s.get(Run, run_id)
                if row is not None:
                    await s.delete(row)
        await engine.dispose()


@pytest.mark.asyncio
async def test_total_timeout_watchdog_fails_and_alerts(monkeypatch):
    """2026-06-15 二⑦:某站超过整跑总预算 → 看门狗令该站超时冒泡,走失败告警链 + 写 failed。"""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    base = get_settings()
    # 总预算压到 0.01 分钟(0.6s),首站故意 sleep 2s → 触发看门狗超时
    settings = base.model_copy(
        update={"run": base.run.model_copy(update={"total_timeout_minutes": 0.01})}
    )
    eng = create_async_engine(settings.database.async_dsn, poolclass=NullPool)
    try:
        conn = await eng.connect()
        await conn.close()
    except (OperationalError, InterfaceError, ConnectionError, OSError):
        await eng.dispose()
        pytest.skip("数据库不可用,跳过编排测试")
    sm = async_sessionmaker(eng, expire_on_commit=False)

    alerts: list[dict] = []

    async def _rec_alert(_settings, **kw):
        alerts.append(kw)
        return {}

    monkeypatch.setattr("pulsewire.pipeline.alert_failure", _rec_alert)

    async def _slow(settings, ctx):
        await asyncio.sleep(2)
        return {}

    run_id = "test_" + uuid.uuid4().hex[:12]
    try:
        with pytest.raises(TimeoutError):  # 看门狗超时冒泡(asyncio.wait_for → TimeoutError)
            await run_pipeline(
                settings, run_id=run_id, stages=[("fetch", _slow)], sessionmaker=sm
            )
        assert len(alerts) == 1 and alerts[0]["stage"] == "fetch"  # 超时也走失败告警
        async with sm() as s:
            row = await get_run(s, run_id)
        assert row.status == "failed"  # 写了 failed(检查点续跑可补救)
    finally:
        async with sm() as s:
            async with s.begin():
                row = await s.get(Run, run_id)
                if row is not None:
                    await s.delete(row)
        await eng.dispose()


def _preflight_settings():
    """backend=api + 预检开 + 阈值 5.0 的 settings(model_copy 不可变覆盖)。"""
    base = get_settings()
    return base.model_copy(update={
        "summarize": base.summarize.model_copy(update={"backend": "api"}),
        "run": base.run.model_copy(
            update={"preflight_balance_enabled": True, "preflight_min_balance": 5.0}),
    })


async def _nullpool_engine_or_skip(settings):
    """自建 NullPool engine(避免全局池跨事件循环撞 'Event loop is closed');连不上则 skip。"""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(settings.database.async_dsn, poolclass=NullPool)
    try:
        conn = await engine.connect()
        await conn.close()
    except (OperationalError, InterfaceError, ConnectionError, OSError):
        await engine.dispose()
        pytest.skip("数据库不可用,跳过编排测试")
    return engine


@pytest.mark.asyncio
async def test_preflight_low_balance_aborts_before_any_stage(monkeypatch):
    """f16 / 2026-07-02 E1:开跑前查到余额低于阈值 → 一站不跑 + 告警 + 写 failed。

    stage=None ⇒ 下次(充值后)从头重跑,不留毒检查点;在烧任何一站前掐断
    「欠费→判官全 fail-open 出毒日报→每5分钟全量重试风暴」整条链。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    settings = _preflight_settings()
    engine = await _nullpool_engine_or_skip(settings)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    alerts: list[dict] = []

    async def _rec_alert(_settings, **kw):
        alerts.append(kw)
        return {}

    async def _low(_settings):
        return 1.0

    monkeypatch.setattr("pulsewire.pipeline.alert_failure", _rec_alert)
    monkeypatch.setattr("pulsewire.preflight.check_deepseek_balance", _low)

    run_id = "test_" + uuid.uuid4().hex[:12]
    ran: list[str] = []
    try:
        res = await run_pipeline(settings, run_id=run_id, stages=_stages(ran), sessionmaker=sm)
        assert res["status"] == "failed" and res.get("aborted") == "preflight_balance"
        assert ran == []  # 一站都没跑
        assert len(alerts) == 1 and alerts[0]["stage"] == "preflight"
        async with sm() as s:
            row = await get_run(s, run_id)
        assert row.status == "failed" and row.stage is None  # 无毒检查点,下次充值后从头重跑
    finally:
        async with sm() as s:
            async with s.begin():
                row = await s.get(Run, run_id)
                if row is not None:
                    await s.delete(row)
        await engine.dispose()


@pytest.mark.asyncio
async def test_preflight_unknown_balance_proceeds(monkeypatch):
    """预检 best-effort:查不到余额(None)→ 放行照常整跑,绝不因预检故障挡掉正常日报。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    settings = _preflight_settings()
    engine = await _nullpool_engine_or_skip(settings)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    async def _noop_alert(_settings, **kw):
        return {}

    async def _unknown(_settings):
        return None

    monkeypatch.setattr("pulsewire.pipeline.alert_failure", _noop_alert)
    monkeypatch.setattr("pulsewire.preflight.check_deepseek_balance", _unknown)

    run_id = "test_" + uuid.uuid4().hex[:12]
    ran: list[str] = []
    try:
        res = await run_pipeline(settings, run_id=run_id, stages=_stages(ran), sessionmaker=sm)
        assert res["status"] == "succeeded"
        assert ran == STAGE_NAMES  # 照常全跑
    finally:
        async with sm() as s:
            async with s.begin():
                row = await s.get(Run, run_id)
                if row is not None:
                    await s.delete(row)
        await engine.dispose()
