"""Independent acceptance tests for _judge_and_merge.

Grader-owned. Does NOT trust the in-repo implementation or its docstrings.
We derive truth from our OWN serial reference and from direct instrumentation.
"""
from __future__ import annotations

import asyncio

import pytest

from pulsewire.events.engine import _UF, _judge_and_merge


# ---------------------------------------------------------------------------
# Our OWN serial reference (written from scratch — not copied from the repo).
# For each pair in list order: await judge_one, and on a truthy verdict
# immediately apply uf.union. Exceptions -> treated as falsy (no union).
# Returns the count of unions performed.
# ---------------------------------------------------------------------------
async def serial_reference(pairs, judge_one, uf):
    merges = 0
    for cx, cy in pairs:
        try:
            verdict = await judge_one(cx, cy)
        except Exception:
            verdict = False
        if verdict:
            uf.union(cx, cy)
            merges += 1
    return merges


def partition(uf, ids):
    """Map each id -> its representative root (for partition + root identity)."""
    return {i: uf.find(i) for i in ids}


def components(root_map):
    """Frozenset of frozensets: the bare partition, ignoring root identity."""
    groups = {}
    for i, r in root_map.items():
        groups.setdefault(r, set()).add(i)
    return frozenset(frozenset(v) for v in groups.values())


def make_deterministic_judge(truthy_pairs, delays=None, raises=None):
    """Stub judge_one with a fixed verdict per (cx, cy).

    truthy_pairs: set of pairs that should return True.
    delays:       optional dict pair -> sleep seconds (to scramble completion order).
    raises:       optional set of pairs that should raise.
    Both serial reference and parallel function use the SAME stub instance so
    they observe identical verdicts.
    """
    truthy_pairs = set(truthy_pairs)
    delays = delays or {}
    raises = set(raises or set())

    async def judge_one(cx, cy):
        d = delays.get((cx, cy), 0)
        if d:
            await asyncio.sleep(d)
        if (cx, cy) in raises:
            raise RuntimeError(f"boom for {(cx, cy)}")
        return (cx, cy) in truthy_pairs

    return judge_one


async def run_both(ids, pairs, truthy_pairs, delays=None, raises=None, conc=4):
    """Run parallel function and serial reference on identical inputs/verdicts.

    Returns (par_root_map, par_count, ser_root_map, ser_count).
    Fresh _UF and fresh stub for each run so they cannot interfere.
    """
    judge_par = make_deterministic_judge(truthy_pairs, delays, raises)
    uf_par = _UF(ids)
    par_count = await _judge_and_merge(pairs, judge_par, uf_par, conc)
    par_roots = partition(uf_par, ids)

    judge_ser = make_deterministic_judge(truthy_pairs, delays, raises)
    uf_ser = _UF(ids)
    ser_count = await serial_reference(pairs, judge_ser, uf_ser)
    ser_roots = partition(uf_ser, ids)
    return par_roots, par_count, ser_roots, ser_count


# ---------------------------------------------------------------------------
# EQUIVALENCE — partition, exact root identity, and returned count must all
# match the serial (list-order) reference, across many scenarios.
# ---------------------------------------------------------------------------
EQUIV_SCENARIOS = {
    "empty": dict(
        ids=["a", "b"], pairs=[], truthy=set(),
    ),
    "single_true": dict(
        ids=["a", "b"], pairs=[("a", "b")], truthy={("a", "b")},
    ),
    "single_false": dict(
        ids=["a", "b"], pairs=[("a", "b")], truthy=set(),
    ),
    "chain": dict(
        ids=["a", "b", "c", "d", "e"],
        pairs=[("a", "b"), ("b", "c"), ("c", "d"), ("d", "e")],
        truthy={("a", "b"), ("b", "c"), ("c", "d"), ("d", "e")},
    ),
    "cyclic_abc": dict(  # (a,b),(b,c),(a,c) — redundant union ordering matters for roots
        ids=["a", "b", "c"],
        pairs=[("a", "b"), ("b", "c"), ("a", "c")],
        truthy={("a", "b"), ("b", "c"), ("a", "c")},
    ),
    "overlap_interspersed_falsy": dict(
        ids=["a", "b", "c", "d", "e", "f"],
        pairs=[("a", "b"), ("c", "d"), ("b", "c"), ("e", "f"), ("a", "e")],
        truthy={("a", "b"), ("b", "c"), ("a", "e")},  # (c,d),(e,f) falsy
    ),
    "two_clusters_then_bridge": dict(
        ids=["1", "2", "3", "4"],
        pairs=[("1", "2"), ("3", "4"), ("2", "3")],
        truthy={("1", "2"), ("3", "4"), ("2", "3")},
    ),
    "all_false": dict(
        ids=["a", "b", "c"],
        pairs=[("a", "b"), ("b", "c")],
        truthy=set(),
    ),
    # Order-sensitive ROOT test: completion order is reversed vs list order via
    # delays. Earlier-listed pairs sleep LONGER so they finish LAST. If the
    # implementation merged in completion order, roots would diverge from serial.
    "root_order_sensitive": dict(
        ids=["a", "b", "c", "d"],
        pairs=[("a", "b"), ("b", "c"), ("c", "d")],
        truthy={("a", "b"), ("b", "c"), ("c", "d")},
        delays={("a", "b"): 0.06, ("b", "c"): 0.03, ("c", "d"): 0.0},
    ),
    # Another root-identity trap: a star where the bridging order decides the
    # final root. Reverse completion order via delays.
    "star_reversed_completion": dict(
        ids=["hub", "x", "y", "z"],
        pairs=[("x", "hub"), ("y", "hub"), ("z", "hub")],
        truthy={("x", "hub"), ("y", "hub"), ("z", "hub")},
        delays={("x", "hub"): 0.05, ("y", "hub"): 0.025, ("z", "hub"): 0.0},
    ),
    # Diamond: two redundant bridges in scrambled completion order.
    "diamond_scrambled": dict(
        ids=["a", "b", "c", "d"],
        pairs=[("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")],
        truthy={("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")},
        delays={("a", "b"): 0.04, ("a", "c"): 0.01, ("b", "d"): 0.03, ("c", "d"): 0.0},
    ),
}


