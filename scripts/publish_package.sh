#!/usr/bin/env bash
# Bump version, commit, tag, push. Tag push triggers .github/workflows/publish.yml.
#
# Usage:
#   scripts/publish_package.sh [patch|minor|major] [explicit_version]
#
# Examples:
#   scripts/publish_package.sh                 # default: patch bump
#   scripts/publish_package.sh minor
#   scripts/publish_package.sh patch 0.2.5     # explicit version wins
set -euo pipefail

BUMP="${1:-patch}"
EXPLICIT="${2:-}"

cd "$(dirname "$0")/.."

if [[ -n "$(git status --porcelain)" ]]; then
  echo "error: working tree is dirty. commit or stash first." >&2
  git status --short >&2
  exit 1
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$BRANCH" != "main" ]]; then
  read -r -p "you are on '$BRANCH', not main. publish anyway? [y/N] " ans
  case "$ans" in
    y|Y|yes|YES) ;;
    *) echo "aborted."; exit 1 ;;
  esac
fi

NEW_VERSION="$(./scripts/bump_version.py "$BUMP" "$EXPLICIT")"
TAG="v${NEW_VERSION}"

if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "error: tag $TAG already exists." >&2
  git checkout -- pyproject.toml src/foldout/__init__.py
  exit 1
fi

echo "bumped to $NEW_VERSION"

git add pyproject.toml src/foldout/__init__.py
git commit -m "release: $NEW_VERSION"
git tag "$TAG"

git push origin "$BRANCH"
git push origin "$TAG"

echo "pushed $TAG. PyPI publish workflow will pick it up."
