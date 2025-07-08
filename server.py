import os
import sys
import hashlib
import json
import argparse
import logging
from pathlib import Path
from fastapi import FastAPI, UploadFile, Form, HTTPException, Request
from datetime import datetime
from paper_summary import PaperSummarizer
import sqlite3
import uvicorn
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.schema import Document
from langchain_community.document_loaders import PyPDFLoader
from markdown_it import MarkdownIt
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

# 初始化限制器
limiter = Limiter(key_func=get_remote_address)

# 初始化 FastAPI 服务器
app = FastAPI()

# 添加中间件支持代理头信息
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=["*"])
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])  # 生产环境建议配置具体域名

# 添加限制器异常处理
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# 配置数据库缓存
DB_PATH = "summary_cache.db"
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()
try:
    cursor.execute(
        """
    CREATE TABLE IF NOT EXISTS summaries (
        title TEXT,
        link TEXT,
        pdf_hash TEXT PRIMARY KEY,
        summary TEXT,
        model_name TEXT
    )
    """
    )
    conn.commit()
except sqlite3.Error as e:
    print(f"Database error: {e}")
except Exception as e:
    print(f"Unexpected error: {e}")


def compute_pdf_hash(file_path: Path) -> str:
    """计算 PDF 文件的哈希值"""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    return hasher.hexdigest()


def get_cached_summary(title: str, link: str, pdf_hash: str):
    """检查是否已有缓存的论文总结，优先匹配 pdf_hash，其次匹配 link，最后匹配 title"""
    try:
        cursor.execute(
            """
            SELECT summary, model_name FROM summaries
            WHERE pdf_hash = ?
        """,
            (pdf_hash,),
        )
        result = cursor.fetchone()
        if result:
            return result

        cursor.execute(
            """
            SELECT summary, model_name FROM summaries
            WHERE link = ?
        """,
            (link,),
        )
        result = cursor.fetchone()
        if result:
            return result

        cursor.execute(
            """
            SELECT summary, model_name FROM summaries
            WHERE title = ?
        """,
            (title,),
        )
        return cursor.fetchone()
    except sqlite3.Error as e:
        print(f"Database query error: {e}")
        return None


def cache_summary(title: str, link: str, pdf_hash: str, summary: str, model_name: str):
    """缓存论文总结"""
    try:
        cursor.execute(
            """
            INSERT INTO summaries (title, link, pdf_hash, summary, model_name)
            VALUES (?, ?, ?, ?, ?)
        """,
            (title, link, pdf_hash, summary, model_name),
        )
        conn.commit()
    except sqlite3.Error as e:
        print(f"Database insert error: {e}")
    except Exception as e:
        print(f"Unexpected error while caching: {e}")


summarizer = PaperSummarizer()


@app.post("/upload")
@limiter.limit("3/minute")  # 限制每个IP每分钟最多3次请求
async def upload_paper(
    request: Request,
    title: str = Form(...),
    link: str = Form(...),
    secret: str = Form(...),
    pdf: UploadFile = UploadFile(...),
):
    """上传论文 PDF 并返回总结"""
    print(f'------------- /upload ({datetime.now().strftime("%Y-%m-%d %H:%M:%S")}) -------------')
    real_secret = os.getenv("SECRET_KEY")
    if secret != real_secret:
        raise HTTPException(status_code=403, detail="Invalid secret key")

    pdf_path = Path(f"uploads/{pdf.filename}")
    os.makedirs(pdf_path.parent, exist_ok=True)
    data = await pdf.read()
    print(f"New request: title={title}, link={link}, pdf.filename={pdf.filename}")
    with open(pdf_path, "wb") as buffer:
        buffer.write(data)

    # 计算 PDF 哈希值
    pdf_hash = compute_pdf_hash(pdf_path)

    # 查询缓存
    cached = get_cached_summary(title, link, pdf_hash)
    if cached:
        summary, model_name = cached
        print("Already cached!")
        return {"summary": summary, "model_name": model_name, "cached": True}

    # 生成论文总结
    try:
        print("Start summary...")
        summary = summarizer.summarize_paper(title, pdf_path)
        model_name = summarizer.model_name
        cache_summary(title, link, pdf_hash, summary, model_name)
        return {"summary": summary, "model_name": model_name, "cached": False}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/parse_pdf")
@limiter.limit("6/minute")  # 限制每个IP每分钟最多5次请求
async def parse_pdf(
    request: Request,
    title: str = Form(...),
    link: str = Form(...),
    chunk_size: int = Form(...),
    chunk_overlap: int = Form(...),
    pdf: UploadFile = UploadFile(...),
):
    """上传并解析 PDF，返回文本片段"""
    print(f'------------- /parse_pdf ({datetime.now().strftime("%Y-%m-%d %H:%M:%S")}) -------------')
    pdf_path = Path(f"uploads/{pdf.filename}")
    os.makedirs(pdf_path.parent, exist_ok=True)
    data = await pdf.read()
    print(f"New parse request: title={title}, link={link}, pdf.filename={pdf.filename}")
    with open(pdf_path, "wb") as buffer:
        buffer.write(data)

    try:
        # 使用 PyPDFLoader 解析 PDF
        loader = PyPDFLoader(pdf_path)
        pages = loader.load()
        full_text = "\n".join(page.page_content for page in pages)
        doc = Document(page_content=full_text)

        # 使用文本分割器切分文本
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size, chunk_overlap=chunk_overlap, length_function=len
        )
        splits = text_splitter.split_documents([doc])

        # 将分割后的文本转换为可序列化的格式
        splits_data = [
            {"content": split.page_content, "metadata": split.metadata}
            for split in splits
        ]

        return {
            "splits": splits_data,
            "total_pages": len(pages),
            "total_chars": len(full_text),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def post_process_summary(summary: str) -> str:
    """Post-process the summary"""
    lines = summary.strip().splitlines()
    if lines[0].startswith("```"):
        lines = lines[1:]
        while lines[0] == "":
            lines = lines[1:]
        while not lines[-1].startswith("```"):
            lines = lines[:-1]
        lines = lines[:-1]
    if "summary" in lines[0].lower() or "overview" in lines[0].lower():
        lines = lines[1:]
        while lines[0] == "":
            lines = lines[1:]
    summary = "\n".join(lines[:-1]).strip()
    return summary


@app.post("/md_to_html")
@limiter.limit("6/minute")  # 限制每个IP每分钟最多10次请求
async def convert_md_to_html(
    request: Request,
    markdown: str = Form(...),
    model_name: str = Form(...),
    header_template: str = Form(default="<h2>AI Generated Summary ({{modelName}})</h2>"),
):
    print(f'------------- /md_to_html ({datetime.now().strftime("%Y-%m-%d %H:%M:%S")}) -------------')
    """将 Markdown 转换为 HTML"""
    try:
        summary = post_process_summary(markdown)
        md = MarkdownIt()
        html = md.render(summary)
        header_html = header_template.replace("{{modelName}}", model_name)
        html = header_html + html
        return {"html": html}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/test_ip")
@limiter.limit("3/minute")
async def test_ip(request: Request):
    """测试客户端 IP 地址和限流"""
    print(f'------------- /test_ip ({datetime.now().strftime("%Y-%m-%d %H:%M:%S")}) -------------')
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

    # 设置日志配置
    logging.basicConfig(
        level=logging.INFO,
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
