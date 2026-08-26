# Upstream evaluation (verified 2026-08-26)

## Public history discovery

### D3AdCa7/cli-anything-xiaoyuzhoufm

Adopt its public architecture: Xiaoyuzhou public metadata, Apple Podcasts search, `feedUrl`, then RSS episodes. Do not copy or vendor its source because the checked repository has no license file. Do not use its login commands. Its exact-title-first selection is insufficient for this Skill, so add author, description, and recent-episode overlap scoring plus ambiguity gates.

Checked commit: `51cfb21d8299e73bed444b036c0728a82036cdcd`.

### xiaoleiy/podpull and host452b/casts_down

Use as operational cross-checks for Apple lookup, RSS enclosure handling, resumable downloads, and safe filenames. Do not add them as mandatory runtime dependencies: the Skill submits public audio URLs directly to cloud ASR and uses local downloads only for `--keep-audio`.

## Batch and recovery

### worldwonderer/xiaoyuzhou-asr (MIT)

Borrow atomic checkpoints, completed-file skipping, per-episode failure isolation, safe filenames, and resume behavior. Do not use its token-based Xiaoyuzhou API path or fixed local ASR environment.

Checked commit: `7f80b17b34432dd35f6b3b82afb88583e5facf83`.

## ASR

Default to configurable `qwen-audio-3.0-asr-flash-filetrans`. Alibaba Cloud currently recommends it for offline files requiring hot words, context enhancement, sentence timestamps, and speaker diarization, with a 12-hour/2-GB limit. Disable diarization above two hours. Keep the model in `.env` so it can be replaced without changing the workflow.

Re-evaluate this file when public page structure, Apple matching behavior, RSS parsing, or the ASR API changes.
