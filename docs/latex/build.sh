#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
docker run --rm -v "$PWD:/work" -w /work texlive/texlive:latest \
  latexmk -pdf -interaction=nonstopmode main.tex
ls -la main.pdf
