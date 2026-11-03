import os
import sys
import json
import argparse
import logging
import asyncio
import resource
import signal
import socket
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import uvicorn
from markdown_it import MarkdownIt
from fastapi import FastAPI, UploadFile, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

logger = logging.getLogger("summarizer")

# 运行状态（供 /healthz 与准入控制使用；只在事件循环线程内读写）
SERVICE_STATE = {
    "inflight": 0,
    "queued": 0,
    "worker_runs": 0,
    "worker_kills": 0,
    "last_worker": None,
    "started_at": time.time(),
}

# 初始化限制器（可用环境变量调，方便压测/运维）
RATE_LIMITS = {
    "parse": os.getenv("PARSE_RATE_LIMIT", "6/minute"),
    "html": os.getenv("HTML_RATE_LIMIT", "6/minute"),
    "test": os.getenv("TEST_RATE_LIMIT", "3/minute"),
}
limiter = Limiter(key_func=get_remote_address)


# ---------------------------------------------------------------- watchdog ---
def sd_notify(message: str) -> bool:
    """最小化的 sd_notify（不依赖 python-systemd）。"""
    addr = os.getenv("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(message.encode())
        return True
    except OSError:
        return False


async def watchdog_loop() -> None:
    """定期给 systemd 发 WATCHDOG=1。

    如果事件循环被长时间阻塞（同步 IO、大 json.load、死循环），
    心跳会变慢或被跳过，systemd 会判定进程“活着但卡死”并重启服务。
    没有配置 NOTIFY_SOCKET 时退化为普通日志。
    """
    usec = int(os.getenv("WATCHDOG_USEC", "0") or 0)
    interval = max(5.0, usec / 1e6 / 3) if usec else 30.0
    sd_notify("READY=1")
    while True:
        tick = time.monotonic()
        await asyncio.sleep(interval)
        lag = time.monotonic() - tick - interval
        if lag < interval / 2:
            sd_notify("WATCHDOG=1")
        else:
            logger.warning(
                "event=watchdog_behind lag=%.1fs interval=%.1fs", lag, interval
            )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(watchdog_loop())
    logger.info(
        "event=service_start pid=%s watchdog_socket=%s inflight_max=%s",
        os.getpid(),
        bool(os.getenv("NOTIFY_SOCKET")),
        os.getenv("MAX_INFLIGHT", "6"),
    )
    try:
        yield
    finally:
        task.cancel()
        sd_notify("STOPPING=1")


# 初始化 FastAPI 服务器
app = FastAPI(lifespan=lifespan)

# 添加中间件支持代理头信息
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=["127.0.0.1"])
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])  # 生产环境建议配置具体域名


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """给每个请求一个 rid，并记录耗时/状态码，便于和 worker 日志对上。"""
    rid = uuid.uuid4().hex[:8]
    request.state.rid = rid
    # 容量检查放在最前面：连请求体都不读就直接拒绝，
    # 避免排队请求先把 50MB 上传/连接/临时文件堆在这里。
    if request.url.path == "/parse_pdf" and (
        SERVICE_STATE["inflight"] >= MAX_INFLIGHT
    ):
        logger.warning(
            "event=reject_busy rid=%s path=%s inflight=%s",
            rid,
            request.url.path,
            SERVICE_STATE["inflight"],
        )
        return JSONResponse(
            status_code=429,
            content={"detail": "server busy, retry later"},
            headers={"Retry-After": "10"},
        )
    started = time.monotonic()
    try:
        response = await call_next(request)
    except BaseException:
        logger.exception(
            "event=request_error rid=%s method=%s path=%s ip=%s wall=%.2f",
            rid,
            request.method,
            request.url.path,
            get_remote_address(request),
            time.monotonic() - started,
        )
        raise
    logger.info(
        "event=request_end rid=%s method=%s path=%s status=%s wall=%.2f ip=%s",
        rid,
        request.method,
        request.url.path,
        response.status_code,
        time.monotonic() - started,
        get_remote_address(request),
    )
    return response

