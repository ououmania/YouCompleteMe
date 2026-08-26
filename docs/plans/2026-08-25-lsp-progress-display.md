# LSP 进度非侵入化展示 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 LSP `$/progress` 的展示从「抢占命令行 echo」改为「右下角浮窗 / statusline 导出 / 完全关闭」三态可切换，消除 hit-enter 干扰。

**Architecture:** 后端状态（`_progress_tokens` + `GetProgressSummary()`）保持不变作为唯一数据源；新增模块级展示模式枚举，`_HandleProgressNotification` 只更新状态并分发到展示层；popup 与 statusline 共用同一份状态。改动仅在 YouCompleteMe 仓库，不涉及 ycmd。

**Tech Stack:** Python 3（YCM 前端）、VimScript、vim `popup_create` API、unittest + hamcrest（项目既有测试框架）。

## Global Constraints

- Vim 8.2+（`popup_create` 可用）；当前环境 Vim 9.1。
- 改动仅在仓库 `~/git/YouCompleteMe`（= `~/.vim/bundle/YouCompleteMe`），**不改 ycmd**。
- 代码风格遵循项目既有约定：2 空格缩进、单引号、行宽 ≤ 80、通过 flake8。
- 测试确定性：不依赖真实 vim / 真实时间 / 网络；使用 `unittest` + `hamcrest`。
- `g:ycm_show_lsp_progress` 取值：`'popup'`（默认）/ `'statusline'` / 其它按 `'none'` 处理。
- commit 信息不加 `Co-Authored-By:` 行（遵循用户全局配置）。

---

### Task 1: 重构进度通知为纯状态更新，引入展示模式枚举

**Files:**
- Modify: `python/ycm/client/messages_request.py`（全文重写进度相关函数）
- Test: `python/ycm/tests/client/messages_request_test.py`

**Interfaces:**
- Consumes: 无（第一个任务）。
- Produces:
  - `PROGRESS_POPUP = 'popup'`、`PROGRESS_STATUSLINE = 'statusline'`、`PROGRESS_NONE = 'none'`（模块常量）。
  - `GetProgressSummary() -> Optional[str]`（保持不变，已有）。
  - `GetLspProgress() -> str`（新，Task 3 使用）。
  - `_HandleProgressNotification( progress: dict ) -> None`（新语义：只更新状态 + 分发展示）。
  - `_GetProgressDisplayMode() -> str`（新，返回 `popup`/`statusline`/`none`）。
  - `_UpdateProgressDisplay() -> None`（新，本任务实现为 popup 分支占位 + 关闭）。
  - `_ClearProgress() -> None`（改：清 token + 关 popup）。
  - `_CloseProgressPopup() -> None`（新，本任务实现）。
  - `_UpdateProgressPopup( summary: Optional[str] ) -> None`（本任务只留桩，Task 2 填充）。

- [ ] **Step 1: 写失败测试**

修改 `python/ycm/tests/client/messages_request_test.py`。在文件顶部 import 处（第 25 行 `from ycm.client.messages_request import _HandlePollResponse` 之后）扩展导入，并在 `MessagesRequestTest` 类开头加入 `setUp`/`tearDown`，再追加状态迁移测试方法。

首先，替换第 25 行的 import 行：

```python
from ycm.client.messages_request import ( _ClearProgress,
                                          _HandleProgressNotification,
                                          _HandlePollResponse,
                                          GetLspProgress,
                                          GetProgressSummary )
```

然后，在 `class MessagesRequestTest( TestCase ):`（第 29 行）之后立即插入 `setUp`/`tearDown`，并新增测试方法。完整新增内容如下（放在类定义第一行之后、`test_HandlePollResponse_NoMessages` 之前）：

