"""verify 对账纯函数测试:占位替换真实数字、核不上标[待核实]、裸数字探测。

每条目两段 tldr/insight 都对账;测试把待测内容放 insight,tldr 用无数字的安全串。
"""

from __future__ import annotations

from pulsewire.summarize.schema import FactToken, ItemSummary
from pulsewire.verify import NEEDS_REVIEW, OK, scrub_unsourced_numbers, verify_item

_TLDR = "今日要点"  # 无数字的安全速读串,避免干扰裸数字探测


def _item(insight, headline="标题"):
    return ItemSummary(item_id="i1", headline=headline, tldr=_TLDR, insight=insight)


def _tok(token, source_id, value, label="HN points"):
    return FactToken(token=token, item_id="i1", source_id=source_id, label=label, value=value)


def test_substitutes_real_number_and_records_source():
    item = _item("获得 {F1} 个赞,讨论热烈。")
    tokens = {"F1": _tok("F1", "i1:hn:points", 901)}
    v = verify_item(item, tokens)
    assert v.insight == "获得 901 个赞,讨论热烈。"  # 真实数字来自库,非模型编
    assert v.tldr == _TLDR
    assert v.status == OK
    assert v.used_source_ids == ["i1:hn:points"]
    assert v.unresolved_tokens == [] and v.suspect_numbers == []


def test_unresolved_token_marked_pending():
    item = _item("评论 {F9} 条。")  # F9 没给
    v = verify_item(item, {"F1": _tok("F1", "i1:hn:points", 5)})
    assert "[待核实]" in v.insight
    assert v.status == NEEDS_REVIEW
    assert v.unresolved_tokens == ["F9"]


def test_bare_number_flagged_as_suspect():
    # 模型没走占位,自己写了裸数字 → 无来源 → suspect + needs_review
    item = _item("据称有 12345 名用户。")
    v = verify_item(item, {})
    assert "12345" in v.suspect_numbers
    assert v.status == NEEDS_REVIEW


def test_no_numbers_is_ok():
    item = _item("值得关注的新工具。")
    v = verify_item(item, {})
    assert v.status == OK
    assert v.insight == "值得关注的新工具。"


def test_number_present_in_source_text_not_suspect():
    # 原文里逐字出现的数字(版本号/产品号)有来源 → 放行;原文没有的照旧标记
    item = _item("Anthropic 发布 Claude Fable 5,得分 95%。", headline="新模型发布")
    src = "Introducing Claude Fable 5\nClaude Fable 5 gets 95% on the benchmark suite."
    v = verify_item(item, {}, source_text=src)
    assert v.status == OK
    assert v.suspect_numbers == []
    # 同一原文,但 insight 里掺了原文没有的数字 → 只标那个编造嫌疑
    item2 = _item("Claude Fable 5 提速 300 倍。", headline="新模型发布")
    v2 = verify_item(item2, {}, source_text=src)
    assert v2.suspect_numbers == ["300"]
    assert v2.status == NEEDS_REVIEW


def test_source_number_normalization():
    # 千分位逗号/尾部 % 归一对照:原文 "2,707" → insight "2707" 也算有来源
    item = _item("评测覆盖 2707 道题。", headline="评测")
    v = verify_item(item, {}, source_text="The benchmark contains 2,707 problems.")
    assert v.status == OK and v.suspect_numbers == []


def test_tldr_bare_number_also_checked():
    # 裸数字探测覆盖 tldr 段(不只是 insight)
    item = ItemSummary(item_id="i1", headline="标题", tldr="据说有 88888 人在用", insight="详细解读无数字。")
    v = verify_item(item, {})
    assert "88888" in v.suspect_numbers
    assert v.status == NEEDS_REVIEW


def test_headline_bare_number_flagged():
    # headline 也直接展示,编造的数字同样要拦(版本号靠 source_text 放行,这里无来源 → 标记)
    item = ItemSummary(item_id="i1", headline="性能暴涨 300%", tldr=_TLDR, insight="无数字解读。")
    v = verify_item(item, {})
    assert "300%" in v.suspect_numbers and v.status == NEEDS_REVIEW
    # 版本号型 headline 有 source 支撑则放行
    ok = ItemSummary(item_id="i1", headline="Claude Fable 5 发布", tldr=_TLDR, insight="解读。")
    assert verify_item(ok, {}, source_text="Claude Fable 5 is out").status == OK


