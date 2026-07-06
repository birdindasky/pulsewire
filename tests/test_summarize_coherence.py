"""标题错位护栏单测(2026-06-17 eval 实锤的回归题)。

用合成正交向量复刻真实事故形态(地缘一批标题整体错位 + 一条因模型写重而配不回),
脱离 embedding 模型即可确定性验证检测+重配+兜底逻辑。
"""

from __future__ import annotations

from pulsewire.summarize.coherence import lead_from_tldr, plan_headline_repair


def _basis(i: int, n: int) -> list[float]:
    v = [0.0] * n
    v[i] = 1.0
    return v


def test_detects_and_repairs_shifted_headlines():
    # 5 个正交"话题" A..E;body i = 话题 i。
    n = 5
    bodies = [_basis(i, n) for i in range(n)]
    # 标题错位形态(复刻真数据):
    #  head0=话题1(漂),head1=话题3(漂),head2=话题4(漂·与head4撞),head3=话题2(漂),head4=话题4(对)
    heads = [_basis(1, n), _basis(3, n), _basis(4, n), _basis(2, n), _basis(4, n)]
    assignment, drifted, unresolved = plan_headline_repair(heads, bodies, floor=0.5, margin=0.1)

    # body0..3 的自配余弦=0(标题讲别的话题)→ 全判错位;body4 自配=1 → 不动
    assert drifted == {0, 1, 2, 3}
    # 错位集内重配:body1←head0, body2←head3, body3←head1(各自余弦=1)
    assert assignment[1] == 0
    assert assignment[2] == 3
    assert assignment[3] == 1
    assert assignment[4] == 4  # 正确条目原样不动
    # body0(话题0)没有任何标题匹配(模型把话题4写了两遍、漏了话题0)→ 兜底
    assert unresolved == {0}


def test_clean_chunk_untouched():
    # 每条标题与自己正文同话题 → 零错位,assignment 恒等,不误判。
    n = 4
    bodies = [_basis(i, n) for i in range(n)]
    heads = [_basis(i, n) for i in range(n)]
    assignment, drifted, unresolved = plan_headline_repair(heads, bodies, floor=0.5, margin=0.1)
    assert drifted == set()
    assert unresolved == set()
    assert assignment == {0: 0, 1: 1, 2: 2, 3: 3}


def test_single_item_noop():
    assignment, drifted, unresolved = plan_headline_repair([[1.0]], [[1.0]], floor=0.5, margin=0.1)
    assert assignment == {0: 0}
    assert drifted == set() and unresolved == set()


def test_margin_guards_against_noise():
    # 自配略低但没有明显更优归属(差距<margin)→ 不判错位,免噪声误伤。
    bodies = [[1.0, 0.0], [0.0, 1.0]]
    heads = [[0.7, 0.71], [0.71, 0.7]]  # 两条对两个 body 都半像,差距小
    assignment, drifted, _ = plan_headline_repair(heads, bodies, floor=0.6, margin=0.3)
    assert drifted == set()
    assert assignment == {0: 0, 1: 1}


def test_lead_from_tldr_takes_first_sentence():
    # 取首句(到句号止),不带后续句
    out = lead_from_tldr("英国上诉法院裁定取缔决定合法,推翻此前判决。后续将上诉。")
    assert out.startswith("英国上诉法院裁定取缔决定合法") and "后续" not in out
    # 首句过长 → 在从句分隔处收口(真实兜底场景:长 tldr 取出干净短标题)
    long = "英国上诉法院裁定内政大臣取缔巴勒斯坦行动组织的决定合法，推翻此前高等法院的相反判决"
    out2 = lead_from_tldr(long, max_len=30)
    assert len(out2) <= 30 and out2 == "英国上诉法院裁定内政大臣取缔巴勒斯坦行动组织的决定合法"
    # 空 tldr 不炸、不返空(schema 要求 min_length>=1)
    assert lead_from_tldr("") == "(标题缺失)"
    assert lead_from_tldr("   ") == "(标题缺失)"
