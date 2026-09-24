# 多 ycmd 并存 + 按 buffer 路由（设计方案）

> 状态：讨论稿，未实现
> 前置提交：`c2533783`（项目级 ycmd 复用）

## 1. 背景与现状

当前（`c2533783`）的 reuse 机制是**单 ycmd**：

- 开了 `reuse_project_ycmd_server` 且能找到 compilation database 时，按 compilation database 目录（`~/.cache/ycmd/<project>/connection.json`）复用**一个** ycmd；
- 复用判断发生在 vim 启动时（`_SetUpServer`），用 `cwd` 向上找 `compile_commands.json`。

由此引出两个已知问题：

1. **cwd 不对**：在项目父目录（如 `~/work/ngr`）打开 vim，启动时找不到 compilation database，reuse 判断失败；之后 MRU 打开项目文件时已经晚了。
2. **多项目并存**：一个 vim 会话里混开多个项目，单 ycmd 只能"锁定"一个 compilation database 目录。

关键事实（已验证）：**clangd 是单实例、按文件动态加载 compilation database**（`clangd_completer.py` 里 `CompilationDatabaseExists` 沿父目录链找 `compile_commands.json`）。所以哪怕用"任意一个" ycmd，clangd 也会按当前文件自己读到正确的 compilation database，补全不会错。

## 2. 目标

- 一个 vim 会话能同时服务多个项目，各自复用各自的 ycmd；
- 打开/切换 buffer 时，自动路由到对应的 ycmd；
- 找不到对应项目（无 compilation database、或该项目还没被复用）时不折腾、有合理 fallback。

## 3. 方案

vim 侧维护**多个 ycmd 连接**，按 compilation database 目录路由。

### 3.1 数据结构

```python
# compilation database 目录 → 该项目的 ycmd 连接信息
self._ycmd_servers = {
    '<compdb_dir>': ConnectionInfo( port, hmac_secret, pid, stdout, stderr ),
}
```

- key 用 `_FindCompilationDatabaseDir()` 返回的项目根（已有）。
- `ConnectionInfo` 复用现有的 dataclass（已含 port/hmac/pid/stdout/stderr）。
- 当前活跃连接仍用 `BaseRequest.server_location` / `BaseRequest.hmac_secret`（类变量，动态切换已验证可行）。

### 3.2 路由逻辑（OnBufferVisit）

```
buffer 文件路径 filepath
  → compdb_dir = _FindCompilationDatabaseDir(filepath)   # 沿父目录链找

有 compdb_dir：
  ├─ 已在 self._ycmd_servers 里且 healthy → 切 server_location/hmac 过去（复用）
  ├─ 不在 dict 里 → 启动新 ycmd，记进 dict，切过去
  └─ 在 dict 里但不 healthy → 重启该项目 ycmd，切过去

无 compdb_dir（rust/go/散文件）：
  → fallback：保持当前连接（或指向任意一个已在 dict 的 ycmd）
```

### 3.3 为什么 fallback 可以"随便"

clangd 按文件动态加载 compilation database（见背景），所以对"没有专属 ycmd 的项目"，用任意一个 ycmd，clangd 也会按该文件读它自己目录链上的 `compile_commands.json`，补全正确。fallback 只会损失"该项目 ycmd 的复用/索引缓存"，不影响正确性。

### 3.4 启动 / 复用 / 退出

- **启动**：惰性，第一次打开某项目的文件时才为该项目起 ycmd（不是 vim 启动时一次起齐）。
- **复用**：跨 vim 会话，通过 `~/.cache/ycmd/<compdb_dir>/connection.json` 读回并 `_CheckServerHealthy` 探活。
- **退出**：`OnVimLeave` 时，需要决定"关哪些 ycmd"——见 4. 的待定项。

## 4. 待定 / 边界

1. **OnVimLeave 关哪些**：多 ycmd 时，退出 vim 是全关、还是关"非复用来的"、还是都不关（全交给 idle_suicide）？这决定 `_ShutdownServer` 的粒度。
2. **内存**：多个 clangd 进程常驻，是"项目隔离 + 各自复用"的代价，需确认可接受。
3. **切换成本**：切 connection = 读 connection + `healthy` 探活（HTTP），远小于重启 clangd，但非零。是否需要"同项目内切 buffer 不重复探活"的优化。
4. **多 vim 实例同项目**：两个 vim 都连同一个项目的 ycmd，行为和单 ycmd 复用一致（现有 connection 机制已支持）。
5. **compdb 变化**：项目目录移动/删除后，connection 失效 → 走"不 healthy → 重启"路径。

## 5. 改动范围

- 只在 `python/ycm/youcompleteme.py`（vim 插件层），**不动 ycmd 服务器层**。
- 主要：
  - `_SetUpServer` 拆分出"为某个 compdb_dir 启动/复用 ycmd"的逻辑；
  - 新增 `self._ycmd_servers` dict 管理；
  - `OnBufferVisit` 加路由；
  - `OnVimLeave` 调整关停粒度；
  - `_FindCompilationDatabaseDir` 增加"按 filepath 起算"的入口（现在只按 cwd）。

## 6. 与"锁定"方案的关系

- "锁定"（启动时定一个 compdb、之后不切）是本方案的特例：相当于 `self._ycmd_servers` 只放一个、且不随 buffer 切换。
- 本方案更通用，改动更大；如果多项目并存不是硬需求，可先上"锁定"，后续再演进到本方案。
