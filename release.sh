#!/bin/bash
set -e

# Get current version from latest tag
LAST=$(git tag -l 'v*' --sort=-v:refname | head -1 | sed 's/^v//')
if [ -z "$LAST" ]; then
    echo "No existing tags found"
    exit 1
fi

MAJOR=$(echo "$LAST" | cut -d. -f1)
MINOR=$(echo "$LAST" | cut -d. -f2)
PATCH=$(echo "$LAST" | cut -d. -f3)
VERSION="${MAJOR}.${MINOR}.$((PATCH + 1))"

# Allow override: ./release.sh 0.4.0
if [ -n "$1" ]; then
    VERSION="$1"
fi

echo "Releasing v${VERSION}..."

# Update pyproject.toml
sed -i '' "s/^version = .*/version = \"${VERSION}\"/" pyproject.toml

git add pyproject.toml
git commit -m "v${VERSION}"
git tag "v${VERSION}"
git push origin main --tags

echo "Done! GitHub Action will update Homebrew tap."
