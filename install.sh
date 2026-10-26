#!/bin/sh
# Install Ishoni into ~/ishoni (a git clone, updated in place on a rerun) and link ~/.local/bin/ishoni, then run the
# Ishoni command given as arguments, if any. Nothing else is touched: no shell profile, no system package.
#
#   curl -fsSL https://raw.githubusercontent.com/zealton/ishoni/main/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/zealton/ishoni/main/install.sh | sh -s -- join https://GATEWAY CODE --name alice
#
# Environment: ISHONI_DIR (default ~/ishoni), ISHONI_REPO (default https://github.com/zealton/ishoni), ISHONI_BRANCH
# (default main). Needs Python 3.8+ and git (or curl and tar).
set -eu
DIR="${ISHONI_DIR:-$HOME/ishoni}"
REPO="${ISHONI_REPO:-https://github.com/zealton/ishoni}"
BRANCH="${ISHONI_BRANCH:-main}"

have() { command -v "$1" >/dev/null 2>&1; }
fail() { echo "ishoni install: $*" >&2; exit 1; }

py=""
for candidate in "${ISHONI_PYTHON:-}" python3 python; do
  [ -n "$candidate" ] || continue
  if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then py=$candidate; break; fi
done
[ -n "$py" ] || fail "Python 3.8 or newer is required (macOS: xcode-select --install, or brew install python3; Linux: apt install python3)"

if [ -d "$DIR/.git" ]; then
  echo "==> updating $DIR"
  git -C "$DIR" fetch -q origin "$BRANCH" && git -C "$DIR" checkout -q "$BRANCH" 2>/dev/null && \
    git -C "$DIR" merge -q --ff-only "origin/$BRANCH" || echo "    (could not fast-forward $DIR; keeping what is there)" >&2
elif [ -e "$DIR" ] && [ ! -f "$DIR/ishoni" ]; then
  fail "$DIR exists and is not an Ishoni checkout; set ISHONI_DIR to another folder"
elif have git; then
  echo "==> cloning $REPO into $DIR"
  git clone -q --depth 1 --branch "$BRANCH" "$REPO" "$DIR"
elif have curl && have tar; then
  echo "==> downloading $REPO ($BRANCH) into $DIR"
  tmp=$(mktemp -d)
  curl -fsSL "$REPO/archive/refs/heads/$BRANCH.tar.gz" | tar -xz -C "$tmp"
  mkdir -p "$DIR"
  cp -R "$tmp"/*/. "$DIR"/
  rm -rf "$tmp"
else
  fail "git (or curl and tar) is required"
fi
[ -f "$DIR/ishoni" ] || fail "$DIR holds no ishoni launcher"

mkdir -p "$HOME/.local/bin"
ln -sf "$DIR/ishoni" "$HOME/.local/bin/ishoni"
echo "==> Ishoni is in $DIR; run it as: sh $DIR/ishoni ...   (also ~/.local/bin/ishoni when that is on your PATH)"
if [ $# -gt 0 ]; then
  echo "==> ishoni $*"
  exec sh "$DIR/ishoni" "$@"
fi
