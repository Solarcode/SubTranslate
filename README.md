# SubTranslate

`sub-translate` — AI subtitle translator + remuxer. Takes a media file that **already has an
embedded text subtitle track**, translates it to **any language you want** via OpenRouter,
remuxes the translated track back into a clean-named copy as the **default** subtitle, and
moves the original aside so you're left with one finished file. One command, runs entirely on
your machine. Stdlib-only Python — no `pip install`.

## Install (the command on your PATH)

```bash
git clone https://github.com/Solarcode/SubTranslate.git
cd SubTranslate
brew install ffmpeg mkvtoolnix     # ffmpeg+ffprobe (required), mkvmerge (recommended for .mkv)
./install.sh                       # symlinks `sub-translate` onto your PATH
```

`install.sh` drops a `sub-translate` symlink into the first writable dir already on your PATH
(prefers `~/.local/bin`, then Homebrew `/opt/homebrew/bin`, then `/usr/local/bin`). If it has to
fall back to a dir that isn't on your PATH, it prints the one line to add. Override the target:
`INSTALL_DIR=/somewhere/on/path ./install.sh`. Undo with `./uninstall.sh`.

Then, from anywhere:

```bash
sub-translate "Rick.and.Morty.S09E01.mkv" finnish
```

(You can also just run `python3 sub_translate.py "file.mkv" finnish` without installing.)

`mkvtoolnix` is recommended, not required: **VLC mishandles SRT muxed into MKV by ffmpeg**, so
for `.mkv` output the tool uses `mkvmerge` to author the embed (VLC renders it reliably). Without
it, MKV embedding falls back to ffmpeg — but the sidecar `.srt` always works regardless.

## API key

You need an **OpenRouter API key** (https://openrouter.ai/keys). Resolved in this order (first
hit wins):

1. **Pasted into the script** — `OPENROUTER_API_KEY = "sk-or-v1-..."` at the top of `sub_translate.py`.
2. `$OPENROUTER_API_KEY` in your shell.
3. `OPENROUTER_API_KEY=...` in a `.env` (CWD / script dir) or `~/.config/sub_translate.env`.
4. **Interactive prompt** — if none of the above are set, it asks you when you run it, then
   **offers to save it** to `~/.config/sub_translate.env` (written `chmod 600`) so you're never
   asked again. Decline with `n` to keep it one-run-only.

> Never commit your key. `.env` and `sub_translate.env` are gitignored; the in-script constant
> ships blank.

## Use

```bash
sub-translate "Rick.and.Morty.S09E01.mkv" finnish      # FILE LANGUAGE
sub-translate finnish "Rick.and.Morty.S09E01.mkv"      # order doesn't matter
sub-translate "Movie.mp4" 'Brazilian Portuguese'       # multi-word is fine
```

Just give it the **file** and the **language**, in any order — there's no flag and no default
language. The file is whichever argument exists on disk; the rest is the target language, so a
multi-word language like `Brazilian Portuguese` works without worrying about order. The language
can be a name (`finnish`, `"Mexican Spanish"`) or an ISO code (`fin`). Any language the model
knows works; the built-in list just resolves the 3-letter track tag (unmapped languages still
translate — pass `--lang-code` to set the tag).

After a run, the source folder is left with **one finished file**:

```
Rick.and.Morty.S09E01_[Finnish_subs].mkv      ← the video, Finnish track embedded + default
subtranslate/                                  ← byproducts moved here, out of the way
├── Rick.and.Morty.S09E01.mkv                  ← your original
└── Rick.and.Morty.S09E01_[Finnish_subs].srt   ← backup sidecar
```

- Output name: `ORIGINAL_[Language_subs].ext` (override with `-o`).
- The original + the backup sidecar `.srt` are **moved** into `subtranslate/` next to the source
  (change with `--archive-dir`, or keep everything in place with `--no-archive`).
- The original is only moved when the embed succeeds (so you're never left without a playable
  file). If a container can't hold soft subs (`.avi`, `.ts`, …), nothing is moved and the sidecar
  `.srt` is your output.

### How it embeds, by container

