"""事件线归线引擎(step 3)DB 测试:新开/归入/幂等/置休。A、B 的 LLM 调用 mock 掉,不打网络。

用独立 interest_key 隔离(生产无此兴趣的总结);db_session 事务回滚,不留脏数据。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from pulsewire.config import get_settings
from pulsewire.store import repo
from pulsewire.store.tables import Thread, ThreadCluster
from pulsewire.threads import engine

NOW = datetime.now(timezone.utc)


async def _seed_summary(session, ik, suffix, headline, tldr):
    """造一条「今日入选簇」:item + cluster + summary(指向该簇)。返回 cluster_id。"""
    iid = await repo.upsert_item(
        session, source="ai-test", url=f"https://ex.com/{ik}/{suffix}", title=headline,
    )
    cid = f"clt_test_{ik}_{suffix}"
    await repo.create_cluster(session, cluster_id=cid, first_item_id=iid, title=headline, seen_at=NOW)
    await repo.upsert_summary(
        session, interest_key=ik, item_id=iid, cluster_id=cid, headline=headline,
        tldr_raw=tldr, tldr_rendered=tldr, insight_raw=tldr, insight_rendered=tldr,
        status="ok", used_source_ids=[], unresolved=[], suspect=[], backend="api",
        model="x", run_id=None,
    )
    return cid


async def _threads(session, ik):
    return list((await session.execute(select(Thread).where(Thread.domain == ik))).scalars().all())


async def _links(session, ik):
    stmt = select(ThreadCluster).join(Thread, Thread.thread_id == ThreadCluster.thread_id).where(
        Thread.domain == ik
    )
    return list((await session.execute(stmt)).scalars().all())


@pytest.mark.asyncio
async def test_same_subject_links_into_one_line(db_session, monkeypatch):
    """同主体两簇:第一簇新开线,第二簇经判官并入 → 1 条线 2 次挂载,heat 累加。"""
    ik = "test_thr_link"
    monkeypatch.setattr(engine, "extract_subject", lambda headline, **k: "OpenAI IPO")
    monkeypatch.setattr(engine, "judge_line", lambda **k: (0, 0.9))  # 接 L1
    await _seed_summary(db_session, ik, "a", "OpenAI 据报道提交上市申请", "OpenAI 秘密递表")
    await _seed_summary(db_session, ik, "b", "OpenAI IPO 获监管放行", "上市再进一步")

    r = await engine.thread_domain(db_session, get_settings(), interest_key=ik, run_id=None, now=NOW)
    assert (r["new"], r["linked"]) == (1, 1)
    threads = await _threads(db_session, ik)
    links = await _links(db_session, ik)
    assert len(threads) == 1 and len(links) == 2
    assert threads[0].heat == 2  # 两簇累加
    assert {link.link_reason for link in links} == {"new", "judge"}


@pytest.mark.asyncio
async def test_idempotent_rerun_skips_linked(db_session, monkeypatch):
    """重跑:已挂线的簇不重复处理(幂等),不产生重复线/挂载。"""
    ik = "test_thr_idem"
    monkeypatch.setattr(engine, "extract_subject", lambda headline, **k: "OpenAI IPO")
    monkeypatch.setattr(engine, "judge_line", lambda **k: (0, 0.9))
    await _seed_summary(db_session, ik, "a", "OpenAI 提交上市申请", "递表")
    await engine.thread_domain(db_session, get_settings(), interest_key=ik, run_id=None, now=NOW)
    r2 = await engine.thread_domain(db_session, get_settings(), interest_key=ik, run_id=None, now=NOW)
    assert (r2["new"], r2["linked"], r2["skipped"]) == (0, 0, 1)
    assert len(await _threads(db_session, ik)) == 1


@pytest.mark.asyncio
async def test_judge_says_new_opens_separate_line(db_session, monkeypatch):
    """判官否决(NEW):即使主体接近,也开成两条线——同公司不同事不该混。"""
    ik = "test_thr_new"
    monkeypatch.setattr(engine, "extract_subject", lambda headline, **k: "OpenAI 动态")
    monkeypatch.setattr(engine, "judge_line", lambda **k: (None, 0.2))  # 都判新开
    await _seed_summary(db_session, ik, "a", "OpenAI 提交上市申请", "递表")
    await _seed_summary(db_session, ik, "b", "OpenAI 发布新模型", "发模型")
    r = await engine.thread_domain(db_session, get_settings(), interest_key=ik, run_id=None, now=NOW)
    assert r["new"] == 2
    assert len(await _threads(db_session, ik)) == 2


@pytest.mark.asyncio
async def test_judge_failure_degrades_to_subject_match(db_session, monkeypatch):
    """B 判官抛错 → 降级只信 A,仍能并入最匹配候选(不中断、不漏归)。"""
    ik = "test_thr_degrade"
    monkeypatch.setattr(engine, "extract_subject", lambda headline, **k: "OpenAI IPO")

    def _boom(**k):
        raise RuntimeError("judge LLM down")

    monkeypatch.setattr(engine, "judge_line", _boom)
    await _seed_summary(db_session, ik, "a", "OpenAI 递表", "递表")
    await _seed_summary(db_session, ik, "b", "OpenAI IPO 获放行", "放行")
    r = await engine.thread_domain(db_session, get_settings(), interest_key=ik, run_id=None, now=NOW)
    assert (r["new"], r["linked"]) == (1, 1)  # 第二簇靠 A 降级并入
    links = await _links(db_session, ik)
    assert "subject" in {link.link_reason for link in links}  # 降级挂载留痕


@pytest.mark.asyncio
async def test_same_cluster_multiple_items_threaded_once(db_session, monkeypatch):
    """一簇多 item(多条 summary 指向同 cluster)→ 只归一次,不重复挂载/不触发唯一约束冲突。"""
    ik = "test_thr_multi"
    monkeypatch.setattr(engine, "extract_subject", lambda headline, **k: "OpenAI IPO")
    monkeypatch.setattr(engine, "judge_line", lambda **k: (0, 0.9))
    cid = f"clt_test_{ik}_shared"
    for suffix, headline in [("i1", "OpenAI 递表 报道A"), ("i2", "OpenAI 递表 报道B")]:
        iid = await repo.upsert_item(
            db_session, source="ai-test", url=f"https://ex.com/{ik}/{suffix}", title=headline
        )
        if suffix == "i1":
            await repo.create_cluster(
                db_session, cluster_id=cid, first_item_id=iid, title=headline, seen_at=NOW
            )
        await repo.upsert_summary(
            db_session, interest_key=ik, item_id=iid, cluster_id=cid, headline=headline,
            tldr_raw="x", tldr_rendered="x", insight_raw="x", insight_rendered="x", status="ok",
            used_source_ids=[], unresolved=[], suspect=[], backend="api", model="x", run_id=None,
        )
    r = await engine.thread_domain(db_session, get_settings(), interest_key=ik, run_id=None, now=NOW)
    assert r["new"] == 1  # 一簇 → 一条新线
    assert len(await _links(db_session, ik)) == 1  # 只挂一次,不冲突


@pytest.mark.asyncio
async def test_dormant_scan_folds_stale_lines(db_session, monkeypatch):
    """超 dormant_after_days 无进展的 active 线 → 置 dormant。"""
    ik = "test_thr_dormant"
    s = get_settings()
    old = NOW - timedelta(days=s.threads.dormant_after_days + 3)
    await repo.create_thread(
        db_session, thread_id="thr_stale_x", name="旧线", subject="Old Story", domain=ik,
        summary="很久没动了", seen_at=old, heat=1,
    )
    monkeypatch.setattr(engine, "extract_subject", lambda headline, **k: "Fresh Story")
    await engine.thread_domain(db_session, s, interest_key=ik, run_id=None, now=NOW)
    stale = (await db_session.execute(select(Thread).where(Thread.thread_id == "thr_stale_x"))).scalar_one()
    assert stale.status == "dormant"


@pytest.mark.asyncio
async def test_touch_thread_updates_name_to_latest(db_session):
    """2026-06-15 二②:touch_thread 传 name → 线名刷成最新簇 headline(标题随现状);
    不传 name 时保持原名(向后兼容)。"""
    await repo.create_thread(
        db_session, thread_id="thr_rename_x", name="最老的标题", subject="story",
        domain="ai", summary="旧现状", seen_at=NOW, heat=1,
    )
    await repo.touch_thread(
        db_session, thread_id="thr_rename_x", seen_at=NOW, summary="新现状",
        name="最新的标题", heat_delta=1,
    )
    t = (await db_session.execute(
        select(Thread).where(Thread.thread_id == "thr_rename_x"))).scalar_one()
    assert t.name == "最新的标题" and t.summary == "新现状"
    await repo.touch_thread(  # name=None → 不覆盖线名
        db_session, thread_id="thr_rename_x", seen_at=NOW, summary="再更新", heat_delta=1)
    t2 = (await db_session.execute(
        select(Thread).where(Thread.thread_id == "thr_rename_x"))).scalar_one()
    assert t2.name == "最新的标题"


@pytest.mark.asyncio
async def test_subject_failure_counted(db_session, monkeypatch):
    """2026-06-15 二③:抽主体失败被计数(不再静默丢),供 run_threads 据失败率告警。"""
    ik = "test_thr_subjfail"

    def _boom(headline, **k):
        raise RuntimeError("flash 抽风返回空")

    monkeypatch.setattr(engine, "extract_subject", _boom)
    await _seed_summary(db_session, ik, "a", "标题A", "t")
    await _seed_summary(db_session, ik, "b", "标题B", "t")
    r = await engine.thread_domain(db_session, get_settings(), interest_key=ik, run_id=None, now=NOW)
    assert r["subj_failed"] == 2 and r["new"] == 0 and r["linked"] == 0


@pytest.mark.asyncio
async def test_run_threads_alerts_on_high_subject_failure(monkeypatch):
    """2026-06-15 二③:抽主体失败率≥阈值 → run_threads 发告警(「在追」静默退化可见)。"""
    from sqlalchemy.exc import InterfaceError, OperationalError
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    settings = get_settings()
    eng = create_async_engine(settings.database.async_dsn, poolclass=NullPool)
    try:
        conn = await eng.connect()
        await conn.close()
    except (OperationalError, InterfaceError, ConnectionError, OSError):
        await eng.dispose()
        pytest.skip("数据库不可用,跳过")
    sm = async_sessionmaker(eng, expire_on_commit=False)

    async def _fake_domain(session, settings, *, interest_key, run_id, now):
        return {"new": 1, "linked": 0, "skipped": 0, "dormant": 0, "subj_failed": 4}  # 4/5=80%

    alerts: list[dict] = []

    async def _rec(_settings, **kw):
        alerts.append(kw)
        return {}

    monkeypatch.setattr(engine, "thread_domain", _fake_domain)
    monkeypatch.setattr(engine, "alert_failure", _rec)
    try:
        r = await engine.run_threads(
            settings, interest_keys=["ai"], run_id="daily_20260615", sessionmaker=sm)
        assert r["subj_failed"] == 4
        assert len(alerts) == 1 and "subject_extraction" in alerts[0]["stage"]
    finally:
        await eng.dispose()


# ---------- step 5:前端「在追」取数 get_threads_for_display ---------- #
async def _seed_linked(session, *, tid, name, domain, heat, points):
    """造一条线 + 按 points=[(suffix, headline, date)] 挂载簇,带耐久落痕(headline/url/source/progress_date)。"""
    await repo.create_thread(
        session, thread_id=tid, name=name, subject=name.lower(), domain=domain,
        summary=f"{name} 现状", seen_at=NOW, heat=heat,
    )
    for suffix, headline, date in points:
        cid = await _seed_summary(session, domain, f"{tid}_{suffix}", headline, headline)
        await repo.link_cluster_to_thread(
            session, thread_id=tid, cluster_id=cid, run_id=None,
            subject=name.lower(), link_reason="judge", confidence=0.9,
            headline=headline, url=f"https://ex.com/{cid}", source="test-src", progress_date=date,
        )


@pytest.mark.asyncio
async def test_get_threads_for_display_threshold_order_and_timeline(db_session):
    """在追取数:跨 >=min_days 天的线出、单天线被门槛挡;按 heat 降序;时间轴新到旧、日期/文案取自落痕。"""
    await _seed_linked(
        db_session, tid="thr_disp_multi", name="OpenAI IPO", domain="ai", heat=5,
        points=[("a", "OpenAI 递交上市申请", "2026-06-11"),
                ("b", "OpenAI IPO 获放行", "2026-06-13")],
    )
    await _seed_linked(
        db_session, tid="thr_disp_hot", name="伊朗 冲突", domain="geo", heat=9,
        points=[("a", "冲突爆发", "2026-06-11"),
                ("b", "停火谈判", "2026-06-14")],
    )
    await _seed_linked(
        db_session, tid="thr_disp_single", name="单日小新闻", domain="ai", heat=3,
        points=[("a", "只有今天", "2026-06-14")],  # 仅 1 天 → 被门槛挡
    )

    rows = await repo.get_threads_for_display(db_session, min_days=2, tz_name="Asia/Shanghai")
    mine = [r for r in rows if r["thread_id"].startswith("thr_disp_")]
    ids = [r["thread_id"] for r in mine]
    assert "thr_disp_single" not in ids  # 单日线被门槛挡
    assert ids == ["thr_disp_hot", "thr_disp_multi"]  # heat 降序:9 在 5 前

    multi = next(r for r in mine if r["thread_id"] == "thr_disp_multi")
    assert multi["days"] == 2 and multi["domain"] == "ai"
    assert [p["date"] for p in multi["timeline"]] == ["2026-06-13", "2026-06-11"]  # 新到旧
    assert multi["timeline"][0]["headline"] == "OpenAI IPO 获放行"  # 最新一条
    assert multi["timeline"][0]["url"] and multi["timeline"][0]["source"]  # 带原文链接/来源


@pytest.mark.asyncio
async def test_get_active_thread_cluster_map(db_session):
    """持续关注徽标基础:active 线的 cluster_id→thread_id 全收;dormant 线的簇不收。"""
    await _seed_linked(
        db_session, tid="thr_map_active", name="活跃线", domain="ai", heat=5,
        points=[("a", "进展1", "2026-06-11"), ("b", "进展2", "2026-06-13")],
    )
    # 休眠线:last_seen 设旧,再 mark_dormant 置休(seed 默认 NOW 标不动)
    await repo.create_thread(
        db_session, thread_id="thr_map_dormant", name="休眠线", subject="x",
        domain="ai", summary="", seen_at=NOW - timedelta(days=30), heat=1,
    )
    dcid = await _seed_summary(db_session, "ai", "dormant_c", "旧进展", "旧进展")
    await repo.link_cluster_to_thread(
        db_session, thread_id="thr_map_dormant", cluster_id=dcid, run_id=None,
        subject="x", link_reason="judge", confidence=0.9,
        headline="旧进展", url="https://ex", source="s", progress_date="2026-05-15",
    )
    assert await repo.mark_dormant_threads(db_session, domain="ai", before=NOW) >= 1

    m = await repo.get_active_thread_cluster_map(db_session)
    active_clusters = [c for c, t in m.items() if t == "thr_map_active"]
    assert len(active_clusters) == 2  # 活跃线两簇都在
    assert dcid not in m and all(t != "thr_map_dormant" for t in m.values())  # 休眠线的簇不收


def test_progress_date_prefers_run_id_then_linked_at():
    """进展日期:run_id(daily_YYYYMMDD)优先;异常退回 linked_at 本地日期;全空兜底空串。"""
    from zoneinfo import ZoneInfo

    from pulsewire.store.repo import _progress_date

    tz = ZoneInfo("Asia/Shanghai")
    assert _progress_date("daily_20260613", None, tz) == "2026-06-13"
    # run_id 异常 → 退回 linked_at 的本地日期(UTC 18:00 → UTC+8 次日 02:00)
    dt = datetime(2026, 6, 13, 18, 0, tzinfo=timezone.utc)
    assert _progress_date("weird", dt, tz) == "2026-06-14"
    assert _progress_date(None, None, tz) == ""


# ---------- step 4:--rebuild 从归档重放 ---------- #
@pytest.mark.asyncio
async def test_clear_threads_empties_both_tables(db_session):
    """clear_threads 清空 threads + thread_clusters(重建前的清场)。"""
    await _seed_linked(
        db_session, tid="thr_clr", name="待清线", domain="ai", heat=1,
        points=[("a", "h1", "2026-06-11"), ("b", "h2", "2026-06-13")],
    )
    nlinks, nthreads = await repo.clear_threads(db_session)
    assert nlinks >= 2 and nthreads >= 1
    left_t = (await db_session.execute(select(func.count()).select_from(Thread))).scalar()
    left_l = (await db_session.execute(select(func.count()).select_from(ThreadCluster))).scalar()
    assert left_t == 0 and left_l == 0


def test_archive_domain_mapping_and_records():
    """归档领域映射:tr0→ai/tr2→bio/tr3→geo,github(tr1)跳过;_records_for_day 摊平并跳空标题/未知域。"""
    from pulsewire.threads.rebuild import _archive_domain_to_short, _records_for_day

    assert _archive_domain_to_short("tr0", "AI 领域") == "ai"
    assert _archive_domain_to_short("tr1", "GitHub 开源生态") is None  # github 不归线
    assert _archive_domain_to_short("tr2", "生物医疗工程") == "bio"
    assert _archive_domain_to_short("tr3", "国际局势") == "geo"
    assert _archive_domain_to_short("geo", "国际局势") == "geo"

    short_to_ik = {"ai": "ik_ai", "bio": "ik_bio", "geo": "ik_geo"}
    data = {"domains": [
        {"key": "tr0", "label": "AI 领域", "items": [
            {"headline": "OpenAI IPO", "tldr": "t", "url": "u1", "source": "s1"},
            {"headline": "", "tldr": "空标题跳过"},  # 跳过
        ]},
        {"key": "tr1", "label": "GitHub 开源生态", "items": [{"headline": "某仓库", "tldr": "t"}]},  # 整域跳过
        {"key": "tr3", "label": "国际局势", "items": [{"headline": "冲突", "tldr": "t", "url": "u2"}]},
    ]}
    recs = _records_for_day(data, short_to_ik)
    assert [r["ik"] for r in recs] == ["ik_ai", "ik_geo"]  # github 域 + 空标题被过滤
    assert recs[0]["headline"] == "OpenAI IPO" and recs[0]["url"] == "u1"


@pytest.mark.asyncio
async def test_rebuild_preflight_aborts_when_flash_sick(monkeypatch):
    """2026-06-15 二⑦:flash 抽风(大批抽主体失败)→ 重建预检在清表前抛,旧线不动。"""
    from pulsewire.threads import rebuild as rb

    async def _all_fail(recs, settings, **k):
        return [None] * len(recs)  # 全失败 = flash 抽风

    monkeypatch.setattr(rb, "_extract_subjects", _all_fail)
    days = [("2026-06-10", {"domains": [{"key": "tr0", "label": "AI 领域",
            "items": [{"headline": "h1", "tldr": "t"}, {"headline": "h2", "tldr": "t"}]}]})]
    with pytest.raises(RuntimeError, match="预检失败"):
        await rb._preflight_flash_health(days, {"ai": "int_ai"}, get_settings())


@pytest.mark.asyncio
async def test_rebuild_preflight_passes_when_flash_healthy(monkeypatch):
    """flash 健康(主体抽得出)→ 预检放行,不抛。"""
    from pulsewire.threads import rebuild as rb

    async def _all_ok(recs, settings, **k):
        return ["某主体"] * len(recs)

    monkeypatch.setattr(rb, "_extract_subjects", _all_ok)
    days = [("2026-06-10", {"domains": [{"key": "tr0", "label": "AI 领域",
            "items": [{"headline": "h1", "tldr": "t"}]}]})]
    await rb._preflight_flash_health(days, {"ai": "int_ai"}, get_settings())  # 不抛即通过


def test_load_archive_days_sorts_and_limits(tmp_path):
    """_load_archive_days 按日期升序、--days 取最近 N;非日期名/坏 JSON 跳过。"""
    import json as _json

    from pulsewire.threads.rebuild import _load_archive_days

    daily = tmp_path / "daily"
    daily.mkdir()
    for d in ["2026-06-10", "2026-06-12", "2026-06-11"]:
        (daily / f"{d}.json").write_text(_json.dumps({"domains": []}), encoding="utf-8")
    (daily / "index.json").write_text("{}", encoding="utf-8")  # 非日期名,跳过
    (daily / "2026-06-09.json").write_text("{坏 json", encoding="utf-8")  # 坏档,跳过

    alld = _load_archive_days(tmp_path, None)
    assert [d for d, _ in alld] == ["2026-06-10", "2026-06-11", "2026-06-12"]  # 升序,坏档/非日期剔除
    recent = _load_archive_days(tmp_path, 2)
    assert [d for d, _ in recent] == ["2026-06-11", "2026-06-12"]  # 最近 2 天