# 添加限制器异常处理
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# 说明：本服务只做 PDF 解析（/parse_pdf）与 Markdown 转 HTML（/md_to_html）。
# 之前挂在 /upload 上的「服务端总结 + sqlite 缓存」已移除（客户端自己调 LLM）。

# ip -> 最近一次上传的文件名（带上限/TTL，供 /md_to_html 落盘用）
ip_title_map: dict[str, tuple[str, float]] = {}

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))
MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "300"))
MAX_PDF_TEXT_CHARS = int(os.getenv("MAX_PDF_TEXT_CHARS", "5000000"))
MAX_SPLITS = int(os.getenv("MAX_SPLITS", "20000"))
# 客户端（zotero_script.js）默认 chunkSize=64000，上限必须显著高于它；
# 早先设成 20000 导致线上真实请求全部 400。
MAX_CHUNK_SIZE = int(os.getenv("MAX_CHUNK_SIZE", "200000"))
WORKER_MEMORY_MB = int(os.getenv("WORKER_MEMORY_MB", "384"))
MAX_RESULT_MB = int(os.getenv("MAX_RESULT_MB", "16"))
MAX_STDERR_BYTES = 8192  # 只保留 worker stderr 的尾部，防止坏 PDF 刷爆父进程内存
PARSE_TIMEOUT_SEC = int(os.getenv("PARSE_TIMEOUT_SEC", "20"))
PAGE_TIMEOUT_SEC = float(os.getenv("PAGE_TIMEOUT_SEC", "5"))
# 准入控制：同时在处理中的请求上限；超出的排队，排太久直接拒绝
MAX_INFLIGHT = int(os.getenv("MAX_INFLIGHT", "6"))
QUEUE_WAIT_SEC = int(os.getenv("QUEUE_WAIT_SEC", "20"))
IP_TITLE_MAX = int(os.getenv("IP_TITLE_MAX", "1024"))
IP_TITLE_TTL_SEC = int(os.getenv("IP_TITLE_TTL_SEC", "1800"))
WORKER_PATH = Path(__file__).with_name("pdf_worker.py")
worker_semaphore = asyncio.Semaphore(1)

# worker 自报的错误码 -> HTTP 状态码
WORKER_ERROR_STATUS = {
    "too_many_pages": 422,
    "too_much_text": 422,
    "too_many_splits": 422,
    "too_many_bad_pages": 422,
    "memory": 413,
    "deadline": 504,
    "bad_mode": 500,
    "internal": 500,
}


def safe_basename(filename: str) -> str:
    """只取文件名部分；拒绝空名、`.`、`..` 和过长名字。"""
    try:
        name = Path(filename or "").name
    except (TypeError, ValueError):
        name = ""
    if not name or name in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid PDF filename")
    if len(name.encode("utf-8", "replace")) > 200:
        raise HTTPException(status_code=400, detail="PDF filename is too long")
    return name


def new_upload_path(filename: str) -> tuple[Path, str]:
    """每次请求一个唯一文件名：同名并发不会再互相覆盖/删除。

    返回 (落盘路径, 用于展示/记录的原始文件名)。
    """
    display = safe_basename(filename)
    return Path("uploads") / f"{uuid.uuid4().hex}.pdf", display


@asynccontextmanager
async def worker_slot():
    """准入控制 + 排队上限 + 单 worker 串行。

    名额在“落盘之前”就要占到，否则排队请求会把 50MB 上传文件、
    内存里的 SpooledTemporaryFile 和连接一起挂着。
    """
    if SERVICE_STATE["inflight"] >= MAX_INFLIGHT:
        raise HTTPException(
            status_code=429,
            detail=f"server busy ({SERVICE_STATE['inflight']} requests in flight), retry later",
        )
    SERVICE_STATE["inflight"] += 1
    try:
        SERVICE_STATE["queued"] += 1
        try:
            await asyncio.wait_for(worker_semaphore.acquire(), timeout=QUEUE_WAIT_SEC)
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=503,
                detail=f"queue wait exceeded {QUEUE_WAIT_SEC}s, retry later",
            )
        finally:
            SERVICE_STATE["queued"] -= 1
        try:
            yield
        finally:
            worker_semaphore.release()
    finally:
        SERVICE_STATE["inflight"] -= 1


