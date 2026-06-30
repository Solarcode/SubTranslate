#!/usr/bin/env python3
"""
sub_translate.py — one-shot subtitle translator + remuxer.

Takes a media file that ALREADY has an embedded text subtitle track, extracts it,
translates it to ANY target language (OpenRouter / google/gemini-3.1-flash-lite),
remuxes the translated track back into a clean-named copy and sets it as the
DEFAULT subtitle track, then moves the original aside so the folder is left with
just the finished file.

Stdlib only — no `pip install`. Needs `ffmpeg`/`ffprobe` on PATH (brew install ffmpeg),
`mkvmerge` recommended for .mkv (brew install mkvtoolnix), and an OPENROUTER_API_KEY.

Usage
-----
    export OPENROUTER_API_KEY=sk-or-...            # or put it in a .env (see below)
    python3 sub_translate.py "Movie.mkv" finnish              # FILE LANGUAGE
    python3 sub_translate.py finnish "Movie.mkv"              # order doesn't matter
    python3 sub_translate.py "Movie.mp4" 'Brazilian Portuguese'

The two positional args are the media FILE and the TARGET LANGUAGE, in any
order — the file is whichever arg exists on disk, the rest is the language
(so a multi-word language needs no special ordering). There is NO default
language; you must name one.

Output (next to the input): "Movie_[Finnish_subs].mkv" with the translated
track embedded + default. The original video + a backup sidecar .srt are
moved into "<source-dir>/subtranslate/".

Options
-------
    FILE LANGUAGE            positional, any order — media file + target language (name or ISO code)
    -o, --output PATH        explicit output path (default: <stem>_[<Lang>_subs]<ext>)
    --src-lang CODE          source subtitle language to translate FROM (default: eng)
    --src-index N            pick subtitle track by ffprobe index, overrides --src-lang
    --lang-name NAME         override the language name used in the prompt
    --lang-code CODE         override the 3-letter ISO code stamped on the track
    --register TEXT          tone hint passed to the model (default: neutral/idiomatic)
    --archive-dir PATH       where to move the original + sidecar (default: <source-dir>/subtranslate)
    --no-archive             leave the original + sidecar next to the output
    --no-sidecar             don't write the backup .srt
    --no-remux               only write the sidecar .srt (no embed, no archive move)
    --keep-original-subs     keep the rip's original sub tracks in the embed
    --list                   just list the subtitle tracks in the file and exit
    --model SLUG             OpenRouter model (default: google/gemini-3.1-flash-lite)

API key resolution order
------------------------
    1. $OPENROUTER_API_KEY
    2. OPENROUTER_API_KEY=... in a .env file in: CWD, this script's dir, or ~/.config/sub_translate.env
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# ============================================================================ #
#  PASTE YOUR OPENROUTER KEY HERE (between the quotes) to skip the prompt.
#  Leave blank to use $OPENROUTER_API_KEY / a .env / be prompted at run time.
#  Get a key at https://openrouter.ai/keys
# ============================================================================ #
OPENROUTER_API_KEY = ""

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "google/gemini-3.1-flash-lite"
BATCH = 30
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}

# Which subtitle codec each output container needs for a soft-embedded track.
# Containers not listed here can't carry a soft subtitle track — for those we
# write the sidecar .srt only (which works in every player regardless).
EMBED_SUB_CODEC = {
    ".mkv": "srt", ".mka": "srt",
    ".mp4": "mov_text", ".m4v": "mov_text", ".mov": "mov_text",
    ".webm": "webvtt",
}

# Common target languages -> ISO 639-2/T (3-letter) track codes. Not exhaustive;
# pass --lang-code to override or add one. The display NAME (what the model is
# told to translate into) is whatever you pass to --to, so any language works
# even if it's not in this map — the map only resolves the track-tag code.
LANG_NAME_TO_CODE = {
    "english": "eng", "spanish": "spa", "finnish": "fin", "french": "fra",
    "german": "deu", "italian": "ita", "portuguese": "por", "dutch": "nld",
    "swedish": "swe", "norwegian": "nor", "danish": "dan", "icelandic": "isl",
    "polish": "pol", "czech": "ces", "slovak": "slk", "hungarian": "hun",
    "romanian": "ron", "greek": "ell", "turkish": "tur", "russian": "rus",
    "ukrainian": "ukr", "bulgarian": "bul", "serbian": "srp", "croatian": "hrv",
    "arabic": "ara", "hebrew": "heb", "persian": "fas", "farsi": "fas",
    "hindi": "hin", "bengali": "ben", "urdu": "urd", "japanese": "jpn",
    "korean": "kor", "chinese": "zho", "mandarin": "zho", "cantonese": "yue",
    "thai": "tha", "vietnamese": "vie", "indonesian": "ind", "malay": "msa",
    "tagalog": "tgl", "filipino": "fil", "tamil": "tam", "telugu": "tel",
    "estonian": "est", "latvian": "lav", "lithuanian": "lit", "slovenian": "slv",
    "catalan": "cat", "basque": "eus", "galician": "glg", "welsh": "cym",
    "irish": "gle", "afrikaans": "afr", "swahili": "swa",
}
CODE_TO_NAME: "dict[str, str]" = {}
for _n, _c in LANG_NAME_TO_CODE.items():
    CODE_TO_NAME.setdefault(_c, _n.title())


def resolve_language(value: str) -> "tuple[str, str | None]":
    """Map a user-supplied language value (a NAME or a 3-letter ISO code, optionally
    region-prefixed like 'Colombian Spanish') to (display_name, iso_code).

    iso_code is None when we can't resolve it — the caller warns and falls back
    to 'und' so an unknown language still translates (only the track tag is
    generic; pass --lang-code to set it). The display name is always preserved,
    so ANY language the model knows works, mapped or not.
    """
    v = value.strip()
    low = v.lower()
    if low in CODE_TO_NAME:                  # passed a code, e.g. 'fin'
        return CODE_TO_NAME[low], low
    if low in LANG_NAME_TO_CODE:             # exact name, e.g. 'finnish'
        return v.title(), LANG_NAME_TO_CODE[low]
    parts = low.split()
    if parts and parts[-1] in LANG_NAME_TO_CODE:   # 'colombian spanish' -> spa
        return v.title(), LANG_NAME_TO_CODE[parts[-1]]
    return v.title(), None                   # unknown -> keep name, code unknown


def filename_label(lang_name: str) -> str:
    """Filename-safe label for the output suffix, e.g. 'Colombian Spanish' ->
    'ColombianSpanish', 'Finnish' -> 'Finnish'."""
    cleaned = re.sub(r"[^0-9A-Za-z ]+", "", lang_name).strip()
    return "".join(cleaned.split()) or "Sub"


# ----------------------------------------------------------------------------- #
# environment / preflight
# ----------------------------------------------------------------------------- #

def die(msg: str, code: int = 1) -> "None":
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def require_tools() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if subprocess.run(["which", tool], capture_output=True).returncode != 0:
            die(f"{tool} not found on PATH. Install with: brew install ffmpeg")


def load_api_key() -> str:
    # 1. in-script constant (paste your key at the top of this file)
    if OPENROUTER_API_KEY.strip():
        return OPENROUTER_API_KEY.strip()
    # 2. environment variable
    env_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if env_key:
        return env_key
    # 3. a .env file (CWD, this script's dir, or ~/.config/sub_translate.env)
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parent / ".env",
        Path.home() / ".config" / "sub_translate.env",
    ]
    for env_path in candidates:
        if env_path.is_file():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith("OPENROUTER_API_KEY"):
                    _, _, val = line.partition("=")
                    val = val.strip().strip('"').strip("'")
                    if val:
                        return val
    # 4. interactive prompt (fallback when nothing above is set)
    if sys.stdin.isatty():
        try:
            entered = input("OpenRouter API key (sk-or-...): ").strip()
        except (EOFError, KeyboardInterrupt):
            entered = ""
        if entered:
            print("  tip: paste this into OPENROUTER_API_KEY at the top of the script "
                  "to skip this prompt next time.", file=sys.stderr)
            return entered
    die(
        "no OpenRouter API key. Paste it into OPENROUTER_API_KEY at the top of this\n"
        "script, set $OPENROUTER_API_KEY, or drop it in ~/.config/sub_translate.env"
    )
    return ""  # unreachable


# ----------------------------------------------------------------------------- #
# probe / extract
# ----------------------------------------------------------------------------- #

@dataclass
class SubStream:
    abs_index: int        # absolute stream index in the container
    rel_index: int        # index among subtitle streams (for 0:s:N mapping)
    codec: str
    lang: str
    title: str
    is_default: bool
    is_forced: bool

    @property
    def is_text(self) -> bool:
        return self.codec in TEXT_SUB_CODECS

    def describe(self) -> str:
        flags = []
        if self.is_default:
            flags.append("default")
        if self.is_forced:
            flags.append("forced")
        if not self.is_text:
            flags.append("IMAGE/non-text")
        flag_s = f" [{', '.join(flags)}]" if flags else ""
        ttl = f' "{self.title}"' if self.title else ""
        return f"  s:{self.rel_index} (stream #{self.abs_index})  {self.lang or '?'}  {self.codec}{ttl}{flag_s}"


def probe_subs(path: Path) -> list[SubStream]:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-select_streams", "s", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        die(f"ffprobe failed on {path}:\n{out.stderr}")
    streams = json.loads(out.stdout or "{}").get("streams", [])
    subs: list[SubStream] = []
    for rel, s in enumerate(streams):
        disp = s.get("disposition", {})
        tags = s.get("tags", {})
        subs.append(SubStream(
            abs_index=int(s.get("index", -1)),
            rel_index=rel,
            codec=(s.get("codec_name") or "").lower(),
            lang=(tags.get("language") or "").lower(),
            title=tags.get("title") or "",
            is_default=bool(disp.get("default")),
            is_forced=bool(disp.get("forced")),
        ))
    return subs


def choose_source(subs: list[SubStream], src_lang: str, src_index: int | None) -> SubStream:
    text_subs = [s for s in subs if s.is_text]
    if not text_subs:
        die("no embedded TEXT subtitle track found (this tool is extract-only — "
            "image-based subs like PGS/VobSub can't be translated).")
    if src_index is not None:
        match = [s for s in subs if s.abs_index == src_index or s.rel_index == src_index]
        if not match:
            die(f"--src-index {src_index} matched no subtitle stream.")
        if not match[0].is_text:
            die(f"--src-index {src_index} is an image-based track; can't translate it.")
        return match[0]
    pool = [s for s in text_subs if s.lang == src_lang.lower()] or text_subs
    non_forced = [s for s in pool if not s.is_forced]
    chosen = (non_forced or pool)[0]
    return chosen


def extract_srt(path: Path, sub: SubStream, dst: Path) -> None:
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(path),
           "-map", f"0:s:{sub.rel_index}", "-c:s", "srt", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        die(f"subtitle extraction failed:\n{r.stderr}")


# ----------------------------------------------------------------------------- #
# translate
# ----------------------------------------------------------------------------- #

@dataclass
class Cue:
    idx: int
    timing: str
    text: str


def parse_srt(path: Path) -> list[Cue]:
    raw = path.read_text(encoding="utf-8-sig")
    blocks = re.split(r"\n\s*\n", raw.strip())
    cues: list[Cue] = []
    for block in blocks:
        lines = block.splitlines()
        if len(lines) < 3:
            continue
        try:
            idx = int(lines[0].strip())
        except ValueError:
            continue
        cues.append(Cue(idx=idx, timing=lines[1].strip(), text="\n".join(lines[2:])))
    return cues


def build_system_prompt(lang_name: str, register: str) -> str:
    return (
        f"You are a professional subtitle translator. Translate the following subtitle "
        f"lines to natural, idiomatic {lang_name}.\n\n"
        "RULES (non-negotiable):\n"
        "1. Output EXACTLY one translated line per input line, with the SAME [number] prefix.\n"
        "2. NEVER merge or split lines. NEVER skip a number. NEVER add commentary.\n"
        "3. Preserve the ' ⏎ ' marker EXACTLY where it appears (it marks line breaks inside a cue).\n"
        f"4. Match register: {register}. Do not sanitize profanity or tone.\n"
        f"5. Use {lang_name} vocabulary and rhythm where it fits naturally; don't force slang.\n"
        "6. Output nothing except the numbered lines."
    )


def call_openrouter(api_key: str, model: str, system: str, user: str) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
    }
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://localhost/sub_translate",
            "X-Title": "sub_translate",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


_NUM_RE = re.compile(r"^\s*\[(\d+)\]\s*(.*)$")
_FENCE_RE = re.compile(r"^\s*```")


def parse_numbered_response(content: str) -> dict[int, str]:
    """Map cue-index -> translated text. Robust to the model putting the text on
    the line(s) AFTER the [N] marker, to code fences, and to blank lines."""
    by_idx: dict[int, str] = {}
    cur: int | None = None
    buf: list[str] = []

    def flush() -> None:
        if cur is not None:
            text = "\n".join(buf).replace(" ⏎ ", "\n").replace("⏎", "\n").strip()
            by_idx[cur] = text

    for raw in content.splitlines():
        if _FENCE_RE.match(raw):
            continue
        m = _NUM_RE.match(raw)
        if m:
            flush()
            cur = int(m.group(1))
            rest = m.group(2)
            buf = [rest] if rest.strip() else []
        elif cur is not None and raw.strip():
            buf.append(raw.strip())
    flush()
    return by_idx


def translate_batch(api_key: str, model: str, system: str, batch: list[Cue]) -> list[str]:
    numbered = "\n".join(f"[{c.idx}] {c.text.replace(chr(10), ' ⏎ ')}" for c in batch)
    content = call_openrouter(api_key, model, system, numbered)
    by_idx = parse_numbered_response(content)

    out: list[str] = []
    misses = 0
    for cue in batch:
        t = by_idx.get(cue.idx)
        if not t or not t.strip():          # missing OR empty -> keep original (never blank)
            misses += 1
            t = cue.text
        out.append(t)
    if misses:
        print(f"[{misses}/{len(batch)} kept-original] ", end="", flush=True)
    return out


def translate_srt(api_key: str, model: str, system: str, cues: list[Cue]) -> list[str]:
    result: list[str] = []
    n_batches = (len(cues) + BATCH - 1) // BATCH
    for i in range(0, len(cues), BATCH):
        batch = cues[i:i + BATCH]
        print(f"  batch {i // BATCH + 1}/{n_batches} "
              f"(cues {batch[0].idx}–{batch[-1].idx})...", end=" ", flush=True)
        t0 = time.time()
        for attempt in (1, 2):
            try:
                result.extend(translate_batch(api_key, model, system, batch))
                print(f"ok ({time.time() - t0:.1f}s)")
                break
            except (urllib.error.HTTPError, urllib.error.URLError, KeyError, TimeoutError) as e:
                if attempt == 2:
                    print(f"FAIL ({e}) — keeping original for this batch")
                    result.extend(c.text for c in batch)
                else:
                    time.sleep(2)
    return result


def write_srt(cues: list[Cue], texts: list[str], dst: Path) -> None:
    lines: list[str] = []
    for cue, txt in zip(cues, texts):
        lines += [str(cue.idx), cue.timing, txt, ""]
    dst.write_text("\n".join(lines), encoding="utf-8")


# ----------------------------------------------------------------------------- #
# remux
# ----------------------------------------------------------------------------- #

def remux(src: Path, trans_srt: Path, out: Path, subs: list[SubStream],
          lang_code: str, title: str, keep_originals: bool) -> str | None:
    """Embed the translated track and make it the default subtitle.

    Returns the tool used ("mkvmerge"/"ffmpeg") or None if the output container
    can't carry a soft subtitle track (caller then relies on the sidecar).

    MKV/MKA is authored with mkvmerge when available — ffmpeg-muxed SRT-in-MKV
    renders unreliably in VLC, mkvmerge's does not (verified 2026-06-09).
    """
    ext = out.suffix.lower()

    def attempt(cmd: list[str], tool: str) -> str | None:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  embed via {tool} failed (sidecar .srt is the fallback):\n"
                  f"    {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else '?'}")
            return None
        return tool

    # --- MKV via mkvmerge (Spanish-only): the VLC-reliable path ---
    if ext in (".mkv", ".mka") and not keep_originals and shutil.which("mkvmerge"):
        return attempt(["mkvmerge", "-o", str(out), "--no-subtitles", str(src),
                        "--language", f"0:{lang_code}", "--track-name", f"0:{title}",
                        "--default-track", "0:yes", str(trans_srt)], "mkvmerge")

    codec = EMBED_SUB_CODEC.get(ext)
    if codec is None:
        return None  # container can't hold soft subs -> sidecar only

    # --- ffmpeg path (MP4/WebM, or MKV when mkvmerge absent / keeping originals) ---
    if ext in (".mkv", ".mka") and not shutil.which("mkvmerge") and not keep_originals:
        print("  note: mkvmerge not found — using ffmpeg (VLC may not render the "
              "muxed MKV track; the sidecar .srt will). Install: brew install mkvtoolnix")

    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-i", str(trans_srt),
           "-map", "0:v?", "-map", "0:a?"]
    kept = [s for s in subs if s.is_text] if keep_originals else []
    for s in kept:
        cmd += ["-map", f"0:{s.abs_index}"]
    cmd += ["-map", "1:0", "-c", "copy", "-c:s", codec]

    new_idx = len(kept)  # output subtitle index of the translated track
    for i, s in enumerate(kept):  # clear default on originals, preserve forced
        cmd += [f"-disposition:s:{i}", "forced" if s.is_forced else "0"]
    cmd += [f"-disposition:s:{new_idx}", "default",
            f"-metadata:s:s:{new_idx}", f"language={lang_code}",
            f"-metadata:s:s:{new_idx}", f"title={title}", str(out)]
    return attempt(cmd, "ffmpeg")


# ----------------------------------------------------------------------------- #
# main
# ----------------------------------------------------------------------------- #

def main() -> int:
    p = argparse.ArgumentParser(
        description="Translate a video's embedded subtitles into another language and remux as the default track.",
        epilog=(
            "examples:\n"
            "  sub-translate Movie.mkv finnish\n"
            "  sub-translate finnish Movie.mkv              # order doesn't matter\n"
            "  sub-translate \"Show.S01E01.mp4\" 'Brazilian Portuguese'\n"
            "  sub-translate --list Movie.mkv               # just show the subtitle tracks\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("tokens", nargs="*", metavar="FILE LANGUAGE",
                   help="the media file and the TARGET language, in any order. The language can be "
                        "a name or ISO code (finnish / fin / 'Brazilian Portuguese'). Required — "
                        "there is no default language.")
    p.add_argument("-o", "--output", type=Path, default=None)
    p.add_argument("--src-lang", default="eng", help="source subtitle language (default: eng)")
    p.add_argument("--src-index", type=int, default=None, help="pick subtitle track by index")
    p.add_argument("--lang-name", default=None, help="override the language NAME used in the prompt")
    p.add_argument("--lang-code", default=None, help="override the 3-letter ISO code stamped on the track")
    p.add_argument("--register", default="match the source's tone and formality; keep it natural and idiomatic")
    p.add_argument("--archive-dir", type=Path, default=None,
                   help="where to move the original file + sidecar after a successful embed "
                        "(default: <source-dir>/subtranslate). Keeps the working dir to just the output.")
    p.add_argument("--no-archive", action="store_true",
                   help="don't move the original/sidecar aside; leave them next to the output")
    p.add_argument("--no-sidecar", action="store_true",
                   help="don't write the matching .srt sidecar (on by default — kept as a backup in "
                        "the archive dir; renders reliably unlike some muxed-in subs)")
    p.add_argument("--no-remux", action="store_true",
                   help="skip embedding into the video; only write the sidecar .srt")
    p.add_argument("--keep-original-subs", action="store_true",
                   help="keep the original subtitle tracks in the embed (default: embed only the "
                        "translated track, so players can't auto-pick the wrong one)")
    p.add_argument("--list", action="store_true", help="list subtitle tracks and exit")
    p.add_argument("--model", default=DEFAULT_MODEL)
    args = p.parse_args()
    require_tools()

    # Positional args are FILE + LANGUAGE in any order. The media file is the one
    # that exists on disk; everything else (joined) is the target language — so a
    # multi-word language like "Brazilian Portuguese" works regardless of order or
    # quoting. --list needs only the file; translation needs both.
    tokens = args.tokens
    files = [t for t in tokens if Path(t).is_file()]
    if not files:
        if not tokens:
            die("usage: sub-translate FILE LANGUAGE   (order doesn't matter, "
                "e.g. 'sub-translate Movie.mkv finnish')")
        die(f"no existing media file among the arguments {tokens}. "
            f"usage: sub-translate FILE LANGUAGE")
    if len(files) > 1:
        die(f"ambiguous — more than one argument is an existing file {files}; "
            f"can't tell which is the video. Run them one at a time.")
    src = Path(files[0])
    language = " ".join(t for t in tokens if t != files[0]).strip()

    subs = probe_subs(src)
    if args.list or not subs:
        if not subs:
            print("No subtitle tracks found in this file.")
            return 0
        print(f"Subtitle tracks in {src.name}:")
        for s in subs:
            print(s.describe())
        if args.list:
            return 0

    # Translation requires a target language — there is no default.
    if not language:
        die(f"no target language given. usage: sub-translate FILE LANGUAGE   "
            f"(e.g. 'sub-translate \"{src.name}\" finnish')")
    res_name, res_code = resolve_language(language)
    lang_name = args.lang_name or res_name
    lang_code = args.lang_code or res_code or "und"
    if res_code is None and not args.lang_code:
        print(f"note: no ISO code known for '{lang_name}' — tagging the track 'und'. "
              f"Pass --lang-code XXX to set it.")

    chosen = choose_source(subs, args.src_lang, args.src_index)
    print(f"Source track: {chosen.describe().strip()}")

    out = args.output or src.with_name(f"{src.stem}_[{filename_label(lang_name)}_subs]{src.suffix}")
    if out.resolve() == src.resolve():
        die("output path equals input path; choose a different -o")

    api_key = load_api_key()

    with tempfile.TemporaryDirectory() as tmp:
        en_srt = Path(tmp) / "source.srt"
        print("Extracting subtitle track...")
        extract_srt(src, chosen, en_srt)

        cues = parse_srt(en_srt)
        if not cues:
            die("extracted subtitle had no parseable cues.")
        print(f"Parsed {len(cues)} cues. Translating to {lang_name} via {args.model}...")

        system = build_system_prompt(lang_name, args.register)
        texts = translate_srt(api_key, args.model, system, cues)

        trans_srt = Path(tmp) / "translated.srt"
        write_srt(cues, texts, trans_srt)

        # Sidecar .srt is written by default as a universal backup (renders in
        # every player; auto-loads when named to match the video). After a
        # successful embed it's moved into the archive dir so the working dir
        # holds only the finished file.
        sidecar = None
        if not args.no_sidecar:
            sidecar = out.with_suffix(".srt")
            sidecar.write_text(trans_srt.read_text(encoding="utf-8"), encoding="utf-8")

        embedded_with = None
        if not args.no_remux:
            print("Embedding translated track as default...")
            embedded_with = remux(src, trans_srt, out, subs, lang_code,
                                  f"{lang_name} (AI)", args.keep_original_subs)
            if embedded_with is None:
                # embed not produced (unsupported container or failed) — sidecar is the file
                if out.suffix.lower() not in EMBED_SUB_CODEC:
                    print(f"  note: {out.suffix} can't carry a soft subtitle track — sidecar .srt only.")
                if sidecar is None:
                    sidecar = out.with_suffix(".srt")
                    sidecar.write_text(trans_srt.read_text(encoding="utf-8"), encoding="utf-8")

    # Tidy up: when we produced a self-contained output (embed succeeded), move
    # the original video + the sidecar backup into the archive dir so the source
    # folder is left with just the finished file. If the embed didn't happen,
    # there's no replacement, so we leave everything in place.
    archived_to = None
    if embedded_with and not args.no_archive:
        archive_dir = args.archive_dir or (src.parent / "subtranslate")
        try:
            archive_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(archive_dir / src.name))
            if sidecar and sidecar.exists():
                shutil.move(str(sidecar), str(archive_dir / sidecar.name))
                sidecar = archive_dir / sidecar.name
            archived_to = archive_dir
        except OSError as e:
            print(f"  note: couldn't move originals to {archive_dir} ({e}); left in place.")

    print("\n✓ Done")
    if embedded_with:
        print(f"  output  : {out}   ← subs embedded as the default track ({embedded_with})")
    if archived_to:
        print(f"  archived: {archived_to}/   (original{' + sidecar .srt' if sidecar else ''})")
    elif sidecar:
        loc = "auto-loads in VLC; works in every player"
        print(f"  sidecar : {sidecar}   ← {loc}")
        if not embedded_with:
            print("  (no embed produced — the sidecar .srt is your subtitle file)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
