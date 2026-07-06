# pulsewire evals

This is the first local eval layer for pulsewire. It turns the product rubric into deterministic, repeatable checks:

- source authority, freshness posture, and domain breadth;
- safety around trusted numbers, unresolved fact tokens, and high-risk claims;
- delivered daily archive integrity;
- summary readability and safety proxies;
- long-horizon tracking quality;
- optional manual hot-topic recall.

Run everything:

```bash
uv run python evals/run_local.py
```

Run one suite:

```bash
uv run python evals/run_local.py --suite safety
uv run python evals/run_local.py --suite summary
uv run python evals/run_local.py --suite threads
```

Grade a specific archive:

```bash
uv run python evals/run_local.py --archive web/archive/daily/2026-06-15.json
```

Results are written to `evals/results/latest.json`. The runner exits non-zero if any hard fail check fails.

## Suites

`source`
: Checks that the configured source pool is broad enough across AI, bio, and geo, has enough high-weight/whitelisted sources, and warns on duplicate enabled feed URLs.

`safety`
: Calls the real `verify_item()` path. These are hard invariants: trusted `{Fn}` values must resolve, unknown tokens must become `[待核实]`, unsourced numbers must be reviewed, and single-source high-risk claims must not pass silently.

`delivery`
: Checks the newest `web/archive/daily/*.json` artifact is fresh, non-empty, multi-domain, includes GitHub board content, exposes tracked threads, and has the fields needed by the app.

`summary`
: Uses deterministic proxies for readability and editorial safety: minimum lengths, explainer language, `[待核实]` marker integrity, high-risk claim hedging, and duplicate-story detection.

`threads`
: Checks visible tracked threads span multiple dates, have timeline depth, and render newest-first.

`hot_news`
: Skipped until a human supplies a reference topic set. This eval deliberately does not invent "today's true hot news." Add topics to the `manual_hot_topic_recall_v0` case when you want to grade recall against a hand-made truth set.

## Adjusting The Bar

The first-layer standard lives in `evals/cases.jsonl`. Tighten thresholds there after observing a few real daily runs. Good candidates for hard-fail gates:

- unsourced numbers shown as normal content;
- single-source IPO/war/clinical/performance claims without review;
- primary daily archive empty or stale;
- tracked thread timelines broken;
- severe duplicate stories in the same domain.