```python
  def setUp( self ):
    _ClearProgress()


  def tearDown( self ):
    _ClearProgress()


  def test_ProgressNotification_BeginReportEnd( self ):
    _HandleProgressNotification( {
      'kind': 'begin',
      'token': 'rust-analyzer/roots',
      'title': 'Indexing',
    } )
    assert_that( GetProgressSummary(), equal_to( 'Indexing' ) )

    _HandleProgressNotification( {
      'kind': 'report',
      'token': 'rust-analyzer/roots',
      'message': 'foo',
      'percentage': 50,
    } )
    assert_that( GetProgressSummary(), equal_to( 'Indexing: foo (50%)' ) )

    _HandleProgressNotification( {
      'kind': 'end',
      'token': 'rust-analyzer/roots',
    } )
    assert_that( GetProgressSummary(), equal_to( None ) )


  def test_ProgressNotification_MultipleTokens( self ):
    _HandleProgressNotification( { 'kind': 'begin', 'token': 'a',
                                   'title': 'A' } )
    _HandleProgressNotification( { 'kind': 'begin', 'token': 'b',
                                   'title': 'B' } )
    assert_that( GetProgressSummary(), equal_to( 'A | B' ) )

    _HandleProgressNotification( { 'kind': 'end', 'token': 'a' } )
    assert_that( GetProgressSummary(), equal_to( 'B' ) )


  def test_GetLspProgress_Idle( self ):
    assert_that( GetLspProgress(), equal_to( '' ) )


  def test_GetLspProgress_Active( self ):
    _HandleProgressNotification( { 'kind': 'begin', 'token': 't',
                                   'title': 'X' } )
    assert_that( GetLspProgress(), equal_to( 'X' ) )
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd ~/git/YouCompleteMe && python run_tests.py --skip-build --no-flake8 python/ycm/tests/client/messages_request_test.py
```

Expected: FAIL —— `ImportError: cannot import name '_HandleProgressNotification'`（因为尚未定义这些函数）。

- [ ] **Step 3: 实现**

修改 `python/ycm/client/messages_request.py`。

**3a. 替换 import 段（第 18-23 行）**，把顶部 import 替换为：

```python
import json
import logging
import time

import vim

from ycm.client.base_request import BaseRequest, BuildRequestData
from ycm.vimsupport import ( GetIntValue,
                             PostVimMessage,
                             VimSupportsPopupWindows )
```

（保留 `_logger = logging.getLogger( __name__ )`、`TIMEOUT_SECONDS = 60` 不动。）

**3b. 替换 `_progress_tokens = {}`（第 30 行）及之后到 `GetProgressSummary` 之前**，改为常量与状态声明：

```python
PROGRESS_POPUP = 'popup'
PROGRESS_STATUSLINE = 'statusline'
PROGRESS_NONE = 'none'

# 两次 popup 刷新的最短间隔（秒），避免 report 刷屏
PROGRESS_UPDATE_MIN_INTERVAL = 0.1

_progress_tokens = {}
_progress_popup_id = None
_last_progress_summary = None
_last_progress_update = 0.0

_PROGRESS_POPUP_OPTIONS = {
  'line': 'cursor+1',
  'col': 'cursor',
  'pos': 'botleft',
  'wrap': 0,
  'fixed': 1,
  'flip': 1,
}
```

**3c. 在 `GetProgressSummary` 之后新增 `GetLspProgress`**（紧跟在现有 `GetProgressSummary` 函数定义结束后，即第 118 行 `return ' | '.join( parts ) if parts else None` 之后）：

```python
def GetLspProgress():
  """Returns a summary of active LSP progress for use in the statusline, or an
  empty string if idle."""
  summary = GetProgressSummary()
  return summary if summary is not None else ''
```

**3d. 重写 `_ClearProgress`（原第 121-125 行）**，替换为：

```python
def _ClearProgress():
  global _progress_tokens
  _progress_tokens.clear()
  _CloseProgressPopup()
```

**3e. 重写 `_HandleProgressNotification`（原第 128-144 行）**，替换为：

```python
def _HandleProgressNotification( progress ):
  global _progress_tokens
  kind = progress.get( 'kind', '' )
  token = str( progress.get( 'token', '' ) )

  if kind == 'begin':
    _progress_tokens[ token ] = progress
  elif kind == 'report':
    if token in _progress_tokens:
      _progress_tokens[ token ].update( progress )
  elif kind == 'end':
    _progress_tokens.pop( token, None )

  _UpdateProgressDisplay()
```

