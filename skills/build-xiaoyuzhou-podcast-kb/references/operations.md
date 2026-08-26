# Operations

## Setup

1. Use Python 3.9 or later. The Skill's runtime uses only the Python standard library.
2. Run `mkdir -p ~/.config/build-xiaoyuzhou-podcast-kb`, copy `.env.example` to `~/.config/build-xiaoyuzhou-podcast-kb/.env`, set `DASHSCOPE_API_KEY` plus the workspace-scoped `DASHSCOPE_BASE_URL`, and run `chmod 600 ~/.config/build-xiaoyuzhou-podcast-kb/.env`. Keep secrets outside the installed Skill directory.
3. Keep `ASR_MODEL` configurable. The current default is `qwen-audio-3.0-asr-flash-filetrans` because it supports long files, hot words, timestamps, and speaker diarization.
4. Run `python3 <skill-dir>/scripts/podcast_kb.py doctor`.

No Xiaoyuzhou account, phone number, SMS code, CAPTCHA, cookie, or token is used.
The default paid-ASR safety gate allows five episodes per run. A larger batch stops before submission and prints its episode count and known total duration; after the user confirms cost, pass `--max-episodes <count>`.

## Commands

```bash
python3 <skill-dir>/scripts/podcast_kb.py build --podcast 'https://www.xiaoyuzhoufm.com/podcast/PID' --time-range '过去四年' --output-root "$PWD/podcast-knowledge-base"
python3 <skill-dir>/scripts/podcast_kb.py build --podcast '播客名称' --time-range '全部节目' --output-root "$PWD/podcast-knowledge-base"
python3 <skill-dir>/scripts/podcast_kb.py update --kb-dir 'podcast-knowledge-base/<name--id>'
python3 <skill-dir>/scripts/podcast_kb.py retry --kb-dir 'podcast-knowledge-base/<name--id>'
python3 <skill-dir>/scripts/podcast_kb.py status --kb-dir 'podcast-knowledge-base/<name--id>'
python3 <skill-dir>/scripts/podcast_kb.py single --episode-url 'https://www.xiaoyuzhoufm.com/episode/EID' --output-root "$PWD/podcast-knowledge-base"
```

## Completeness gate

- `history_source=rss` means the episode list came from the original RSS discovered through Apple Podcasts.
- `history_source=rss+xiaoyuzhou_public` means a verified exact RSS count gap was filled from public Xiaoyuzhou episode metadata.
- Set `history_complete=true` only when the merged count is not lower than the independently verified public catalog count, public-to-RSS matching is one-to-one with the same normalized title and publication day, and recent-title matching confirms the same show. Media paths alone never prove catalog identity because publishers may reuse them.
- Otherwise keep `history_complete=false` and report `history_reason`. Do not claim that a date window is complete when the source history is unverified.

## Recovery

- Re-run the same build command after interruption. Valid completed sidecars are skipped.
- `update` re-reads the saved RSS and processes only unseen `rss_guid`/stable episode IDs.
- Previously verified public supplements remain in the source inventory after they leave Xiaoyuzhou's recent window; if they later appear in RSS, media aliases prevent duplicate paid ASR.
- If a publisher reuses an old media address while also changing identity metadata, update stops before paid ASR. Confirm the item is genuinely new, then rerun update with `--accept-ambiguous-as-new`.
- `retry` processes failed episodes without touching completed transcripts.
- A failed episode is recorded and the batch continues.
- When ASR submission outcome is uncertain, inspect the provider task list before using `--allow-uncertain-resubmit`.

## Operational limits

- The configured cloud model accepts public audio URLs. Audio is downloaded locally only with `--keep-audio`.
- Speaker diarization is disabled above two hours to reduce timeout risk.
- Audio or feeds that are not HTTPS, or resolve to local/private networks, are rejected. Redirects are revalidated.
- Re-evaluate model availability and pricing before a large paid batch.
