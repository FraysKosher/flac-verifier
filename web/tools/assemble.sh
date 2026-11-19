#!/usr/bin/env bash
# Build web/_site: the static site plus the unmodified engine files.
set -euo pipefail
web="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
root="$(cd "$web/.." && pwd)"
site="$web/_site"
rm -rf "$site"
mkdir -p "$site"
shopt -s nullglob
for f in "$web"/*.html "$web"/*.js "$web"/*.css "$web"/*.py; do
  case "$(basename "$f")" in
    playwright.config.js) continue ;;
  esac
  cp "$f" "$site/"
done
cp "$root"/motor_flac.py "$root"/_motor_*.py "$site/"