def remember_title(ip: str, name: str) -> None:
    """ip -> 上传文件名 的映射，带上限和 TTL（不再无限增长）。"""
    if len(ip_title_map) >= IP_TITLE_MAX and ip not in ip_title_map:
        ip_title_map.pop(next(iter(ip_title_map)), None)
    ip_title_map[ip] = (name, time.time())


def pop_title(ip: str) -> str | None:
    entry = ip_title_map.pop(ip, None)
    if not entry:
        return None
    name, ts = entry
    if time.time() - ts > IP_TITLE_TTL_SEC:
        return None
    return name


async def save_upload_limited(pdf: UploadFile, dest: Path) -> int:
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with open(dest, "wb") as buffer:
            while chunk := await pdf.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"PDF exceeds the {MAX_UPLOAD_MB} MB limit",
                    )
                buffer.write(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    if total == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Empty PDF file")
    return total


def _parse_worker_report(stderr: bytes) -> tuple[str, dict]:
    """worker 最后一行 stderr 是 JSON 报告（rss_kb/skipped 等），其余是错误文本。"""
    text = stderr.decode("utf-8", "replace")
    lines = [line for line in text.splitlines() if line.strip()]
    report: dict = {}
    if lines:
        try:
            candidate = json.loads(lines[-1])
            if isinstance(candidate, dict):
                report = candidate
        except (TypeError, ValueError):
            report = {}
    tail = " | ".join(lines[-3:])[:600] if lines else ""
    return tail, report


def _worker_error(payload, stderr: bytes, returncode: int) -> tuple[str, str, dict]:
    """把 worker 的结构化错误（或被杀信号）翻译成 (code, message, extra)。"""
    tail, report = _parse_worker_report(stderr)
    err = (payload or {}).get("error") or {}
    code = err.get("code") or report.get("error") or "internal"
    message = err.get("message") or report.get("message") or tail
    if not message and returncode and returncode < 0:
        message = f"PDF worker killed by signal {-returncode}"
    if not message:
        message = "PDF worker failed"
    return str(code), str(message)[:500], err


def _read_tail(path: str, limit: int = MAX_STDERR_BYTES) -> bytes:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - limit))
            return f.read(limit)
    except OSError:
        return b""


