#!/usr/bin/env bash
# Sluice CLI installer — pulls the tagged source from GitHub and BUILDS the `sluice` command locally.
#
#   curl -LsSf https://raw.githubusercontent.com/jugrajsingh/Sluice/main/install.sh | bash
#
# Options (env vars):
#   SLUICE_VERSION=v0.2.2   pin a release tag (default: the latest published release)
#   SLUICE_DIRECT=1         also install the [direct] extra (sluice-drivers) for `sluice apply --direct`
#
# Requires Python 3.13+. Uses uv (recommended) → pipx → pip --user, whichever is found.
set -euo pipefail

REPO="jugrajsingh/Sluice"
VERSION="${SLUICE_VERSION:-latest}"

say() { printf '\033[1;36msluice-install:\033[0m %s\n' "$*"; }
err() { printf '\033[1;31msluice-install error:\033[0m %s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || err "curl is required"

# Resolve "latest" → the newest published release tag (skips drafts/prereleases).
if [ "$VERSION" = "latest" ]; then
  say "resolving the latest release…"
  VERSION="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
    | grep -m1 '"tag_name"' | sed -E 's/.*"tag_name"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')" || true
  [ -n "${VERSION:-}" ] || err "could not resolve the latest release — set SLUICE_VERSION=vX.Y.Z and retry"
fi
say "installing the Sluice CLI ${VERSION} (building from source)"

base="git+https://github.com/${REPO}.git@${VERSION}"
CLI="${base}#subdirectory=packages/cli"
CORE="${base}#subdirectory=packages/core"      # sluice-cli depends on sluice-core (also unpublished)
DRIVERS="${base}#subdirectory=packages/drivers" # only for the optional --direct extra

want_direct() { [ "${SLUICE_DIRECT:-0}" = "1" ]; }

if command -v uv >/dev/null 2>&1; then
  say "using: uv tool install"
  withs=( --with "$CORE" )
  want_direct && withs+=( --with "$DRIVERS" )
  uv tool install --force --python 3.13 "$CLI" "${withs[@]}"
elif command -v pipx >/dev/null 2>&1; then
  say "using: pipx"
  pre=( --preinstall "$CORE" )
  want_direct && pre+=( --preinstall "$DRIVERS" )
  pipx install --force "${pre[@]}" "$CLI"
elif command -v pip3 >/dev/null 2>&1 || command -v pip >/dev/null 2>&1; then
  PIP="$(command -v pip3 || command -v pip)"
  say "using: ${PIP} --user  (install uv or pipx for an isolated install)"
  pkgs=( "$CORE" "$CLI" )
  want_direct && pkgs+=( "$DRIVERS" )
  "$PIP" install --user "${pkgs[@]}"
else
  err "need one of: uv (recommended — https://astral.sh/uv), pipx, or pip"
fi

if command -v sluice >/dev/null 2>&1; then
  say "done — $(command -v sluice)"
  say "try:  sluice --help    (point it at your console:  sluice --api https://<console-host> get)"
else
  say "installed, but 'sluice' is not on PATH yet — add your tool bin dir to PATH:"
  say '  export PATH="$HOME/.local/bin:$PATH"   # (uv/pipx)  — then restart your shell'
fi