def test_percent_claim_not_in_source_flagged():
    # 源里有 "50"(无 %),模型写 "50%" 是不同语义主张 → 不放行
    item = _item("命中率高达 50%。")
    v = verify_item(item, {}, source_text="得了 50 分,表现不错")
    assert "50%" in v.suspect_numbers and v.status == NEEDS_REVIEW
    # 源里就是 "50%" 则放行
    assert verify_item(_item("命中率 50%。"), {}, source_text="准确率 50% 起").status == OK


def test_scrub_unsourced_numbers_for_digest():
    # digest 等无占位整段文本:源里没有的裸数字替换成 [待核实]
    text = "中国投资 2950 亿,模型提速 50%。"
    clean, flagged = scrub_unsourced_numbers(text, "中国宣布 295 billion 计划")
    assert "[待核实]" in clean
    assert "2950" in flagged and "50%" in flagged
    # 源里逐字出现的数字放行
    clean2, flagged2 = scrub_unsourced_numbers("已 295 亿", "投入 295 billion")
    assert clean2 == "已 295 亿" and flagged2 == []


# ---- 高风险定性断言闸门(2026-06-12 双审计后加) ---- #

def test_single_source_ipo_rumor_flagged():
    # 审计实锤:"OpenAI 申请上市"(单源假传闻)曾溜过 needs_review → 现在单源即标待核实
    item = ItemSummary(
        item_id="i1", headline="OpenAI 申请上市", tldr=_TLDR,
        insight="据知情人士透露,OpenAI 已秘密提交上市申请,估值惊人。",
    )
    v = verify_item(item, {}, corroboration=1)
    assert v.status == NEEDS_REVIEW
    assert any("上市/融资" in c for c in v.risky_claims)


def test_corroborated_ipo_passes():
    # 同样的断言,多源同报(≥2 源)→ 真大事,放行
    item = ItemSummary(
        item_id="i1", headline="OpenAI 申请上市", tldr=_TLDR,
        insight="OpenAI 已提交上市申请,多家媒体证实。",
    )
    v = verify_item(item, {}, corroboration=3)
    assert v.status == OK
    assert v.risky_claims == []


def test_marketing_multiplier_claim_flagged():
    # 审计实锤:"性能翻倍"营销话术(无数字,裸数字探测兜不住)→ 单源标待核实
    item = _item("官方称新版本性能翻倍,体验大幅提升。")
    v = verify_item(item, {}, corroboration=1)
    assert v.status == NEEDS_REVIEW
    assert any("倍数话术" in c for c in v.risky_claims)


def test_war_casualty_claim_flagged():
    # 审计实锤:地缘战报伤亡(定性表述无数字)→ 单源标待核实
    item = _item("据称此次空袭造成重大伤亡,港口已被封锁。")
    v = verify_item(item, {}, corroboration=1)
    assert v.status == NEEDS_REVIEW
    assert any("战报/制裁" in c for c in v.risky_claims)


def test_geopolitical_strong_claim_flagged_single_source():
    # 2026-06-15 二④:和平"已敲定"、海峡"自由通行"这类把传闻写成既成事实 → 单源标待核实
    v1 = verify_item(_item("据报道,双方和平协议已正式达成。"), {}, corroboration=1)
    assert v1.status == NEEDS_REVIEW and any("地缘断言" in c for c in v1.risky_claims)
    v2 = verify_item(_item("官方称霍尔木兹海峡已恢复自由通航。"), {}, corroboration=1)
    assert v2.status == NEEDS_REVIEW and any("地缘断言" in c for c in v2.risky_claims)


def test_geopolitical_claim_multisource_passes():
    # 多源同报放行(真大事必多源):同样的强断言佐证≥risk_min_sources 不标
    v = verify_item(_item("双方和平协议已正式达成。"), {}, corroboration=3, risk_min_sources=2)
    assert v.status == OK and v.risky_claims == []


def test_comparative_claim_flagged_single_source():
    # 2026-06-15 三②:"反超/新王/全球第一"这类对比座次写成定论 → 单源标待核实
    v1 = verify_item(_item("据称该模型已反超 Ollama,成为开源新王。"), {}, corroboration=1)
    assert v1.status == NEEDS_REVIEW and any("对比断言" in c for c in v1.risky_claims)
    v2 = verify_item(_item("该芯片性能全球第一,遥遥领先竞品。"), {}, corroboration=1)
    assert v2.status == NEEDS_REVIEW and any("对比断言" in c for c in v2.risky_claims)


