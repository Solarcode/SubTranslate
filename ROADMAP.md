# Roadmap

Planned features. Nothing here is implemented yet — these are design notes for future work.

## Auto-fetch subtitles from OpenSubtitles (for files with no embedded track)

**Status:** planned / not implemented.

**Problem it solves.** SubTranslate is currently *extract-only*: it can only translate a subtitle
track that's **already inside the file**. The two cases where it has nothing to work with are
(a) files with no embedded subtitles at all, and (b) image-based subs (PGS/VobSub) that can't be
read as text. Case (a) is the common one — plenty of MP4/MKV files ship with no text track.

**The idea.** When `probe_subs()` finds no usable text track, fetch a matching source-language
subtitle from [OpenSubtitles](https://www.opensubtitles.com) (e.g. English), then feed it into
the existing translate → embed → archive pipeline unchanged. The download becomes the "extracted
SRT" and everything downstream works as it does today.

### Where it slots in

```
probe_subs(file)
  ├─ text track found ──────────────► (today) extract → translate → embed → archive
  └─ no text track found ──► [NEW] fetch from OpenSubtitles ─► translate → embed → archive
                                       └─ still nothing ──► fail loud (as today)
```

A new flag gates it, e.g. `--fetch-subs` (opt-in), or auto-on when no track is found **and** an
OpenSubtitles key is configured. Source language to fetch via `--os-lang eng` (defaults to
`--src-lang`).

### Matching strategy (best → fallback)

1. **Movie hash** — OpenSubtitles' `moviehash` is a 64-bit hash of the file size + first and last
   64 KiB. It gives an exact match to the right release/timing. Computable in **pure stdlib**
   (`struct` + two `file.read()`s), so no new dependency.
2. **Filename / title + IMDB id** query as a fallback when the hash misses.
3. On multiple hits, rank by downloads + rating and pick the best; expose `--os-pick` to override.

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
