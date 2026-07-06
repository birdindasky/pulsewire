"""INDEPENDENT GRADER — GitHub board v2: "rank by recent star velocity".

Blind acceptance tests for the v2 change. Expected values are DERIVED HERE from
the spec handed to me, not copied from the repo's own comments/tests. I do NOT
trust any "it works" claim — every number is computed independently and asserted
against what the code actually produces.

Spec under test
---------------
1. `_recent_velocity(stars, prev, created_at_raw, now)`:
   - prev=(prev_stars, prev_at)  -> (stars - prev_stars) / delta_days,
     delta_days floored at 1.0; delta may be negative.
   - prev=None (cold start)      -> falls back to `_star_velocity` (lifetime).
   - same unit as lifetime velocity: stars/day.
2. `_ranked_candidates` orders the candidate pool by `_recent_velocity` using the
   PREVIOUS snapshot: an old repo whose lifetime avg is low but which surged
   recently must outrank a placid old repo (which pure lifetime-avg would bury).
3. `run_github_board` snapshots the ENTIRE candidate pool (item_timeline rows ==
   #candidates), not just the top-N pick — so non-charting repos accrue history.
4. Cold start (no history at all) == old behaviour: ordering identical to ranking
   by `_star_velocity`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import async_sessionmaker

from pulsewire.config import get_settings
from pulsewire.github_board import engine as gh
from pulsewire.github_board.engine import (
    _ranked_candidates,
    _recent_velocity,
    _star_velocity,
    run_github_board,
)
from pulsewire.store import repo

UTC = timezone.utc
REG = "github-search-ai-agents"  # registered, enabled, matches ILIKE '%agent%'


def _board_settings(*, limit=500, recency=720, exclude=None):
    base = get_settings()
    rank = base.rank.model_copy(update={
        "github_board_exclude": exclude or [],
        "github_board_limit": limit,
        "github_board_recency_days": recency,
    })
    return base.model_copy(update={"rank": rank})


async def _mk(db_session, slug, stars, created_days_ago, *, uniq):
    """Insert a candidate repo. uniq -> owner slot so owners never share tokens."""
    now = datetime.now(UTC)
    facts = {"github": {"stars": stars}}
    if created_days_ago is not None:
        facts["github"]["created_at"] = (now - timedelta(days=created_days_ago)).isoformat()
    return await repo.upsert_item(
        db_session, source=REG, url=f"https://github.com/{uniq}/{slug}",
        title=slug, published_at=now, facts=facts,
    )


async def _insert_snapshot(db_session, item_id, stars, observed_days_ago, *, observed_at=None):
    """Insert an item_timeline row with an EXPLICIT past observed_at.

    add_item_timeline() uses server_default now() so it can't backdate; for the
    'previous snapshot' I write the row directly with a chosen observed_at.

    Pass an explicit `observed_at` when two repos must share the SAME prev
    timestamp (the equal-velocity tiebreak test): otherwise each call's own
    datetime.now() differs by microseconds, so delta_days (and thus velocity)
    is subtly unequal and the tiebreak can't be isolated.
    """
    if observed_at is None:
        observed_at = datetime.now(UTC) - timedelta(days=observed_days_ago)
    await db_session.execute(
        sa_text(
            "INSERT INTO item_timeline (item_id, stars, observed_at) "
            "VALUES (:iid, :stars, :obs)"
        ),
        {"iid": item_id, "stars": stars, "obs": observed_at},
    )


# ===========================================================================
# Requirement 1 — _recent_velocity (pure function, no DB)
# ===========================================================================

def test_recent_velocity_uses_delta_over_days():
    """prev present -> (stars - prev_stars) / delta_days. 1300 - 1000 = 300 over
    3 days = 100/day. created_at is irrelevant when prev exists."""
    now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)
    prev_at = now - timedelta(days=3)
    v = _recent_velocity(1300, (1000, prev_at), "2020-01-01", now)
    assert v == pytest.approx(100.0)


def test_recent_velocity_negative_delta_allowed():
    """Lost stars -> negative velocity (sinks). 900 - 1000 = -100 over 2 days = -50/day."""
    now = datetime(2026, 6, 20, tzinfo=UTC)
    prev_at = now - timedelta(days=2)
    v = _recent_velocity(900, (1000, prev_at), None, now)
    assert v == pytest.approx(-50.0)
    assert v < 0


def test_recent_velocity_same_day_floor_one_day():
    """Two runs the same day must NOT inflate: delta floored to 1.0 day.
    Without the floor, 500 gained over 2h = 6000/day; floor clamps to 500/day."""
    now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)
    prev_at = now - timedelta(hours=2)
    v = _recent_velocity(1500, (1000, prev_at), None, now)
    assert v == pytest.approx(500.0)  # delta 500 / floored 1.0 day
    # exactly 1 day apart -> delta / 1
    v1 = _recent_velocity(1500, (1000, now - timedelta(days=1)), None, now)
    assert v1 == pytest.approx(500.0)
    # 5 days apart -> 500 / 5 = 100
    v5 = _recent_velocity(1500, (1000, now - timedelta(days=5)), None, now)
    assert v5 == pytest.approx(100.0)


def test_recent_velocity_cold_start_falls_back_to_lifetime():
    """prev=None -> identical to _star_velocity (lifetime). 10000 over 100d age = 100/day."""
    now = datetime(2026, 6, 20, tzinfo=UTC)
    created = (now - timedelta(days=100)).isoformat()
    v = _recent_velocity(10000, None, created, now)
    assert v == pytest.approx(_star_velocity(10000, created, now))
    assert v == pytest.approx(100.0)
    # missing created_at on cold start -> lifetime returns 0
    assert _recent_velocity(99999, None, None, now) == pytest.approx(0.0)


def test_recent_velocity_same_unit_as_lifetime():
    """Both are stars/day, hence co-sortable. An old repo surging recently can have
    a recent velocity FAR above its own lifetime average."""
    now = datetime(2026, 6, 20, tzinfo=UTC)
    created = (now - timedelta(days=1000)).isoformat()
    lifetime = _star_velocity(50000, created, now)        # 50/day
    prev_at = now - timedelta(days=2)
    recent = _recent_velocity(52000, (50000, prev_at), created, now)  # 2000/2 = 1000/day
    assert lifetime == pytest.approx(50.0)
    assert recent == pytest.approx(1000.0)
    assert recent > lifetime  # surge visible only via recent velocity


def test_recent_velocity_tz_naive_prev_at_treated_as_utc():
    """prev_at without tzinfo must be treated as UTC (no crash, correct delta)."""
    now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)
    naive_prev = datetime(2026, 6, 18, 12, 0, 0)  # 2 days ago, tz-naive
    v = _recent_velocity(1400, (1000, naive_prev), None, now)
    assert v == pytest.approx(200.0)  # 400 / 2 days


# ===========================================================================
# Requirement 2 — _ranked_candidates orders by recent velocity using prev snapshot
# ===========================================================================

@pytest.mark.asyncio
async def test_ranked_candidates_surging_old_repo_beats_placid_old_repo(db_session):
    """The headline v2 scenario.

    A: old repo, HUGE total stars, LOW lifetime avg, but SURGED recently.
       created 2000 days ago, now 100000 stars -> lifetime 50/day.
       prev snapshot 2 days ago = 90000 -> recent (100000-90000)/2 = 5000/day.
    B: placid old repo, also old, NOT moving recently.
       created 2000 days ago, now 80000 stars -> lifetime 40/day.
       prev snapshot 2 days ago = 79990 -> recent (80000-79990)/2 = 5/day.

    Pure lifetime ranking (50 vs 40) is close and A only barely leads; but the
    REAL test is the recent surge: 5000/day vs 5/day. A must rank above B.
    To make this discriminating, also seed a DECOY that lifetime-ranks ABOVE A:
    C: created 100 days ago, 9000 stars -> lifetime 90/day (beats A's lifetime 50),
       prev snapshot 2 days ago = 8990 -> recent (9000-8990)/2 = 5/day.
    If ranking were by lifetime, order would be C(90) > A(50) > B(40).
    By recent velocity it must be A(5000) > {C(5)~B(5)}, i.e. A first.
    """
    a = await _mk(db_session, "surger", 100_000, 2000, uniq="gv2a")
    b = await _mk(db_session, "placid", 80_000, 2000, uniq="gv2b")
    c = await _mk(db_session, "lifetimelead", 9_000, 100, uniq="gv2c")
    await _insert_snapshot(db_session, a, 90_000, observed_days_ago=2)
    await _insert_snapshot(db_session, b, 79_990, observed_days_ago=2)
    await _insert_snapshot(db_session, c, 8_990, observed_days_ago=2)

    cands = await _ranked_candidates(db_session, _board_settings())
    order = [iid for iid, *_ in cands]
    targets = {a, b, c}
    assert targets <= set(order), "all three candidates must be present"
    pos = {iid: order.index(iid) for iid in targets}

    # A surged -> must be first among the three (recent 5000/day dwarfs the rest)
    assert pos[a] < pos[b], "surging old repo A must outrank placid old repo B"
    assert pos[a] < pos[c], (
        "surging A must outrank lifetime-leader C — proves recent velocity, not "
        f"lifetime avg, drives the order. positions={pos}"
    )

    # Sanity: under LIFETIME avg, C(90/day) would lead A(50/day). Confirm the code
    # did NOT produce that (would mean it ignored snapshots).
    now = datetime.now(UTC)
    life_a = _star_velocity(100_000, (now - timedelta(days=2000)).isoformat(), now)
    life_c = _star_velocity(9_000, (now - timedelta(days=100)).isoformat(), now)
    assert life_c > life_a, "precondition: C's lifetime avg exceeds A's"


@pytest.mark.asyncio
async def test_ranked_candidates_tiebreak_by_total_stars(db_session):
    """Equal recent velocity -> higher TOTAL stars first (the documented tiebreak)."""
    hi = await _mk(db_session, "tiehi", 50_000, 500, uniq="gv2th")
    lo = await _mk(db_session, "tielo", 5_000, 500, uniq="gv2tl")
    # both gain 200 over 2 days -> recent 100/day each. SAME prev timestamp for
    # both so the two velocities are bit-identical and only the tiebreak decides.
    shared_prev = datetime.now(UTC) - timedelta(days=2)
    await _insert_snapshot(db_session, hi, 49_800, 2, observed_at=shared_prev)
    await _insert_snapshot(db_session, lo, 4_800, 2, observed_at=shared_prev)

    cands = await _ranked_candidates(db_session, _board_settings())
    order = [iid for iid, *_ in cands]
    # verify equal recent velocity precondition (shared prev_at -> identical delta)
    now = datetime.now(UTC)
    assert _recent_velocity(50_000, (49_800, shared_prev), None, now) == (
        _recent_velocity(5_000, (4_800, shared_prev), None, now)
    )
    assert order.index(hi) < order.index(lo), "equal velocity -> more total stars first"


# ===========================================================================
# Requirement 3 — run_github_board snapshots the FULL candidate pool, not top-N
# ===========================================================================

@pytest.mark.asyncio
async def test_run_github_board_snapshots_whole_candidate_pool(db_session, monkeypatch):
    """End-to-end: with #candidates > limit, item_timeline gains exactly
    #candidates rows (not limit rows). This is the 'chicken-and-egg' fix — repos
    that did NOT chart still get a snapshot so a future delta is computable.

    summarize + PNG render are stubbed (external/LLM); we only grade the snapshot
    write inside the run_github_board transaction. The whole thing rides the test
    transaction (rolled back by the db_session fixture).
    """
    # Stub the heavy/external tail so the run reaches its end cleanly.
    async def _fake_summarize(*a, **k):
        return {"needs_review": 0}

    async def _fake_render(*a, **k):
        return None

    monkeypatch.setattr(gh, "run_summarize", _fake_summarize)
    import pulsewire.render.engine as render_engine
    monkeypatch.setattr(render_engine, "render_overview_png", _fake_render)

    # Isolate: clear pre-existing github candidates so the count is deterministic.
    await db_session.execute(
        sa_text("DELETE FROM items WHERE facts->'github'->>'stars' IS NOT NULL")
    )

    # 5 distinct-theme candidates, all with created_at (cold start -> lifetime sort).
    ids = []
    for i in range(5):
        iid = await _mk(db_session, f"grdrpool{i}-x", 10_000 - i * 100, 10 + i, uniq=f"pool{i}")
        ids.append(iid)
    n_candidates = len(ids)

    limit = 2  # picked top-N is far fewer than the candidate pool
    settings = _board_settings(limit=limit)

    # Build a sessionmaker whose sessions ride THIS test's connection/transaction,
    # so run_github_board's `async with sm()` shares the rollback-on-exit tx and
    # we can read the rows it wrote.
    conn = db_session.bind
    sm = async_sessionmaker(bind=conn, expire_on_commit=False)

    timeline_before = (
        await db_session.execute(sa_text("SELECT count(*) FROM item_timeline"))
    ).scalar_one()

    await run_github_board(settings, run_id=None, trigger_type="daily", sessionmaker=sm)

    timeline_after = (
        await db_session.execute(sa_text("SELECT count(*) FROM item_timeline"))
    ).scalar_one()
    written = timeline_after - timeline_before

    # Confirm there genuinely were more candidates than the pick limit.
    assert n_candidates > limit, "precondition: candidate pool must exceed limit"
    # The crux: snapshots written == candidate count, NOT the (smaller) limit.
    assert written == n_candidates, (
        f"expected {n_candidates} snapshot rows (whole pool), got {written}; "
        f"if it equals limit ({limit}) the code only snapshotted the top-N pick"
    )
    assert written != limit, "must not snapshot only the top-N pick"

    # And exactly one snapshot row per candidate item.
    rows = (
        await db_session.execute(
            sa_text("SELECT item_id, count(*) c FROM item_timeline GROUP BY item_id")
        )
    ).all()
    snapped = {r.item_id: r.c for r in rows}
    for iid in ids:
        assert snapped.get(iid) == 1, f"candidate {iid} should have exactly 1 snapshot row"


# ===========================================================================
# Requirement 4 — cold start == old behaviour (rank by _star_velocity)
# ===========================================================================

@pytest.mark.asyncio
async def test_cold_start_order_equals_star_velocity_order(db_session):
    """No history anywhere -> _recent_velocity falls back to _star_velocity for
    every candidate, so the produced order must equal sorting by lifetime velocity
    (tiebreak total stars), i.e. zero regression vs v1.

    Distinct themes so theme-dedup doesn't interfere; isolate the live corpus.
    """
    await db_session.execute(
        sa_text("DELETE FROM items WHERE facts->'github'->>'stars' IS NOT NULL")
    )
    now = datetime.now(UTC)

    # (slug, stars, age_days) -> lifetime velocity
    specs = [
        ("grdrcoldalpha", 3_000, 6),     # 500/day
        ("grdrcoldbeta", 9_000, 90),     # 100/day
        ("grdrcoldgamma", 120_000, 2000),  # 60/day
        ("grdrcolddelta", 800, 4),       # 200/day
        ("grdrcoldeps", 50_000, 1000),   # 50/day
    ]
    id_by_slug = {}
    for i, (slug, stars, age) in enumerate(specs):
        iid = await _mk(db_session, slug, stars, age, uniq=f"cold{i}")
        id_by_slug[iid] = (stars, age)

    # No item_timeline rows inserted -> pure cold start.
    cands = await _ranked_candidates(db_session, _board_settings())
    order = [iid for iid, *_ in cands if iid in id_by_slug]

    # Independently compute the expected v1 order: _star_velocity desc, tiebreak stars desc.
    expected = sorted(
        id_by_slug,
        key=lambda iid: (
            _star_velocity(id_by_slug[iid][0],
                           (now - timedelta(days=id_by_slug[iid][1])).isoformat(), now),
            id_by_slug[iid][0],
        ),
        reverse=True,
    )
    assert order == expected, (
        f"cold-start order must match lifetime _star_velocity order.\n"
        f"got      {order}\nexpected {expected}"
    )


@pytest.mark.asyncio
async def test_cold_start_matches_select_trending_v1_semantics(db_session):
    """Cross-check: with no history, the surging-vs-placid scenario from req 2
    collapses to lifetime ranking — proving cold start really is v1 behaviour and
    the snapshot path is what flips it. Here C (lifetime 90/day) leads A (50/day)."""
    await db_session.execute(
        sa_text("DELETE FROM items WHERE facts->'github'->>'stars' IS NOT NULL")
    )
    a = await _mk(db_session, "coldsurger", 100_000, 2000, uniq="cs2a")   # 50/day
    c = await _mk(db_session, "coldlifelead", 9_000, 100, uniq="cs2c")    # 90/day
    # NO snapshots -> cold start

    cands = await _ranked_candidates(db_session, _board_settings())
    order = [iid for iid, *_ in cands]
    assert order.index(c) < order.index(a), (
        "cold start (no snapshots) must rank by lifetime: C(90/day) before A(50/day)"
    )
