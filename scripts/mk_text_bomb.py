#!/usr/bin/env python3
"""Generate a "text bomb" PDF: small on disk, enormous after text extraction.

This is the realistic worst case for the extraction path: a modest upload that
decompresses into hundreds of MB of extractable text (dense OCR dumps, generated
reports, ...). It is what the worker's page/char budgets must stop.

Usage: mk_text_bomb.py [pages] [mb_per_page] [out_path]
Default: 100 pages x 3MB -> ~300MB of text in a ~3MB file.
Pages are written one at a time so generation stays at a few MB of RSS.
"""
import sys

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

BLOCK = b"AAAAAAAA BBBBBBBB CCCCCCCC DDDDDDDD "


def make(path: str, pages: int = 100, mb_per_page: int = 3) -> None:
    target = mb_per_page * 1024 * 1024
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)

    for index in range(pages):
        page = writer.add_blank_page(width=612, height=792)
        resources = DictionaryObject()
        fonts = DictionaryObject()
        fonts[NameObject("/F1")] = font_ref
        resources[NameObject("/Font")] = fonts
        page[NameObject("/Resources")] = resources

        chunks = [b"BT /F1 9 Tf 20 780 Td\n"]
        written = 0
        while written < target:
            chunks.append(b"(" + BLOCK * 16 + b") Tj T*\n")
            written += len(BLOCK) * 16 + 10
        chunks.append(b"ET")
        stream = DecodedStreamObject()
        stream.set_data(b"".join(chunks))
        page[NameObject("/Contents")] = stream
        page.compress_content_streams()  # keep the file tiny on disk
        del chunks

    with open(path, "wb") as f:
        writer.write(f)
    import os

    print(
        f"{path}: {pages} pages x {mb_per_page}MB text, "
        f"{os.path.getsize(path)} bytes on disk"
    )


if __name__ == "__main__":
    page_count = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    per_page = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    out = sys.argv[3] if len(sys.argv) > 3 else "/tmp/zotero-regress/text_bomb.pdf"
    make(out, page_count, per_page)
