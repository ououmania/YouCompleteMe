# LSP 进度非侵入化展示

日期：2026-08-25
状态：已批准（待实现）

## 背景与问题

YouCompleteMe（ououmania fork）近期新增了 LSP `$/progress` 转发与命令行展示（commit `c8528ae6`）。
rust-analyzer 在打开文件时会通过 `window/workDoneProgress/create` + `$/progress` 上报进度
（如 `Fetching: discovering sysroot`、`Roots Scanned: 0/349`）。当前前端实现在每次 `report`
时执行 `redraw` + `echo`，把进度刷进命令行。

问题：进度更新频繁（rust-analyzer 逐个 root 扫描，349 次 `report`）、消息长，触发 vim 的
hit-enter 提示（`Press ENTER or type command to continue`），严重干扰编辑。

## 目标

把 LSP 进度从"抢占命令行"改为**非侵入展示**：

- 不再 `echo` 抢占命令行，彻底消除 hit-enter 干扰。
- 提供三种可选形态，共用同一份后端状态，用户可自由切换：
  - `popup`：右下角浮窗，begin 出现、report 更新、end 消失。
  - `statusline`：不弹任何 UI，导出函数供用户放入 statusline。
  - 关闭：完全不显示。

## 非目标

- 不改 ycmd 侧 `$/progress` → `lsp_progress` 的转发逻辑。
- 不做进度历史、取消、多项目聚合等增强。
- 不支持 neovim 的 `nvim_open_win` 浮窗路径（当前环境是 Vim 9.1，`popup_create` 已可用；
  后续需要 neovim 支持再单独评估）。

## 设计

### 配置项

新增 vim 全局选项 `g:ycm_show_lsp_progress`（字符串）：

| 值 | 行为 |
|---|---|
| `'popup'`（默认） | 右下角浮窗展示 |
| `'statusline'` | 不弹 UI，仅导出 `youcompleteme#LspProgress()` |
| `''` / `'none'` | 完全关闭（等同上游 ycmd 静默行为） |

默认 `'popup'`。Vim 8.2+ 支持 `popup_create`；当前环境 Vim 9.1，满足要求。
极老 Vim 不支持 popup 时自动回退为不显示，不报错。

### 后端状态（保持不变）

`python/ycm/client/messages_request.py` 中：

- `_progress_tokens` 字典继续作为**唯一数据源**，记录每个 token 的 begin/report/end。
- `GetProgressSummary()` 继续返回拼接后的进度摘要字符串（idle 返回 `None`），供 popup 与
  statusline 共用。

### 前端展示（改动核心，均在 `messages_request.py`）

`_HandleProgressNotification` 不再 `echo`，改为：

1. 更新 `_progress_tokens`（begin/report/end 语义不变）。
2. 若当前为 `popup` 模式且支持 popup：
   - begin 或 report 时，计算摘要文本，用 `popup_create`（首次）或 `popup_settext`（更新）
     在右下角展示/刷新浮窗。
   - end 且无剩余 token 时，`popup_close` 关闭浮窗。
   - report 阶段节流：仅当摘要文本发生变化时才刷新，且两次刷新间隔不小于 100ms，
     避免 349 次 root 扫描把浮窗刷闪。
3. 若当前为 `statusline` 模式：不弹任何 UI（statusline 通过导出的函数自行读取摘要）。
4. 若为关闭模式：仅维护状态，不产生任何 UI。

### 导出函数（statusline 模式）

`autoload/youcompleteme.vim` 新增：

```vim
function! youcompleteme#LspProgress() abort
  return py3eval( 'ycm_state.GetLspProgress()' )
endfunction
```

`python/ycm/youcompleteme.py` 的 `YouCompleteMe` 类新增 `GetLspProgress()`，返回
`GetProgressSummary()` 结果（idle 时返回空字符串 `''`，避免 statusline 显示 `None`）。

### 配置读取

- `plugin/youcompleteme.vim` 声明 `g:ycm_show_lsp_progress` 默认 `'popup'`。
- Python 侧在初始化时通过 `vim.vars` 读取该值，转成枚举（`popup` / `statusline` / `none`）。
  非法值按 `none` 处理，保持宽容。

### 节流策略

popup 的 report 刷新条件：摘要文本变化 **且** 距上次刷新 ≥ 100ms。
文本变化指 `GetProgressSummary()` 返回的拼接字符串发生改变（标题、消息、百分比任一变化）。
这天然覆盖"percentage 变化才刷新"的需求，并兼顾多 token 场景。

## 错误处理

- popup 不可用（`VimSupportsPopupWindows()` 为 False）：静默回退，不报错、不 echo。
- 非法 `g:ycm_show_lsp_progress` 值：按 `none` 处理。
- popup 文本中可能含单引号，`popup_settext` 参数用 `json.dumps` 序列化（沿用
  `signature_help.py` 的既有做法），避免 vim 脚本注入/转义问题。

## 改动文件清单

仓库：`~/git/YouCompleteMe`（= vim 实际加载的 `~/.vim/bundle/YouCompleteMe`）

| 文件 | 改动 |
|---|---|
| `plugin/youcompleteme.vim` | 声明 `g:ycm_show_lsp_progress` 默认 `'popup'` |
| `python/ycm/client/messages_request.py` | `_HandleProgressNotification` 改为 popup/状态更新，移除 echo；新增 popup 生命周期与节流 |
| `python/ycm/youcompleteme.py` | 新增 `GetLspProgress()` |
| `autoload/youcompleteme.vim` | 新增 `youcompleteme#LspProgress()` |
| `doc/youcompleteme.txt` | 记录新选项与函数（可选，随实现补充） |

不涉及 ycmd 仓库。

## 测试

- 单测（若存在对应测试）：`messages_request.py` 的 `_HandleProgressNotification` 状态迁移
  （begin/report/end → token 增改删）与摘要生成，使用 fake/mock，不依赖真实 vim。
- 手动验证：打开 rust 文件，确认右下角浮窗出现并随进度刷新、结束后消失，命令行不再被
  抢占、不再出现 hit-enter；切换 `g:ycm_show_lsp_progress` 三个值分别验证。

## 验收标准

1. 打开 rust 文件，命令行不再出现 `Press ENTER or type command to continue`。
2. 默认 `popup`：进度在右下角浮窗展示，结束后自动消失。
3. `g:ycm_show_lsp_progress='statusline'`：无 UI，`youcompleteme#LspProgress()` 可返回摘要。
4. `g:ycm_show_lsp_progress='none'`：完全静默。
