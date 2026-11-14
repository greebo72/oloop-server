#!/bin/sh
# Packs what the Dockerfile needs into dist/oloop-server-<revision>.tar.gz.
# For a build without Docker on this computer: Portainer builds the image from
# this archive (Images, Build a new image, Upload). See README.md.
set -eu
cd "$(dirname "$0")/.."

rev=$(git rev-parse --short HEAD)
# Say so when the archive holds something that is not committed.
if [ -n "$(git status --porcelain -- Dockerfile pyproject.toml src)" ]; then
    rev="$rev-dirty"
fi

mkdir -p dist
out="dist/oloop-server-$rev.tar.gz"
# COPYFILE_DISABLE: no ._ files of macOS in the archive.
COPYFILE_DISABLE=1 tar --exclude='__pycache__' --exclude='*.egg-info' --exclude='.DS_Store' \
    -czf "$out" Dockerfile pyproject.toml src
echo "$out"
