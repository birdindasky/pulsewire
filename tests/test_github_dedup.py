"""INDEPENDENT GRADER — GitHub board theme dedup.

Grades two units against the spec handed to me (NOT the repo's comments/tests):

  Unit 1: _name_tokens(repo_key)
    repo part only (after last '/'), lowercased, split on non-alnum,
    keep tokens len>=3 AND not in _GH_NAME_STOP, return a set.

  Unit 2: _select_trending theme-dedup behaviour (DB)
    candidates sorted by velocity desc (tiebreak abs stars), then greedy
    theme dedup from high to low: a candidate is dropped IFF it has
    significant tokens AND shares >=1 with the already-kept set.
    No significant tokens -> never dropped. Keep <= github_board_limit,
    preserve velocity order. The survivor of a theme group must be the
    fastest-velocity one.

Expected values are derived here, independently, from the spec above.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pulsewire.config import get_settings
from pulsewire.github_board.engine import _GH_NAME_STOP, _name_tokens, _select_trending
from pulsewire.store import repo

UTC = timezone.utc
REG = "github-search-ai-agents"  # registered, enabled, matches ILIKE '%agent%'


# ===========================================================================
# Unit 1 — _name_tokens
# ===========================================================================

def test_name_tokens_takes_repo_part_not_owner():
    """owner is dropped; only the repo part contributes tokens.

    'nousresearch/hermes-agent': repo part 'hermes-agent' -> {'hermes'}
    ('agent' is a stopword, 'hermes' len>=3 kept). owner 'nousresearch'
    (len 12, not a stopword) MUST NOT appear.
    """
    toks = _name_tokens("nousresearch/hermes-agent")
    assert toks == {"hermes"}
    assert "nousresearch" not in toks
    assert "agent" not in toks  # stopword


def test_name_tokens_owner_only_significant_still_excluded():
    """Even when ONLY the owner carries a would-be-significant token, it's excluded.

    'zephyr/ai': repo part is 'ai' (stopword, also len<3-class stop) -> {}.
    owner 'zephyr' (len6, not stopword) must NOT leak into tokens.
    Result must be empty set, proving owner is never tokenized.
    """
    toks = _name_tokens("zephyr/ai")
    assert toks == set()
    assert "zephyr" not in toks


def test_name_tokens_last_slash_is_the_split():
    """Repo part = everything after the LAST slash.

    'a/b/zephyr-core' -> repo part 'zephyr-core' -> {'zephyr', 'core'}.
    'core' len4, not a stop; 'zephyr' len6 not a stop.
    """
    toks = _name_tokens("a/b/zephyr-core")
    assert toks == {"zephyr", "core"}


def test_name_tokens_no_slash_uses_whole_string():
    assert _name_tokens("hermes-toolkit") == {"hermes", "toolkit"}


def test_name_tokens_stopwords_removed():
    """Every token that is a stopword must be dropped, significant ones kept.

    'awesome-llm-zephyr-studio' -> split: awesome, llm, zephyr, studio.
    awesome/llm/studio are stops -> only {'zephyr'}.
    """
    toks = _name_tokens("owner/awesome-llm-zephyr-studio")
    assert toks == {"zephyr"}


def test_name_tokens_short_tokens_removed():
    """Tokens shorter than 3 chars dropped regardless of being a stopword.

    'go-ml-x9-quasar' -> go(2), ml(2), x9(2), quasar(6).
    Only 'quasar' survives (others len<3).
    """
    toks = _name_tokens("owner/go-ml-x9-quasar")
    assert toks == {"quasar"}
    # 'ab' (len2) dropped, 'abc' (len3, not stop) kept
    assert _name_tokens("o/ab-abc") == {"abc"}


def test_name_tokens_case_insensitive():
    assert _name_tokens("OWNER/Hermes-Quasar") == {"hermes", "quasar"}
    assert _name_tokens("OWNER/HERMES") == {"hermes"}


def test_name_tokens_returns_set_type():
    assert isinstance(_name_tokens("owner/zephyr-quasar"), set)
    # duplicate token only once
    assert _name_tokens("owner/zephyr-zephyr") == {"zephyr"}


def test_name_tokens_all_stop_is_empty():
    """A name made only of stopwords/short tokens -> empty set (never folds)."""
    assert _name_tokens("foo/ai-app") == set()
    assert _name_tokens("foo/agent-tool-cli") == set()


def test_name_tokens_numeric_tokens_kept_if_len3():
    """Token is alnum: a >=3-char numeric run that isn't a stopword survives.

    'gpt4-123' -> gpt4(len4, not in stop -> 'gpt' is stop but 'gpt4' is not),
    123(len3). Both kept.
    """
    toks = _name_tokens("owner/gpt4-123")
    assert "gpt4" in toks   # 'gpt4' != 'gpt'; not a stopword
    assert "123" in toks


# ===========================================================================
# Unit 2 — _select_trending theme dedup (DB)
# ===========================================================================

def _board_settings(limit=500, recency=720):
    base = get_settings()
    rank = base.rank.model_copy(update={
        "github_board_exclude": [],
        "github_board_limit": limit,
        "github_board_recency_days": recency,
    })
    return base.model_copy(update={"rank": rank})


async def _mk(db_session, slug, stars, created_days_ago, *, uniq):
    """Create a candidate repo. uniq goes in the OWNER slot so owner names
    never accidentally share a significant token across repos."""
    now = datetime.now(UTC)
    facts = {"github": {"stars": stars,
                        "created_at": (now - timedelta(days=created_days_ago)).isoformat()}}
    return await repo.upsert_item(
        db_session, source=REG,
        url=f"https://github.com/{uniq}/{slug}",
        title=slug, published_at=now, facts=facts,
    )


@pytest.mark.asyncio
async def test_same_theme_folds_keep_highest_velocity(db_session, clean_github_candidates):
    """Three repos share token 'zephyr', different velocities -> only the
    fastest-velocity one survives, the other two are dropped.

    velocities (stars/age):
      zephyr-agent   : 6000/10  = 600   (highest)
      zephyr-desktop : 3000/30  = 100
      zephyr-studio  : 500/50   = 10    (lowest)
    All three -> token {'zephyr'} (agent/desktop/studio are stops).
    Spec: greedy from fastest; agent kept, desktop & studio share 'zephyr' -> dropped.
    """
    a = await _mk(db_session, "zephyr-agent", 6000, 10, uniq="ownerA")
    b = await _mk(db_session, "zephyr-desktop", 3000, 30, uniq="ownerB")
    c = await _mk(db_session, "zephyr-studio", 500, 50, uniq="ownerC")

    picked = await _select_trending(db_session, _board_settings())
    ids = {iid for iid, _ in picked}
    assert a in ids, "fastest-velocity zephyr repo must be kept"
    assert b not in ids, "slower zephyr repo must be folded away"
    assert c not in ids, "slowest zephyr repo must be folded away"


@pytest.mark.asyncio
async def test_survivor_is_fastest_not_arbitrary(db_session, clean_github_candidates):
    """Order the fastest one in the MIDDLE of insertion to prove the survivor
    is chosen by velocity, not insertion/star order.

    velocities:
      quasar-agent  : 1000/100 = 10    (created first, lowest velocity)
      quasar-studio : 9000/10  = 900   (HIGHEST velocity, fewer total... actually more)
      quasar-tool   : 2000/40  = 50
    Survivor must be quasar-studio (highest velocity), even though quasar-agent
    was created first. Also studio has FEWER stars than... no, has 9000 which is most.
    To isolate velocity-vs-stars, make the survivor NOT the max-stars one:
    """
    # redo with survivor having neither first-inserted nor max-stars:
    low = await _mk(db_session, "nimbus-agent", 100000, 5000, uniq="o1")   # 20/day, MAX stars
    win = await _mk(db_session, "nimbus-studio", 3000, 3, uniq="o2")        # 1000/day, fewer stars
    mid = await _mk(db_session, "nimbus-tool", 5000, 50, uniq="o3")         # 100/day

    picked = await _select_trending(db_session, _board_settings())
    ids = {iid for iid, _ in picked}
    # survivor must be the highest-velocity (nimbus-studio), NOT highest-stars (nimbus-agent)
    assert win in ids, "highest-velocity repo must survive the fold"
    assert low not in ids, "highest-STARS but low-velocity repo must be folded away"
    assert mid not in ids


@pytest.mark.asyncio
async def test_different_themes_all_kept(db_session, clean_github_candidates):
    """Repos sharing NO significant token are all kept (no false fold).

    NB: the test DB holds ~200 real AI repos that _select_trending also sees.
    So I use deliberately collision-free coined tokens (verified not present in
    the live corpus) and assert all four of MY distinct-theme repos survive.
    """
    x = await _mk(db_session, "grdralpha-agent", 5000, 10, uniq="d1")   # {grdralpha}
    y = await _mk(db_session, "grdrbeta-agent", 4000, 10, uniq="d2")    # {grdrbeta}
    z = await _mk(db_session, "grdrgamma-agent", 3000, 10, uniq="d3")   # {grdrgamma}
    w = await _mk(db_session, "grdrdelta-tool", 2000, 10, uniq="d4")    # {grdrdelta}

    picked = await _select_trending(db_session, _board_settings())
    ids = {iid for iid, _ in picked}
    assert {x, y, z, w} <= ids, "distinct-theme repos must NOT be folded"


@pytest.mark.asyncio
async def test_shared_stopword_only_does_not_fold(db_session, clean_github_candidates):
    """Two repos sharing ONLY a stopword ('agent') must both be kept.

    'x-agent' -> {} (x len1 dropped, agent stop) ; 'y-agent' -> {}.
    Both have empty significant-token sets -> never folded.
    Use len>=3 owners/extra so we are sure it's the stopword, not emptiness alone:
    actually both ARE empty here, which still must keep both.
    """
    p = await _mk(db_session, "ax-agent", 9000, 10, uniq="s1")  # ax(2)+agent(stop) -> {}
    q = await _mk(db_session, "by-agent", 1000, 10, uniq="s2")  # by(2)+agent(stop) -> {}

    picked = await _select_trending(db_session, _board_settings())
    ids = {iid for iid, _ in picked}
    assert p in ids and q in ids, "sharing only a stopword must not fold"


@pytest.mark.asyncio
async def test_shared_stopword_with_distinct_significant_does_not_fold(db_session, clean_github_candidates):
    """Stronger version: each repo HAS a distinct significant token plus the
    same stopword. Sharing the stopword must not cause a fold.

    'falcon-agent' -> {falcon} ; 'condor-agent' -> {condor}. Disjoint -> keep both.
    """
    p = await _mk(db_session, "falcon-agent", 9000, 10, uniq="ss1")
    q = await _mk(db_session, "condor-agent", 1000, 10, uniq="ss2")

    picked = await _select_trending(db_session, _board_settings())
    ids = {iid for iid, _ in picked}
    assert p in ids and q in ids


@pytest.mark.asyncio
async def test_no_significant_token_never_dropped(db_session, clean_github_candidates):
    """A repo whose name is all stopwords/short tokens is never folded, even if
    another kept repo would 'contain' its (empty) tokens."""
    plain = await _mk(db_session, "ai-app", 5000, 10, uniq="n1")     # {} significant
    plain2 = await _mk(db_session, "llm-cli", 4000, 10, uniq="n2")   # {} significant
    themed = await _mk(db_session, "zephyr-core", 3000, 10, uniq="n3")  # {zephyr, core}

    picked = await _select_trending(db_session, _board_settings())
    ids = {iid for iid, _ in picked}
    assert plain in ids and plain2 in ids and themed in ids


@pytest.mark.asyncio
async def test_kept_preserves_velocity_order(db_session, clean_github_candidates):
    """Survivors must appear in velocity-desc order."""
    fast = await _mk(db_session, "zephyr-x", 9000, 9, uniq="ord1")    # 1000/day {zephyr}
    mid = await _mk(db_session, "quasar-y", 6000, 60, uniq="ord2")    # 100/day {quasar}
    slow = await _mk(db_session, "nimbus-z", 500, 50, uniq="ord3")    # 10/day {nimbus}

    picked = await _select_trending(db_session, _board_settings())
    order = [iid for iid, _ in picked]
    pos = {iid: order.index(iid) for iid in (fast, mid, slow)}
    assert pos[fast] < pos[mid] < pos[slow]


@pytest.mark.asyncio
async def test_limit_caps_kept(db_session, clean_github_candidates):
    """github_board_limit caps the number kept.

    The test DB has ~200 real repos competing for the cap, so I give MY three
    repos overwhelming velocity (huge stars / age 1 day -> velocity == stars,
    far above any real repo) so they are the global top three. With limit=3 all
    three survive; the final list length equals the limit exactly.
    Distinct coined tokens so no inter-fold among mine.
    """
    fast = await _mk(db_session, "grdrlima", 900_000_000, 1, uniq="lim1")   # vel 9e8
    mid = await _mk(db_session, "grdrlimb", 800_000_000, 1, uniq="lim2")    # vel 8e8
    slow = await _mk(db_session, "grdrlimc", 700_000_000, 1, uniq="lim3")   # vel 7e8

    # limit=3 -> exactly my three dominate and fill the whole list
    picked3 = await _select_trending(db_session, _board_settings(limit=3))
    ids3 = [iid for iid, _ in picked3]
    assert len(ids3) == 3, f"limit=3 must cap at 3, got {len(ids3)}"
    assert set(ids3) == {fast, mid, slow}, "my 3 fastest must be the only survivors at limit=3"

    # limit=2 -> the slowest of my three is cut by the cap
    picked2 = await _select_trending(db_session, _board_settings(limit=2))
    ids2 = [iid for iid, _ in picked2]
    assert len(ids2) == 2, f"limit=2 must cap at 2, got {len(ids2)}"
    assert ids2 == [fast, mid], "cap keeps the two highest-velocity, in velocity order"
    assert slow not in ids2


@pytest.mark.asyncio
async def test_partial_token_overlap_folds(db_session, clean_github_candidates):
    """If a later repo shares ANY one significant token with the kept set
    (even while having an extra distinct token), it must still fold.

    kept:  'zephyr-core'  -> {zephyr, core}
    later: 'zephyr-quasar' -> {zephyr, quasar}; shares 'zephyr' -> dropped.
    """
    keep = await _mk(db_session, "zephyr-core", 9000, 9, uniq="pt1")  # 1000/day
    drop = await _mk(db_session, "zephyr-quasar", 1000, 50, uniq="pt2")  # 20/day, shares zephyr

    picked = await _select_trending(db_session, _board_settings())
    ids = {iid for iid, _ in picked}
    assert keep in ids
    assert drop not in ids, "shares 'zephyr' with kept set -> must fold"


@pytest.mark.asyncio
async def test_transitive_token_union_folds_via_second_member(db_session, clean_github_candidates):
    """Adversarial: token sets chain through the union. Spec says tokens of EVERY
    kept repo accumulate into kept_tokens.

    Order by velocity:
      A 'zephyr-core'   1000/day -> kept, tokens {zephyr, core}
      B 'core-quasar'    100/day -> shares 'core' with kept -> dropped (NOT kept)
      C 'quasar-x'        10/day -> 'quasar' only; quasar entered kept_tokens? NO,
        because B was DROPPED (dropped repos do NOT add their tokens). So C's
        'quasar' is NOT in kept_tokens -> C is KEPT.
    This pins the exact semantics: only KEPT repos contribute tokens.
    """
    a = await _mk(db_session, "zephyr-core", 9000, 9, uniq="tr1")    # 1000/day
    b = await _mk(db_session, "core-quasar", 5000, 50, uniq="tr2")   # 100/day shares 'core'
    c = await _mk(db_session, "quasar-x", 500, 50, uniq="tr3")       # 10/day {quasar}

    picked = await _select_trending(db_session, _board_settings())
    ids = {iid for iid, _ in picked}
    assert a in ids, "fastest kept"
    assert b not in ids, "shares 'core' with A -> folded"
    assert c in ids, "C's 'quasar' never entered kept_tokens (B was dropped) -> kept"


def test_stopword_set_contains_expected_generics():
    """Sanity-pin a few stopwords the spec names, so the dedup tests rest on
    a known stop set."""
    for w in ("agent", "agents", "ai", "llm", "studio", "desktop", "app", "tool"):
        assert w in _GH_NAME_STOP, f"{w} expected in stop set"