| Output container | Embed method | VLC-reliable? |
|---|---|---|
| `.mkv` / `.mka` | `mkvmerge` (or ffmpeg `srt` if mkvtoolnix absent) | ✅ with mkvmerge |
| `.mp4` / `.m4v` / `.mov` | ffmpeg `mov_text` | ✅ |
| `.webm` | ffmpeg `webvtt` | ✅ |
| anything else (`.avi`, `.ts`, …) | can't hold soft subs → **sidecar only** | ✅ (sidecar) |

By default the embed contains **only** the translated track (so a player can't auto-pick the
wrong language out of a multi-track rip). A backup sidecar `.srt` is written too (unless
`--no-sidecar`) and tucked into `subtranslate/` — the universal fallback that renders in every
player for every container.

### Flags

| flag | what |
|---|---|
| `FILE LANGUAGE` | positional, **any order** — the media file + the target language (name or ISO code). Required. |
| `--list` | just show the subtitle tracks in the file and exit (no language needed) |
| `-o out.mkv` | explicit output path |
| `--src-lang spa` | translate FROM a different source track (default: `eng`) |
| `--src-index 5` | pick the source track by index (see `--list`) |
| `--lang-name "Mexican Spanish"` | override the language name used in the prompt |
| `--lang-code spa` | override the 3-letter code stamped on the track |
| `--archive-dir PATH` | where to move the original + sidecar (default: `<source-dir>/subtranslate`) |
| `--no-archive` | leave the original + sidecar next to the output |
| `--no-sidecar` | don't write the backup `.srt` |
| `--no-remux` | skip embedding; write only the sidecar `.srt` (no archive move) |
| `--keep-original-subs` | keep the rip's original sub tracks in the embed (default: translated only) |
| `--model SLUG` | override the model (default `google/gemini-3.1-flash-lite`) |

## What it does, in order

1. `ffprobe` the file for subtitle tracks.
2. Pick the source track (non-forced `eng` by default; overridable).
3. Extract it to a temp SRT (`ffmpeg -c:s srt`).
4. Translate in batches of 30 cues via OpenRouter, preserving cue numbers + timing exactly
   (1 line in → 1 line out, `⏎` marks in-cue line breaks).
5. Write the translated track into a clean-named copy, embedded as the `default` subtitle, using
   the right tool/codec for the container. Embed is best-effort — if it fails or the container
   can't hold subs, the sidecar `.srt` is your file (and the original is left in place).
6. Move the original + the backup sidecar `.srt` into `subtranslate/` so the source folder is
   left with just the finished file (`--no-archive` to skip).

## Notes / limits

- **Extract-only.** Only handles files that already carry a *text* subtitle track
  (SRT/ASS/mov_text/WebVTT). Image-based subs (PGS/VobSub from some Blu-ray rips) can't be
  translated and the script fails loud. Files with no subtitles at all are out of scope today —
  fetching them from OpenSubtitles or generating them from the audio with Whisper are planned
  features ([ROADMAP.md](ROADMAP.md)).
- Cost ≈ **$0.002** per episode, ~85s wall-clock (flash-lite).
- Degrades gracefully: a failed batch keeps the original text for that batch rather than
  aborting — you never lose the whole run to one hiccup.
- Re-running on an already-translated `.spa.mkv` would translate the AI track again; always run
  on the original.

## Why mkvmerge for MKV?

VLC frequently won't render an SRT track that ffmpeg muxed into an MKV — even when the track is
correct, complete, and flagged `default`. The identical content authored with `mkvmerge` (from
mkvtoolnix) renders reliably, and the same content as an external sidecar `.srt` always works.
So: MKV embeds go through `mkvmerge`, everything else through ffmpeg, and the sidecar `.srt` is
the universal guarantee.

## Roadmap

Make it work on **any** video, even ones with no subtitles at all — a two-step fallback when no
embedded track is found: (1) fetch a matching subtitle from **OpenSubtitles** (cut-verified by
runtime/fps and auto-synced so you never get director's-cut or drifting subs), else (2) **generate
one from the audio with Whisper** (local or cloud) — both in the source language and translated.
Design notes in [ROADMAP.md](ROADMAP.md).

## License

MIT — see [LICENSE](LICENSE).
