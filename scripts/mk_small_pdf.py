#!/usr/bin/env python3
"""Generate a small, valid 10-page PDF (normal-path baseline)."""
import sys


def make(path: str, pages: int = 10) -> None:
    import pymupdf

    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=612, height=792)
        page.insert_text(
            (60, 90),
            f"Page {i}\n" + "lorem ipsum dolor sit amet " * 60,
            fontsize=10,
        )
    doc.save(path)
    doc.close()
    print(f"{path}: {pages} pages (pymupdf)")


if __name__ == "__main__":
    make(sys.argv[1] if len(sys.argv) > 1 else "/tmp/zotero-regress/small10.pdf")
