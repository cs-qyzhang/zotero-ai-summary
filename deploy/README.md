# 部署与运维（加固版）

## 这套改动解决了什么

| 问题 | 之前 | 现在 |
|---|---|---|
| pypdf 在部分 PDF 上退化（单页 17–33s、790MB） | 整个服务卡死/整机 OOM | 换 PyMuPDF：同文档 11 页 2s / 96MB |
| 内存无上限 | 8 亿字节级增长 | 逐页提取 + 累计字符预算 + 单页超时 + `RLIMIT_AS` 兜底 |
| 一个坏 PDF 拖垮所有请求 | 串行排队、无限等待 | 准入控制 `MAX_INFLIGHT` + 排队超时 + 先占名额再落盘 |
| 同名文件并发 | 互相覆盖/删除 → 422 | 每次请求唯一文件名 |
| 限流可被 `X-Forwarded-For` 绕过 | 5/5 通过 | 只信任 127.0.0.1，取最后一个 XFF |
| `post_process_summary` 丢最后一行 | 单行总结变空 | 保留全部内容；空输入 400 |
| 无探活、卡死不自愈 | `WatchdogUSec=0` | `/healthz` + `sd_notify` watchdog（可选） |
| OOM 会停掉整个服务 | `OOMPolicy=stop` | `OOMPolicy=continue`（只死 worker） |

回归数据见 `deploy/REGRESSION-BASELINE.md`（改造前）和 `deploy/REGRESSION-AFTER.md`（改造后）。

## 应用宿主机配置（需要 sudo）

```bash
cd <repo>
./deploy/apply.sh --dry-run     # 只渲染/打印，不写文件
./deploy/apply.sh               # 资源限制 + nginx + logrotate + 每日重启 timer
./deploy/apply.sh --watchdog    # 额外开启 systemd watchdog（Type=notify, WatchdogSec=120）
```

`apply.sh` 会：

1. 把 `deploy/` 下的模板渲染成实际配置（占位符见下表），
2. 备份现有配置到 `deploy/backup/<时间戳>/`（`.gitignore` 忽略，只留在本机），
3. 安装 systemd unit（已存在则**保留**并只显示 diff）、drop-in、每日重启 service+timer、nginx 站点、logrotate，
4. `daemon-reload` / `nginx -t` / reload / `restart`，最后打印实际生效的 `OOMPolicy/MemoryMax` 与 `/healthz` 状态。

### 可覆盖的渲染参数

| 变量 | 默认 | 说明 |
|---|---|---|
| `APP_DIR` | 仓库所在目录 | 日志路径、WorkingDirectory、logrotate 路径 |
| `APP_USER` / `APP_GROUP` | `$SUDO_USER` / 其主组 | systemd 的 `User=/Group=` |
| `PYTHON_BIN` | 已安装 unit 的 `ExecStart` 解释器，否则 `python3` | 必须是装了本仓库依赖的解释器 |
| `APP_PORT` | `13210` | server 监听端口 + nginx `proxy_pass` |
| `SERVER_NAME` | 已安装 nginx 站点里的 `server_name`，否则 `_` | 首次部署时请显式指定你的域名 |
| `REPO_URL` | 本仓库地址 | nginx `location = /` 的跳转目标 |
| `UNIT` | `paper-summarizer` | unit 名前缀 |
| `INSTALL_UNIT` | `auto` | `auto`=缺失才装，`always`=覆盖，`never`=不碰 |

示例（首次在别的机器部署）：

```bash
APP_USER=www-data SERVER_NAME=paper.example.com \
PYTHON_BIN=/opt/venv/bin/python ./deploy/apply.sh
sudo systemctl enable --now paper-summarizer
```

### 回滚

```bash
# 1) 宿主配置：用 apply.sh 自动备份的快照
sudo cp deploy/backup/<时间戳>/paper-summarizer.service   /etc/systemd/system/
sudo cp deploy/backup/<时间戳>/10-resource-limits.conf    /etc/systemd/system/paper-summarizer.service.d/
sudo cp deploy/backup/<时间戳>/paper-summarizer-restart.*  /etc/systemd/system/
sudo cp deploy/backup/<时间戳>/paper-summarizer          /etc/nginx/sites-enabled/paper-summarizer
sudo rm -f /etc/systemd/system/paper-summarizer.service.d/20-watchdog.conf
sudo systemctl daemon-reload && sudo nginx -t && sudo systemctl reload nginx
sudo systemctl restart paper-summarizer

# 2) 代码
git checkout <old-tag-or-commit>
```

## 环境变量（都有安全默认值）