def test_comparative_claim_no_false_positive_on_common_words():
    # 不误伤常见无害表述:"第一次""超越自我"不触发
    v = verify_item(_item("这是该团队第一次发布,鼓励大家超越自我。"), {}, corroboration=1)
    assert v.status == OK and v.risky_claims == []
    # 2026-06-15 盲验收紧:多义的"全球第一例/世界第一大/国内第一时间/登山登顶"不再误标
    for benign in (
        "全球第一例基因编辑临床案例公布。",
        "美国仍是世界第一大经济体。",
        "国内第一时间报道了该发布会。",
        "登山队成功登顶珠峰。",
    ):
        vb = verify_item(_item(benign), {}, corroboration=1)
        assert vb.status == OK and vb.risky_claims == [], f"误伤:{benign}"


def test_clinical_breakthrough_flagged_but_routine_clinical_not():
    # "治愈/攻克"强断言单源标记;日常临床表述(获批上市、临床试验进行中)不误伤
    v = verify_item(_item("该疗法宣称能治愈晚期癌症。"), {}, corroboration=1)
    assert v.status == NEEDS_REVIEW and any("临床突破" in c for c in v.risky_claims)
    ok = verify_item(_item("该药物已获批上市,临床试验仍在进行。"), {}, corroboration=1)
    assert ok.status == OK and ok.risky_claims == []


def test_routine_funding_news_not_flagged():
    # 常规融资/收购报道(bio 行业媒体主体内容)不触发闸门——只有 IPO/上市/估值类强断言才标,
    # 否则徽标被刷成噪音(真数据回放定的边界)
    v = verify_item(_item("该公司宣布完成新一轮融资,并收购一家初创公司。"), {}, corroboration=1)
    assert v.status == OK and v.risky_claims == []


def test_risk_gate_disabled_with_min_sources_one():
    # risk_min_sources=1 等于关闭闸门(佐证恒 ≥1)
    item = _item("据知情人士,公司已秘密提交上市申请。")
    v = verify_item(item, {}, corroboration=1, risk_min_sources=1)
    assert v.status == OK and v.risky_claims == []


def test_risky_claim_does_not_affect_number_checks():
    # 断言闸门与数字对账独立:多源放行断言,但编造数字照旧拦
    item = _item("公司完成融资,金额达 99999 万。")
    v = verify_item(item, {}, corroboration=5)
    assert v.risky_claims == []
    assert "99999" in v.suspect_numbers and v.status == NEEDS_REVIEW


def test_multiple_tokens_same_item():
    item = _item("{F1} stars、{F2} forks。", headline="项目")
    tokens = {
        "F1": _tok("F1", "i1:github:stars", 221751, "GitHub stars"),
        "F2": _tok("F2", "i1:github:forks", 50731, "GitHub forks"),
    }
    v = verify_item(item, tokens)
    assert v.insight == "221751 stars、50731 forks。"
    assert v.status == OK
    assert set(v.used_source_ids) == {"i1:github:stars", "i1:github:forks"}


# --- 2026-06-16 eval 回归:GitHub 榜裸漏 { F13 } / 复刻[^1] ---

def test_token_with_inner_whitespace_resolves():
    # 推理模型吐 "{ F13 }" 带空格,必须照样替换成真值,不能裸漏给用户
    item = _item("相当于用 { F13 } 颗星换来一个军师团。")
    tokens = {"F13": _tok("F13", "i1:github:stars", 35155, "GitHub stars")}
    v = verify_item(item, tokens)
    assert v.insight == "相当于用 35155 颗星换来一个军师团。"
    assert v.unresolved_tokens == [] and v.status == OK


def test_token_with_inner_whitespace_unmapped_is_flagged():
    # 带空格且映射不到 → 必须变 [待核实] + needs_review,绝不原样漏出
    item = _item("社区给它投出了 { F99 } 颗星。")
    v = verify_item(item, {})
    assert "{ F99 }" not in v.insight and "[待核实]" in v.insight
    assert "F99" in v.unresolved_tokens and v.status == NEEDS_REVIEW


def test_orphan_footnote_marker_is_stripped():
    # LLM 吐 markdown 脚注 [^1],成稿无脚注系统 → 清掉,不残留乱码且不误进裸数字探测
    item = _item("项目积累了 35155 个星和 4366 个复刻[^1],社区相当买账。")
    v = verify_item(item, {}, source_text="35155 4366")
    assert "[^1]" not in v.insight
    assert v.insight == "项目积累了 35155 个星和 4366 个复刻,社区相当买账。"
    assert v.suspect_numbers == []
