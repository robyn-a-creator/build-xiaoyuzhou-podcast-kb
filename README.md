# build-xiaoyuzhou-podcast-kb

A Codex Skill for turning public Xiaoyuzhou podcasts into complete, resumable, incrementally updatable Markdown transcript libraries.

> Public Podcast → Historical Episodes → Public Audio → ASR → Markdown Transcript Library

This v1 is a transcript-library builder. It does not perform host analysis, opinion extraction, RAG, or personality research. Those may be built later on top of the transcript library, but they are not part of this project.

## What it does

- Resolves a public Xiaoyuzhou podcast through public page metadata, Apple Podcasts, and the original RSS feed.
- Discovers historical episodes and filters calendar-based ranges such as `过去四年`.
- Verifies whether the discovered history is complete before claiming completeness.
- Sends public audio URLs to configurable ASR and writes one faithful Markdown transcript per episode.
- Preserves timestamps, speaker labels, raw ASR output, episode metadata, `index.csv`, and resumable checkpoint state.
- Resumes interrupted work, retries isolated failures, and incrementally processes newly published episodes.
- Avoids duplicate paid ASR when stable IDs or signed media URLs change.
- Submits public audio URLs directly to ASR; downloads and retains local audio under `raw/audio/` only when `--keep-audio` is explicitly requested.

## What it does not do

- No Xiaoyuzhou login, phone number, SMS code, CAPTCHA bypass, cookie, access token, or app simulation.
- No transcript rewriting or automatic summarization in the immutable source layer.
- No web UI, SaaS, vector database, graph, RAG pipeline, host profiling, or viewpoint analysis in v1.

## Repository layout

```text
.
├── README.md
├── LICENSE
└── skills/
    └── build-xiaoyuzhou-podcast-kb/
        ├── SKILL.md
        ├── agents/openai.yaml
        ├── config/defaults.json
        ├── references/
        ├── scripts/
        └── tests/
```

The installable Skill is `skills/build-xiaoyuzhou-podcast-kb/`. The repository README stays outside the Skill so the Skill package remains focused on agent instructions and runtime resources.

## Requirements

- Python 3.9 or newer
- A DashScope API key
- A workspace-scoped DashScope API base URL
- Publicly accessible HTTPS podcast feeds and audio

The runtime uses only the Python standard library. The default ASR model is `qwen-audio-3.0-asr-flash-filetrans`.

## Install

Clone the repository, then copy the Skill into your Codex skills directory:

```bash
git clone https://github.com/robyn-a-creator/build-xiaoyuzhou-podcast-kb.git
mkdir -p ~/.codex/skills
cp -R build-xiaoyuzhou-podcast-kb/skills/build-xiaoyuzhou-podcast-kb ~/.codex/skills/
```

Keep secrets outside both the repository and installed Skill:

```bash
mkdir -p ~/.config/build-xiaoyuzhou-podcast-kb
cp ~/.codex/skills/build-xiaoyuzhou-podcast-kb/.env.example \
  ~/.config/build-xiaoyuzhou-podcast-kb/.env
chmod 600 ~/.config/build-xiaoyuzhou-podcast-kb/.env
```

Edit that private `.env` file and set both values:

```dotenv
DASHSCOPE_API_KEY=
DASHSCOPE_BASE_URL=https://YOUR_WORKSPACE_ID.cn-beijing.maas.aliyuncs.com/api/v1
```

Then verify readiness:

```bash
python3 ~/.codex/skills/build-xiaoyuzhou-podcast-kb/scripts/podcast_kb.py doctor
```

## Usage

Build a transcript library for the past four years:

```bash
python3 ~/.codex/skills/build-xiaoyuzhou-podcast-kb/scripts/podcast_kb.py build \
  --podcast 'https://www.xiaoyuzhoufm.com/podcast/PID' \
  --time-range '过去四年' \
  --output-root "$PWD/podcast-knowledge-base"
```

The default paid-ASR safety gate allows five episodes per run. Before a larger batch, the tool reports the episode count and known duration. After reviewing the cost, explicitly approve the intended limit:

```bash
python3 ~/.codex/skills/build-xiaoyuzhou-podcast-kb/scripts/podcast_kb.py build \
  --podcast 'https://www.xiaoyuzhoufm.com/podcast/PID' \
  --time-range '过去四年' \
  --output-root "$PWD/podcast-knowledge-base" \
  --max-episodes 49
```

Resume by running the same build command again. Valid completed artifacts are skipped.

```bash
# Inspect status
python3 ~/.codex/skills/build-xiaoyuzhou-podcast-kb/scripts/podcast_kb.py status \
  --kb-dir 'podcast-knowledge-base/<name--id>'

# Retry failed episodes only
python3 ~/.codex/skills/build-xiaoyuzhou-podcast-kb/scripts/podcast_kb.py retry \
  --kb-dir 'podcast-knowledge-base/<name--id>'

# Discover and transcribe newly published episodes
python3 ~/.codex/skills/build-xiaoyuzhou-podcast-kb/scripts/podcast_kb.py update \
  --kb-dir 'podcast-knowledge-base/<name--id>'
```

## Output

```text
podcast-knowledge-base/<podcast--id>/
├── README.md
├── index.csv
├── metadata.json
├── episodes/       # One Markdown transcript per episode
├── raw/
│   ├── audio/       # Empty by default; files retained only with --keep-audio
│   ├── metadata/    # Authoritative per-episode recovery sidecars
│   └── transcripts/ # Raw ASR responses
└── state/
    └── checkpoint.json
```

The Markdown transcript is a faithful source record. The tool adds punctuation, timestamps, and speaker breaks but does not summarize, polish, or remove substantial spoken language.

## Completeness and recovery

The public Xiaoyuzhou page is treated as recent evidence, not complete history. The Skill checks the original RSS against an independently observed public catalog count and recent-episode overlap. If the evidence is insufficient, it reports `history_complete=false` instead of making a false completeness claim.

ASR submission intent and task IDs are persisted around cloud requests. If a submission result is uncertain, the tool stops rather than blindly resubmitting and risking duplicate charges.

## Tests

Run the full suite from the Skill directory:

```bash
cd skills/build-xiaoyuzhou-podcast-kb
python3 -m unittest discover -s tests -v
```

The current release contains 50 automated tests covering source matching, completeness, date ranges, HTTPS and private-network rejection, resumability, update deduplication, uncertain ASR submissions, paid-batch limits, and filesystem safety.

## Cost and upstream changes

ASR is a paid external service after any applicable free quota. Review current model availability and pricing before a large batch. Apple Podcasts, RSS publishers, Xiaoyuzhou public pages, and DashScope may change independently; see `references/upstream-evaluation.md` before replacing upstream dependencies.

## Copyright and responsible use

Publicly accessible content is not automatically public domain or licensed for redistribution. You are responsible for confirming that you have the right to transcribe, store, and redistribute the relevant podcast content and for complying with platform terms and applicable law.

The MIT License in this repository covers the project code only. It does not grant rights to podcast audio, episode content, or generated transcripts.

## License

[MIT](LICENSE)
