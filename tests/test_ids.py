"""确定性 ID 生成测试(无需数据库)。"""

from __future__ import annotations

from pulsewire.store.ids import (
    content_fingerprint,
    make_cluster_id,
    make_item_id,
    make_source_id,
    normalize_url,
)


def test_normalize_url_strips_tracking_and_trailing_slash():
    a = normalize_url("HTTPS://Example.com/Path/?utm_source=x&b=2&a=1#frag")
    b = normalize_url("https://example.com/Path?a=1&b=2")
    assert a == b
    assert "utm_source" not in a
    assert "#frag" not in a


def test_normalize_url_drops_default_port():
    assert normalize_url("https://example.com:443/x") == normalize_url("https://example.com/x")


def test_item_id_deterministic_and_url_invariant():
    id1 = make_item_id("https://example.com/a?utm_source=feed", "Title", "body")
    id2 = make_item_id("https://example.com/a", "Title", "body")
    assert id1 == id2
    assert len(id1) == 32


def test_item_id_changes_with_content():
    assert make_item_id("https://x.com/a", "T1") != make_item_id("https://x.com/a", "T2")


def test_fingerprint_whitespace_insensitive():
    assert content_fingerprint("A  B", "x") == content_fingerprint("A B", " x ")


def test_cluster_id_derived_from_first_item():
    item = make_item_id("https://x.com/a", "T")
    cid = make_cluster_id(item)
    assert cid.startswith("clt_")
    assert item[:16] in cid


def test_source_id_format():
    item = "abc123"
    assert make_source_id(item, "hn", "points") == "abc123:hn:points"
    assert make_source_id(item, "hn", "comment", 3) == "abc123:hn:comment:3"