**3f. 删除 `_UpdateProgressEcho`（原第 147-163 行整段）**，替换为以下新函数：

```python
def _GetProgressDisplayMode():
  mode = vim.vars.get( 'ycm_show_lsp_progress', PROGRESS_POPUP )
  if mode in ( PROGRESS_POPUP, PROGRESS_STATUSLINE ):
    return mode
  return PROGRESS_NONE


def _UpdateProgressDisplay():
  if _GetProgressDisplayMode() != PROGRESS_POPUP:
    return
  if not VimSupportsPopupWindows():
    return
  _UpdateProgressPopup( GetProgressSummary() )


def _UpdateProgressPopup( summary ):
  # 本任务先占位，Task 2 填充实现。
  pass


def _CloseProgressPopup():
  global _progress_popup_id, _last_progress_summary
  if _progress_popup_id is not None:
    vim.eval( f'popup_close( { _progress_popup_id } )' )
    _progress_popup_id = None
  _last_progress_summary = None
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd ~/git/YouCompleteMe && python run_tests.py --skip-build --no-flake8 python/ycm/tests/client/messages_request_test.py
```

Expected: PASS（状态迁移 + `GetLspProgress` 测试全部通过；`_HandleProgressNotification` 在默认测试 mock（`vim.vars` 为 MagicMock，模式解析为 `none`）下不触碰 popup 分支）。

- [ ] **Step 5: 提交**

```bash
cd ~/git/YouCompleteMe && git add python/ycm/client/messages_request.py python/ycm/tests/client/messages_request_test.py && git commit -m "refactor: 将 LSP 进度通知改为纯状态更新，引入展示模式枚举"
```

---

### Task 2: 实现 popup 展示与节流

**Files:**
- Modify: `python/ycm/client/messages_request.py`（填充 `_UpdateProgressPopup`）
- Test: `python/ycm/tests/client/messages_request_test.py`

**Interfaces:**
- Consumes: Task 1 的 `_UpdateProgressPopup( summary )` 桩、`PROGRESS_UPDATE_MIN_INTERVAL`、`_PROGRESS_POPUP_OPTIONS`、`_progress_popup_id`/`_last_progress_summary`/`_last_progress_update`、`GetIntValue`、`vim.eval`、`json.dumps`。
- Produces: 完整的 popup 生命周期（`popup_create` → `popup_settext` → `popup_close`）与节流逻辑。

- [ ] **Step 1: 写失败测试**

在 `python/ycm/tests/client/messages_request_test.py` 的 import 段追加 `patch`（已有 `from unittest.mock import patch, call`，直接用）。在 `MessagesRequestTest` 类内追加以下两个测试方法（放在 `test_GetLspProgress_Active` 之后）：

```python
  @patch( 'ycm.client.messages_request._GetProgressDisplayMode',
          return_value = 'popup' )
  @patch( 'ycm.client.messages_request.VimSupportsPopupWindows',
          return_value = True )
  @patch( 'ycm.client.messages_request.GetIntValue', return_value = 7 )
  @patch( 'ycm.client.messages_request.time.time',
          side_effect = [ 0.0, 0.20 ] )
  @patch( 'ycm.client.messages_request.vim.eval' )
  def test_ProgressNotification_PopupLifecycle( self, vim_eval, time_time,
                                                get_int_value,
                                                vim_supports_popup,
                                                progress_mode ):
    _HandleProgressNotification( { 'kind': 'begin', 'token': 't',
                                   'title': 'Indexing' } )
    get_int_value.assert_called_once()
    vim_eval.assert_not_called()

    _HandleProgressNotification( { 'kind': 'report', 'token': 't',
                                   'message': 'foo', 'percentage': 50 } )
    vim_eval.assert_called_once_with( 'popup_settext( 7, ["Indexing: foo (50%)"] )' )

    _HandleProgressNotification( { 'kind': 'end', 'token': 't' } )
    vim_eval.assert_called_with( 'popup_close( 7 )' )
```

