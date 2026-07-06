"""结构化对账:把模型产出的 {Fn} 占位替换成库里的真实数字,核不上的标 [待核实]。

铁律「绝不展示无来源数字」的落地:
- {Fn} 能映射到本条目的事实(来自 facts.enriched,带 source_id)→ 用真实值替换。
- {Fn} 映射不到(模型引用了别条目的/编的 token)→ 替换成 `[待核实]`,记进 unresolved,状态 needs_review。
- 模型若偷偷写了**裸数字**(没走占位)→ 先对照原文:原文(标题+正文)里逐字出现过的数字
  视为有来源(出处=原文,如产品版本号 "Fable 5"/"Tabbit 1.0"),放行;
  原文里没有的数字才是编造嫌疑 → 记进 suspect_numbers,状态 needs_review
  (v1 不自动抹掉,避免误伤;交给报告标注,可后续收紧)。
取代旧版正则挖空:这里是"按 source_id 回库核对",不是猜。

高风险定性断言闸门(2026-06-12 双审计后加):数字对账只兜得住"有数字可对账"的;
"OpenAI 申请上市"这类没有数字 fact 的重大断言会原样溜过。这里按类目(上市/IPO、
倍数营销话术、临床突破、战报伤亡/制裁)做关键词探测,**单源条目命中即 needs_review
(展示层出"待核实"徽标)**;多源同报(corroboration ≥ risk_min_sources)的放行——
真大事必然多源,传闻/营销往往只有一家在说。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pulsewire.summarize.schema import (
    BARE_NUMBER_RE,
    TOKEN_RE,
    FactToken,
    ItemSummary,
)

# 高风险定性断言类目 → 关键词正则。只圈"写成事实会出大事"的强断言措辞,
# 刻意避开日常弱表述(如药物"获批上市的概率"不归 IPO 类、"新突破"不算临床突破)。
RISKY_CLAIM_PATTERNS: list[tuple[str, re.Pattern]] = [
    # 注意刻意不含裸"融资/收购/并购":bio 行业媒体的常规融资报道极少是假传闻,
    # 全标会把徽标刷成噪音(2026-06-12 真数据回放:bio 19 条标 8 条,全是常规融资)。
    # IPO/上市/估值类才是传闻高发区(实锤:"OpenAI 申请上市"假传闻)。
    ("上市/融资", re.compile(
        r"IPO|(申请|提交|秘密|筹备|赴[美港]|挂牌)[^。,;\n]{0,6}上市|上市(申请|计划|传闻)"
        r"|敲钟|估值", re.IGNORECASE)),
    ("倍数话术", re.compile(
        r"翻[倍番]|倍增|[0-9零一两二三四五六七八九十百千几数]+\s*倍")),
    ("临床突破", re.compile(
        r"治愈|根治|攻克|突破性疗法|临床突破")),
    ("战报/制裁", re.compile(
        r"伤亡|阵亡|击毙|击落|击沉|歼灭|空袭|轰炸|封锁|制裁|宣战|停火")),
    # 地缘强断言(2026-06-15 二④):和平"已敲定"、海峡"自由/免费通行"这类把传闻写成既成事实的,
    # 单源即标待核实(多源同报放行)。补 战报/制裁 漏掉的"局势已定"型断言(如霍尔木兹免费通行漏标)。
    ("地缘断言", re.compile(
        r"和平(协议|方案|计划)?[^。,;\n]{0,6}(达成|敲定|签署|生效)|(全面|永久|正式)停火"
        r"|(自由|免费|恢复|开放)[^。,;\n]{0,4}(通航|通行|航行)|(解除|取消|撤销)[^。,;\n]{0,4}(封锁|制裁)")),
    # 对比/超越型断言(2026-06-15 三②):"反超 Ollama""IPO 新王"这类把对比座次写成定论的,
    # 单源即标待核实。确定性兜底层——LLM 审计已能语义抓,这里保证审计宕机时也拦得住(防御纵深)。
    # **只留高辨识度炒作词**:多义的"全球第一""登顶"误伤太多(第一例/第一时间/登山登顶),
    # 那些交给 LLM 审计语义判,关键词层只保精度(2026-06-15 盲验收紧)。
    ("对比断言", re.compile(
        r"反超|碾压|吊打|遥遥领先|霸榜|新(王|霸主|王者)"
        r"|(超越|击败|打败|反杀)[^。,;\n]{0,8}(成为|登顶|夺冠|问鼎|称王)")),
]


def detect_risky_claims(text: str) -> list[str]:
    """探测文本里的高风险定性断言,返回 ["类目:命中词", ...](去重保序)。"""
    found: list[str] = []
    for category, pat in RISKY_CLAIM_PATTERNS:
        for m in pat.finditer(text):
            tag = f"{category}:{m.group(0)}"
            if tag not in found:
                found.append(tag)
    return found


# 裸露脚注标记:LLM 偶尔吐 markdown 脚注语法 [^1],但成稿没有脚注系统,残留在正文里就是乱码
# (2026-06-16 eval 实锤:GitHub 榜 6 条 "复刻[^1]" 裸漏)。渲染前清掉。
FOOTNOTE_RE = re.compile(r"\[\^\d+\]")


def _norm_number(num: str) -> str:
    """数字字符串归一(对照用):去千分位逗号与尾部句点,**保留 %**。

    保留 %:源里有 "50" 而模型写 "50%" 是不同的语义主张(百分比未必有来源),
    不能因剥掉 % 就放行——宁可标待核实。
    """
    return num.replace(",", "").rstrip(".")

NEEDS_REVIEW = "needs_review"
OK = "ok"


@dataclass(slots=True)
class VerifiedItem:
    item_id: str
    headline: str  # 已替换占位
    tldr: str  # 已替换占位(一句话速读)
    insight: str  # 已替换占位(详细白话解读)
    status: str  # ok | needs_review
    used_source_ids: list[str] = field(default_factory=list)  # 实际引用到的、核对通过的来源 id
    unresolved_tokens: list[str] = field(default_factory=list)  # 映射不到的占位
    suspect_numbers: list[str] = field(default_factory=list)  # 未走占位的裸数字(无来源)
    risky_claims: list[str] = field(default_factory=list)  # 单源高风险定性断言(非空 ⟹ needs_review)


def _render(text: str, tokens: dict[str, FactToken], used: dict[str, None],
            unresolved: list[str]) -> str:
    def repl(m):
        tok = m.group(1)
        ft = tokens.get(tok)
        if ft is None:
            if tok not in unresolved:
                unresolved.append(tok)
            return "[待核实]"
        used.setdefault(ft.source_id, None)
        return str(ft.value)

    return TOKEN_RE.sub(repl, text)


_RESIDUAL_BRACE_RE = re.compile(r"\{([^{}]*)\}")
_GARBLED_TOKEN_RE = re.compile(r"F\d+S\d+")  # 损坏的 {Fn} 残片(如 F2S26),LLM 把星数/计数 token 写崩


def scrub_residual_markup(text: str) -> str:
    """清成稿里漏出的非 token 残留标记(_render 之后调用)。

    治 LLM 偶发吐**字面花括号 / 损坏 token**裸漏给用户(2026-06-25 三考:litellm insight
    漏 `它覆盖了{包括…等在内的}F2S26个平台`)。是 06-16 `TOKEN_RE`/`FOOTNOTE_RE` 的通用补充——
    那两个只认 `{Fn}`/`[^n]` 形态,管不了"LLM 自发吐的字面 `{解释文}` + 乱码 token"。
    - 损坏 token 残片 `F\\d+S\\d+`(如 F2S26,本该是星数/计数)→ 标 `[待核实]`(同未解析占位约定)。
    - 残留 `{…}`(过了 _render 仍在 = 非 `{Fn}` 的字面花括号)→ 脱括号留内文。
    - 兜底脱掉配不成对的孤儿花括号。
    通用消毒,比逐个 bug 追正则治本。
    """
    text = _GARBLED_TOKEN_RE.sub("[待核实]", text)
    text = _RESIDUAL_BRACE_RE.sub(r"\1", text)
    return text.replace("{", "").replace("}", "")


def scrub_unsourced_numbers(text: str, source_text: str) -> tuple[str, list[str]]:
    """把 text 里在 source_text 中找不到的裸数字替换成 `[待核实]`,返回(成稿, 被标记的数字)。

    用于 digest 概述等"无 {Fn} 占位"的整段文本——它也直接展示给用户,数字同样要回源,
    不能绕过"绝不展示无来源数字"铁律。源里逐字出现过的数字(含 %)放行。
    """
    source_nums = {_norm_number(n) for n in BARE_NUMBER_RE.findall(source_text)}
    flagged: list[str] = []

    def repl(m):
        n = m.group(0)
        if _norm_number(n) in source_nums:
            return n
        if n not in flagged:
            flagged.append(n)
        return "[待核实]"

    return BARE_NUMBER_RE.sub(repl, text), flagged


def verify_item(
    item: ItemSummary,
    item_tokens: dict[str, FactToken],
    source_text: str = "",
    *,
    corroboration: int = 1,
    risk_min_sources: int = 2,
) -> VerifiedItem:
    """对账一条:替换占位、核对来源、探测裸数字 + 高风险定性断言。

    `item_tokens` 只含本条目允许引用的事实。
    source_text:本条目的原文(标题+正文)。原文里出现过的数字有来源,不算可疑。
    corroboration:本条目的"多源同报"佐证数(max(簇内源数, 事件热度),与 rank 同口径)。
    headline / tldr / insight 三段都对账;任一段有未解析占位、无来源裸数字,
    或(佐证 < risk_min_sources 时)命中高风险定性断言 → needs_review。
    risk_min_sources=1 等于关闭断言闸门(佐证恒 ≥1)。
    """
    used: dict[str, None] = {}
    unresolved: list[str] = []
    # 先清脚注残留(再 render),既清成稿、也不让 [^1] 的 "1" 误进裸数字探测。
    hl_raw = FOOTNOTE_RE.sub("", item.headline)
    td_raw = FOOTNOTE_RE.sub("", item.tldr)
    ins_raw = FOOTNOTE_RE.sub("", item.insight)
    # render 后再过一道通用消毒:清 LLM 漏出的字面花括号 / 损坏 token(F2S26 这类),别裸漏给用户。
    headline = scrub_residual_markup(_render(hl_raw, item_tokens, used, unresolved))
    tldr = scrub_residual_markup(_render(td_raw, item_tokens, used, unresolved))
    insight = scrub_residual_markup(_render(ins_raw, item_tokens, used, unresolved))

    # 裸数字探测:headline + tldr + insight 三段都查(headline 也直接展示,编的数字同样不能漏)。
    # 去掉占位后剩的裸数字,对照原文:原文逐字出现过 → 有来源放行(版本号/产品号/正文已有的指标);
    # 原文没有 → 编造嫌疑。版本号如 "Fable 5" 因正文必含该词而放行,不会误伤。
    source_nums = {_norm_number(n) for n in BARE_NUMBER_RE.findall(source_text)}
    suspect: list[str] = []
    for body in (hl_raw, td_raw, ins_raw):
        raw_wo_tokens = TOKEN_RE.sub("", body)
        for n in BARE_NUMBER_RE.findall(raw_wo_tokens):
            if _norm_number(n) not in source_nums and n not in suspect:
                suspect.append(n)

    # 高风险定性断言:扫展示成稿(占位已换真值的三段);单源命中 → 待核实,多源同报放行。
    risky: list[str] = []
    if corroboration < risk_min_sources:
        risky = detect_risky_claims(f"{headline}\n{tldr}\n{insight}")

    status = OK if not unresolved and not suspect and not risky else NEEDS_REVIEW
    return VerifiedItem(
        item_id=item.item_id,
        headline=headline,
        tldr=tldr,
        insight=insight,
        status=status,
        used_source_ids=list(used),
        unresolved_tokens=unresolved,
        suspect_numbers=suspect,
        risky_claims=risky,
    )