@pytest.mark.parametrize("name", list(EQUIV_SCENARIOS))
async def test_equivalence_roots_partition_count(name):
    sc = EQUIV_SCENARIOS[name]
    par_roots, par_count, ser_roots, ser_count = await run_both(
        sc["ids"], sc["pairs"], sc["truthy"],
        delays=sc.get("delays"), conc=4,
    )
    # Returned count must match serial.
    assert par_count == ser_count, (
        f"[{name}] count parallel={par_count} serial={ser_count}"
    )
    # Bare partition must match.
    assert components(par_roots) == components(ser_roots), (
        f"[{name}] partition mismatch\n par={components(par_roots)}\n ser={components(ser_roots)}"
    )
    # EXACT root identity per id must match (the load-bearing property).
    assert par_roots == ser_roots, (
        f"[{name}] root identity mismatch\n par={par_roots}\n ser={ser_roots}"
    )


async def test_equivalence_root_divergent_completion_trap():
    """DETERMINISTIC trap that provably distinguishes list-order from
    completion-order union application at the level of ROOT IDENTITY (not just
    partition). Verified offline: applying these unions in list order yields
    root 'c' for all ids; applying in the delay-induced completion order
    ((c,b) before (b,c)) yields root 'b'. The function under test must match the
    list-order serial reference exactly.

    pairs = [(a,b),(a,b),(b,c),(c,b),(a,c)]  (redundant + reversed pairs)
    Delays are keyed by pair value so that (c,b) completes before (b,c).
    """
    ids = ["a", "b", "c"]
    pairs = [("a", "b"), ("a", "b"), ("b", "c"), ("c", "b"), ("a", "c")]
    # Completion order forced via per-pair delays: (a,b) first, then (c,b),
    # then (b,c), then (a,c) — which is the order-divergent permutation.
    delays = {("a", "b"): 0.00, ("c", "b"): 0.02, ("b", "c"): 0.03, ("a", "c"): 0.05}
    truthy = set(pairs)

    par_roots, par_count, ser_roots, ser_count = await run_both(
        ids, pairs, truthy, delays=delays, conc=10,
    )
    # Serial (list-order) reference must resolve all ids to root 'c'.
    assert ser_roots == {"a": "c", "b": "c", "c": "c"}, f"ref sanity failed: {ser_roots}"
    assert par_count == ser_count
    # The load-bearing assertion: parallel roots match list-order serial exactly
    # (a completion-order impl would give root 'b' here and fail this).
    assert par_roots == ser_roots, (
        f"ROOT IDENTITY diverged: par={par_roots} ser={ser_roots} "
        f"(completion-order bug would yield root 'b')"
    )


async def test_equivalence_randomized_fuzz():
    """Adversarial fuzz: many random scenarios with scrambled delays, comparing
    EXACT root identity against the serial reference. Designed to catch any
    completion-order leakage into root selection."""
    import random

    rng = random.Random(20260619)
    for trial in range(300):
        n = rng.randint(2, 9)
        ids = [f"c{i}" for i in range(n)]
        # Generate a pile of candidate pairs (allow duplicates / cycles).
        n_pairs = rng.randint(0, 14)
        pairs = []
        for _ in range(n_pairs):
            i, j = rng.sample(range(n), 2) if n >= 2 else (0, 0)
            pairs.append((ids[i], ids[j]))
        # Random truthy subset.
        truthy = {p for p in pairs if rng.random() < 0.6}
        # Random delays to scramble completion order hard.
        delays = {p: round(rng.uniform(0, 0.02), 4) for p in set(pairs)}
        conc = rng.randint(1, 6)

        par_roots, par_count, ser_roots, ser_count = await run_both(
            ids, pairs, truthy, delays=delays, conc=conc,
        )
        assert par_count == ser_count, f"trial {trial}: count {par_count} != {ser_count}\npairs={pairs}\ntruthy={truthy}"
        assert par_roots == ser_roots, (
            f"trial {trial}: ROOT IDENTITY mismatch\n pairs={pairs}\n truthy={truthy}\n"
            f" delays={delays}\n conc={conc}\n par={par_roots}\n ser={ser_roots}"
        )


