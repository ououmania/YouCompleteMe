# 多 ycmd 并存 + 按 buffer 路由（设计方案）

> 状态：已定稿（开放决策已确认，待实现）
> 前置提交：`c2533783`（项目级 ycmd 复用，现状记录见
> [`reuse-project-ycmd-server.md`](reuse-project-ycmd-server.md)）

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
# 项目根（.ycm_project.json 或 compile_commands.json 所在目录）→ 该项目的 ycmd 连接信息
self._ycmd_servers = {
    '<project_root>': ConnectionInfo( port, hmac_secret, pid, stdout, stderr ),
}
```

- key 用 `_FindProjectRoot()` 返回的项目根：沿父目录链**先找 `.ycm_project.json`**（工程标记文件），找不到再回退到 `compile_commands.json` 目录。
- `.ycm_project.json` 可配 `compilation_database`（目录，或 compile_commands.json 文件路径，相对 marker 目录解析）：
  客户端归一成绝对目录后作为 `clangd_compilation_database_dir` 传给 ycmd，clangd 启动加
  `--compile-commands-dir=<目录>`，显式指定用哪份 compile_commands.json（不配置则不传、clangd 自动发现）。
- marker 目录本身作为 `clangd_project_directory` 传给 ycmd，clangd 的 LSP `Project Directory` /
  `Open Workspaces` 显示为工程根而非 vim 的 cwd。
- 注：`compilation_database` 只控制 clangd **用哪份 compile_commands.json（编译 flags）**，不控制
  **跨文件索引**；跳转到定义所需的索引由 clangd 自己的 `~/.config/clangd/config.yaml` 里的
  `Index.Background` / `Index.External` 配置，与本工程改动无关。两者易混：前者是"编译参数来源"，
  后者是"符号定义在哪个文件"的索引。
- `ConnectionInfo` 复用现有的 dataclass（已含 port/hmac/pid/stdout/stderr）。
- 当前活跃连接仍用 `BaseRequest.server_location` / `BaseRequest.hmac_secret`（类变量，动态切换已验证可行）。
- 每个项目另有一份 per-project 文件 LRU，用于驱逐该项目的解析缓存（`BufferUnload`）。
- `_ycmd_servers` 受 `g:ycm_reuse_max_project_servers`（默认 5）上限约束，超限软淘汰。
- 启动新项目 server 时按"是否本会话第一个路由到的项目"选 idle_suicide：首个 1800s，其余 300s。

### 3.2 路由逻辑（OnBufferVisit）

```
buffer 文件路径 filepath
  → project_root = _FindProjectRoot(filepath)   # 沿父目录链找 .ycm_project.json，回退 compile_commands.json

有 project_root：
  ├─ 已在 self._ycmd_servers 里且 healthy → 切 server_location/hmac 过去（复用）
  ├─ 不在 dict 里 → 启动新 ycmd，记进 dict，切过去
  └─ 在 dict 里但不 healthy → 重启该项目 ycmd，切过去

无 project_root（rust/go/散文件）：
  → fallback：保持当前连接（或指向任意一个已在 dict 的 ycmd）
