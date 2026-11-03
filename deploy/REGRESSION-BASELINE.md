# 回归基线（改造前，2026-11-03）

被测实例：`python server.py --port 13299`，`WORKER_MEMORY_MB=384`、`PARSE_TIMEOUT_SEC=15`，
其余参数同线上（`MAX_UPLOAD_MB=50`、`MAX_PDF_PAGES=100`、`MAX_PDF_TEXT_CHARS=5e6`、
`MAX_SPLITS=20000`、`MAX_RESULT_MB=64`）。机器 2C / 1.6GB。

| 用例 | HTTP | wall | 服务树峰值 RSS | worker 峰值 RSS | 结论 |
|---|---|---|---|---|---|
| `small10.pdf`（10 页 110KB，正常） | 200 | 1.75s | 133 MB | 78 MB | 正常路径基线 |
| `text_bomb.pdf`（100 页×3MB 文本，1.6MB 文件） | **504** | 15.04s | 171 MB | 116 MB | 被墙钟超时杀死；若放宽超时会持续涨内存 |
| 真实论文 Wang 2024（11 页 22MB，含病态页） | **504** | 15.18s | 323 MB | 265 MB | 正常论文无法解析 |
| 5 并发同名文件 | 1×200 2×422 2×429 | - | - | - | **422 = `FileNotFoundError: uploads/collide.pdf`**（被同伴删除） |
| XFF 伪造 5 次（模拟 nginx 追加真实 IP） | 200 200 200 429 429 | - | - | - | 解析出 `client_ip=198.51.100.7`（已修正，见下） |

补充：进程无残留，`uploads/` 无残留，服务未崩溃（`Restart` 未触发）。

## 单页病态证据（raw pypdf，无限额）

同一篇 11 页论文逐页 `extract_text()`：

```
page 0:  0.07s   rss=46MB
page 3: 17.86s   rss=436MB
page 5: 33.42s   rss=790MB
page 6: 20.00s   rss=790MB    合计 ~70s CPU / 790MB
```

## 内存限额失效方式的证据

`RLIMIT_AS=128MB` 解析同一篇论文：`wall=31.6s cpu=13.7+16.2 rss=57MB`，即内存被限住后
pypdf 不抛错退出，而是持续缺页/重试直到 `SIGXCPU`。`RLIMIT_AS=400MB` 解析 3MB 单页时
连续打印数十次 `MemoryError()` 并以 `SystemError` 结束。→ 结论：`RLIMIT_AS` 只能兜底，
不能作为主防线；必须在提取过程中做"逐页预算 + 累计字符数上限 + 换提取器"。

## 已有配置（改造前）

- `OOMPolicy=stop`、`RestartUSec=100ms`、`StartLimitBurst=5/10s`、`WatchdogUSec=0`
- `MemoryMax=700M`、`MemoryHigh=500M`、`MemorySwapMax=256M`、`CPUQuota=150%`
- nginx `client_max_body_size 100M`、`limit_req 10r/s burst=5`、`limit_conn 3`
- 无 logrotate（`server.log` 96MB / 126 万行）
