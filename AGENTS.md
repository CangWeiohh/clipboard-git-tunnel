# AGENTS.md — 给 AI 协作代理的项目导读

> 本文件面向将在这个仓库上工作的 AI 代理（或任何新接手的人）。先读本文件，再读代码。README.md 是给人类用户的快速上手；本文件是给代理的完整上下文：架构、协议约束、目录职责、部署拓扑、常见坑与修改守则。

## 一句话

这是一个 **Git Smart HTTP 隧道**：在 A 端（Windows VM）提供 HTTP 代理，把 Git 请求通过 **HSR 双向剪贴板** 逐帧传到 B 端（云桌面），由 B 端转发到内网 GitLab，响应再按同样方式传回 A 端。目标是让 Git/IDEA 能直接使用 `http://<A端IP>:9999/...` 作为远程地址，而业务数据只经过剪贴板通道。

```text
Git / IDEA ──HTTP──> A proxy ──QTC1 剪贴板帧──> B forwarder ──HTTP──> 内网 GitLab
                          <──────────────────────┘
```

## 最重要的约束（改代码前必须懂）

HSR 剪贴板是 **单槽位、事件驱动、双向但一次只能承载一个内容** 的通道。由此衍生出整套协议纪律：

1. **严禁同一侧连续写剪贴板**。一次写入要等对面 ACK（stop-and-wait）或等传播窗口过去，否则后写覆盖先写、先写被静默丢弃。请求间用 `wait_write_gap()`（默认 3s）显式隔离。
2. **屏障帧不能省**：`req_commit`（请求提交）+ `resp_begin`（响应开始）双屏障保证 A/B 两端的写不会在传播窗口内互相踩踏。resp_begin 刻意 **不 ACK**（ACK 会是 B 侧第二次写，反而制造竞态）。
3. **单帧协议（2026-08-28 引入）**：请求/响应若整体（meta+body，或 meta+body+SHA-256）能装进一个 800KiB chunk，就发单个 `req_single` / `resp_single` 帧，替代原先 4 帧请求握手 / 3 帧响应序列。**修改帧流程时必须同时保证单帧与多帧两条路径都正确**（多帧是超大响应的回退）。
4. **响应终帧（`resp_single` / `resp_end`）fire-and-forget**：写完**绝不等待 ACK**。客户端自带 SHA-256 校验，重发无意义；等 ACK 会让 B 端陷入 retries×ack_timeout 的忙窗口（5×5s=25s），期间 A 端新请求全部 504（真实事故，commit e210625）。
5. **HSR 剪贴板同步依赖 HSRClient 窗口在前台**。A 端每次写前调用 `focus.before_clipboard_write()` 尝试激活窗口；若持续 `focus.foreground_failed`，剪贴板写入不会传播到 B——这不是协议 bug，是部署/使用问题（保持 A 端 VM 窗口前台）。
6. **日志永不记录**：剪贴板正文、HTTP body、Authorization、Cookie、URL userinfo。路径用 `safe_http_path()` 清洗。DEBUG 只记帧元数据（kind/seq/total/payload 字节数）。

## 目录结构

