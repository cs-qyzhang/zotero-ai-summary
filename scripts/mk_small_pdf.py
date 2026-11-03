#!/usr/bin/env python3
"""Generate a small, valid 10-page PDF (normal-path baseline).

Uses PyMuPDF when available (produces a file both PyMuPDF and pypdf can read),
and falls back to pypdf otherwise.
"""
import sys


def make_with_pymupdf(path: str, pages: int) -> None:
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


def make_with_pypdf(path: str, pages: int) -> None:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    for i in range(pages):
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        resources = DictionaryObject()
        fonts = DictionaryObject()
        fonts[NameObject("/F1")] = writer._add_object(font)
        resources[NameObject("/Font")] = fonts
        page[NameObject("/Resources")] = resources
        content = (
            b"BT /F1 12 Tf 72 720 Td (Page %d " % i
            + b"lorem ipsum dolor sit amet " * 400
            + b") Tj ET"
        )
        stream = DecodedStreamObject()
        stream.set_data(content)
        page[NameObject("/Contents")] = stream
    with open(path, "wb") as f:
        writer.write(f)


def make(path: str, pages: int = 10) -> None:
    try:
        make_with_pymupdf(path, pages)
        engine = "pymupdf"
    except ImportError:
        make_with_pypdf(path, pages)
        engine = "pypdf"
    print(f"{path}: {pages} pages ({engine})")


if __name__ == "__main__":
    make(sys.argv[1] if len(sys.argv) > 1 else "/tmp/zotero-regress/small10.pdf")
