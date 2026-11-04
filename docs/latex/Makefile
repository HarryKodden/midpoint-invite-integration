# Build the documentation PDF with TeX Live (Docker; no local texlive required).
#
#   make -C docs/latex pdf
#   # or:
#   ./build.sh

IMAGE ?= texlive/texlive:latest
DOC   := main

.PHONY: pdf clean

pdf:
	docker run --rm -v "$(CURDIR):/work" -w /work $(IMAGE) \
		latexmk -pdf -interaction=nonstopmode -halt-on-error $(DOC).tex

clean:
	rm -f *.aux *.log *.out *.toc *.fls *.fdb_latexmk *.synctex.gz
