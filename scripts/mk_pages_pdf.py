#!/usr/bin/env python3
"""Generate a PDF with a fixed number of text pages (page-limit regression)."""
import sys

import pymupdf

PARA = (
    "We report a meta-analysis of soil properties influencing biochar-pesticide "
    "interactions across 128 studies. "
) * 6


def make(path: str, pages: int) -> None:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=612, height=792)
        y = 60
        for _ in range(8):
            page.insert_text((50, y), f"{i}: {PARA[:110]}", fontsize=7)
            y += 12
    doc.save(path)
    doc.close()
    print(f"{path}: {pages} pages")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/zotero-regress/pages.pdf"
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    make(out, count)
