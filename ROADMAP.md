# Roadmap

Planned features. Nothing here is implemented yet — these are design notes for future work.

## Handling files with no embedded subtitle track

Today SubTranslate is *extract-only* — it needs a text subtitle track already inside the file.
Two planned fallbacks make it work on **any** video, tried in order:

```
probe_subs(file)
  ├─ text track found ─────────► (today) extract → translate → embed → archive
  └─ no text track ─► 1. fetch from OpenSubtitles ─┐
                      2. else transcribe w/ Whisper ┴─► (source SRT) → translate → embed → archive
                                                        └─ still nothing ──► fail loud (as today)
```

1 (OpenSubtitles) is preferred when a match exists — human-authored, perfectly timed, near-free.
2 (Whisper) is the universal floor — generates subs straight from the audio when nothing else
exists. Both produce a source-language SRT that feeds the **existing** translate → embed → archive
pipeline unchanged.

## 1. Auto-fetch subtitles from OpenSubtitles

**Status:** planned / not implemented.

**The idea.** When `probe_subs()` finds no usable text track, fetch a matching source-language
subtitle from [OpenSubtitles](https://www.opensubtitles.com) (e.g. English), then feed it into
the existing translate → embed → archive pipeline unchanged. The download becomes the "extracted
SRT" and everything downstream works as it does today. Preferred over Whisper (item 2) when a
match exists — it's human-authored and already correctly timed.

A new flag gates it, e.g. `--fetch-subs` (opt-in), or auto-on when no track is found **and** an
OpenSubtitles key is configured. Source language to fetch via `--os-lang eng` (defaults to
`--src-lang`).

### Matching strategy (best → fallback)

1. **Movie hash** — OpenSubtitles' `moviehash` is a 64-bit hash of the file size + first and last
   64 KiB. It gives an exact match to the right release/timing. Computable in **pure stdlib**
   (`struct` + two `file.read()`s), so no new dependency.
2. **Filename / title + IMDB id** query as a fallback when the hash misses.
3. On multiple hits, rank by downloads + rating and pick the best; expose `--os-pick` to override.

### Getting the RIGHT cut, and keeping it in sync

The danger with the fuzzy paths (2/3) is grabbing subs for the **wrong release** — director's cut
vs theatrical, extended edition, a different framerate, or a differently-encoded rip — which gives
text that drifts or is offset against *this* file. Three layers, strongest first:

1. **Exact match wins by construction.** The `moviehash` path matches subs uploaded *for this exact
   file*, so the cut + timing are already right. Prefer it and skip the rest when it hits.
2. **Reject wrong cuts before downloading.** OpenSubtitles results carry duration / fps / release
   metadata. `ffprobe` the file for its real runtime + fps; **discard candidates whose runtime
   differs beyond a small tolerance** (e.g. >~30 s) or whose fps mismatches. A director's cut is
   minutes longer than theatrical → filtered out automatically. Prefer candidates whose release
   tag matches the filename (e.g. `EXTENDED`, `1080p.WEB`, group name).
3. **Verify + auto-sync after downloading.** Sanity-check that the subtitle's first/last cue fall
   within the file's duration (subs running well past the end = wrong cut → reject or re-pick). For
   residual constant offset or fps drift, **auto-sync against the audio**: extract a VAD/speech
   track with `ffmpeg` and align the subtitle event timing to it (the approach `ffsubsync` / `alass`
   use — cross-correlate speech onsets vs cue starts, solve for offset + linear scale). Cloud-free,
   but needs an audio pass; ship as `--sync` (auto-on when a non-hash match is used). If alignment
   confidence is low, **fail loud** rather than embed drifting subs.

Net rule: **hash match → trust it; fuzzy match → duration/fps-gate it, then verify-and-sync, never
blindly embed.** (Whisper-generated subs (item 2) are derived from this file's own audio, so they're
always the right cut and already in sync — this whole concern only applies to fetched subs.)

### Implementation notes (keeps the stdlib-only / single-file ethos)

- **API:** OpenSubtitles REST API (`api.opensubtitles.com`, v1). Plain HTTPS + JSON → `urllib`
  works; no SDK. **Verify the live contract at build time** (endpoints, headers, and the
  download/quota rules change — see the citation-rule for fresh-fact APIs).
- **Auth:** an `Api-Key` header + a login token for `/download`. Resolve the key the same way the
  OpenRouter key is resolved (env / `.env` / `~/.config/sub_translate.env` / prompt). Add
  `OPENSUBTITLES_API_KEY`.
- **Download:** the returned subtitle is usually gzipped SRT → decompress with the stdlib `gzip`
  module. Normalize encoding to UTF-8 before parsing (reuse `parse_srt`).
- **Rate limits:** the free tier has a small daily download cap. Surface the remaining quota from
  the response and fail with a clear message when exhausted (don't silently degrade).
- **Caveats to document for the user:** match ambiguity (wrong release → out-of-sync timing —
  the hash path avoids this), and that downloading subtitles is the user's responsibility under
  OpenSubtitles' terms (personal use).

### Why it's worth it

It removes the single biggest "doesn't work on this file" case. Combined with the existing
any-language translation, the end-to-end story becomes: *point it at any video, get a correctly
subtitled file in the language you want* — even when the source had no subtitles at all.

## 2. Generate subtitles from the audio with Whisper (local or cloud)

**Status:** planned / not implemented.

**The idea.** When a file has no embedded track *and* OpenSubtitles has no match, there's still
the audio. Transcribe it with [Whisper](https://github.com/openai/whisper) to produce a
timestamped SRT in the **source** language, then run that through the existing translate → embed →
archive pipeline to also get the **translated** track. This is the universal floor: it works on
anything with speech, including original/home/indie content that exists nowhere online.

**Two outputs from one transcription:**
- a **source-language** SRT (the transcription itself — e.g. embed the original English back in), and
- a **translated** SRT in the target language (transcription → existing translate step).

Both are embedded/sidecar'd exactly like a normal run.

### Local vs cloud (a deliberate fork)

| | Local Whisper | Cloud Whisper |
|---|---|---|
| Examples | `whisper.cpp`, `faster-whisper`, `openai-whisper` | Groq `whisper-large-v3`, OpenAI audio API |
| Cost | free after model download | cents per hour of audio |
| Privacy | fully offline | audio leaves the machine |
| Speed | depends on CPU/GPU | fast, no local compute |
| **Fits stdlib-only?** | **No** — needs a binary/model or a pip dep | **Yes** — just an HTTPS multipart upload via `urllib` |

Cloud is the natural first cut (keeps the single-file, no-`pip-install` ethos — it's one more API
call alongside OpenRouter). Local is the privacy/offline option and would be **opt-in**, gated on
a present binary (`whisper.cpp`) so the core stays dependency-free. Flag e.g. `--transcribe`
(force) / auto-on when no track and no OpenSubtitles match; `--whisper local|cloud`,
`--whisper-model`.

### Implementation notes

- **Audio extract:** `ffmpeg -i in -vn -ac 1 -ar 16000 audio.wav` (already have ffmpeg). 16 kHz
  mono is what Whisper wants; chunk long files to respect cloud upload-size limits.
- **Source language:** Whisper auto-detects, or pass it explicitly (`--src-lang`) for accuracy.
- **Timestamps:** use Whisper's segment timestamps for cue timing; optionally word-level for
  tighter cues. Keep cue count sane (don't emit one cue per word).
- **Whisper's own `translate` task** only ever targets English — so for arbitrary target languages
  we transcribe in the source language and reuse our existing translate step (which already does
  any language). Don't rely on Whisper for the translation.
- **Cloud auth:** reuse the key-resolution pattern; add e.g. `GROQ_API_KEY` / `OPENAI_API_KEY`.
  **Verify the live audio-endpoint contract at build time** (model slugs, size limits, pricing
  are fresh-fact).
- **Quality caveat to surface:** ASR isn't perfect (names, jargon, music, overlapping speech) —
  generated subs are best-effort, clearly a notch below human subs from OpenSubtitles, which is
  why it's the *last* fallback, not the first.

### Why it's worth it

It makes "no subtitles anywhere" no longer a dead end. With items 1 + 2, the chain is complete:
embedded → OpenSubtitles → Whisper, so the promise becomes *literally any video with speech →
subtitled in the language you want* — even content that has never been subtitled before.
