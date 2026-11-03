#!/usr/bin/env python3
"""Isolated worker for CPU/memory heavy PDF parsing.

Protocol
--------
stdin        JSON config (see run_pdf_worker() in server.py)
stdout       unused
stderr       human readable diagnostics, then exactly one JSON report line
<output>     {"ok": true,  ...payload...}
             {"ok": false, "error": {"code": ..., "message": ...}}
exit         0 on success, 1 on failure (parent maps error.code to HTTP)

Budgets enforced *inside* the worker, in order of precedence:
  * page count            -> too_many_pages   (before touching page content)
  * cumulative text chars -> too_much_text    (abort mid-extraction, not after)
  * per-page wall time    -> page skipped, extraction continues
  * overall wall deadline -> deadline         (clean error before the parent's kill)
  * RLIMIT_AS / RLIMIT_CPU / RLIMIT_FSIZE / RLIMIT_CORE as a last resort
"""
import json
import os
import resource
import signal
import sys
import time
from contextlib import contextmanager

PAGE_TIMEOUT_SEC = 5.0
MIN_PAGES_FOR_BAD_PAGE_ABORT = 3
_MUPDF_SINK = None


class PdfBudgetError(Exception):
    """A budget was exceeded; carries a stable machine readable code."""

    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


class PageTimeout(Exception):
    pass


def write_json(path: str, data: dict) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp_path, path)


