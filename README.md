# Clipboard Git Tunnel

基于 `qrtunnel` 的实验性独立仓库：使用 HSRClient 已打通的**双向剪贴板**，把 Git Smart HTTP 请求和响应都做成可靠分块传输。

> 当前版本是协议和本地模拟闭环的第一版，不替换现有 `qrtunnel`，也不建议直接用于生产 Git。真实部署前必须在目标 Windows/HSR 环境完成剪贴板容量、延迟、丢帧和大请求测试。

## 目标

- A 端继续提供 Git/IDEA 可直接使用的 HTTP 代理。
- B 端访问云桌面内网 Git 服务。
- A→B 和 B→A 都通过 HSR 双向剪贴板传输。
- 每个分块 stop-and-wait，必须收到 ACK 才发送下一块。
- 每块带 SHA-256，整段响应再次校验 SHA-256。
- 不把 Git 协议改成自定义命令，保持 Smart HTTP 兼容。
- 后续可将现有 qrtunnel QR 作为 `auto` 模式的 fallback。

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
B  ← ACK
```

线格式为 `QTC1:<base64(JSON)>`。JSON 字段包括：

- `v`: `qtc-clipboard-1`
- `kind`: `req_meta`、`req_data`、`req_end`、`req_commit`、`resp_begin`、`resp_meta`、`resp_data`、`resp_end`、`ack`、`error`
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

- `python`: Python 解释器绝对路径；留空 `""` 则使用 PATH 中的 `python`
- `a_*`: A 端参数（`a_listen`、`a_chunk_bytes`、`a_ack_timeout`、`a_retries`、`a_timeout`、`a_write_gap`、`a_max_request_bytes`、`a_window_keywords`、`a_log_level`、`a_log_dir`）
- `b_*`: B 端参数（`b_target`、`b_chunk_bytes`、`b_ack_timeout`、`b_retries`、`b_timeout`、`b_log_level`、`b_log_dir`）

优先级：**命令行参数 > config.yaml > 内置默认值**。Python 入口支持 `--config <path>` 指定其他配置文件（默认自动读取仓库根的 `config.yaml`）。

### 启动脚本

仓库根目录提供启动脚本（`%~dp0` 自动定位，仓库拷到任何位置都能用；自动读取 `config.yaml` 里的 `python` 路径和各项参数）：

```text
start_a.bat   # A 端：监听 0.0.0.0:9999（Windows VM，供 Mac 访问）
start_b.bat   # B 端：转发 192.168.21.14:8888（云桌面）
```

双击即可启动；也可以在后面追加参数临时覆盖，例如 `start_a.bat --log-level DEBUG`。

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

## 与原 qrtunnel 的关系

原项目位于 `../python/qrtunnel`，继续作为稳定 QR 隧道使用。本仓库独立演进，避免影响现有部署；成熟后再择机抽取公共 HTTP/日志/探针代码。
