---
name: build-xiaoyuzhou-podcast-kb
description: Build, resume, inspect, retry, and incrementally update a login-free Xiaoyuzhou podcast transcript knowledge base from public Xiaoyuzhou pages, Apple Podcasts, RSS feeds, public audio, and ASR. Use when the user asks for one Xiaoyuzhou host or podcast's complete public history, all episodes, a date range such as the past N years, episode transcripts, a source knowledge base, progress, retry, update, or synchronization. Also use for one public Xiaoyuzhou episode URL. Never use login, phone numbers, SMS codes, CAPTCHA, cookies, or tokens.
---

# Build Xiaoyuzhou Podcast Knowledge Base

Build a durable Source Layer: one faithful Markdown transcript per public episode, plus source metadata, `index.csv`, completeness evidence, and resumable state.

## Workflow

1. Read `references/operations.md` before the first run on a machine, when ASR changes, or when public upstream behavior changes.
2. Normalize the request into `podcast_name`, `xiaoyuzhou_url`, `time_range`, `mode`, and `keep_audio`. Treat a host name as a query for one podcast; if it maps to multiple shows, require the user to choose because v1 never merges shows. Never guess among similarly named shows.
3. Resolve this Skill's absolute directory and run `<python> <skill-dir>/scripts/podcast_kb.py doctor` when readiness is unknown.
4. Run the matching command:
   - Build/range: `<python> <skill-dir>/scripts/podcast_kb.py build --podcast <name-or-url> --time-range '<range>' --output-root <workspace>/podcast-knowledge-base`
   - Update: `<python> <skill-dir>/scripts/podcast_kb.py update --kb-dir <podcast-dir>`
   - Single: `<python> <skill-dir>/scripts/podcast_kb.py single --episode-url <url> --output-root <workspace>/podcast-knowledge-base`
   - Status: `<python> <skill-dir>/scripts/podcast_kb.py status --kb-dir <podcast-dir>`
   - Retry: `<python> <skill-dir>/scripts/podcast_kb.py retry --kb-dir <podcast-dir>`
   For update, status, retry, or continue requests without a path, search the current workspace under `podcast-knowledge-base/*/state/checkpoint.json`, validate checkpoints, and match the stored podcast title or ID. If zero or multiple knowledge bases match, show the smallest candidate list and ask the user to choose.
5. Let the batch finish. Resume the same command after interruption. Never add `--force` unless the user explicitly asks to reprocess completed files.
   Before more than the configured safe batch size is submitted, report episode count, known hours, unknown-duration count, and request cost confirmation. After confirmation, rerun with `--max-episodes <count>`.
6. Report discovered, in-range, completed, ASR, failed, skipped, `history_source`, `history_complete`, and the completeness reason.

## Source rules

- Resolve a Xiaoyuzhou URL through public page metadata, Apple Podcasts search, and the original RSS feed.
- Match candidates using title, author, description, and recent-episode overlap. Stop with candidates when confidence is low or two results are too close.
- Treat RSS as complete only when its episode count can be checked against a public directory count and recent-episode matching passes. Otherwise set `history_complete=false` with a reason.
- Never treat the public Xiaoyuzhou page's recent episodes as complete history.
- Normalize RSS, Apple, and public-page episodes to one schema before ASR. Read `references/schemas.md` before changing fields.
- Use `published_at` in Asia/Shanghai for `过去一年`, `过去三年`, `过去四年`, `全部节目`, `2023年至今`, `2024年`, and `最近50期`.

## Transcript rules

- Use public audio and configurable ASR. Extract hot words from podcast, author, title, show notes, guests, companies, brands, and terminology.
- Enable speaker diarization only where the configured model supports it; disable it for audio over two hours.
- Keep source wording. Add punctuation, timestamps, and speaker breaks, but do not summarize, rewrite, polish, or remove substantial spoken language.
- Use `Speaker 1`, `Speaker 2`, and so on. Replace labels with names only when direct, high-confidence metadata supports the mapping; never infer identity from speaking order.
- Write raw ASR and episode metadata before marking an episode complete. Skip valid completed artifacts on resume.
- Isolate failures per episode. Never resubmit an ASR task with an uncertain submission outcome until the operator confirms no cloud task exists.
- Delete temporary audio after successful ASR by default. Keep it only when the user asks for `--keep-audio`.

## Boundaries

- Do not implement or request Xiaoyuzhou login, phone numbers, SMS codes, CAPTCHA, cookies, access tokens, refresh tokens, app simulation, or verification bypasses.
- Do not build a web UI, SaaS, database server, vector database, graph, RAG layer, or automatic viewpoint summary in v1.
- Keep analysis and future intelligence layers separate from immutable `episodes/` source transcripts.

## Resources

- Read `references/operations.md` for setup, commands, recovery, and current upstream choices.
- Read `references/schemas.md` before changing output or checkpoint fields.
- Read `references/upstream-evaluation.md` before replacing public-source or ASR dependencies.
- Execute `scripts/podcast_kb.py`; its public-source resolver is `scripts/public_sources.py`.
