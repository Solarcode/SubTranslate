# SubTranslate — notes for Claude Code

Single-file, stdlib-only Python CLI. Takes a media file with an **already-embedded text
subtitle track**, translates it to any target language via OpenRouter, remuxes the translated
track into a clean-named copy as the default subtitle, and moves the original + a backup sidecar
`.srt` into `<source-dir>/subtranslate/` so the folder is left with one finished file.

## Run

```bash
# FILE + LANGUAGE positional, ANY ORDER (no --to flag, no default language):
python3 sub_translate.py "Movie.mkv" finnish         # or: sub-translate "Movie.mkv" finnish (after ./install.sh)
python3 sub_translate.py finnish "Movie.mkv"         # order doesn't matter
python3 sub_translate.py "Movie.mp4" 'Brazilian Portuguese'
python3 sub_translate.py --list "file.mkv"           # show subtitle tracks, exit (no language needed)
```

File = whichever positional exists on disk; the rest (joined) = the target language (name or ISO
code). Language is REQUIRED — there is no default. Output: `<name>_[<Language>_subs]<ext>` next to
the input; original + sidecar moved to `subtranslate/` (`--no-archive` keeps them in place,
`--archive-dir` changes the target).

## Code rules

- **Keep it stdlib-only and single-file.** No `pip install`, no dependency creep — that's the
  point. External deps are CLI tools on PATH only: `ffmpeg`/`ffprobe` (required), `mkvmerge`
  (recommended for `.mkv`).
- **Never commit an API key.** The in-script `OPENROUTER_API_KEY` constant ships blank; keys come
  from env / `.env` / `~/.config/sub_translate.env` / interactive prompt at runtime.
- The sidecar `.srt` is the default output and stays the universal fallback; the embed is
  best-effort (a failed embed must never be fatal).

## Footgun

**VLC mishandles SRT muxed into MKV by ffmpeg** — even a correct, single, default-flagged track
often won't render. So for `.mkv` output the tool authors the embed with `mkvmerge` (renders
reliably); the sidecar `.srt` is the guarantee for every other case. ffmpeg is fine for MP4
(`mov_text`) and WebM (`webvtt`).
