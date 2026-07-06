"""适配器解析 + SSRF + file:// 越权 单测(无网络、无数据库)。"""

from __future__ import annotations

import pytest

from pulsewire.config import PROJECT_ROOT
from pulsewire.config.models import SourceType
from pulsewire.fetch.ssrf import SSRFError, assert_http_url_allowed
from pulsewire.sources import get_adapter
from pulsewire.sources.file_src import FilePathError, resolve_file_url
from pulsewire.sources.github import _parse as gh_parse
from pulsewire.sources.hackernews import _parse as hn_parse
from pulsewire.sources.hf_papers import _parse as hf_parse
from pulsewire.sources.ossinsight import _is_ai_repo as oss_is_ai
from pulsewire.sources.ossinsight import _parse_trends as oss_parse_trends
from pulsewire.sources.ossinsight import _repo_to_item as oss_repo_to_item
from pulsewire.sources.rss import _parse as rss_parse

FIXTURE = (PROJECT_ROOT / "tests" / "fixtures" / "sample_feed.xml").read_text(encoding="utf-8")


# ----- RSS ----- #
def test_rss_parse_extracts_item_with_published():
    items = rss_parse(FIXTURE)
    assert len(items) == 1
    it = items[0]
    assert it.url == "https://example.com/a"
    assert it.title == "Sample headline for offline parsing"
    assert it.published_at is not None
    assert it.published_at.year == 2024


def test_rss_parse_skips_entry_without_link_or_title():
    feed = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>no link</title></item>
      <item><link>https://x.com/a</link></item>
      <item><title>ok</title><link>https://x.com/b</link></item>
    </channel></rss>"""
    items = rss_parse(feed)
    assert [i.url for i in items] == ["https://x.com/b"]


def test_rss_parse_prefers_longer_content_encoded():
    """content:encoded 里的全文比 summary 长 → 取全文(Substack/one-useful-thing/fox 同病)。"""
    full = "FULL BODY " * 50
    feed = f"""<?xml version="1.0"?>
    <rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
      <item><title>t</title><link>https://x.com/a</link>
        <description>short teaser</description>
        <content:encoded><![CDATA[{full}]]></content:encoded>
      </item>
    </channel></rss>"""
    items = rss_parse(feed)
    assert len(items) == 1
    assert "FULL BODY" in (items[0].content or "")
    assert len(items[0].content) >= len(full.strip())


def test_rss_parse_keeps_summary_when_no_content_encoded():
    feed = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>t</title><link>https://x.com/a</link><description>only summary</description></item>
    </channel></rss>"""
    items = rss_parse(feed)
    assert items[0].content == "only summary"


def test_rss_parse_link_fallback_guid_url_then_enclosure():
    """megaphone 系播客:无 <link>,guid 是裸 UUID、音频在 enclosure → 逐级兜底,别丢条目。"""
    feed = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>guid is url</title>
        <guid isPermaLink="true">https://pod.example/ep1</guid></item>
      <item><title>bare uuid guid, has enclosure</title>
        <guid isPermaLink="false">fb9d2daa-719d-11f1</guid>
        <enclosure url="https://traffic.megaphone.fm/EP2.mp3" type="audio/mpeg" length="1"/></item>
      <item><title>nothing usable</title>
        <guid isPermaLink="false">just-a-uuid</guid></item>
    </channel></rss>"""
    items = rss_parse(feed)
    assert [i.url for i in items] == [
        "https://pod.example/ep1",
        "https://traffic.megaphone.fm/EP2.mp3",
    ]


def test_rss_parse_fallback_date_format_fierce():
    """Fierce 系非 RFC822 日期『Jun 24, 2026 3:33pm』:feedparser 挂 → 兜底格式接住(按 UTC)。"""
    feed = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>t</title><link>https://x.com/a</link>
        <pubDate>Jun 24, 2026 3:33pm</pubDate></item>
    </channel></rss>"""
    items = rss_parse(feed)
    assert len(items) == 1
    dt = items[0].published_at
    assert dt is not None
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 6, 24, 15, 33)