| 路径 | 职责 |
|---|---|
| `a_end/a_proxy.py` | A 端入口：`ThreadingHTTPServer` 监听 9999，把每个 HTTP 请求经剪贴板隧道转发（`tunnel.lock` 串行化并发），`ClipboardGitClient` 发送请求/接收响应 |
| `b_end/b_tunnel.py` | B 端入口：单线程 `serve_one` 循环，等待 `req_meta`/`req_single`，转发到内网 Git，回传响应 |
| `qtc_tunnel/protocol.py` | 帧定义（`Frame`）、`QTC1:<base64(JSON)>` 线格式、`DEFAULT_CHUNK_BYTES=800KiB`、`MAX_CLIPBOARD_CHARS=1_500_000` |
| `qtc_tunnel/clipboard.py` | `WindowsClipboard`（CF_UNICODETEXT，OpenClipboard 15s 重试）、`ClipboardEndpoint`（轮询、write_gap、send_and_wait_ack）、`MemoryClipboard`（测试用） |
| `qtc_tunnel/git_transport.py` | 核心协议实现：`ClipboardGitClient.request()`、`ClipboardGitServer.serve_one()`、单帧 pack/unpack、多帧分块 |
| `qtc_tunnel/config.py` | 零依赖 YAML 子集解析（无 PyYAML，A 端嵌入 Python 无 pip）、`side_defaults()` 把 `a_*`/`b_*` 前缀映射到 argparse 参数 |
| `qtc_tunnel/focus.py` | HSRClient 窗口发现 + 前台激活（Windows API） |
| `qtc_tunnel/logging_utils.py` | 北京时间 `yyyy-MM-dd HH:mm:ss` 结构化日志、滚动文件（5MiB×5）、`safe_http_path` |
| `qtc_tunnel/transfer.py` | `frame_chunks()` 分块、`reassemble()` 重组 |
| `config.yaml` | 全部可调参数集中地（`a_*`/`b_*` 前缀），优先级 CLI > config > 内置默认 |
| `start_a.bat` / `start_b.bat` | A/B 启动脚本。**必须保持 CRLF**（`.gitattributes` 已强制） |
| `tests/` | unittest（`python -m unittest discover -s tests`），MemoryClipboard 模拟单槽位 |
| `tools/clipboard_bench.py` | 剪贴板裸通道基准（可选） |

## 协议速查

帧种类：`req_meta` / `req_data` / `req_end` / `req_commit` / `resp_begin`（不 ACK）/ `resp_meta` / `resp_data` / `resp_end` / `resp_single` / `req_single` / `ack` / `error`。

单帧模式帧序列（小请求）：
```text
A ─req_single→ B   （meta+body 打包：[4B 大端 meta 长][meta JSON][body]）
B ─ack→ A
A ─resp_begin→ B   （无 ACK）
B ─resp_single→ A  （meta+body+SHA-256 打包，写完不等 ACK = fire-and-forget）
A ─ack→ B
```

多帧模式（大请求/大响应回退）见 README「协议概览」的完整序列。帧校验：每帧 payload 有 `sha256`，整段响应在 `resp_end.meta.sha256` 复核。

## 运行与测试

```bash
# 本地模拟闭环（无需 Windows/网络 Git；所有逻辑可用 MemoryClipboard 测）
python -m unittest discover -s tests -v
```

启动（真实环境，Windows）：

```text
start_b.bat   # 先 B 后 A；B=云桌面，转发 192.168.21.14:8888
start_a.bat   # A=Windows VM，监听 0.0.0.0:9999
```

等价手动：`python b_end/b_tunnel.py --config config.yaml`、`python a_end/a_proxy.py --config config.yaml`。Git remote 示例：`http://<user>:<pass>@127.0.0.1:9999/<group>/<repo>.git`。

## 部署拓扑

- **A 端**：Windows VM，`C:\Python311`（embeddable，无 pip → 代码零第三方依赖，`sys.path.insert` 定位包），目录 `C:\Users\cangwei\Desktop\clipboard-git-tunnel`
- **B 端**：云桌面，`python` 3.11.9，目录 `C:\Users\wangchu2\Desktop\clipboard-git-tunnel`
- **部署必须整目录替换**（zip 解压覆盖），绝不单文件替换——两端版本不一致（混版）会因不认新帧种类而 504/502（真实教训）
- 启动顺序：先 B 后 A；部署后需保持 A 端 VM/HSRClient 窗口在前台

## 常见坑（按事故历史）

