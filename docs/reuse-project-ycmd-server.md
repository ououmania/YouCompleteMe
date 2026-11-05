# 项目级 ycmd 复用（现状记录）

日期：2026-09-24
状态：已实现（commit `9182bcda`，"Reuse ycmd server per project with compilation database"）

> 这是当前（单 ycmd）复用机制的现状记录。多 ycmd 并存方案见
> [`reuse-multi-server-design.md`](reuse-multi-server-design.md)。

## 背景与问题

clangd 启动慢，且跨 vim 会话每次重启都会重复索引。对于有 compilation database
（`compile_commands.json`）的项目，让 ycmd 常驻、跨会话复用，避免重复启动与索引。

## 目标

- 对有 compilation database 的项目，跨 vim 会话复用**一个** ycmd。

## 非目标

- 一个 vim 会话内多项目并存（各自复用各自的 ycmd）——见多连接方案。

## 设计

### 配置项（`plugin/youcompleteme.vim`）

| 选项 | 默认 | 含义 |
|---|---|---|
| `g:ycm_reuse_project_ycmd_server` | `0` | 总开关 |
| `g:ycm_idle_suicide_seconds` | `1800` | ycmd 空闲多久后退出（首个/唯一项目） |
| `g:ycm_secondary_idle_suicide_seconds` | `300` | 多项目时非首个 ycmd 的空闲超时 |

> 两个 idle 超时必须显著大于 keepalive ping 间隔（当前 75s），否则 vim 还开着
> ycmd 就可能被 watchdog 杀掉。`idle_suicide` 触发时 ycmd 会连同**所有**子 server
> （包括 clangd）一起关闭，所以想让 clangd 跨夜常驻需要调大这两个值。

> 文件级 LRU 驱逐与内存上限已移到 ycmd 侧（`lsp_max_open_files` /
> `lsp_max_memory_mb`，clangd 可用 `clangd_max_open_files` /
> `clangd_max_memory_mb` 单独覆盖）。vim 侧只保留 per-project 打开文件计数，
> 用于项目级淘汰。见 [`reuse-multi-server-design.md`](reuse-multi-server-design.md)。

### 判定是否可复用

`_FindCompilationDatabaseDir()`：从 `os.getcwd()` 出发，沿父目录链向上找
`compile_commands.json`，命中则返回其目录，否则 `None`。

`_SetUpServer` 里：`reuse_project = 开关开启 and _FindCompilationDatabaseDir() is not None`。

### 连接文件

- 路径：`~/.cache/ycmd/<ProjectDirName()>/connection.json`。
- `_ProjectDirName()`：取 `_FindCompilationDatabaseDir()` 或 cwd 的 `realpath`，
  去掉前导 `/`，非 `[A-Za-z0-9_]` 字符替换为 `_`。
- `ConnectionInfo`（dataclass）：`port` / `hmac_secret: bytes` / `pid` / `stdout` / `stderr`。
- 写入时 `hmac_secret` 用 base64 编码后 `json.dump`；读取时 base64 解码还原。
- 复用项目启动时，日志也写到 `~/.cache/ycmd/<project>/ycmd_<port>_{stdout,stderr}.log`
  并加 `--keep_logfiles`，保证跨会话可追查。

### 启动流程（`_SetUpServer`，仅 vim 启动时调用一次）

1. 若复用开启且找到 compilation database：
   - 读 `connection.json`；读成功且 `_CheckServerHealthy(port, hmac)` 为真 →
     复用（`_reusing_server = True`，`_server_popen = None`），直接 `return`。
   - 否则落到下面的正常启动。
2. 正常启动新 ycmd（`--idle_suicide_seconds` 取自 `g:ycm_idle_suicide_seconds`，
   多项目时非首个用 `g:ycm_secondary_idle_suicide_seconds`）；若是复用项目，启动后
   写 `connection.json`。

### 探活