def test_rss_parse_unparseable_date_stays_none():
    """认不出的日期 → None(交上游按无日期处理),绝不编时间。"""
    feed = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>t</title><link>https://x.com/a</link>
        <pubDate>someday soon</pubDate></item>
    </channel></rss>"""
    items = rss_parse(feed)
    assert items[0].published_at is None


# ----- HackerNews (Algolia) ----- #
def test_hn_parse_points_and_url_fallback():
    payload = """{"hits":[
      {"objectID":"1","title":"Story A","url":"https://a.com","points":120,"num_comments":33,"created_at_i":1700000000},
      {"objectID":"2","title":"Ask HN: B","points":5,"num_comments":1,"created_at_i":1700000100}
    ]}"""
    items = hn_parse(payload)
    assert len(items) == 2
    assert items[0].url == "https://a.com"
    assert items[0].facts["hn"]["points"] == 120
    # 无外链 → 指向 HN 讨论页
    assert items[1].url == "https://news.ycombinator.com/item?id=2"
    assert items[0].published_at is not None


# ----- GitHub ----- #
def test_github_parse_stars_and_pushed_at():
    payload = """{"items":[
      {"full_name":"a/b","html_url":"https://github.com/a/b","description":"d",
       "stargazers_count":900,"forks_count":12,"pushed_at":"2024-05-01T00:00:00Z"}
    ]}"""
    items = gh_parse(payload)
    assert len(items) == 1
    assert items[0].title == "a/b"
    assert items[0].facts["github"]["stars"] == 900
    assert items[0].published_at.year == 2024


# ----- HuggingFace Daily Papers(2026-07-05 卷一) ----- #
def test_hf_parse_maps_paper_fields():
    """url 由 paper.id 拼、title/content/published_at/upvotes 全取 paper.*(一手),不碰顶层。"""
    payload = """[
      {"publishedAt":"2026-06-30T20:00:00.000Z","title":"top-level title (featured page)",
       "paper":{"id":"2607.01283","title":"Paper A","summary":"Abstract A",
                "publishedAt":"2026-07-01T00:00:00.000Z","upvotes":42}},
      {"paper":{"id":"2607.99999","title":"Paper B","summary":"","upvotes":0}}
    ]"""
    items = hf_parse(payload)
    a = items[0]
    assert a.url == "https://huggingface.co/papers/2607.01283"
    assert a.title == "Paper A"  # paper.title,不是顶层精选页 title
    assert a.content == "Abstract A"
    # 一手论文时间 = paper.publishedAt(7-1),不是顶层精选页时间(6-30)
    assert (a.published_at.year, a.published_at.month, a.published_at.day) == (2026, 7, 1)
    assert a.facts["hf"]["upvotes"] == 42
    # 缺 paper.publishedAt 的条目**整条丢弃**(考官 2026-07-05 铁律1:None 交给流水线
    # 会被 trust_published_at 默认路径兜底成抓取时间=假新鲜进榜;与 ossinsight 同纪律)
    assert len(items) == 1


def test_hf_parse_skips_entry_missing_id_or_title():
    payload = """[
      {"paper":{"title":"no id","summary":"x"}},
      {"paper":{"id":"1234.5","summary":"no title"}},
      {"no_paper_key":true},
      {"paper":{"id":"2607.1","title":"ok","summary":"s","publishedAt":"2026-07-01T00:00:00.000Z"}}
    ]"""
    items = hf_parse(payload)
    assert [i.title for i in items] == ["ok"]


def test_hf_parse_empty_and_malformed():
    assert hf_parse("[]") == []
    with pytest.raises(ValueError):  # 畸形 JSON:JSONDecodeError(ValueError 子类)冒泡,单源 fail-loud
        hf_parse("{not json")
    with pytest.raises(ValueError):  # 顶层不是数组(API 结构变了)→ 吵,不静默产空
        hf_parse('{"error":"nope"}')


# ----- OSS Insight 涨速榜(2026-07-05 卷二) ----- #
OSS_TRENDS_PAYLOAD = """{"type":"sql_endpoint","data":{"columns":[{"col":"repo_name"}],"rows":[
  {"repo_name":"langchain-ai/openwiki","description":"CLI that writes agent documentation","primary_language":"TypeScript","stars":"71"},
  {"repo_name":"ZhuLinsen/daily_stock_analysis","description":"stock analysis for traders","primary_language":"Python","stars":"50"},
  {"repo_name":"torvalds/linux","description":"Linux kernel source tree","primary_language":"C","stars":"999"},
  {"repo_name":"acme/vector-lab","description":"An LLM-powered RAG toolkit","primary_language":"Python","stars":"10"},
  {"repo_name":"noslash","description":"repo_name 不带 owner/ 应被丢","stars":"1"}
]}}"""


def test_ossinsight_parse_trends_rows_and_shape_guard():
    rows = oss_parse_trends(OSS_TRENDS_PAYLOAD)
    assert [r["repo_name"] for r in rows] == [
        "langchain-ai/openwiki", "ZhuLinsen/daily_stock_analysis",
        "torvalds/linux", "acme/vector-lab",
    ]  # 保 API 原序(total_score 降序);非 owner/repo 形状的行被丢
    with pytest.raises(ValueError):  # 畸形 JSON
        oss_parse_trends("{broken")
    with pytest.raises(ValueError):  # 结构不对(缺 data.rows)→ 吵,不静默产空
        oss_parse_trends('{"data":{"result":{}}}')
    assert oss_parse_trends('{"data":{"rows":[]}}') == []


def test_ossinsight_ai_filter_word_boundary():
    """词边界:'daily' 里的 ai 子串不算;langchain/agent/LLM/RAG 算;纯内核仓不算。"""
    rows = oss_parse_trends(OSS_TRENDS_PAYLOAD)
    kept = [r["repo_name"] for r in rows if oss_is_ai(r)]
    assert kept == ["langchain-ai/openwiki", "acme/vector-lab"]


GH_REPO_PAYLOAD = {
    "full_name": "acme/vector-lab",
    "html_url": "https://github.com/acme/vector-lab",
    "description": "An LLM-powered RAG toolkit",
    "stargazers_count": 1234,
    "forks_count": 56,
    "created_at": "2026-05-01T00:00:00Z",
    "pushed_at": "2026-07-04T12:00:00Z",
}


def test_ossinsight_repo_to_item_maps_github_fields():
    it = oss_repo_to_item(dict(GH_REPO_PAYLOAD))
    assert it.url == "https://github.com/acme/vector-lab"
    assert it.title == "acme/vector-lab"
    assert it.content == "An LLM-powered RAG toolkit"
    # published_at = 一手 pushed_at(与 github.py 同语义)
    assert (it.published_at.year, it.published_at.month, it.published_at.day) == (2026, 7, 4)
    # facts.github 形状与 github.py 完全一致:榜池 SQL / 涨速 created_at / enrich 零特判
    assert it.facts["github"] == {
        "stars": 1234, "forks": 56, "created_at": "2026-05-01T00:00:00Z",
    }


def test_ossinsight_repo_to_item_drops_dateless_and_fallback_desc():
    dateless = {k: v for k, v in GH_REPO_PAYLOAD.items() if k not in ("pushed_at", "created_at")}
    assert oss_repo_to_item(dateless) is None  # 🔴 无一手日期 → 丢,绝不留给抓取时间兜底
    no_push = {k: v for k, v in GH_REPO_PAYLOAD.items() if k != "pushed_at"}
    assert oss_repo_to_item(no_push).published_at.month == 5  # 缺 pushed_at → 退 created_at
    no_desc = dict(GH_REPO_PAYLOAD, description=None)
    assert oss_repo_to_item(no_desc, fallback_description="from trends").content == "from trends"


class _StubResp:
    def __init__(self, text: str):
        self.text = text
        self.not_modified = False


class _StubClient:
    """按 URL 回罐装响应的假 FetchClient;值为 Exception 时抛出(模拟 404/限速)。"""

    def __init__(self, responses: dict):
        self._responses = responses
        self.calls: list[str] = []

    async def get(self, url: str, **kwargs):
        self.calls.append(url)
        r = self._responses[url]
        if isinstance(r, Exception):
            raise r
        return _StubResp(r)


@pytest.mark.asyncio
async def test_ossinsight_collect_hydrates_skips_failures_and_dateless():
    import json as _json

    from pulsewire.config.models import Source
    from pulsewire.sources.ossinsight import collect as oss_collect

    trends = """{"data":{"rows":[
      {"repo_name":"acme/vector-lab","description":"An LLM toolkit"},
      {"repo_name":"gone/renamed-llm","description":"llm repo that 404s"},
      {"repo_name":"odd/agent-nodate","description":"agent repo, GitHub 响应缺日期"},
      {"repo_name":"boring/spreadsheet","description":"a plain spreadsheet app"}
    ]}}"""
    nodate = {"full_name": "odd/agent-nodate", "html_url": "https://github.com/odd/agent-nodate",
              "stargazers_count": 5, "forks_count": 0}
    src = Source(id="ossinsight-trends-ai", type=SourceType.ossinsight,
                 url="https://oss.test/trends", category="devtools", region="global", lang="en")
    client = _StubClient({
        "https://oss.test/trends": trends,
        "https://api.github.com/repos/acme/vector-lab": _json.dumps(GH_REPO_PAYLOAD),
        "https://api.github.com/repos/gone/renamed-llm": RuntimeError("404 Not Found"),
        "https://api.github.com/repos/odd/agent-nodate": _json.dumps(nodate),
    })
    items = await oss_collect(src, client)
    # 非 AI 仓不回源;404 skip;无日期 skip;只有全须全尾的 vector-lab 产出
    assert [i.title for i in items] == ["acme/vector-lab"]
    assert "https://api.github.com/repos/boring/spreadsheet" not in client.calls
    assert items[0].facts["github"]["stars"] == 1234
    assert items[0].published_at is not None


@pytest.mark.asyncio
async def test_ossinsight_collect_all_hydration_failures_raises():
    """有 AI 候选但回源全军覆没 → 冒泡单源失败,绝不静默产空(源假活=静默死源)。"""
    from pulsewire.config.models import Source
    from pulsewire.sources.ossinsight import collect as oss_collect

    trends = '{"data":{"rows":[{"repo_name":"a/llm-one","description":"llm"}]}}'
    src = Source(id="ossinsight-trends-ai", type=SourceType.ossinsight,
                 url="https://oss.test/trends", category="devtools", region="global", lang="en")
    client = _StubClient({
        "https://oss.test/trends": trends,
        "https://api.github.com/repos/a/llm-one": RuntimeError("rate limited"),
    })
    with pytest.raises(RuntimeError, match="全军覆没"):
        await oss_collect(src, client)


# ----- file:// 解析与越权 ----- #
def test_resolve_file_url_relative_inside_project():
    p = resolve_file_url("file://./tests/fixtures/sample_feed.xml")
    assert p.is_file()
    assert p.name == "sample_feed.xml"


def test_resolve_file_url_blocks_escape():
    with pytest.raises(FilePathError):
        resolve_file_url("file:///etc/passwd")
    with pytest.raises(FilePathError):
        resolve_file_url("file://../../../../etc/passwd")


# ----- SSRF ----- #
def test_ssrf_blocks_non_http_scheme():
    with pytest.raises(SSRFError):
        assert_http_url_allowed("ftp://example.com/x", resolve=False)


def test_ssrf_blocks_private_ip_literal():
    for url in (
        "http://127.0.0.1/x",
        "http://10.0.0.5/x",
        "http://169.254.169.254/latest",  # 云元数据
        "http://[::1]/x",
    ):
        with pytest.raises(SSRFError):
            assert_http_url_allowed(url, resolve=False)


def test_ssrf_allows_public_ip_literal():
    assert_http_url_allowed("http://1.1.1.1/x", resolve=False)  # 不抛即通过


# ----- 注册表 ----- #
def test_registry_resolves_known_types():
    for t in (
        SourceType.rss, SourceType.hackernews, SourceType.github,
        SourceType.hf_papers, SourceType.ossinsight, SourceType.file,
    ):
        assert callable(get_adapter(t))


def test_registry_rejects_unimplemented_type():
    with pytest.raises(NotImplementedError):
        get_adapter(SourceType.reddit)