def report(**fields) -> None:
    fields.setdefault("rss_kb", resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    print(json.dumps(fields, ensure_ascii=False), file=sys.stderr, flush=True)


@contextmanager
def time_limit(seconds: float):
    """SIGALRM based deadline. Interrupts Python-level work reliably; long C
    calls (PyMuPDF) are only interrupted once the GIL is handed back, which is
    why the parent also enforces a hard kill."""
    if not seconds or seconds <= 0:
        yield
        return

    def handler(signum, frame):
        raise PageTimeout()

    previous = signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def open_document(path: str):
    """Return (engine_name, page_count, get_text(page_index), close)."""
    import pymupdf  # PyMuPDF >= 1.24

    # A malformed PDF can make MuPDF emit a warning per object; redirected so
    # a 50MB file cannot flood stderr (which the parent has to read).
    try:
        global _MUPDF_SINK
        _MUPDF_SINK = open(os.devnull, "w")
        pymupdf.set_messages(stream=_MUPDF_SINK)
    except Exception:
        pass
    doc = pymupdf.open(path)
    return (
        f"pymupdf-{getattr(pymupdf, '__version__', '?')}",
        doc.page_count,
        lambda index: doc.load_page(index).get_text("text"),
        doc.close,
    )


def extract_pages(cfg: dict, deadline: float) -> dict:
    """Extract text page by page, enforcing all budgets while doing so."""
    max_pages = int(cfg["max_pages"])
    max_chars = int(cfg["max_text_chars"])
    per_page_timeout = float(cfg.get("page_timeout_sec", PAGE_TIMEOUT_SEC))

    engine, page_count, get_text, close = open_document(cfg["pdf_path"])
    try:
        if page_count > max_pages:
            raise PdfBudgetError(
                "too_many_pages",
                f"PDF has {page_count} pages; limit is {max_pages}",
            )

        texts: list[str] = []
        skipped: list[int] = []
        total_chars = 0
        max_bad_pages = max(MIN_PAGES_FOR_BAD_PAGE_ABORT, page_count // 2)

        for index in range(page_count):
            if time.monotonic() > deadline:
                raise PdfBudgetError(
                    "deadline",
                    f"time budget exhausted after {index}/{page_count} pages",
                    partial_pages=index,
                )
            try:
                with time_limit(per_page_timeout):
                    text = get_text(index)
            except MemoryError:
                raise PdfBudgetError(
                    "memory", f"out of memory while extracting page {index + 1}"
                )
            except Exception as exc:  # one bad page is not fatal
                skipped.append(index)
                print(
                    f"skipped page {index + 1}/{page_count}: "
                    f"{type(exc).__name__}: {str(exc)[:200]}",
                    file=sys.stderr,
                )
                if len(skipped) > max_bad_pages:
                    raise PdfBudgetError(
                        "too_many_bad_pages",
                        f"{len(skipped)} pages could not be extracted "
                        f"(of {page_count}); giving up",
                        skipped_pages=skipped[:50],
                    )
                continue

            total_chars += len(text)
            if total_chars > max_chars:
                raise PdfBudgetError(
                    "too_much_text",
                    f"extracted text exceeds {max_chars} chars "
                    f"at page {index + 1}/{page_count}",
                    total_chars=total_chars,
                )
            texts.append(text)

        return {
            "engine": engine,
            "page_count": page_count,
            "texts": texts,
            "skipped_pages": skipped,
            "total_chars": total_chars,
        }
    finally:
        close()


def budget_splits(cfg: dict, char_count: int) -> None:
    chunk_size = int(cfg["chunk_size"])
    chunk_overlap = int(cfg["chunk_overlap"])
    max_splits = int(cfg["max_splits"])
    step = max(1, chunk_size - chunk_overlap)
    estimated = max(1, char_count // step + 1)
    if estimated > max_splits:
        raise PdfBudgetError(
            "too_many_splits",
            f"estimated {estimated} chunks; limit is {max_splits}. "
            "Use a larger chunk_size or fewer pages.",
        )


def parse_pdf(cfg: dict, deadline: float) -> dict:
    from langchain_core.documents import Document
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    extracted = extract_pages(cfg, deadline)
    full_text = "\n".join(extracted["texts"])
    del extracted["texts"]
    budget_splits(cfg, len(full_text))

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=int(cfg["chunk_size"]),
        chunk_overlap=int(cfg["chunk_overlap"]),
        length_function=len,
    )
    splits = splitter.split_documents([Document(page_content=full_text)])
    return {
        "ok": True,
        "splits": [
            {"content": split.page_content, "metadata": split.metadata}
            for split in splits
        ],
        "total_pages": extracted["page_count"],
        "total_chars": len(full_text),
        "engine": extracted["engine"],
        "skipped_pages": extracted["skipped_pages"],
    }


def apply_limits(cfg: dict) -> None:
    memory_bytes = int(cfg["memory_mb"]) * 1024 * 1024
    cpu_seconds = int(cfg["cpu_seconds"])
    max_result_bytes = int(cfg["max_result_mb"]) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 5))
    resource.setrlimit(resource.RLIMIT_FSIZE, (max_result_bytes, max_result_bytes))
    # Never dump core: a crashed 400MB worker must not write a 400MB core file.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def main() -> int:
    cfg = json.load(sys.stdin)
    apply_limits(cfg)

    mode = cfg["mode"]
    output_path = cfg["output_path"]
    started = time.monotonic()
    cpu_seconds = int(cfg["cpu_seconds"])
    # Leave headroom so we fail with a clean message instead of being SIGKILLed.
    deadline = started + max(1.0, cpu_seconds - 3.0)

    try:
        if mode != "parse":
            raise PdfBudgetError("bad_mode", f"unsupported worker mode: {mode}")
        payload = parse_pdf(cfg, deadline)
    except PdfBudgetError as exc:
        write_json(
            output_path,
            {"ok": False, "error": {"code": exc.code, "message": exc.message, **exc.extra}},
        )
        report(
            ok=False,
            error=exc.code,
            message=exc.message,
            wall=round(time.monotonic() - started, 2),
            skipped_pages=exc.extra.get("skipped_pages", []),
        )
        return 1
    except MemoryError:
        write_json(
            output_path,
            {"ok": False, "error": {"code": "memory", "message": "out of memory"}},
        )
        report(ok=False, error="memory", wall=round(time.monotonic() - started, 2))
        return 1
    except BaseException as exc:  # noqa: BLE001 - must always produce a reason
        write_json(
            output_path,
            {
                "ok": False,
                "error": {
                    "code": "internal",
                    "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                },
            },
        )
        report(
            ok=False,
            error="internal",
            message=f"{type(exc).__name__}: {exc}",
            wall=round(time.monotonic() - started, 2),
        )
        return 1

    write_json(output_path, payload)
    report(
        ok=True,
        engine=payload.get("engine"),
        pages=payload.get("total_pages"),
        chars=payload.get("total_chars"),
        splits=len(payload.get("splits", [])) or None,
        skipped_pages=payload.get("skipped_pages", []),
        wall=round(time.monotonic() - started, 2),
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # pragma: no cover - config/limit failures
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
