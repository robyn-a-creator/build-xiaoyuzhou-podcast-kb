# Output schemas

## Episode

```json
{
  "episode_id": "rss-stable-hash",
  "rss_guid": "publisher-guid",
  "title": "",
  "published_at": "2026-08-26T08:00:00+08:00",
  "duration_seconds": 3600,
  "description": "",
  "audio_url": "https://...",
  "media_fingerprint": "/stable/media/path?id=publisher-id",
  "xiaoyuzhou_url": "",
  "source": "rss"
}
```

## Directory

```text
podcast-knowledge-base/<podcast-slug>/
├── README.md
├── index.csv
├── metadata.json
├── episodes/
├── raw/
│   ├── audio/
│   ├── metadata/
│   └── transcripts/
└── state/checkpoint.json
```

## index.csv

```text
episode_id,rss_guid,title,published_at,duration,audio_url,xiaoyuzhou_url,source,transcript_path,status,asr_model,processed_at,error
```

## metadata.json and checkpoint

Store `history_source`, `history_complete`, `history_reason`, RSS count, public supplement count, merged count, public catalog count, oldest/newest episode, and match confidence under `coverage`. Checkpoint version 2 is atomic and contains `podcast`, `range`, `coverage`, `episodes`, `source_snapshot_ids`, `updated_at`, and `stats`. When a verified RSS gap is filled from Xiaoyuzhou's public recent list, `podcast.public_supplement_episodes` preserves those source records for later updates. The source snapshot stores both episode-ID and hashed media-fingerprint tokens so a later GUID correction or a public-supplement-to-RSS migration does not trigger duplicate ASR.

Each completed `raw/metadata/<episode_id>.json` sidecar is authoritative for recovery and requires a non-empty Markdown transcript plus raw ASR JSON. Never promote a completion state when either artifact is missing.

## Episode Markdown

```markdown
[00:00:03] Speaker 1:
原始内容
```

Use ASR `speaker_id` when available. Do not infer real names from speaker order.