| 变量 | 默认 | 说明 |
|---|---|---|
| `MAX_UPLOAD_MB` | 50 | 单次上传上限（nginx 也设为 50M） |
| `MAX_PDF_PAGES` | 300 | 页数上限（在解析前检查） |
| `MAX_PDF_TEXT_CHARS` | 5000000 | **累计**可提取字符上限，提取中途就会中止 |
| `MAX_SPLITS` | 20000 | 分片数上限（估算） |
| `WORKER_MEMORY_MB` | 384 | worker 的 `RLIMIT_AS`，必须小于 `MemoryMax` 预算 |
| `MAX_RESULT_MB` | 16 | worker 结果文件上限 + `RLIMIT_FSIZE` |
| `PARSE_TIMEOUT_SEC` | 20 | parse 的墙钟上限（worker 内部会比它早 3s 主动退出） |
| `PAGE_TIMEOUT_SEC` | 5 | 单页提取超时，超时的页会被跳过而不是整篇失败 |
| `MAX_INFLIGHT` | 6 | 准入控制：超过直接 429（连请求体都不读） |
| `QUEUE_WAIT_SEC` | 20 | 排队等待上限，超时 503 |
| `PARSE_RATE_LIMIT` | 6/minute | `/parse_pdf` |
| `HTML_RATE_LIMIT` | 6/minute | `/md_to_html` |
| `TEST_RATE_LIMIT` | 3/minute | `/test_ip` |
| `IP_TITLE_MAX` / `IP_TITLE_TTL_SEC` | 1024 / 1800 | `ip -> 文件名` 映射的上限与 TTL |

### 日志轮转的两个坑（都必须靠 `su root root` 同时满足）

`/etc/logrotate.d/paper-summarizer` 里必须是 `su root root`，原因是两个约束互相夹：

| 现象 | 原因 |
|---|---|
| `error: skipping ... because parent directory has insecure permissions` | 项目目录对组可写、且组不是 root（如 `drwxrwxr-x <user> <group>`）。**不带 `su`** 时 logrotate 拒绝操作 |
| `error: error opening .../server.log: Permission denied` | `server.log` / `server.err.log` 是 systemd（`StandardOutput=append:`）以 root 创建的 `root:root 0644`。**`su qyzhang qyzhang`** 降权后写不动 |

所以让 logrotate 保持 root：`su root root`（既满足安全检查，又有权限截断 root 的日志文件）。

```bash
sudo install -m 644 deploy/logrotate-paper-summarizer /etc/logrotate.d/paper-summarizer
sudo logrotate -d /etc/logrotate.d/paper-summarizer   # dry-run
sudo logrotate -f /etc/logrotate.d/paper-summarizer   # 真跑
ls -la server.log*                                    # server.log 归零，server.log.1.gz 已是压缩包
```

服务进程用 `O_APPEND` 打开日志（fd flags `0102001`），`copytruncate` 截断后下一次写从 0 开始，不会留下空洞。

替代方案（不想动 logrotate 配置时）：把目录的组写权限去掉 `chmod g-w <repo>`（后者让目录变成 755，安全检查自然通过），但不推荐——目录权限将来可能被别的工具改回来。

## 观测

```bash
curl -s localhost:13210/healthz | jq .
# {"status":"ok","inflight":0,"queued":0,"worker_runs":12,"worker_kills":1,"last_worker":{...}}

grep -E "worker_exit|worker_failed|reject_busy|request_error" server.log | tail
# event=worker_exit rid=.. mode=parse rc=0 killed=none wall=3.12 child_cpu=3.05 worker_rss_kb=95384 skipped_pages=[] pdf=..pdf
```

杀掉 worker 的三种情况都在日志里可区分：`killed=timeout`（墙钟）、`rc=-24`（`SIGXCPU`，CPU 预算）、
`error=memory`（内存预算）。

## 测试与巡检脚本

```bash
./scripts/smoke.sh <pdf> [base_url]        # 单次冒烟：/healthz + parse_pdf + md_to_html
./scripts/regress.sh [base_url] [pid]      # 全量回归（需要隔离的测试实例）

# 起一个隔离的测试实例（不要打线上）
cd /tmp/test-server && cp <repo>/{server.py,pdf_worker.py,paper_summary.py,prompts/*.txt} . \
  && mkdir -p prompts && mv *.txt prompts/ \
  && PARSE_RATE_LIMIT=100/minute python server.py --port 13299
```

回归用例：10 页正常 PDF、客户端默认参数（`chunkSize=64000`）、100 页×3MB 文本炸弹、
真实论文（已知病态页）、300 页 / 301 页、5 并发同名文件、XFF 伪造。

## 可选：健康检查定时器（不用 watchdog 的替代方案）

```
# /etc/systemd/system/paper-summarizer-healthcheck.service
[Unit]
Description=paper-summarizer liveness probe
[Service]
Type=oneshot
ExecStart=/path/to/zotero-ai-summary/scripts/healthcheck.sh
# /etc/systemd/system/paper-summarizer-healthcheck.timer
[Unit]
Description=Run paper-summarizer liveness probe
[Timer]
OnBootSec=5min
OnUnitActiveSec=2min
[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now paper-summarizer-healthcheck.timer
```
