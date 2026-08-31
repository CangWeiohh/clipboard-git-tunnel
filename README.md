# Clipboard Git Tunnel

基于 [`qr-git-tunnel`](https://github.com/CangWeiohh/qr-git-tunnel) 的实验性独立仓库：使用 HSRClient 已打通的**双向剪贴板**，把 Git Smart HTTP 请求和响应都做成可靠分块传输。

> 当前版本是协议和本地模拟闭环的第一版，不替换现有 [`qr-git-tunnel`](https://github.com/CangWeiohh/qr-git-tunnel)，也不建议直接用于生产 Git。真实部署前必须在目标 Windows/HSR 环境完成剪贴板容量、延迟、丢帧和大请求测试。

## 给 AI 协作工具：请先读 AGENTS.md

本仓库面向 AI 代理（Cursor、Copilot、Claude 等）编写了 **[AGENTS.md](AGENTS.md)**，其中包含代理开展工作所需的完整上下文：

- 项目架构与数据流
- **关键协议约束**（HSR 单槽位 → stop-and-wait、屏障帧、单帧协议、响应终帧 fire-and-forget、窗口焦点依赖）
- 目录结构与每个文件的职责
- 协议速查（帧种类、单帧/多帧序列、SHA-256 校验）
- 运行与测试方法、部署拓扑
- 按事故历史整理的常见坑与修改守则

**使用方式**：AI 工具在本仓库上做任何分析、改代码、排查问题之前，先读取 `AGENTS.md`；人类新接手者也可先读它快速建立全貌，再按需查阅 `README.md` 其余章节与 `qtc_tunnel/` 源码。修改协议、帧流程或启动脚本时尤其要遵守其中的约束，避免重蹈 504 事故。

## 目标

- A 端继续提供 Git/IDEA 可直接使用的 HTTP 代理。
- B 端访问云桌面内网 Git 服务。
- A→B 和 B→A 都通过 HSR 双向剪贴板传输。
- 每个分块 stop-and-wait，必须收到 ACK 才发送下一块。
- 每块带 SHA-256，整段响应再次校验 SHA-256。
- 不把 Git 协议改成自定义命令，保持 Smart HTTP 兼容。
- 后续可将现有 `qr-git-tunnel` QR 通道作为 `auto` 模式的 fallback。

## 数据流

```text
Git / IDEA
   │ HTTP
   ▼
A proxy ── QTC1 clipboard frames ── B forwarder ── HTTP ──> internal Git
   ▲                                      │
   └────────────── QTC1 response frames ──┘
```

## 协议概览

剪贴板是单槽位共享状态，因此协议不是连续写入，而是严格交替：

```text
A  REQ_META  → B
A  ← ACK
A  REQ_DATA  → B
A  ← ACK
...
A  REQ_END   → B
A  ← ACK
A  REQ_COMMIT → B
A  ← ACK
A  RESP_BEGIN → B
(无 ACK；B 看到后才开始发响应)

B  RESP_META → A
B  ← ACK
B  RESP_DATA → A
B  ← ACK
...
B  RESP_END  → A
(客户端校验响应 SHA-256；响应终帧不再回写 ACK)
```

小请求/响应会自动使用 `req_single` / `resp_single` 单帧路径；大请求或大响应超过单帧容量时回退到上述多帧协议。数据帧仍严格 ACK，只有响应终帧使用 fire-and-forget，以避免无意义的 A→B 剪贴板写入干扰下一个 Git 请求。

线格式为 `QTC1:<base64(JSON)>`。JSON 字段包括：

- `v`: `qtc-clipboard-1`
- `kind`: `peer_probe`（启动就绪探针）、`req_meta`、`req_data`、`req_end`、`req_commit`、`req_single`、`resp_begin`、`resp_meta`、`resp_data`、`resp_end`、`resp_single`、`ack`、`error`
- `session`: 每次 HTTP 请求唯一 ID
- `seq` / `total`: 分块序号
- `payload`: Base64 数据
- `sha256`: 当前块校验值
- `meta`: 结束帧可携带整段数据校验值

## 本地运行模拟闭环

无需 Windows 剪贴板或网络 Git：

```bash
python -m unittest discover -s tests -v
```

测试使用 `MemoryClipboard` 模拟 HSR 单槽位，并用本地 HTTP server 模拟内网 Git。

## Windows 实验运行

### 配置文件（config.yaml）

仓库根目录的 `config.yaml` 集中管理所有可调参数，A/B 两端和启动脚本共用：

- `a_python` / `b_python`: A/B 端各自 Python 解释器绝对路径；留空 `""` 则使用 PATH 中的 `python`
- `a_*`: A 端参数（`a_listen`、`a_chunk_bytes`、`a_ack_timeout`、`a_retries`、`a_timeout`、`a_write_gap`、`a_max_request_bytes`、`a_window_keywords`、`a_log_level`、`a_log_dir`）
- `b_*`: B 端参数（`b_target`、`b_chunk_bytes`、`b_ack_timeout`、`b_retries`、`b_timeout`、`b_upstream_header_timeout`、`b_upstream_idle_timeout`、`b_log_level`、`b_log_dir`）

其中 `b_upstream_header_timeout`（默认 30s）限制内网 Git 连接/响应头等待，`b_upstream_idle_timeout`（默认 2s）限制响应体连续无数据。对于缺少 `Content-Length`/chunked 的异常响应（现场 401），收到部分正文后空闲 2s 按 EOF 收尾；有明确长度或 chunked 的响应超时会报截断，不能静默返回半包。

优先级：**命令行参数 > config.yaml > 内置默认值**。Python 入口支持 `--config <path>` 指定其他配置文件（默认自动读取仓库根的 `config.yaml`）。

### 启动脚本

仓库根目录提供启动脚本（`%~dp0` 自动定位，仓库拷到任何位置都能用；自动读取 `config.yaml` 里的 `python` 路径和各项参数）：

```text
start_a.bat   # A 端：监听 0.0.0.0:9999（Windows VM，供 Mac 访问）
start_b.bat   # B 端：转发 192.168.21.14:8888（云桌面）
```

双击即可启动；也可以在后面追加参数临时覆盖，例如 `start_a.bat --log-level DEBUG`。

**启动顺序无需人工等待**：A 启动后先用 `peer_probe → ack` 自动验证 B 进程及 HSR 双向剪贴板，只有握手成功（日志 `peer.ready`）才开放 9999 并打印 `listener.ready`。因此可以先启 B 再立即启 A，也可以先启 A（A 会持续重试等待 B/HSR，期间 Mac/IDEA 无法连入 9999，不会先收请求再 25s 504）。看到 A 的 `listener.ready` 后即可使用。

等价的手动命令：

A 端（能访问 HSRClient 窗口的机器）：

```powershell
python a_end/a_proxy.py --config config.yaml
```

B 端（云桌面）：

```powershell
python b_end/b_tunnel.py --config config.yaml
```

然后将 Git remote 临时指向：

```text
http://<user>:<password>@127.0.0.1:9999/<group>/<repo>.git
```

A 端会在每个协议帧写入剪贴板前自动发现并激活 HSRClient 窗口；这是 HSR 剪贴板同步生效的必要条件。若自动识别失败，可用 `--window-keywords "窗口标题片段"` 指定标题关键字。


## 真实环境验收顺序

1. 运行独立 clipboard benchmark，确认 B 写入能稳定回到 A。
2. 测试 1 KiB、64 KiB、256 KiB、512 KiB、1 MiB 的成功率和 P95 延迟。
3. 测试相同内容重复写入是否被 HSR 去重；每块必须带随机 session/seq。
4. 先验证 `GET /info/refs?service=git-upload-pack`。
5. 再验证浅 clone、小 fetch、大 fetch。
6. 最后验证大 push 的 A→B 请求体分块。
7. 发生超时、截断或用户剪贴板介入时，必须能明确失败，不覆盖未知剪贴板内容。
8. 稳定后再把 QRTransport 接入 `--transport auto` 降级策略。

## 安全边界

- 不记录剪贴板正文、Git body、Authorization 或 Cookie。
- `WindowsClipboard` 只使用 `CF_UNICODETEXT`，因此会有文本/编码/容量限制；它不是任意二进制剪贴板。
- B 端写剪贴板只应发生在明确的 QTC 会话中；检测到用户非 QTC 内容时应中止当前实验传输。
- 当前代码为研究基线，尚未实现用户原剪贴板恢复、断点续传、QR fallback 和自动能力探针。

## 日志规范

A 端和 B 端统一使用结构化日志，控制台与滚动文件同格式。时间戳固定为**北京时间（UTC+8）** `yyyy-MM-dd HH:mm:ss`：

```text
2026-08-28 15:28:03 | INFO | A | http.request.begin | method="GET" path="/fsdp/a.git/info/refs?service=git-upload-pack" request_bytes=0 session="abcd1234"
```

- 日志文件：项目目录 `logs/a-tunnel.log`、`logs/b-tunnel.log`（滚动文件，默认 5 MiB × 5 份）
- 端标识：`A` / `B`（benchmark 用 `BENCH`）
- 级别：`--log-level DEBUG|INFO|WARNING|ERROR`（默认 `INFO`），`--log-dir` 可改目录
- 事件命名：`process.start`、`listener.ready`、`http.request.begin/received/complete`、`http.response.sent`、`http.request.timeout/failed`、`clipboard.busy`、`clipboard.ack_timeout`、`focus.foreground_failed` 等
- `DEBUG` 级别会记录逐帧发送/接收元数据（`frame.send` / `frame.receive`：kind/seq/total/payload 字节数），不记录正文
- **安全边界**：绝不记录剪贴板正文、HTTP 请求/响应正文、`Authorization`、Cookie 或 URL userinfo；日志路径由 `safe_http_path` 清洗

## 与 qr-git-tunnel 的关系

[`qr-git-tunnel`](https://github.com/CangWeiohh/qr-git-tunnel) 继续作为稳定的二维码 Git 隧道独立演进；本仓库使用双向剪贴板作为传输通道，不依赖或导入 qr-git-tunnel 的代码。两者是面向同一类内网 Git 同步场景的姊妹项目，成熟后可择机抽取公共 HTTP、日志与探针代码。