`_CheckServerHealthy(server_location, hmac_secret)`：临时把 `BaseRequest.server_location`
与 `BaseRequest.hmac_secret`（模块级类变量）切到目标 server，发 `healthy` 请求，
`finally` 恢复原值，返回 `bool`。

### 生命周期

- `IsServerAlive`：复用时用 healthy 探活；否则 `self._server_popen.poll() is None`。
- `ServerPid`：复用时重新读 `connection.json` 取 pid。
- `NotifyUserIfServerCrashed`：复用时探活失败则提示 `SERVER_SHUTDOWN_MESSAGE`。
- `RestartServer`：`SendShutdownRequest` → 删 `connection.json` → `_SetUpServer`。
- `OnVimLeave`：未建立复用（`_active_project_root is None`）才走 `_ShutdownServer`
  （发 `/shutdown`，ycmd 连同所有子 server 一起关）；复用激活时发 `POST /detach`，
  让 ycmd 关掉**不参与复用**的 completer，ycmd 本体留给 `idle_suicide` 回收。
- 打开文件 LRU（`_open_files_lru` + `_EvictOldOpenFiles`）：超过
  `reuse_max_open_files` 或子进程 RSS 超过 `reuse_max_memory_mb` 时，向 ycmd 发
  `BufferUnload` event_notification，释放其解析缓存。

### 哪些子 server 参与复用

`ShouldKeepAliveOnClientDetach()`（`Completer` 基类默认 `False`）决定某个 completer
的下游 server 是否跨会话保留。目前只有 `ClangdCompleter` 覆写为 `True`（建索引慢），
其它 LSP（如 proto 的 buf-lsp）在 `/detach` 时走正常的 LSP `shutdown`+`exit`，并从
`_filetype_completers` 移除，下次打开文件时重新构造实例、正常启动。

多个 vim 复用同一个 ycmd 时，任一 vim 退出都会发 `/detach`；非复用 completer 被关后，
仍在线的 vim 下次请求时会自动重新拉起（不做引用计数）。

### 关键实现点

`BaseRequest.server_location` / `hmac_secret` 是模块级类变量，探活时动态切换已验证可行。
这正是后续"多 ycmd 并存 + 按 buffer 路由"方案的基础。

## 错误处理

- `_ReadConnectionFile`：捕获 `OSError/KeyError/ValueError/TypeError` → 返回 `None`
  → 走正常启动。
- `_CheckServerHealthy`：捕获一切异常 → 返回 `False` → 走正常启动。
- `_RemoveConnectionFile`：捕获 `OSError` → `pass`。

## 改动文件清单（commit `9182bcda`）

| 文件 | 改动 |
|---|---|
| `plugin/youcompleteme.vim` | 声明 3 个 `g:ycm_reuse_*` 选项 |
| `python/ycm/youcompleteme.py` | `_FindCompilationDatabaseDir`、连接文件读写、`_SetUpServer` 复用分支、LRU 驱逐、`OnVimLeave`/`RestartServer` 等 |

## 测试

- 复用与选择性关闭的单测：`python/ycm/tests/youcompleteme_test.py`（`MultiServerTest`）
  覆盖 `OnVimLeave` 分流、按配置传递 idle 超时；`ycmd/tests/detach_test.py` 覆盖
  per-completer 接口、`ServerState.ShutdownCompletersOnClientDetach()` 与 `/detach`。
- 仍缺：`_FindCompilationDatabaseDir` / 连接文件读写的单测。

## 已知问题 / 限制

1. **cwd 问题**：在项目父目录（如 `~/work/ngr`）打开 vim，启动时找不到 compilation
   database，reuse 判定失败；之后 MRU 打开项目文件时已经晚了。
2. **多项目并存**：单 ycmd 只能锁定一个 compilation database 目录，混开多个项目时
   只有一个项目能复用。
3. **内存**：clangd 常驻时长由 `g:ycm_idle_suicide_seconds`（默认 1800s）决定，
   超时后 watchdog 会连 clangd 一起关掉；调大可跨夜复用，但 ycmd 及其子 server
   也会更久占内存。
