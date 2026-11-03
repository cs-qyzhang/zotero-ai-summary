#!/usr/bin/env python3
"""Generate a "text bomb" PDF: small on disk, enormous after text extraction.

This is the realistic worst case for the extraction path: a modest upload that
decompresses into hundreds of MB of extractable text (dense OCR dumps, generated
reports, ...). It is what the worker's page/char budgets must stop.

Usage: mk_text_bomb.py [pages] [mb_per_page] [out_path]
Default: 100 pages x 3MB -> ~300MB of text in a ~1MB file.
Pages are written one at a time so generation stays at a few MB of RSS.
"""
import os
import sys

import pymupdf

BLOCK = b"AAAAAAAA BBBBBBBB CCCCCCCC DDDDDDDD "
CHARS_PER_LINE = len(BLOCK) * 16
LINE_HEIGHT = 9
PAGE_WIDTH = 8000
MAX_PAGE_HEIGHT = 60000


def content_stream(lines: int, height: float) -> bytes:
    chunks = [f"BT /F1 8 Tf {LINE_HEIGHT} TL 20 {height - 20} Td\n".encode()]
    chunks.extend(b"(" + BLOCK * 16 + b") Tj T*\n" for _ in range(lines))
    chunks.append(b"ET")
    return b"".join(chunks)


def make(path: str, pages: int = 100, mb_per_page: int = 3) -> None:
    target = mb_per_page * 1024 * 1024
    lines = min(
        max(1, target // CHARS_PER_LINE),
        (MAX_PAGE_HEIGHT - 40) // LINE_HEIGHT,
    )
    height = lines * LINE_HEIGHT + 40
    doc = pymupdf.open()
    font_xref = doc.get_new_xref()
    doc.update_object(
        font_xref, "<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"
    )

    for _ in range(pages):
        page = doc.new_page(width=PAGE_WIDTH, height=height)
        doc.xref_set_key(
            page.xref, "Resources", f"<</Font<</F1 {font_xref} 0 R>>>>"
        )
        stream_xref = doc.get_new_xref()
        doc.update_object(stream_xref, "<<>>")
        doc.update_stream(
            stream_xref, content_stream(lines, height), compress=True
        )
        doc.xref_set_key(page.xref, "Contents", f"{stream_xref} 0 R")

    doc.save(path, garbage=4, deflate=True)
    doc.close()

    print(
        f"{path}: {pages} pages x {lines * CHARS_PER_LINE / 1024 / 1024:.1f}MB text, "
        f"{os.path.getsize(path)} bytes on disk"
    )


if __name__ == "__main__":
    page_count = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    per_page = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    out = sys.argv[3] if len(sys.argv) > 3 else "/tmp/zotero-regress/text_bomb.pdf"
    make(out, page_count, per_page)
