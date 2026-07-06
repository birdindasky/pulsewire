"""events.cluster —— A+B 事件聚类(Phase 1a 验过:firm 真值 κ=0.968 上 precision 1.0/recall 0.868)。

A 层(候选,宽松):簇代表抽事件主体短语(复用 threads/subject.py)→ 主体短语向量近邻
  (cosine ≥ CAND_COSINE_FLOOR,top-CAND_TOP_K)∪ 词法 Jaccard ≥ CAND_LEX_JACCARD 圈候选。
B 层(判官,把关):JUDGE_SYS_V2 判"同一底层现实事件";失败/无 key → **保守不合(绝不误合)**。
事件 = A 候选且 B 判同 的簇,并查集合并。

⚠️ **本模块参数是 Phase 1a 校准锁定值,硬编码无 env 覆盖口(回应 codex M2)**——改任一参数 = 偏离
已验证配置,须重过 Phase 1a 校准门(`calibration/`,误合 precision≥0.95 / 漏合 recall≥0.85)。
LLM 成本硬闸由调用方(events.engine)按 max_subject_clusters/max_judge_pairs_per_run 施加。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pulsewire.events.cleantext import clean_text
from pulsewire.events.judge_cache import hash_input, prompt_hash_of
from pulsewire.obs import get_logger
from pulsewire.summarize.backends import parse_json
from pulsewire.threads.subject import cosine, subjects_close

if TYPE_CHECKING:
    from pulsewire.config import Settings

log = get_logger()

# ── Phase 1a 锁定值(勿改;改须重过校准门)──────────────────────────────
CAND_COSINE_FLOOR = 0.55   # A 主体短语向量近邻候选门(校准争议带低端)
CAND_LEX_JACCARD = 0.5     # A 词法 Jaccard 候选门(subjects_close 默认)
CAND_TOP_K = 8             # A 每簇最多取多少近邻为判官候选
CONTENT_TRUNCATE = 500     # 喂判官的干净正文截断(校准 5a 验过)

# 判官 v2(校准 firm 真值上 precision 1.0/recall 0.868 那版;**逐字照搬 calibration/run_ab.py 验过的
# PAIR_JUDGE_SYS,勿改**)。注:不是更保守的原始版(那版 recall 仅 0.755,移植保真门已逮过),也不是
# 花哨 anchor v3(无增益反掉精度)。
JUDGE_SYS_V2 = (
    "你是新闻事件去重编辑。判断两条报道是不是**同一个底层现实事件**(同一次发布/同一笔交易融资/"
    "同一份报告或论文/同一天对同一目标的同一次行动/同一份榜单/同一次访谈)。\n"
    "关键:**同一件事的不同角度、不同措辞、不同侧重、不同语言、甚至标题误导,只要底层是同一次发生,就判同(same=true)**。\n"
    "例:'某公司发债' 与 '某基金投向' 若实为同一笔融资=同;'冲突' 与 '协议' 若是同一桩外交进展的两面=同;\n"
    "同一临床试验(同 NCT 编号/同药/同适应症)的不同报道=同;同一份榜单(同机构同主题)的不同报道=同。\n"
    "只有**确实是不同的现实发生**(不同日期或不同目标的两次行动、两笔不同交易、两个不同产品/repo)才判不同。\n"
    "请看正文摘要里的硬线索(机构名/编号/金额/日期/人物)再定。只输出 JSON:{\"same\":true/false,\"confidence\":0~1}"
)


def _judge_user(a: dict, b: dict) -> str:
    return (
        f"A:\n  主体:{a.get('subject','')}\n  标题:{a.get('headline','')}\n"
        f"  摘要:{(a.get('snippet') or '')[:CONTENT_TRUNCATE]}\n\n"
        f"B:\n  主体:{b.get('subject','')}\n  标题:{b.get('headline','')}\n"
        f"  摘要:{(b.get('snippet') or '')[:CONTENT_TRUNCATE]}\n\n"
        '只输出 JSON:{"same":true/false,"confidence":0~1}'
    )


JUDGE_NAME = "same_event"  # 判决缓存(S1)标识


def same_event_prompt_hash(settings: Settings) -> str:
    """同事件判官失效键 = JUDGE_SYS_V2 + 模型/max_tokens + 候选/截断口径(Phase 1a 锁定值)。

    ⚠️ 折进 CONTENT_TRUNCATE:改了喂判官的正文截断口径,裁决会变,须换 key(与 magnitude 同纪律)。
    votes=1(同事件判官单判,无投票)。工厂与引擎预载须都用本函数算,保证键一致。
    """
    cfg = settings.threads
    return prompt_hash_of(JUDGE_SYS_V2, model=cfg.judge_model,
                          max_tokens=cfg.judge_max_tokens, votes=1,
                          extra=f"trunc={CONTENT_TRUNCATE}")


def _same_key_side(x: dict) -> str:
    """一侧的规范化内容 = **稳定的文章内容**(标题 + 截断正文),**故意不含主体短语**。

    2026-07-04 影子 A/B 实锤:主体是 flash 每轮现抽的、天然抖(温度 0 也漂)——键含主体则同一对
    文章每轮键都变,same_event 缓存命中率仅 ~15%(第二遍 800 对仍新判 679)= 白建。
    "是不是同一件现实事件"取决于文章本身;主体只是喂给 LLM 的派生提示,不进键。
    代价=同文章不同主体措辞下裁决被复用(把 LLM 措辞噪声抹平,反而稳定跨轮选稿)。
    """
    return f"{x.get('headline', '') or ''}\x00{(x.get('snippet') or '')[:CONTENT_TRUNCATE]}"


def same_event_item_hash(a: dict, b: dict) -> str:
    """一对(a,b)的对称内容哈希 = 缓存 item_hash。

    🔴 对称(命门):同事件判官是对称关系(same(a,b)==same(b,a)),两侧内容排序后拼哈希,
    保证 (a,b) 与 (b,a) 命中同一 key(否则同一对判两次、缓存翻倍还可能不一致)。
    与工厂/引擎读同一份内容(_judge_user 口径),预载与判定用本函数算,键必一致。
    """
    ka, kb = _same_key_side(a), _same_key_side(b)
    lo, hi = (ka, kb) if ka <= kb else (kb, ka)
    return hash_input(lo + "\x00\x00" + hi)


def judge_same_event_verdict(a: dict, b: dict, *, settings: Settings) -> bool | None:
    """B 判官(带缓存可判性):返回 True/False(干净裁决)或 **None(脏返回/空,不可缓存)**。

    🔴 只有 LLM 明确返回了 "same" 字段才是真裁决(可缓存);脏返回 / 字段缺失 → None,
    调用方按保守 False(绝不误合)处理、且**不写进缓存**(不让一次抽风的空返回被永久记成"不同")。
    LLM 失败/无 key/PermanentLLMError 由 complete_json 冒泡,调用方兜底。
    """
    from pulsewire.threads.llm import complete_json

    out = parse_json(
        complete_json(
            JUDGE_SYS_V2, _judge_user(a, b),
            model=settings.threads.judge_model,
            max_tokens=settings.threads.judge_max_tokens,
            settings=settings, stage="event_judge",
        )
    )
    if "same" not in out:  # 脏返回/空 → 不可判(None):保守不合 + 绝不缓存这次空返回
        return None
    return bool(out.get("same", False))


def judge_same_event(a: dict, b: dict, *, settings: Settings) -> bool:
    """B 判官:两簇代表是不是同一件现实事件。失败冒泡由调用方兜成保守 False(绝不误合)。

    a/b: {"subject","headline","snippet"(干净正文)}。复用 threads.judge_model。
    脏返回 → False(与 judge_same_event_verdict 的 None 同向:保守不合)。
    """
    v = judge_same_event_verdict(a, b, settings=settings)
    return bool(v)  # None(脏)/False → False(保守不合)


def surface_candidates(
    ids: list[str], subjects: dict[str, str], vecs: dict[str, object],
) -> dict[str, list[str]]:
    """A 层候选圈选(纯函数,可测):每个簇 → 候选簇列表(主体短语向量近邻 ∪ 词法接近,top-K)。

    ids:簇 id 列表(顺序即扫描序,只对 i<j 配对去重)。subjects/vecs:按簇 id 取主体短语 / 主体向量。
    返回 {cluster_id: [候选 cluster_id, ...]}(无向,只存一侧 i<j,避免重复判官)。
    """
    out: dict[str, list[str]] = {i: [] for i in ids}
    for x in range(len(ids)):
        ix = ids[x]
        scored: list[tuple[float, str]] = []
        for y in range(x + 1, len(ids)):
            iy = ids[y]
            lex = subjects_close(subjects.get(ix, ""), subjects.get(iy, ""), CAND_LEX_JACCARD)
            sem = 0.0
            vx, vy = vecs.get(ix), vecs.get(iy)
            if vx is not None and vy is not None:
                sem = cosine(vx, vy)
            if lex or sem >= CAND_COSINE_FLOOR:
                scored.append((1.0 if lex else sem, iy))
        scored.sort(key=lambda t: t[0], reverse=True)
        out[ix] = [iy for _s, iy in scored[:CAND_TOP_K]]
    return out


def clean_snippet(content: str | None) -> str:
    """供调用方:把原始 content 清洗 + 截断到判官口径。"""
    return clean_text(content)[:CONTENT_TRUNCATE]
