"""INDEPENDENT GRADER tests — derived expected values, distinct numbers from repo tests.

Covers:
  Unit 1: _star_velocity(stars, created_at_raw, now)
  Unit 2: _apply_rolling_window(url)
  Unit 3: ranking contract of _select_trending (DB)
Do NOT trust repo comments/tests; expected values computed here from the spec.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pulsewire.config import get_settings
from pulsewire.github_board.engine import _select_trending, _star_velocity
from pulsewire.sources.github import _apply_rolling_window
from pulsewire.store import repo

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Unit 1 — _star_velocity
# ---------------------------------------------------------------------------

def test_velocity_basic_value():
    """stars / age_days, age computed from now-created. 10000 stars, 100 days -> 100.0."""
    now = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)
    created = (now - timedelta(days=100)).isoformat()
    v = _star_velocity(10000, created, now)
    assert v == pytest.approx(100.0)


def test_velocity_more_stars_higher_for_fixed_age():
    now = datetime(2026, 6, 19, tzinfo=UTC)
    created = (now - timedelta(days=50)).isoformat()
    low = _star_velocity(1000, created, now)
    high = _star_velocity(5000, created, now)
    assert high > low
    # exact: 5000/50=100, 1000/50=20
    assert high == pytest.approx(100.0)
    assert low == pytest.approx(20.0)


def test_velocity_older_repo_lower_for_fixed_stars():
    now = datetime(2026, 6, 19, tzinfo=UTC)
    young = _star_velocity(3000, (now - timedelta(days=30)).isoformat(), now)   # 100/day
    old = _star_velocity(3000, (now - timedelta(days=300)).isoformat(), now)    # 10/day
    assert young > old
    assert young == pytest.approx(100.0)
    assert old == pytest.approx(10.0)


def test_velocity_young_few_stars_beats_old_many_stars():
    """A young repo with FEWER absolute stars can exceed an old repo with MANY."""
    now = datetime(2026, 6, 19, tzinfo=UTC)
    young_few = _star_velocity(2000, (now - timedelta(days=5)).isoformat(), now)   # 400/day
    old_many = _star_velocity(80000, (now - timedelta(days=1000)).isoformat(), now)  # 80/day
    assert young_few > old_many
    assert young_few == pytest.approx(400.0)
    assert old_many == pytest.approx(80.0)


def test_velocity_missing_created_at_is_zero():
    now = datetime(2026, 6, 19, tzinfo=UTC)
    assert _star_velocity(99999, None, now) == 0.0
    assert _star_velocity(99999, "", now) == 0.0


def test_velocity_garbage_created_at_is_zero():
    now = datetime(2026, 6, 19, tzinfo=UTC)
    assert _star_velocity(123, "not-a-date", now) == 0.0
    assert _star_velocity(123, "2026-13-99", now) == 0.0       # impossible month/day
    assert _star_velocity(123, "garbage-Z", now) == 0.0
    # leftover non-iso text after Z-replace also must not crash
    assert _star_velocity(123, "hello Z world", now) == 0.0


def test_velocity_younger_than_one_day_clamped_to_floor():
    """age floor of 1.0 day: a repo a few hours old must NOT inflate."""
    now = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)
    created = (now - timedelta(hours=2)).isoformat()  # 2 hours old
    v = _star_velocity(500, created, now)
    # without floor would be 500 / (2/24) = 6000; floor clamps age to 1.0 day -> 500
    assert v == pytest.approx(500.0)
    # exactly 1 day old -> stars / 1
    created_1d = (now - timedelta(days=1)).isoformat()
    assert _star_velocity(500, created_1d, now) == pytest.approx(500.0)


def test_velocity_future_created_at_no_crash_no_negative_inversion():
    """now < created: must not crash, must not yield negative age that inverts ranking.

    Spec: age clamps to 1.0 floor, so velocity = stars (positive).
    """
    now = datetime(2026, 6, 19, tzinfo=UTC)
    future = (now + timedelta(days=365)).isoformat()
    v = _star_velocity(1000, future, now)
    assert v == pytest.approx(1000.0)   # age floored to 1.0 -> stars/1
    assert v > 0
    # ranking sanity: a future-dated repo with fewer stars must NOT outrank
    # a normal recent repo with a genuinely higher velocity.
    normal = _star_velocity(5000, (now - timedelta(days=2)).isoformat(), now)  # 2500/day
    assert normal > v


def test_velocity_tz_naive_treated_as_utc_equals_tz_aware():
    now = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)
    aware = "2026-03-21T12:00:00+00:00"     # tz-aware
    naive = "2026-03-21T12:00:00"           # tz-naive, must be treated as UTC
    z_form = "2026-03-21T12:00:00Z"         # Z suffix
    va = _star_velocity(4000, aware, now)
    vn = _star_velocity(4000, naive, now)
    vz = _star_velocity(4000, z_form, now)
    assert va == pytest.approx(vn)
    assert va == pytest.approx(vz)
    # sanity on the actual value: 2026-03-21 12:00 -> 2026-06-19 12:00 = 90 days; 4000/90
    assert va == pytest.approx(4000.0 / 90.0)


def test_velocity_date_only_iso():
    """Date-only ISO string (no time) is valid ISO-8601, tz-naive -> UTC."""
    now = datetime(2026, 6, 19, 0, 0, 0, tzinfo=UTC)
    v = _star_velocity(1000, "2026-05-20", now)  # 30 days before
    assert v == pytest.approx(1000.0 / 30.0)


# ---------------------------------------------------------------------------
# Unit 2 — _apply_rolling_window
# ---------------------------------------------------------------------------

def test_rolling_window_n60_matches_today_minus_60():
    url = "https://api.github.com/search/repositories?q=stars:>10+created:>{created_since:60d}"
    out = _apply_rolling_window(url)
    expected_date = (datetime.now(UTC).date() - timedelta(days=60)).isoformat()
    assert expected_date in out
    assert out == (
        "https://api.github.com/search/repositories?q=stars:>10+created:>"
        + expected_date
    )
    # token fully gone
    assert "{created_since" not in out


def test_rolling_window_no_token_identity():
    url = "https://api.github.com/search/repositories?q=stars:>500+language:python"
    assert _apply_rolling_window(url) == url


def test_rolling_window_two_different_tokens_both_resolve():
    url = "a={created_since:7d}&b={created_since:30d}&tail=stays"
    out = _apply_rolling_window(url)
    today = datetime.now(UTC).date()
    d7 = (today - timedelta(days=7)).isoformat()
    d30 = (today - timedelta(days=30)).isoformat()
    assert out == f"a={d7}&b={d30}&tail=stays"
    assert "{created_since" not in out


def test_rolling_window_rest_of_url_untouched():
    url = "X{created_since:1d}Y"
    out = _apply_rolling_window(url)
    d1 = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
    assert out == f"X{d1}Y"


def test_rolling_window_repeated_same_token():
    url = "{created_since:5d}/{created_since:5d}"
    out = _apply_rolling_window(url)
    d5 = (datetime.now(UTC).date() - timedelta(days=5)).isoformat()
    assert out == f"{d5}/{d5}"


def test_rolling_window_zero_days():
    url = "created:>{created_since:0d}"
    out = _apply_rolling_window(url)
    today = datetime.now(UTC).date().isoformat()
    assert out == f"created:>{today}"


# ---------------------------------------------------------------------------
# Unit 3 — _select_trending ranking contract (DB)
# ---------------------------------------------------------------------------

def _board_settings():
    """Large limit + wide recency so nothing is filtered for capacity reasons."""
    base = get_settings()
    rank = base.rank.model_copy(update={
        "github_board_exclude": [],
        "github_board_limit": 500,
        "github_board_recency_days": 720,
    })
    return base.model_copy(update={"rank": rank})


@pytest.mark.asyncio
async def test_select_trending_velocity_order_contradicts_absolute_stars(db_session, clean_github_candidates):
    """Independent scenario (numbers distinct from the repo's own test).

    Velocity (stars/age_days):
      blaze   : 1500 / 6   = 250.0   (newest, modest absolute stars)
      surge   : 9000 / 90  = 100.0
      titan   : 120000 / 2000 = 60.0 (HUGE absolute stars, very old)
      ghost   : 250000 stars, NO created_at -> velocity 0 (must sink to bottom)

    Absolute-stars order would be: ghost(250000) > titan(120000) > surge(9000) > blaze(1500)
    Required velocity order is the near-opposite: blaze > surge > titan > ghost.
    """
    now = datetime.now(UTC)
    reg = "github-search-ai-agents"
    uniq = "gradervel"

    async def mk(slug, stars, created_days_ago):
        facts = {"github": {"stars": stars}}
        if created_days_ago is not None:
            facts["github"]["created_at"] = (now - timedelta(days=created_days_ago)).isoformat()
        return await repo.upsert_item(
            db_session, source=reg,
            url=f"https://github.com/{uniq}/{slug}",
            title=slug, published_at=now, facts=facts,
        )

    blaze = await mk("blaze", 1500, 6)        # 250/day
    surge = await mk("surge", 9000, 90)       # 100/day
    titan = await mk("titan", 120000, 2000)   # 60/day
    ghost = await mk("ghost", 250000, None)   # velocity 0

    picked = await _select_trending(db_session, _board_settings())
    order = [iid for iid, _ in picked]
    targets = {blaze, surge, titan, ghost}
    sub = [iid for iid in order if iid in targets]

    # all four present
    assert set(sub) == targets, f"missing some candidates: got {sub}"

    pos = {iid: order.index(iid) for iid in targets}
    # Required velocity DESC order
    assert pos[blaze] < pos[surge] < pos[titan] < pos[ghost], (
        f"velocity order violated: positions={pos}"
    )

    # And confirm this CONTRADICTS pure absolute-stars order (ghost would be #1).
    stars_map = {blaze: 1500, surge: 9000, titan: 120000, ghost: 250000}
    by_abs = sorted(targets, key=lambda i: stars_map[i], reverse=True)
    velocity_sub = sorted(targets, key=lambda i: pos[i])  # actual order
    assert velocity_sub != by_abs, "velocity order must differ from absolute-stars order"
    # specifically: the highest-absolute-stars repo (ghost) must be LAST, not first
    assert pos[ghost] == max(pos.values())
    assert pos[blaze] == min(pos.values())


@pytest.mark.asyncio
async def test_select_trending_tiebreak_by_absolute_stars(db_session, clean_github_candidates):
    """Equal velocity -> absolute stars is the tiebreaker (higher stars ranks first)."""
    now = datetime.now(UTC)
    reg = "github-search-ai-agents"
    uniq = "gradertie"

    async def mk(slug, stars, created_days_ago):
        facts = {"github": {"stars": stars,
                            "created_at": (now - timedelta(days=created_days_ago)).isoformat()}}
        return await repo.upsert_item(
            db_session, source=reg,
            url=f"https://github.com/{uniq}/{slug}",
            title=slug, published_at=now, facts=facts,
        )

    # Same velocity = 100/day for both, but different absolute stars.
    # NB: slugs share no significant name-token (kepler vs hubble) so the board's
    # theme-diversity dedup doesn't fold one away — this isolates the tiebreak contract.
    low = await mk("kepler", 1000, 10)    # 100/day, 1000 stars
    high = await mk("hubble", 5000, 50)   # 100/day, 5000 stars

    # sanity: velocities are actually equal
    assert _star_velocity(1000, (now - timedelta(days=10)).isoformat(), now) == pytest.approx(
        _star_velocity(5000, (now - timedelta(days=50)).isoformat(), now)
    )

    picked = await _select_trending(db_session, _board_settings())
    order = [iid for iid, _ in picked]
    assert order.index(high) < order.index(low), (
        "tiebreak failed: higher absolute stars should rank first on equal velocity"
    )
