# -*- coding: utf-8 -*-
"""Optional local CLI: summarize a PDF with an OpenAI-compatible endpoint.

The server no longer calls this module (it only parses PDFs); the Zotero client
does the summarisation. Kept for command-line use:

    python paper_summary.py <URI-encoded title> <URI-encoded pdf_path>

Prompts live in prompts/{stuff,map,reduce}_prompt.txt.
"""
import json
import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import unquote
from markdown_it import MarkdownIt
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain.chains.summarize import load_summarize_chain
from langchain_core.prompts import PromptTemplate
from langchain_core.documents import Document
from langchain_openai import ChatOpenAI

HERE = Path(__file__).resolve().parent
PROMPTS_DIR = HERE / "prompts"
logger = logging.getLogger("paper_summary")


class PaperSummarizer:
    def __init__(self):
        # Read the API key from the configuration file
        api_key_path = os.path.expanduser("~/.config/llm.json")
        try:
            with open(api_key_path, "r", encoding="utf-8") as f:
                api_json = json.load(f)
                api_key = api_json["api_key"]
                context_len = api_json["context_length"]
                self.model_name = api_json["name"]
                self.base_url = api_json["base_url"]
                self.model = api_json["model"]
                self.llm = ChatOpenAI(
                    api_key=api_key,
                    base_url=self.base_url,
                    model=self.model,
                    temperature=0.3,
                )
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Can't find the API key file: {api_key_path}. Please create the file and add your API key."
            )
        except Exception as e:
            raise Exception(f"Error reading API key: {str(e)}")

        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=context_len, chunk_overlap=1000, length_function=len
        )

        with open(
            PROMPTS_DIR / "stuff_prompt.txt",
            "r",
            encoding="utf-8",
        ) as f:
            stuff_prompt_template = f.read()
            stuff_prompt = PromptTemplate(
                template=stuff_prompt_template, input_variables=["text", "title"]
            )

        with open(
            PROMPTS_DIR / "reduce_prompt.txt",
            "r",
            encoding="utf-8",
        ) as f:
            reduce_prompt_template = f.read()
            reduce_prompt = PromptTemplate(
                template=reduce_prompt_template, input_variables=["text", "title"]
            )

        with open(
            PROMPTS_DIR / "map_prompt.txt",
            "r",
            encoding="utf-8",
        ) as f:
            map_prompt_template = f.read()
            map_prompt = PromptTemplate(
                template=map_prompt_template, input_variables=["text", "title"]
            )

        # Create summarize chain
        self.stuff_chain = load_summarize_chain(
            llm=self.llm,
            chain_type="stuff",
            prompt=stuff_prompt,
        )

        self.mapreduce_chain = load_summarize_chain(
            llm=self.llm,
            chain_type="map_reduce",
            map_prompt=map_prompt,
            combine_prompt=reduce_prompt,
        )

    def post_process_summary(self, summary: str) -> str:
        """去掉模型常见的 ``` 包裹和开头的 "Summary:" 标题，保留全部内容。

        与 server.py 的 post_process_summary 保持一致（服务端不 import 本模块）。
        """
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

    def summarize_text(self, title: str, full_text: str) -> str:
        """Summarize already-extracted text (text extraction is the worker's job)."""
        safe_title = Path(title).name or "untitled"
        doc = Document(page_content=full_text)
        splits = self.text_splitter.split_documents([doc])
        print(f"Total characters: {len(full_text)}")
        print(f"Number of splits: {len(splits)}")
        summary_path = Path(os.path.dirname(__file__), f"summary/{safe_title}.html")
        summary_path.parent.mkdir(parents=True, exist_ok=True)

        if summary_path.exists():
            print(f"Found existing summary: {summary_path}")
            with open(summary_path, "r", encoding="utf-8") as f:
                return f.read()

        start_time = time.time()
        chain = self.mapreduce_chain if len(splits) > 1 else self.stuff_chain
        summary = chain.invoke(
            {"input_documents": splits, "title": safe_title},
            return_only_outputs=True,
        )
        end_time = time.time()
        print(
            f"Summary time: {end_time - start_time} seconds, input length: {len(full_text)}, "
            f"output length: {len(summary['output_text'])}"
        )
        summary = self.post_process_summary(summary["output_text"])
        md_path = Path(os.path.dirname(__file__), f"summary/{safe_title}.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(summary)
        md = MarkdownIt()
        html = md.render(summary)
        html = f"<h2>AI Generated Summary ({self.model_name})</h2>" + html
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(html)
        return html

    def summarize_paper(self, title: str, pdf_path: Path) -> str:
        """Extract text with PyMuPDF (pypdf degenerates on some PDFs) and summarise."""
        import pymupdf

        print(f"Loading PDF: {pdf_path}")
        doc = pymupdf.open(str(pdf_path))
        try:
            pages = [doc.load_page(i).get_text("text") for i in range(doc.page_count)]
        finally:
            doc.close()
        print(f"PDF loaded: {len(pages)} pages")
        return self.summarize_text(title, "\n".join(pages))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    if len(sys.argv) < 3:
        print(
            "Usage: python paper_summary.py <URI-encoded title> <URI-encoded pdf_path>",
            file=sys.stderr,
        )
        sys.exit(2)

    title = unquote(sys.argv[1])
    pdf_path = Path(unquote(sys.argv[2]))
    if not pdf_path.is_file():
        print(f"No such PDF: {pdf_path}", file=sys.stderr)
        sys.exit(2)

    try:
        summarizer = PaperSummarizer()
        logger.info("summarizing %s (%s)", title, pdf_path)
        print(summarizer.summarize_paper(title, pdf_path))
    except Exception:
        logger.exception("summarization failed")
        sys.exit(1)
