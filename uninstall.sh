#!/usr/bin/env bash
# uninstall.sh — remove the `sub-translate` symlink from your PATH.
# Only removes a symlink that points back into THIS repo (won't touch a real
# binary that happens to share the name).

set -euo pipefail

REPO_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
WRAPPER="$REPO_DIR/bin/sub-translate"
CMD_NAME="sub-translate"

removed=0
# scan every PATH dir + the usual install targets
SCAN_DIRS="$HOME/.local/bin /opt/homebrew/bin /usr/local/bin"
SCAN_DIRS="$SCAN_DIRS $(echo "$PATH" | tr ':' ' ')"

for d in $SCAN_DIRS; do
  link="$d/$CMD_NAME"
  [ -L "$link" ] || continue
  target="$(readlink "$link" || true)"
  if [ "$target" = "$WRAPPER" ]; then
    rm -f "$link"
    echo "✓ removed $link"
    removed=1
  fi
done

[ "$removed" = "1" ] || echo "nothing to remove (no $CMD_NAME symlink pointing into $REPO_DIR found)"
