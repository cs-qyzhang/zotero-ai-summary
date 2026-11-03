# 改造后回归结果（阶段 0–3，2026-11-03）

被测实例：`python server.py --port 13299`（新代码，默认参数：`WORKER_MEMORY_MB=384`、
`PARSE_TIMEOUT_SEC=20`、`PAGE_TIMEOUT_SEC=5`、`MAX_RESULT_MB=16`、`MAX_INFLIGHT=6`、
`QUEUE_WAIT_SEC=20`），仅把 `PARSE_RATE_LIMIT` 调到 100/minute 以便一次跑完并发用例。
机器 2C / 1.6GB。

| 用例 | 改造前 | 改造后 | 说明 |
|---|---|---|---|
| `small10.pdf`（10 页正常） | 200 / 1.75s / 133MB | 200 / 1.04s / 143MB | 持平 |
| **客户端真实默认参数** `chunkSize=64000/overlap=1000` | **400**（上限写成 20000） | **200 / 1.05s** | 线上真实请求此前全部 400，已修 |
| `text_bomb.pdf`（100 页×3MB 文本） | **504 / 15.0s** | **422 / 8.0s**，`extracted text exceeds 5000000 chars at page 10/100` | 累计字符预算在提取中途生效，不再靠超时兜底 |
| 真实论文 Wang 2024（病态页） | **504 / 15.2s / 265MB** | **200 / 3.4s / 93MB** | 换 PyMuPDF 后单页 17–33s → 毫秒级 |
| 5 并发同名文件 | 1×200 2×422 2×429 | **5×200** | 唯一文件名落盘 |
| XFF 伪造（模拟 nginx 追加） | 5×200（限流失效） | 3×200 + 2×429，`client_ip=198.51.100.7` | `trusted_hosts=["127.0.0.1"]` |
| 300 页文档（`MAX_PDF_PAGES=300`） | 422（上限 100） | **200 / 1.3s / 91MB** |
| 301 页文档 | 422 | 422 `PDF has 301 pages; limit is 300` |
| 结束状态 | - | 服务存活、`uploads/` 无残留、无孤儿 worker、常驻 RSS 58MB | - |

## 单页对比（同一篇论文，逐页提取）

| | pypdf（旧） | PyMuPDF（新） |
|---|---|---|
| 全 11 页 CPU | ~70s | ~2.0s |
| 峰值 RSS | 790MB | ~96MB |

## `post_process_summary` 修正验证

| 输入 | 旧行为 | 新行为 |
|---|---|---|
| `hello` | 空字符串 | `<p>hello</p>` |
| `# Title\nhello world\nlast line` | 丢掉 `last line` | 保留 |
| 空 markdown | 500 | 400 |
| ` ```markdown\nSummary:\n- a\n- b\n``` ` | - | 正确去掉围栏和标题，保留列表 |

## 阶段 4（移除服务端总结）后的复查

- `/upload` 已删除 → 现在返回 404；`sqlite` 缓存、`SECRET_KEY`、`compute_pdf_hash` 一并移除，
  服务启动不再创建 `summary_cache.db`。
- `pdf_worker.py` 只剩 `parse` 模式；`paper_summary.py` 降级为本地 CLI（改用 PyMuPDF 提取）。
- `/md_to_html` 落盘路径验证：`filename=../../../tmp/pwn.pdf` → 只写 `summary/pwn.pdf.md`，
  不会穿越目录。
- 全部用例复跑仍然全绿（同上表）。

## 仍需宿主机操作（sudo）

- `deploy/apply.sh`：`OOMPolicy=continue` / `RestartSec=3` / `StartLimitIntervalSec=0` /
  `MemoryMax=800M` / nginx 50M + 90s 超时 / logrotate / timer `reset-failed`。
- `deploy/apply.sh --watchdog`：`Type=notify` + `WatchdogSec=120`（代码侧已用假 systemd socket 验证）。