1. 504 五轮排查：焦点丢失（第一轮）、req_commit/resp_meta 屏障缺失（第二轮）、跨请求 write_gap 缺失（第三轮）、OpenClipboard 窗口太短（第四轮）、A/B 混版（第五轮）、HSRClient 窗口失焦导致剪贴板同步整体跳过（第六轮，`req_single` 5 次重写 B 端零接收）。第七轮（resp_begin 空等）：A 端 `http.request.begin` 后日志静止 100s+、剪贴板持续新增同类 QTC1 内容、B 端零日志——根因是传输层全是 DEBUG 日志无法定位 + resp_begin 首写违反同侧写间隔被 HSR 静默丢弃 + B 端屏障无上限饿死新请求。
2. bat 脚本：LF-only 会让多行块解析错乱 → `.gitattributes` 强制 CRLF；`setlocal EnableDelayedExpansion` 后用 `!VAR!` 而非 `%VAR%`（括号块内 `%VAR%` 只在解析时展开一次）。
3. config 值取自 `findstr /b` + 子串裁剪（bat 不适合复杂解析）；python 路径带引号需 `"=!VAR:"=!"` 去引号。
4. 改协议前先想清：新帧是否会与同侧相邻写碰撞？终帧是否误等 ACK？单帧/多帧两条路径都要测。
5. **B 端会被剪贴板遗留的旧请求卡住**：B 启动时把当前剪贴板当接收基线（不重放上次运行的帧）；请求打包带 `created_at`，B 丢弃在剪贴板滞留超过 `STALE_REQUEST_AFTER_SECONDS`（60s）的请求（`http.request.stale_discarded`），不写 error 帧避免占用单槽。
6. **上游缺 Content-Length 的响应会挂死 B**：B 转发时强制 `Connection: close`，否则 HTTP/1.1 服务器返回无长度 401 时 `response.read()` 会一直阻塞 B 的 serve_one。
7. **不匹配当前等待条件的帧会先暂存**：`ClipboardEndpoint.wait_frame` 把无关帧放进有界队列，后续匹配的等待可直接取回，避免新请求在响应等待期被吞掉。
8. **上游读取必须有界**：`_forward` 分阶段超时并诊断日志（`upstream.request.begin` → `upstream.request.sent` → `upstream.response.headers` → `upstream.response.complete`）。连接/响应头由 `--upstream-header-timeout`（默认 30s）限制；响应体由 `--upstream-idle-timeout`（默认 2s）限制。响应既无 `Content-Length` 又非 chunked 时，空闲即按 EOF 收尾（`upstream.response.idle_boundary`）；有明确边界时超时是截断错误，绝不静默返回半包。改上游读取必须同时覆盖这几种边界（HEAD/204、chunked、keep-alive 401、截断）。
9. **resp_begin 首写也在同侧传播窗口内**：A 收到 req_single 的 ACK 后立刻写 resp_begin，距上一次 A 侧写只有约一个 ACK 往返（≈2s，小于 write_gap=3s）——HSR 会静默丢弃该 marker，B 空等 resp_begin。修复：resp_begin 循环内每次写前都调 `wait_write_gap()`，保证任何 A 侧写之间 ≥3s。
10. **传输层日志必须是 INFO 级**：`frame.receive`/`frame.unmatched`/`clipboard.write` 全部提高到 INFO（旧版是 DEBUG，INFO 下 A/B 两端对剪贴板活动完全隐形，故障时双方日志都静止）。写帧带 `kind/session/seq/total/retry/payload_bytes`，收到任何不匹配帧打 WARNING（`frame.unmatched`）并暂存。
11. **B 端也要 HSR 窗口焦点**：A 端有 `WindowsHSRFocus`，云桌面 B 端同样需要——HSR 渲染窗口不是前台时同步会整体跳过。b_tunnel 现在也接 focus（`b_window_keywords` 配置，默认进程名自动发现）。
12. **B 端屏障有时间上限**：`_wait_barrier` 上限 `MIN_BARRIER_TIMEOUT_SECONDS=60s`（A 每 5s 重发一次 marker，60s 覆盖十余次重发）。超时抛 `BarrierTimeout`，serve_one 放弃该会话返回 False，不让死会话占住 300s 把新请求饿死。

## 修改守则

- **加功能**：先在 `tests/test_protocol.py` 里用 MemoryClipboard 写模拟闭环（任何传输逻辑都能本地测），再考虑真实环境。
- **改配置**：同时改 `config.yaml`、`config.py`（若无新增键则不需要）、入口 argparse 默认值、测试夹具中的显式值。
- **改日志**：事件名用点分小写（`http.request.begin`），时间戳北京时间，绝不记正文/凭据。
- **改 .bat**：改完用 `perl -pi -e 's/\r?\n/\r\n/g'` 转 CRLF，或在 git 里确认不会变 LF（`.gitattributes` 兜底）。
- **提交信息**：遵循仓库现有风格（`fix:`/`feat:`/`perf:`/`chore:` 前缀 + 一句中文/英文正文）。