# ---------------------------------------------------------------------------
# CONCURRENCY is real & bounded.
# ---------------------------------------------------------------------------
async def test_concurrency_bounded_and_real():
    conc = 3
    n_pairs = 12
    ids = [f"c{i}" for i in range(n_pairs * 2)]
    pairs = [(f"c{2*i}", f"c{2*i+1}") for i in range(n_pairs)]

    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def judge_one(cx, cy):
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        # Hold the slot so overlap can actually accumulate.
        await asyncio.sleep(0.02)
        async with lock:
            in_flight -= 1
        return False

    uf = _UF(ids)
    await _judge_and_merge(pairs, judge_one, uf, conc)

    # (b) never exceeds conc
    assert max_in_flight <= conc, f"max_in_flight={max_in_flight} exceeded conc={conc}"
    # (a) actually concurrent (not secretly serial)
    assert max_in_flight > 1, f"never ran >1 concurrently (max_in_flight={max_in_flight})"
    # And with enough pairs it should saturate the bound.
    assert max_in_flight == conc, f"expected saturation at {conc}, got {max_in_flight}"


async def test_concurrency_saturates_higher_bound():
    conc = 8
    n_pairs = 20
    ids = [f"c{i}" for i in range(n_pairs * 2)]
    pairs = [(f"c{2*i}", f"c{2*i+1}") for i in range(n_pairs)]

    in_flight = 0
    max_in_flight = 0

    async def judge_one(cx, cy):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return True

    uf = _UF(ids)
    merges = await _judge_and_merge(pairs, judge_one, uf, conc)
    assert max_in_flight <= conc
    assert max_in_flight == conc, f"expected saturation at {conc}, got {max_in_flight}"
    assert merges == n_pairs


# ---------------------------------------------------------------------------
# CONSERVATIVE on exception — a raising pair yields NO union, no crash.
# ---------------------------------------------------------------------------
async def test_conservative_on_exception_no_union_no_crash():
    ids = ["a", "b", "c", "d", "e", "f"]
    pairs = [("a", "b"), ("c", "d"), ("e", "f")]
    truthy = {("a", "b"), ("c", "d"), ("e", "f")}
    raises = {("c", "d")}  # this pair must not merge and must not crash

    judge = make_deterministic_judge(truthy, raises=raises)
    uf = _UF(ids)
    merges = await _judge_and_merge(pairs, judge, uf, 4)

    # c and d must remain separate (exception => falsy => no union).
    assert uf.find("c") != uf.find("d"), "raising pair was merged anyway"
    # The other two pairs still merged.
    assert uf.find("a") == uf.find("b")
    assert uf.find("e") == uf.find("f")
    # Count excludes the raising pair.
    assert merges == 2, f"expected 2 merges, got {merges}"


async def test_conservative_on_exception_matches_serial():
    """With exceptions interspersed, parallel must still equal serial exactly."""
    ids = ["a", "b", "c", "d", "e"]
    pairs = [("a", "b"), ("b", "c"), ("c", "d"), ("d", "e")]
    truthy = {("a", "b"), ("b", "c"), ("c", "d"), ("d", "e")}
    raises = {("b", "c"), ("d", "e")}  # break the chain at two points

    par_roots, par_count, ser_roots, ser_count = await run_both(
        ids, pairs, truthy, raises=raises, conc=4,
    )
    assert par_count == ser_count
    assert par_roots == ser_roots, f"par={par_roots} ser={ser_roots}"
    # Sanity: chain should split into {a,b}, {c,d}, {e}
    assert components(par_roots) == frozenset(
        {frozenset({"a", "b"}), frozenset({"c", "d"}), frozenset({"e"})}
    )


# ---------------------------------------------------------------------------
# RETURN VALUE & IN-PLACE MUTATION.
# ---------------------------------------------------------------------------
async def test_return_count_and_inplace_mutation():
    ids = ["a", "b", "c", "d"]
    pairs = [("a", "b"), ("b", "c"), ("c", "d")]
    truthy = {("a", "b"), ("c", "d")}  # 2 truthy, 1 falsy

    judge = make_deterministic_judge(truthy)
    uf = _UF(ids)
    uf_id_before = id(uf)
    merges = await _judge_and_merge(pairs, judge, uf, 4)

    assert merges == 2, f"expected 2 merges, got {merges}"
    # Same object mutated in place.
    assert id(uf) == uf_id_before
    assert uf.find("a") == uf.find("b")
    assert uf.find("c") == uf.find("d")
    assert uf.find("a") != uf.find("c")  # (b,c) was falsy


async def test_return_count_zero_on_empty():
    uf = _UF(["a", "b"])
    judge = make_deterministic_judge(set())
    merges = await _judge_and_merge([], judge, uf, 4)
    assert merges == 0
    assert uf.find("a") == "a" and uf.find("b") == "b"
