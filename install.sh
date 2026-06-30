#!/usr/bin/env bash
# install.sh — put `sub-translate` on your PATH.
#
# Symlinks bin/sub-translate into the first writable directory that's already on
# your PATH (preferring ~/.local/bin, then Homebrew, then /usr/local/bin). If
# none are writable+on-PATH it falls back to ~/.local/bin and tells you the one
# line to add to your shell profile.
#
# Override the target dir explicitly:   INSTALL_DIR=/somewhere/on/path ./install.sh
#
# Idempotent: re-running just refreshes the symlink. Undo with ./uninstall.sh.

set -euo pipefail

REPO_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
WRAPPER="$REPO_DIR/bin/sub-translate"
CMD_NAME="sub-translate"

chmod +x "$WRAPPER" "$REPO_DIR/sub_translate.py"

# --- dependency preflight (warn, don't fail) -------------------------------- #
if ! command -v python3 >/dev/null 2>&1; then
  echo "✗ python3 not found on PATH — the command won't run until you install it." >&2
fi
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  echo "⚠ ffmpeg/ffprobe not found. Install with:  brew install ffmpeg   (macOS)" >&2
fi
if ! command -v mkvmerge >/dev/null 2>&1; then
  echo "⚠ mkvmerge not found (recommended for reliable .mkv embeds). Install:" >&2
  echo "    brew install mkvtoolnix   (macOS)   /   apt install mkvtoolnix   (Linux)" >&2
fi

# --- pick an install dir ---------------------------------------------------- #
on_path() { case ":$PATH:" in *":$1:"*) return 0;; *) return 1;; esac; }
# create the dir if missing, then REQUIRE it to be writable (mkdir -p on an
# existing non-writable dir succeeds, so the -w test is what actually gates).
usable()  { mkdir -p "$1" 2>/dev/null && [ -w "$1" ]; }

DEST_DIR=""
WARN_PATH=0

if [ -n "${INSTALL_DIR:-}" ]; then
  usable "$INSTALL_DIR" || { echo "✗ INSTALL_DIR=$INSTALL_DIR is not writable" >&2; exit 1; }
  DEST_DIR="$INSTALL_DIR"
  on_path "$DEST_DIR" || WARN_PATH=1
else
  CANDIDATES=("$HOME/.local/bin" "/opt/homebrew/bin" "/usr/local/bin")
  # first candidate that is on PATH AND writable
  for d in "${CANDIDATES[@]}"; do
    if on_path "$d" && usable "$d"; then DEST_DIR="$d"; break; fi
  done
  # fall back to ~/.local/bin (create it) and warn about PATH
  if [ -z "$DEST_DIR" ]; then
    DEST_DIR="$HOME/.local/bin"
    usable "$DEST_DIR" || { echo "✗ cannot create $DEST_DIR" >&2; exit 1; }
    WARN_PATH=1
  fi
fi

LINK="$DEST_DIR/$CMD_NAME"
ln -sf "$WRAPPER" "$LINK"
echo "✓ linked $CMD_NAME → $LINK"

if [ "$WARN_PATH" = "1" ]; then
  echo
  echo "⚠ $DEST_DIR is not on your PATH. Add it (then restart your shell):"
  # zsh is macOS default; print the right profile file
  PROFILE="$HOME/.zshrc"; [ -n "${BASH_VERSION:-}" ] && PROFILE="$HOME/.bashrc"
  echo "    echo 'export PATH=\"$DEST_DIR:\$PATH\"' >> $PROFILE"
fi

echo
echo "Done. Try:   $CMD_NAME --help"
echo "Usage:       $CMD_NAME \"Some.Movie.mkv\""