```python
  @patch( 'ycm.client.messages_request._GetProgressDisplayMode',
          return_value = 'popup' )
  @patch( 'ycm.client.messages_request.VimSupportsPopupWindows',
          return_value = True )
  @patch( 'ycm.client.messages_request.GetIntValue', return_value = 7 )
  @patch( 'ycm.client.messages_request.time.time',
          side_effect = [ 0.0, 0.05, 0.20 ] )
  @patch( 'ycm.client.messages_request.vim.eval' )
  def test_ProgressNotification_PopupThrottle( self, vim_eval, time_time,
                                               get_int_value,
                                               vim_supports_popup,
                                               progress_mode ):
    _HandleProgressNotification( { 'kind': 'begin', 'token': 't',
                                   'title': 'Indexing' } )

    # 距上次刷新仅 0.05s，低于 0.1s 阈值，应被节流跳过（不 settext）
    _HandleProgressNotification( { 'kind': 'report', 'token': 't',
                                   'message': 'early' } )
    vim_eval.assert_not_called()

    # 距上次刷新 0.20s，超过阈值，应刷新
    _HandleProgressNotification( { 'kind': 'report', 'token': 't',
                                   'message': 'late' } )
    vim_eval.assert_called_once_with( 'popup_settext( 7, ["Indexing: late"] )' )
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd ~/git/YouCompleteMe && python run_tests.py --skip-build --no-flake8 python/ycm/tests/client/messages_request_test.py
```

Expected: FAIL —— popup 生命周期测试失败（当前 `_UpdateProgressPopup` 是空桩，`GetIntValue`/`vim.eval` 均不被调用）。

- [ ] **Step 3: 实现 `_UpdateProgressPopup`**

将 `python/ycm/client/messages_request.py` 中的 `_UpdateProgressPopup` 桩替换为：

```python
def _UpdateProgressPopup( summary ):
  global _progress_popup_id, _last_progress_summary, _last_progress_update

  if summary is None:
    _CloseProgressPopup()
    return

  if summary == _last_progress_summary and _progress_popup_id is not None:
    return

  now = time.time()
  if ( _progress_popup_id is not None and
       now - _last_progress_update < PROGRESS_UPDATE_MIN_INTERVAL ):
    return

  lines = [ summary ]
  if _progress_popup_id is None:
    _progress_popup_id = GetIntValue(
      f'popup_create( { json.dumps( lines ) }, '
      f'{ json.dumps( _PROGRESS_POPUP_OPTIONS ) } )' )
  else:
    vim.eval( f'popup_settext( { _progress_popup_id }, '
              f'{ json.dumps( lines ) } )' )

  _last_progress_summary = summary
  _last_progress_update = now
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd ~/git/YouCompleteMe && python run_tests.py --skip-build --no-flake8 python/ycm/tests/client/messages_request_test.py
```

Expected: PASS（popup 生命周期与节流测试通过）。

- [ ] **Step 5: 提交**

```bash
cd ~/git/YouCompleteMe && git add python/ycm/client/messages_request.py python/ycm/tests/client/messages_request_test.py && git commit -m "feat: LSP 进度改为右下角浮窗展示，带节流"
```

---

### Task 3: 导出 statusline 进度函数

**Files:**
- Modify: `python/ycm/youcompleteme.py`（import + 新增方法）
- Modify: `autoload/youcompleteme.vim`（新增函数）

**Interfaces:**
- Consumes: Task 1 的 `GetLspProgress()`（模块级，返回 `str`）。
- Produces: `YouCompleteMe.GetLspProgress()` 方法（返回 `str`）；`youcompleteme#LspProgress()` vim 函数（返回进度字符串或空串）。

- [ ] **Step 1: 在 `youcompleteme.py` 新增方法**

修改 `python/ycm/youcompleteme.py`：

**1a.** 将第 42 行的 import：

```python
from ycm.client.messages_request import GetProgressSummary
```