```

### 3.3 为什么 fallback 可以"随便"

clangd 按文件动态加载 compilation database（见背景），所以对"没有专属 ycmd 的项目"，用任意一个 ycmd，clangd 也会按该文件读它自己目录链上的 `compile_commands.json`，补全正确。fallback 只会损失"该项目 ycmd 的复用/索引缓存"，不影响正确性。

### 3.4 启动 / 复用 / 退出

- **启动**：惰性，第一次打开某项目的文件时才为该项目起 ycmd（不是 vim 启动时一次起齐）。
- **复用**：跨 vim 会话，通过 `~/.cache/ycmd/<project_root>/connection.json` 读回并 `_CheckServerHealthy` 探活。
- **退出**：`OnVimLeave` 时，需要决定"关哪些 ycmd"——见 4. 的待定项。

## 4. 决策记录

1. **OnVimLeave 关哪些**：有 project server 时**全不关**，交给 `idle_suicide`。若本会话从未路由到
   任何项目（reuse 开但 cwd 无 compdb，起的是 fallback 散文件 server），退出时**关掉**——它没有
   connection.json、无法复用。`IndividualServerPolicy` 恒关。
2. **server 数量上限**：`g:ycm_reuse_max_project_servers` 默认 5。超出时**软淘汰**——只从 dict 移除 +
   删 connection.json，**不显式 shutdown**（避免误杀别的 vim 会话正在用的 server），进程交给 idle_suicide。
3. **idle_suicide 分级**：本会话**第一个路由到的项目** → 1800s；其余项目 → 300s（硬编码，不做成选项）。
   临时项目快速自清，代价是超时后重访会整机重启、解析慢（已接受）。
4. **per-project LRU**：`_open_files_lru` 按项目分，各自独立驱逐（超 `reuse_max_open_files` 或该
   server RSS 超 `reuse_max_memory_mb`）。顺带修掉"跨项目发 `BufferUnload` 给错 server"的 bug。
5. **切换成本**：只在 `compdb_dir` 变化时才路由（读 connection + 探活）；同项目内切 buffer 不重复探活。
6. **多 vim 实例同项目**：行为与单 ycmd 复用一致，无需额外处理。
7. **compdb 变化**：connection 失效 → 走"不 healthy → 重启"路径。
8. **`:YcmRestartServer` 语义**：只重启当前 buffer 对应的那个 ycmd（与路由一致）。
9. **Policy 拆分（Strategy）**：`ServerPolicy` 基类 + `IndividualServerPolicy`（旧默认：单 server、
   退出关、不路由）+ `ReusableServerPolicy`（多连接路由 / 软淘汰 / 分级 idle_suicide）。option 只在
   init 读一次选 policy，避免散落检查。
10. **分步落地**：参数化 helper → 抽 `_StartServer`/`_EnsureServerForCompdbDir` → policy 拆分
    （行为不变）→ 修 cwd（单连接路由）→ 多连接并存 → 手动集成验证。
11. **验证**：单测（mock 文件系统 / http）+ 手动真实多项目走查。

## 5. 改动范围

- vim 插件层：`python/ycm/youcompleteme.py`（+ 测试）与 `plugin/youcompleteme.vim`（仅新增
  `g:ycm_reuse_max_project_servers` 一个选项）。
- ycmd 层（`.ycm_project.json` 透传所需，改动很小）：`third_party/ycmd` 的 `clangd_completer.py`
  + `default_settings.json`，新增 `clangd_compilation_database_dir`（→ `--compile-commands-dir`）
  与 `clangd_project_directory`（→ LSP `Project Directory`）两个选项。
- 主要：
  - 新增 `ServerPolicy` / `IndividualServerPolicy` / `ReusableServerPolicy`（Strategy 拆分生命周期方法）；
  - `_SetUpServer` 拆分出 `_StartServer(project_root, idle_suicide_seconds)` 与 `_EnsureProjectServer`；
  - `ReusableServerPolicy` 持有 `_ycmd_servers` dict、`_active_project_root`、per-project LRU、
    `reuse_max_project_servers` cap 与 first-project 标志；
  - `OnBufferVisit` 加路由（委托给 policy）；
  - `OnVimLeave` 调整关停粒度（委托给 policy）；
  - `_FindProjectRoot` 按 filepath 起算、`.ycm_project.json` 优先、回退 compile_commands.json。

## 6. 与"锁定"方案的关系

- "锁定"（启动时定一个 compdb、之后不切）是本方案的特例：相当于 `self._ycmd_servers` 只放一个、且不随 buffer 切换。
- 本方案更通用，改动更大；如果多项目并存不是硬需求，可先上"锁定"，后续再演进到本方案。
