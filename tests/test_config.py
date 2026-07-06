"""阶段 0 验收测试:typed config 与信源注册表能正确加载与校验。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pulsewire.config import get_settings, load_sources, source_label
from pulsewire.config.models import RankCfg, Source, SourcesFile


def test_settings_load_and_defaults():
    s = get_settings()
    assert s.app.name == "pulsewire"
    assert s.database.name == "pulsewire"
    # 已锁决策:大事 = 多源汇聚(≥N 源)
    assert s.event.min_sources >= 2
    # 已锁决策:全程 DeepSeek
    assert s.summarize.provider == "deepseek"
    # 已锁决策:永久全留
    assert s.retention.mode == "forever"


def test_rank_engine_live_events_model_default_legacy():
    """2026-06-19 已切上线:live config.yaml=events(全局事件池,/eval 37.5/38 过+用户签字);
    但**模型默认仍 legacy**——回滚安全网(rollback=config.yaml 改回 legacy 重跑,一行);非法值拒绝。"""
    assert get_settings().rank.engine == "events"  # 已切上线(2026-06-19)
    assert RankCfg().engine == "legacy"  # 代码默认仍 legacy,回滚安全网
    assert RankCfg(engine="events").engine == "events"
    with pytest.raises(ValidationError):  # 非法值不静默吞,Pydantic 拒绝
        RankCfg(engine="turbo")


def test_async_dsn_built():
    dsn = get_settings().database.async_dsn
    assert dsn.startswith("postgresql+asyncpg://")
    assert "/pulsewire" in dsn


def test_sources_registry_loads():
    sources = load_sources()
    assert len(sources) >= 1
    assert all(isinstance(s, Source) for s in sources)
    # id 唯一
    ids = [s.id for s in sources]
    assert len(ids) == len(set(ids))


def test_load_sources_backfills_display_name_from_comments():
    """sources.yaml 行内注释回填成 display_name(展示用人类可读名),覆盖率应接近全量。"""
    sources = load_sources()
    with_name = [s for s in sources if s.display_name]
    assert len(with_name) >= len(sources) - 2  # 仅极少数(如 local-fixture)无注释
    s0 = with_name[0]
    assert s0.display_name and s0.display_name != s0.id  # 不是机器 slug 原样


def test_source_label_uses_display_name():
    """有 display_name 的源 → source_label 返回可读名,而非机器 slug。"""
    s = next(x for x in load_sources() if x.display_name)
    assert source_label(s.id) == s.display_name


def test_source_label_humanizes_unknown_slug():
    """未注册/孤儿源 slug → 启发式美化(去 google-news 尾巴、连字符转空格),绝不露机器串。"""
    assert source_label("acme-orphan-via-google-news") == "acme orphan"
    assert source_label("some-unknown-feed") == "some unknown feed"
    assert "-" not in source_label("a-b-c")  # 永不返回带连字符的原始 slug


def test_duplicate_source_id_rejected():
    import pytest

    dup = {
        "sources": [
            {"id": "a", "type": "rss", "url": "http://x"},
            {"id": "a", "type": "rss", "url": "http://y"},
        ]
    }
    with pytest.raises(Exception):
        SourcesFile.model_validate(dup)