改为：

```python
from ycm.client.messages_request import ( GetLspProgress as LspProgress,
                                          GetProgressSummary )
```

**1b.** 在 `GetWarningCount` 方法（第 786-787 行）之后新增：

```python
  def GetLspProgress( self ):
    return LspProgress()
```

（放在 `def _PopulateLocationListWithLatestDiagnostics` 之前即可。）

- [ ] **Step 2: 在 `autoload/youcompleteme.vim` 新增 vim 函数**

修改 `autoload/youcompleteme.vim`，在 `youcompleteme#GetWarningCount()`（第 262-264 行）之后新增：

```vim
function! youcompleteme#LspProgress() abort
  return py3eval( 'ycm_state.GetLspProgress()' )
endfunction
```

- [ ] **Step 3: 运行测试（回归）**

```bash
cd ~/git/YouCompleteMe && python run_tests.py --skip-build --no-flake8 python/ycm/tests/client/messages_request_test.py
```

Expected: PASS（`GetLspProgress` 委托逻辑已在 Task 1 单测覆盖；本任务仅改委托与 vim 导出，无新单测，跑回归确认不破坏）。

- [ ] **Step 4: 提交**

```bash
cd ~/git/YouCompleteMe && git add python/ycm/youcompleteme.py autoload/youcompleteme.vim && git commit -m "feat: 导出 youcompleteme#LspProgress() 供 statusline 使用"
```

---

### Task 4: 声明配置项、补文档、全量验证

**Files:**
- Modify: `plugin/youcompleteme.vim`（声明 `g:ycm_show_lsp_progress`）
- Modify: `doc/youcompleteme.txt`（文档）

**Interfaces:**
- Consumes: 无。
- Produces: `g:ycm_show_lsp_progress` 默认 `'popup'`；用户文档。

- [ ] **Step 1: 声明配置项**

修改 `plugin/youcompleteme.vim`，在 `let g:ycm_auto_hover`（第 195-196 行）之后、`let g:ycm_update_diagnostics_in_insert_mode`（第 198 行）之前新增：

```vim
let g:ycm_show_lsp_progress =
      \ get( g:, 'ycm_show_lsp_progress', 'popup' )
```

- [ ] **Step 2: 补文档**

修改 `doc/youcompleteme.txt`，在 `g:ycm_auto_hover` 相关文档段落之后追加（查找该选项文档位置后插入）：

```text
g:ycm_show_lsp_progress			*g:ycm_show_lsp_progress*

	LSP 进度通知的展示方式。可取 'popup'（默认，右下角浮窗）、
	'statusline'（不显示，通过 |youcompleteme#LspProgress()| 自行读取）、
	'none'（完全关闭）。
```

- [ ] **Step 3: 全量 flake8 + 单测验证**

```bash
cd ~/git/YouCompleteMe && python run_tests.py --skip-build python/ycm/tests/client/messages_request_test.py
```

Expected: flake8 通过（无风格告警），单测通过。若 flake8 报错，修复后重跑（禁止用 `# noqa` 绕过）。

- [ ] **Step 4: 提交**

```bash
cd ~/git/YouCompleteMe && git add plugin/youcompleteme.vim doc/youcompleteme.txt && git commit -m "feat: 声明 g:ycm_show_lsp_progress 配置项并补文档"
```

---

### 验收标准（全部满足才算完成）

1. 打开 rust 文件，命令行不再出现 `Press ENTER or type command to continue`。
2. 默认 `popup`：进度在浮窗展示，结束后自动消失。
3. `g:ycm_show_lsp_progress='statusline'`：无 UI，`:echo youcompleteme#LspProgress()` 可返回摘要。
4. `g:ycm_show_lsp_progress='none'`：完全静默。

手动验证：`vim` 打开 rust 文件，观察浮窗；`:let g:ycm_show_lsp_progress='statusline'` 后 `:echo youcompleteme#LspProgress()`；`:let g:ycm_show_lsp_progress='none'` 后确认无任何进度显示。