async def run_pdf_worker(mode: str, pdf_path: Path, rid: str = "-", **kwargs) -> dict:
    timeout = PARSE_TIMEOUT_SEC
    config = {
        "mode": mode,
        "pdf_path": str(pdf_path),
        "memory_mb": WORKER_MEMORY_MB,
        "max_result_mb": MAX_RESULT_MB,
        "max_pages": MAX_PDF_PAGES,
        "cpu_seconds": timeout,
        "page_timeout_sec": PAGE_TIMEOUT_SEC,
        **kwargs,
    }
    # 调用方（worker_slot）已经持有 worker_semaphore：这里不再重复获取，
    # 否则会自己把自己锁死。
    if not worker_semaphore.locked():
        raise RuntimeError("run_pdf_worker() must be called inside worker_slot()")
    Path("uploads").mkdir(parents=True, exist_ok=True)
    fd, output_path = tempfile.mkstemp(
        prefix="pdf-worker-", suffix=".json", dir="uploads"
    )
    os.close(fd)
    err_fd, err_path = tempfile.mkstemp(
        prefix="pdf-worker-", suffix=".err", dir="uploads"
    )
    err_file = os.fdopen(err_fd, "wb")
    started = time.monotonic()
    cpu_started = resource.getrusage(resource.RUSAGE_CHILDREN)
    proc = None
    stderr = b""
    killed = "none"
    rc = None
    try:
        config["output_path"] = output_path
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(WORKER_PATH),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                # stderr 落文件，父进程只读回尾部：不再把 child 输出整体放进内存
                stderr=err_file,
                start_new_session=True,
            )
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to start PDF worker: {exc}"
            )

        try:
            await asyncio.wait_for(
                proc.communicate(json.dumps(config).encode("utf-8")),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            killed = "timeout"
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            await proc.wait()
            raise HTTPException(
                status_code=504,
                detail=f"PDF processing timed out after {timeout}s",
            )
        finally:
            err_file.flush()
            stderr = _read_tail(err_path)

        if proc.returncode != 0:
            code, message, extra = _worker_error(None, stderr, proc.returncode)
            status = WORKER_ERROR_STATUS.get(code, 422)
            logger.warning(
                "event=worker_failed rid=%s mode=%s code=%s status=%s extra=%s",
                rid,
                mode,
                code,
                status,
                extra,
            )
            raise HTTPException(status_code=status, detail=message)

        if os.path.getsize(output_path) > MAX_RESULT_MB * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Parsed result is too large")
        with open(output_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if isinstance(payload, dict) and payload.get("ok") is False:
            code, message, _ = _worker_error(payload, stderr, 0)
            status = WORKER_ERROR_STATUS.get(code, 422)
            raise HTTPException(status_code=status, detail=message)
        if isinstance(payload, dict):
            payload.pop("ok", None)
        return payload
    finally:
        wall = time.monotonic() - started
        cpu_end = resource.getrusage(resource.RUSAGE_CHILDREN)
        child_cpu = (cpu_end.ru_utime - cpu_started.ru_utime) + (
            cpu_end.ru_stime - cpu_started.ru_stime
        )
        tail, report = _parse_worker_report(stderr)
        if killed == "none" and proc is not None:
            rc = proc.returncode
            if rc is not None and rc < 0:
                killed = f"signal{-rc}"
        SERVICE_STATE["worker_runs"] += 1
        if killed != "none":
            SERVICE_STATE["worker_kills"] += 1
        SERVICE_STATE["last_worker"] = {
            "rid": rid,
            "mode": mode,
            "rc": rc,
            "killed": killed,
            "wall": round(wall, 2),
            "child_cpu": round(child_cpu, 2),
            "worker_rss_kb": report.get("rss_kb"),
            "skipped_pages": report.get("skipped_pages"),
            "at": datetime.now().isoformat(timespec="seconds"),
        }
        logger.info(
            "event=worker_exit rid=%s mode=%s rc=%s killed=%s wall=%.2f "
            "child_cpu=%.2f worker_rss_kb=%s skipped_pages=%s pdf=%s stderr_tail=%s",
            rid,
            mode,
            rc,
            killed,
            wall,
            child_cpu,
            report.get("rss_kb", "-"),
            report.get("skipped_pages", "-"),
            pdf_path.name,
            tail or "-",
        )
        for path in (output_path, f"{output_path}.tmp", err_path):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        try:
            err_file.close()
        except OSError:
            pass
@app.post("/parse_pdf")
@limiter.limit(RATE_LIMITS["parse"])
async def parse_pdf(
    request: Request,
    title: str = Form(...),
    link: str = Form(...),
    chunk_size: int = Form(...),
    chunk_overlap: int = Form(...),
    pdf: UploadFile = UploadFile(...),
):
    """上传并解析 PDF，返回文本片段"""
    rid = getattr(request.state, "rid", "-")
    logger.info(
        "event=parse_start rid=%s title=%r link=%r chunk_size=%s chunk_overlap=%s",
        rid,
        title,
        link,
        chunk_size,
        chunk_overlap,
    )
    if not 100 <= chunk_size <= MAX_CHUNK_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"chunk_size must be between 100 and {MAX_CHUNK_SIZE}",
        )
    if not 0 <= chunk_overlap <= chunk_size // 2:
        raise HTTPException(
            status_code=400,
            detail="chunk_overlap must be between 0 and half of chunk_size",
        )

    # 先抢到执行名额再落盘：排队请求不再占磁盘/内存/连接
    async with worker_slot():
        pdf_path, display_name = new_upload_path(pdf.filename)
        await save_upload_limited(pdf, pdf_path)
        logger.info(
            "event=parse_saved rid=%s file=%s display=%r", rid, pdf_path.name, display_name
        )
        # 记录 IP 和标题（带上限与 TTL）
        remember_title(get_remote_address(request), display_name)
        try:
            # 在独立子进程中解析 PDF，限制内存并设置超时
            return await run_pdf_worker(
                "parse",
                pdf_path,
                rid=rid,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                max_text_chars=MAX_PDF_TEXT_CHARS,
                max_splits=MAX_SPLITS,
            )
        finally:
            pdf_path.unlink(missing_ok=True)


def post_process_summary(summary: str) -> str:
    """去掉模型常见的 ``` 包裹和开头的 "Summary:" 标题，保留全部内容。"""
    text = (summary or "").strip()
    if not text:
        return ""
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
        while lines and not lines[-1].startswith("```"):
            lines.pop()
        if lines:
            lines = lines[:-1]
    while lines and not lines[0].strip():
        lines = lines[1:]
    if lines and ("summary" in lines[0].lower() or "overview" in lines[0].lower()):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
    return "\n".join(lines).strip()


@app.post("/md_to_html")
@limiter.limit(RATE_LIMITS["html"])
async def convert_md_to_html(
    request: Request,
    markdown: str = Form(...),
    model_name: str = Form(...),
    header_template: str = Form(default="<h2>AI Generated Summary ({{modelName}})</h2>"),
):
    """将 Markdown 转换为 HTML"""
    rid = getattr(request.state, "rid", "-")
    if not markdown.strip():
        raise HTTPException(status_code=400, detail="markdown is empty")
    try:
        summary = post_process_summary(markdown)
        md = MarkdownIt()
        html = md.render(summary)
        header_html = header_template.replace("{{modelName}}", model_name)
        html = header_html + html
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("event=md_to_html_error rid=%s", rid)
        raise HTTPException(status_code=500, detail=str(e))

    # 把之前 /parse_pdf 记住的文件名和本次 markdown 对应起来（带 TTL，不会泄漏）
    title = pop_title(get_remote_address(request))
    if title:
        try:
            os.makedirs("summary", exist_ok=True)
            with open(Path("summary") / f"{title}.md", "w", encoding="utf-8") as f:
                f.write(markdown)
        except OSError:
            logger.exception("event=summary_write_error rid=%s file=%r", rid, title)

    return {"html": html}

@app.get("/healthz")
async def healthz():
    """探活/自检：不读数据库、不限流、不阻塞，供监控与 systemd watchdog 使用。"""
    return {
        "status": "ok",
        "uptime_s": int(time.time() - SERVICE_STATE["started_at"]),
        "inflight": SERVICE_STATE["inflight"],
        "queued": SERVICE_STATE["queued"],
        "worker_runs": SERVICE_STATE["worker_runs"],
        "worker_kills": SERVICE_STATE["worker_kills"],
        "last_worker": SERVICE_STATE["last_worker"],
    }


@app.get("/test_ip")
@limiter.limit(RATE_LIMITS["test"])
async def test_ip(request: Request):
    """测试客户端 IP 地址和限流"""
    client_ip = get_remote_address(request)
    forwarded_for = request.headers.get("X-Forwarded-For")
    real_ip = request.headers.get("X-Real-IP")

    return {
        "client_ip": client_ip,
        "x_forwarded_for": forwarded_for,
        "x_real_ip": real_ip,
    }


if __name__ == "__main__":
    print(f"============================== {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ==============================")

    # 设置日志配置（结构化：key=value，方便 grep / 统计）
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout)  # 将日志输出到 stdout
        ],
    )

    parser = argparse.ArgumentParser()
    parser.add_argument("--unix-socket", help="Unix socket path")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=13210, help="Bind port")
    args = parser.parse_args()

    if args.unix_socket:
        # Unix socket模式
        uvicorn.run(app, uds=args.unix_socket, log_config=None)
    else:
        # TCP模式
        uvicorn.run(app, host=args.host, port=args.port, log_config=None)
