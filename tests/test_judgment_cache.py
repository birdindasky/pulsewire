"""判决缓存(S1 / f20)测试:哈希键 + 水货闸缓存命中/未命中 + repo 读写幂等。

核心:同内容 + 同 prompt 上一轮判过 → 直接读裁决不调 LLM(省钱);prompt 改则失效隔离。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pulsewire.config import get_settings
from pulsewire.events.judge_cache import hash_input, hash_prompt, prompt_hash_of


def _dom(key="ai", label="人工智能", interest="AI 前沿", tags=None):
    return SimpleNamespace(key=key, label=label, interest=interest, tags=tags or [])


# ---------------- 哈希键 ---------------- #
def test_hash_input_deterministic_and_distinct():
    assert hash_input("abc") == hash_input("abc")
    assert hash_input("abc") != hash_input("abd")
    assert len(hash_input("x")) == 64


def test_hash_prompt_16_and_invalidates_on_change():
    assert len(hash_prompt("prompt v1")) == 16
    assert hash_prompt("prompt v1") != hash_prompt("prompt v2")  # prompt 改 → 键变 → 失效


def test_prompt_hash_folds_model_and_tokens():
    """失效键含模型/口径(考官 2026-07-03 指出):换判官模型(flash→pro)→ 键变 → 旧裁决自然失效。"""
    from pulsewire.events import magnitude_judge as mj

    base = get_settings()
    assert len(mj.magnitude_prompt_hash(base)) == 16
    assert mj.magnitude_prompt_hash(base) == mj.magnitude_prompt_hash(base)  # 确定性
    m_a = base.model_copy(update={"threads": base.threads.model_copy(update={"judge_model": "model-a"})})
    m_b = base.model_copy(update={"threads": base.threads.model_copy(update={"judge_model": "model-b"})})
    assert mj.magnitude_prompt_hash(m_a) != mj.magnitude_prompt_hash(m_b)  # 换模型 → 键变
    tok = base.model_copy(update={"threads": base.threads.model_copy(update={"judge_max_tokens": 999})})
    assert mj.magnitude_prompt_hash(tok) != mj.magnitude_prompt_hash(base)  # 换 max_tokens → 键变


# ---------------- 水货闸:缓存命中不调 LLM ---------------- #
def test_water_judge_cache_hit_skips_llm(monkeypatch):
    from pulsewire.events import magnitude_judge as mj

    calls = []

    def _spy(*a, **k):
        calls.append(1)
        return (True, "不该被调用")

    monkeypatch.setattr(mj, "judge_is_water", _spy)
    ev = {"headline": "某AI新品发布", "snippet": "正文"}
    ihash = mj.magnitude_item_hash(ev)
    cache = {ihash: {"water": True}}  # 预载:上轮判过=水货
    judge = mj.make_water_judge(get_settings(), judgment_cache=cache)
    assert judge(ev) is True   # 直接读缓存
    assert calls == []         # LLM 一次没调


def test_water_judge_cache_miss_records_verdict(monkeypatch):
    from pulsewire.events import magnitude_judge as mj

    monkeypatch.setattr(mj, "judge_is_water", lambda *a, **k: (True, "water"))
    ev = {"headline": "某AI新品发布", "snippet": "正文"}
    new_verdicts: list = []
    judge = mj.make_water_judge(get_settings(), judgment_cache={}, new_verdicts=new_verdicts)
    assert judge(ev) is True
    assert len(new_verdicts) == 1
    rec = new_verdicts[0]
    assert rec["item_hash"] == mj.magnitude_item_hash(ev)
    assert rec["judge_name"] == "magnitude"
    assert rec["prompt_hash"] == mj.magnitude_prompt_hash(get_settings())
    assert rec["verdict"] == {"water": True}


def test_water_judge_no_cache_args_unchanged(monkeypatch):
    """不传缓存参数(默认关路径)→ 行为与原来一致,照常调 LLM、不记录。"""
    from pulsewire.events import magnitude_judge as mj

    calls = []
    monkeypatch.setattr(mj, "judge_is_water", lambda *a, **k: (calls.append(1), (False, ""))[1])
    judge = mj.make_water_judge(get_settings())
    assert judge({"headline": "x", "snippet": "y"}) is False
    assert len(calls) >= 1  # 照常调了 LLM


# ---------------- repo 读写幂等(DB)---------------- #
@pytest.mark.asyncio
async def test_judgment_repo_roundtrip_idempotent():
    from sqlalchemy import delete
    from sqlalchemy.exc import InterfaceError, OperationalError
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from pulsewire.store import get_cached_judgments, upsert_judgments
    from pulsewire.store.tables import Judgment

    settings = get_settings()
    engine = create_async_engine(settings.database.async_dsn, poolclass=NullPool)
    try:
        conn = await engine.connect()
        await conn.close()
    except (OperationalError, InterfaceError, ConnectionError, OSError):
        await engine.dispose()
        pytest.skip("数据库不可用,跳过")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    ih = hash_input("s1-repo-test-unique-xyz")
    try:
        async with sm() as s:
            async with s.begin():
                await upsert_judgments(s, [{
                    "item_hash": ih, "judge_name": "magnitude",
                    "prompt_hash": "ph1", "verdict": {"water": True},
                }])
                # 同键再写(裁决不同)→ on_conflict_do_nothing,不炸、不覆盖
                await upsert_judgments(s, [{
                    "item_hash": ih, "judge_name": "magnitude",
                    "prompt_hash": "ph1", "verdict": {"water": False},
                }])
        async with sm() as s:
            got = await get_cached_judgments(
                s, judge_name="magnitude", prompt_hash="ph1", item_hashes=[ih])
            assert got == {ih: {"water": True}}  # 保留首次(幂等 do_nothing)
            # 不同 prompt_hash → 查不到(prompt 改=失效隔离)
            assert await get_cached_judgments(
                s, judge_name="magnitude", prompt_hash="ph2", item_hashes=[ih]) == {}
            # 空 item_hashes → 空 dict(不查库)
            assert await get_cached_judgments(
                s, judge_name="magnitude", prompt_hash="ph1", item_hashes=[]) == {}
    finally:
        async with sm() as s:
            async with s.begin():
                await s.execute(delete(Judgment).where(Judgment.item_hash == ih))
        await engine.dispose()


# ==================== S1 扩展:其余 4 道判官的缓存 ==================== #

# ---------------- 共享失效键工具 ---------------- #
def test_prompt_hash_of_folds_all_dims():
    base = prompt_hash_of("SYS", model="m", max_tokens=100, votes=1)
    assert len(base) == 16
    assert prompt_hash_of("SYS", model="m", max_tokens=100, votes=1) == base  # 确定性
    assert prompt_hash_of("SYS2", model="m", max_tokens=100, votes=1) != base  # 改 prompt
    assert prompt_hash_of("SYS", model="m2", max_tokens=100, votes=1) != base  # 改模型
    assert prompt_hash_of("SYS", model="m", max_tokens=999, votes=1) != base   # 改 tokens
    assert prompt_hash_of("SYS", model="m", max_tokens=100, votes=3) != base   # 改票数
    assert prompt_hash_of("SYS", model="m", max_tokens=100, votes=1, extra="e") != base  # 改 extra


# ---------------- same_event(47% 大头):对称键 + 只缓存干净裁决 ---------------- #
def test_same_event_item_hash_symmetric():
    from pulsewire.events import cluster
    a = {"subject": "s1", "headline": "h1", "snippet": "b1"}
    b = {"subject": "s2", "headline": "h2", "snippet": "b2"}
    assert cluster.same_event_item_hash(a, b) == cluster.same_event_item_hash(b, a)  # 对称
    c = {"subject": "s3", "headline": "h3", "snippet": "b3"}
    assert cluster.same_event_item_hash(a, b) != cluster.same_event_item_hash(a, c)  # 内容不同→键不同


def test_same_event_prompt_hash_folds_model():
    from pulsewire.events import cluster
    base = get_settings()
    m2 = base.model_copy(update={"threads": base.threads.model_copy(update={"judge_model": "other"})})
    assert cluster.same_event_prompt_hash(base) != cluster.same_event_prompt_hash(m2)


def test_same_event_verdict_none_on_dirty(monkeypatch):
    """脏返回(无 same 字段)→ None:调用方保守不合 + **绝不缓存**(不让一次抽风被记成永久'不同')。"""
    from pulsewire.events import cluster
    monkeypatch.setattr("pulsewire.threads.llm.complete_json", lambda *a, **k: "irrelevant")
    a = {"subject": "s", "headline": "h", "snippet": "b"}
    monkeypatch.setattr(cluster, "parse_json", lambda _x: {})  # 无 same 字段
    assert cluster.judge_same_event_verdict(a, a, settings=get_settings()) is None
    monkeypatch.setattr(cluster, "parse_json", lambda _x: {"same": True})  # 干净裁决
    assert cluster.judge_same_event_verdict(a, a, settings=get_settings()) is True
    monkeypatch.setattr(cluster, "parse_json", lambda _x: {"same": False})
    assert cluster.judge_same_event_verdict(a, a, settings=get_settings()) is False


# ---------------- worthiness(默认踢,fail-safe 留)---------------- #
def test_worthiness_cache_hit_skips_llm(monkeypatch):
    from pulsewire.events import worthiness_judge as wj
    calls = []
    monkeypatch.setattr(wj, "judge_is_worthy", lambda *a, **k: (calls.append(1), (True, ""))[1])
    ev = {"headline": "某公司发布新模型", "snippet": "正文"}
    ih = wj.worthiness_item_hash(ev)
    judge = wj.make_worthiness_judge(get_settings(), judgment_cache={ih: {"worthy": False}})
    assert judge(ev) is False   # 命中缓存:不够格(踢)
    assert calls == []          # LLM 一次没调


def test_worthiness_cache_miss_records(monkeypatch):
    from pulsewire.events import worthiness_judge as wj
    monkeypatch.setattr(wj, "judge_is_worthy", lambda *a, **k: (False, "小众"))  # 判不够格
    ev = {"headline": "某小众论文", "snippet": "正文"}
    nv: list = []
    judge = wj.make_worthiness_judge(get_settings(), judgment_cache={}, new_verdicts=nv)
    assert judge(ev) is False
    assert len(nv) == 1 and nv[0]["judge_name"] == "worthiness"
    assert nv[0]["item_hash"] == wj.worthiness_item_hash(ev)
    assert nv[0]["verdict"] == {"worthy": False}


# ---------------- topic(board-相关键)---------------- #
def test_topic_item_hash_board_dependent():
    from pulsewire.events import topic_judge as tj
    ev = {"headline": "h", "subject": "s", "snippet": "b", "representative_source": "src"}
    ai, bio = _dom("ai", "人工智能"), _dom("bio", "生物医药")
    assert tj.topic_item_hash(ai, ev, None) != tj.topic_item_hash(bio, ev, None)  # 换板→键变
    assert tj.topic_item_hash(ai, ev, "画像X") != tj.topic_item_hash(ai, ev, None)  # 换画像→键变


def test_topic_cache_hit_skips_llm(monkeypatch):
    from pulsewire.events import topic_judge as tj
    calls = []
    monkeypatch.setattr(tj, "judge_off_topic", lambda *a, **k: (calls.append(1), (False, ""))[1])
    ev = {"headline": "电视剧群星", "subject": "群星", "snippet": "b", "representative_source": "src"}
    ai = _dom("ai", "人工智能")
    settings = get_settings()
    # 工厂用**配置里的画像**算键(portraits.get(d.key)),测试须同口径,否则键不一致=假 miss。
    portrait = (settings.rank.event_pool.topic_portraits or {}).get("ai")
    ih = tj.topic_item_hash(ai, ev, portrait)
    for_board = tj.make_topic_judge(settings, judgment_cache={ih: {"off_topic": True}})
    assert for_board(ai)(ev) is True   # 命中缓存:跑题(踢)
    assert calls == []


# ---------------- board(丢弃方向:只缓存干净裁决,dirty 不记)---------------- #
def test_board_cache_hit_skips_llm(monkeypatch):
    from pulsewire.events import board_classifier as bc
    calls = []
    monkeypatch.setattr(bc, "classify_board",
                        lambda *a, **k: (calls.append(1), (None, 0.0, False, ""))[1])
    ev = {"headline": "h", "subject": "s", "snippet": "b"}
    ih = bc.board_item_hash(ev)
    clf = bc.make_board_classifier(get_settings(), [_dom("ai")], judgment_cache={ih: {"board": "ai"}})
    assert clf(ev) == "ai"     # 命中缓存:归 ai 板
    assert calls == []


def test_board_clean_drop_is_cached(monkeypatch):
    from pulsewire.events import board_classifier as bc
    monkeypatch.setattr(bc, "classify_board", lambda *a, **k: (None, 0.0, False, "other"))  # 真判 other
    ev = {"headline": "纯财经", "subject": "s", "snippet": "b"}
    nv: list = []
    clf = bc.make_board_classifier(get_settings(), [_dom("ai")], judgment_cache={}, new_verdicts=nv)
    assert clf(ev) is None                       # 真丢弃
    assert len(nv) == 1 and nv[0]["verdict"] == {"board": None}  # 干净丢弃→缓存


def test_board_dirty_drop_not_cached(monkeypatch):
    """LLM 故障导致的丢弃**绝不缓存**——否则本该归板的真事件被永久误杀(丢弃是可见性损失方向)。"""
    from pulsewire.events import board_classifier as bc

    def _boom(*a, **k):
        raise RuntimeError("llm down")

    monkeypatch.setattr(bc, "classify_board", _boom)
    ev = {"headline": "本该归 AI 板的真事件", "subject": "s", "snippet": "b"}
    nv: list = []
    clf = bc.make_board_classifier(get_settings(), [_dom("ai")], judgment_cache={}, new_verdicts=nv)
    assert clf(ev) is None    # 故障→丢
    assert nv == []           # dirty→不缓存(下轮重判,不永久误杀)


# ---------------- 回归:预算耗尽/故障的裁决绝不缓存(考官 2026-07-03 逮到 HIGH)---------------- #
def _with_ep(settings, **kw):
    ep = settings.rank.event_pool.model_copy(update=kw)
    return settings.model_copy(update={"rank": settings.rank.model_copy(update={"event_pool": ep})})


def test_worthiness_budget_exhaustion_not_cached(monkeypatch):
    """考官 HIGH:预算饿死→fail-closed 踢(可见损失),绝不能缓存,否则真够格新闻被永久误杀。"""
    from pulsewire.events import worthiness_judge as wj
    s = _with_ep(get_settings(), max_worthiness_judges_per_run=0)  # 预算=0,一票没投
    monkeypatch.setattr(wj, "judge_is_worthy", lambda *a, **k: (True, ""))  # 真判会"留",但预算挡死
    nv: list = []
    judge = wj.make_worthiness_judge(s, judgment_cache={}, new_verdicts=nv)
    assert judge({"headline": "某公司发布真新闻", "snippet": "正文"}) is False  # 饿死→踢
    assert nv == [], "预算饿死的踢绝不能进缓存(否则永久误杀真新闻)"


def test_worthiness_llm_failure_not_cached(monkeypatch):
    """LLM 故障兜底(留)也不缓存——统一'只缓存真判'不变式。"""
    from pulsewire.events import worthiness_judge as wj

    def _boom(*a, **k):
        raise RuntimeError("llm down")

    monkeypatch.setattr(wj, "judge_is_worthy", _boom)
    nv: list = []
    judge = wj.make_worthiness_judge(get_settings(), judgment_cache={}, new_verdicts=nv)
    judge({"headline": "h", "snippet": "s"})
    assert nv == [], "故障兜底票不是真判 → 不缓存"


def test_magnitude_budget_exhaustion_not_cached(monkeypatch):
    from pulsewire.events import magnitude_judge as mj
    s = _with_ep(get_settings(), max_water_judges_per_run=0)
    monkeypatch.setattr(mj, "judge_is_water", lambda *a, **k: (True, ""))
    nv: list = []
    judge = mj.make_water_judge(s, judgment_cache={}, new_verdicts=nv)
    judge({"headline": "h", "snippet": "s"})
    assert nv == [], "预算截断的保守留也不缓存(统一不变式)"


def test_topic_budget_exhaustion_not_cached(monkeypatch):
    from pulsewire.events import topic_judge as tj
    s = _with_ep(get_settings(), max_topic_judges_per_run=0)
    monkeypatch.setattr(tj, "judge_off_topic", lambda *a, **k: (True, ""))
    nv: list = []
    for_board = tj.make_topic_judge(s, judgment_cache={}, new_verdicts=nv)
    d = _dom("ai")
    for_board(d)({"headline": "h", "subject": "s", "snippet": "b", "representative_source": "src"})
    assert nv == [], "预算截断的保守 KEEP 也不缓存(统一不变式)"


# ---------------- 回归:缓存键锚稳定内容,主体措辞抖不换键(2026-07-04 A/B 逮到)---------------- #
def test_same_event_key_ignores_volatile_subject():
    """主体是 flash 每轮现抽的(天然抖);键含主体=同内容跨轮键变=缓存白建(实测命中仅~15%)。"""
    from pulsewire.events import cluster
    a1 = {"subject": "措辞甲", "headline": "h1", "snippet": "b1"}
    a2 = {"subject": "措辞乙完全不同", "headline": "h1", "snippet": "b1"}
    b = {"subject": "x", "headline": "h2", "snippet": "b2"}
    assert cluster.same_event_item_hash(a1, b) == cluster.same_event_item_hash(a2, b)  # 主体抖→键不变
    a3 = {"subject": "措辞甲", "headline": "h1", "snippet": "内容真变了"}
    assert cluster.same_event_item_hash(a1, b) != cluster.same_event_item_hash(a3, b)  # 内容变→键变


def test_topic_and_board_keys_ignore_volatile_subject():
    from pulsewire.events import board_classifier as bc
    from pulsewire.events import topic_judge as tj
    d = _dom("ai")
    e1 = {"headline": "h", "subject": "甲", "snippet": "b", "representative_source": "s"}
    e2 = {"headline": "h", "subject": "乙", "snippet": "b", "representative_source": "s"}
    assert tj.topic_item_hash(d, e1, None) == tj.topic_item_hash(d, e2, None)
    assert bc.board_item_hash(e1) == bc.board_item_hash(e2)
    e3 = {"headline": "h", "subject": "甲", "snippet": "变了", "representative_source": "s"}
    assert tj.topic_item_hash(d, e1, None) != tj.topic_item_hash(d, e3, None)
    assert bc.board_item_hash(e1) != bc.board_item_hash(e3)


# ---------------- 回归:判官正文截 800(2026-07-05 考官发现①,防全文级 token 放大)---------------- #
def test_judges_truncate_body_and_hash_matches_fed_text(monkeypatch):
    """键=输入铁律:800 字后的尾巴既不该进 LLM,也不该进缓存键(否则全文重抓=键变=缓存白建)。"""
    from pulsewire.events import magnitude_judge as mj
    from pulsewire.events import worthiness_judge as wj

    fed = {}
    monkeypatch.setattr(mj, "complete_json", lambda sys, user, **k: fed.__setitem__('m', user) or '{"water": false}')
    mj.judge_is_water("h", "x" * 5000, get_settings())
    assert len(fed['m']) < 1200, "水货判官该只吃截断后的正文"
    monkeypatch.setattr(wj, "complete_json", lambda sys, user, **k: fed.__setitem__('w', user) or '{"worthy": true}')
    wj.judge_is_worthy("h", "x" * 5000, get_settings())
    assert len(fed['w']) < 1200, "够格判官该只吃截断后的正文"
    # 键只看前 800:尾巴变不换键;前 800 内变才换键
    a = {"headline": "h", "snippet": "x" * 800 + "尾巴A"}
    b = {"headline": "h", "snippet": "x" * 800 + "尾巴B"}
    c = {"headline": "h", "snippet": "y" * 800}
    for f in (mj.magnitude_item_hash, wj.worthiness_item_hash):
        assert f(a) == f(b) and f(a) != f(c)
