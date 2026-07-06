<p align="center"><img src="assets/banner.png" alt="Pulsewire Daily" width="100%"></p>

<p align="center">
  <a href="https://github.com/birdindasky/pulsewire/actions/workflows/ci.yml"><img src="https://github.com/birdindasky/pulsewire/actions/workflows/ci.yml/badge.svg" alt="ci"></a>
  <img src="https://img.shields.io/badge/license-MIT-8b8474" alt="MIT">
  <img src="https://img.shields.io/badge/python-3.12%2B-8b8474" alt="python 3.12+">
  <img src="https://img.shields.io/badge/for-macOS%20(Apple%20Silicon)-8b8474" alt="macOS">
  <a href="README.md"><img src="https://img.shields.io/badge/README-中文-D9331F" alt="中文"></a>
</p>

**pulsewire is a news intelligence engine that runs entirely on your own Mac.** Every day it pulls from **331 registered sources** (323 currently enabled: journals, lab blogs, major outlets, communities, GitHub) across AI / biotech / geopolitics / open source, clusters them into *events*, runs them through a panel of LLM judges, verifies every number against its source, and delivers a scrapbook-style daily digest — as a web page, a macOS desktop app, and (optionally) a Feishu push.

Three principles are welded into the architecture:

- **Scarcity over filler** — fluff, off-topic and not-newsworthy items go to the wastebasket. A thin news day produces a thin issue, never a padded one.
- **Zero fabricated numbers** — the LLM never sees numbers, only placeholders. The system fills real values back in from the source; anything untraceable gets a visible "unverified" stamp.
- **No reruns** — a story covered on previous days gets a red "Day N of coverage" stamp and only the *new* developments are written; the full timeline lives in the tracking view.

> **Note:** the digest output is written in **Chinese** — global sources in, plain-Chinese daily briefing out. That's the product. The codebase, config and docs are navigable for non-Chinese speakers, but the daily issue itself is Chinese.

## What it looks like

A daily issue with masthead, editor's note, and index tabs — all functional:

<img src="assets/shot-front.png" alt="front page" width="100%">

Ongoing stories get a red tracking stamp; copy covers only the increment:

<img src="assets/shot-tracking.png" alt="Day-3 tracking stamp" width="100%">

Shaky single-source claims are circled honestly:

<img src="assets/shot-review.png" alt="unverified stamp" width="100%">

The tracking view chains multi-day coverage into one evolving thread:

<img src="assets/shot-threads.png" alt="event threads" width="100%">

## How it differs from an RSS reader

| | RSS reader | pulsewire |
|---|---|---|
| Unit | articles | **events** (multi-source reports clustered; one card per event) |
| Ranking | reverse-chron | **heat** (count of credible sources × acceleration) + hard freshness window |
| Quality control | none | **majority-vote LLM judge panel**: board classifier → topic gate → magnitude gate → worthiness gate |
| Numbers | whatever the article says | **source-verified**: the model can't invent numbers; untraceable ones are flagged |
| Duplicates | daily déjà vu | **clip memory**: already-covered stories are cut; sagas continue incrementally |
| Archive | unsearchable | **semantic Q&A**: `pulsewire ask "..."` answers only from archived cards, with citations, or says "not found" |

## Pipeline

```mermaid
flowchart LR
  A["fetch<br>323 sources"] --> B["dedup<br>local embeddings"] --> C["enrich<br>traceable facts"]
  C --> D["events<br>cluster + heat rank"] --> E["judge panel<br>board/topic/magnitude/worthiness"]
  E --> F["summarize<br>numbers as placeholders"] --> G["verify<br>fill from source"]
  G --> H["threads<br>multi-day linking"] --> I["render<br>scrapbook PNG"] --> J["deliver<br>web/desktop/Feishu"]
```

A single async Python process plus one postgres (pgvector) container. Every stage checkpoints; failures alert and resume — it never silently ships an empty issue. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (architecture) and [`docs/DESIGN.md`](docs/DESIGN.md) (design rationale) — both in Chinese.

## System requirements

The engine is cross-platform (CI runs the full suite on Ubuntu). The only platform-specific piece is the **local embedding model**, which decides your path:

| | Apple Silicon (M1+) | Intel Mac / Linux / Windows (incl. AMD) |
|---|---|---|
| Embedder | Qwen3-Embedding-0.6B on Metal GPU (default, fastest) | jina-embeddings-v3, fastembed/ONNX, **CPU-only** |
| Config change | none, works out of the box | one line: `dedup.provider: mlx` → `local` in `config.yaml` |
| Model size | ~630MB (8-bit) | ~2.2GB (fp32 ONNX) |
| GPU | uses Apple GPU | **no GPU needed**, pure CPU (Intel/AMD CPU both fine) |

- **RAM**: 8GB works (embedder + Docker postgres + headless browser for rendering), 16GB comfortable.
- **Disk**: keep ~5GB free (model + postgres image + Chromium + caches).
- **Shared prereqs**: [Docker Desktop](https://www.docker.com/products/docker-desktop/) (postgres) · [uv](https://docs.astral.sh/uv/) · a [DeepSeek API key](https://platform.deepseek.com/) (judge panel + writing run in the cloud, ≈ ¥2 ≈ $0.28/day heavy use).
- **Scheduling / desktop app are macOS-only**: daily automation uses launchd, the desktop app is Electron/mac. On other OSes, trigger `uv run pulsewire run` from your own cron / scheduled task — the core digest still works.

> **Non-Apple-Silicon users**: set `dedup.provider` to `local` in `config.yaml` (keep the default `dedup.model: jinaai/jina-embeddings-v3`) — runs cross-platform on CPU with good Chinese dedup quality. For stronger Chinese use `BAAI/bge-m3`, or the ONNX build of Qwen3-0.6B to match the Apple path; both need a bit of custom registration, whereas jina-v3 works out of the box.

## Getting started

Prereqs: see System requirements above. Apple Silicon default path:

```bash
git clone https://github.com/birdindasky/pulsewire.git && cd pulsewire
cp .env.example .env            # set PULSEWIRE_DEEPSEEK_API_KEY
# Non-Apple-Silicon: switch dedup.provider from mlx to local in config.yaml (see above)
docker compose up -d postgres
uv run alembic upgrade head
uv run pulsewire run --force    # full pipeline, ~20–35 min
open web/app/index.html         # Linux: use xdg-open
```

- **Daily schedule**: `uv run pulsewire schedule --hour=6` generates launchd files and prints install instructions (auto-starts Docker, shuts it down after, catches up after wake).
- **Desktop app**: `cd desktop && npm install && npm start`.
- **Ask the archive**: `uv run pulsewire ask "any progress on fusion?"`
- Boards, quotas and every judge gate live in `config.yaml` with inline comments and one-line rollbacks; the source registry is [`sources.yaml`](sources.yaml).

## Honest edges

- **Mac-first, not Mac-locked**: tuned out of the box for Apple Silicon (MLX/Metal embeddings, launchd scheduling, Electron/mac desktop app). Intel/Linux/Windows/AMD run the core digest too — flip one line (`dedup.provider: local`, CPU-only) and wire your own cron; see System requirements. CI runs the full suite on Ubuntu.
- **Output is Chinese** — by design.
- **Bring your own DeepSeek key** (litellm-compatible; swapping providers is a one-line config change).
- **Feishu push is optional**; web + desktop work without it.
- **Single-player**: no accounts, no server, no subscription. Clone it, run yours.

## License

[MIT](LICENSE) © birdindasky
