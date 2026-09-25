# 多 ycmd 并存 + 按 buffer 路由 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: 用 superpowers:subagent-driven-development（推荐）
> 或 superpowers:executing-plans 按 task 逐个实现。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 一个 vim 会话同时服务多个项目，各自复用各自的 ycmd，切换 buffer 自动路由到对应 ycmd，
并用 Strategy 模式把"复用/单发"两套行为收敛到 policy，消除散落的 option 检查。

**Architecture:** 只在 vim 插件层改动。`YouCompleteMe` 保留共享的 server 原语（启动/探活/连接文件/
崩溃通知），把生命周期分歧点委托给 `ServerPolicy`；`ReusableServerPolicy` 持有 `compdb_dir → ConnectionInfo`
字典 + per-project LRU，按 buffer 文件找 compilation database 目录，惰性复用/启动/重启，超上限软淘汰，
并按"是否首个项目"分级 idle_suicide。

**Tech Stack:** Python（vim 插件）、unittest + hamcrest + `unittest.mock`、flake8（仓库自带，2 空格缩进，
`run_tests.py` 驱动）。

## Global Constraints

- 只改 `python/ycm/youcompleteme.py`、`plugin/youcompleteme.vim`（仅新增一个选项）、
  测试 `python/ycm/tests/youcompleteme_test.py`；**不改 ycmd 服务器层**。
- 命名沿用现有风格：模块级 `_FindCompilationDatabaseDir`（`_CamelCase`），方法 `_SetUpServer`，
  成员 `_ycmd_servers`（snake_case），类 `CapWords`（`ServerPolicy`）。
- 不写魔鬼数字；`reuse_project_ycmd_server` / `reuse_max_project_servers` 等从 `_user_options` 读取。
- 每行 ≤ 80 字符（flake8），缩进 2 空格。提交信息不加 `Co-Authored-By:`。
- 已确认决策：OnVimLeave **全不关**；`:YcmRestartServer` **只重启当前 buffer 的**；
  server 上限 `reuse_max_project_servers` 默认 **5**，超限**软淘汰**（不 shutdown），
  淘汰策略为**优先淘汰当前 buffer 中打开文件最少的项目**（这种一般是临时打开，保留常用大项目）；
  idle_suicide 分级：**首个项目 1800s / 其余 300s**（300 硬编码，不做选项）；per-project LRU。
- 测试：`python run_tests.py --skip-build <unittest 目标>`；lint：`python -m flake8 <files>`。

## 现状回顾（实现者必读）

- `_SetUpServer` 在 `__init__` 调一次，用 `os.getcwd()` 判 compdb，复用或启动**一个** ycmd。
- 5 个连接文件 helper 隐式绑定 cwd。
- `_reuse_project` 单一 bool 散在 6 处（`_SetUpServer`/`OnBufferVisit`/`OnVimLeave`/`RestartServer`/
  `ServerPid`/`_StartServer`）。
- `BaseRequest.server_location`/`hmac_secret` 是模块级类变量，动态切换已验证可行。
- 全局 `_open_files_lru` 在多连接下会"跨项目发 `BufferUnload` 给错 server"（per-project LRU 修掉）。
- reuse 机制目前**零单测**。

## File Structure

- `python/ycm/youcompleteme.py`：实现改动（helper 参数化 + `_StartServer` 抽取 + policy 类 + 路由/多连接）。
- `plugin/youcompleteme.vim`：新增 `g:ycm_reuse_max_project_servers` 选项声明。
- `python/ycm/tests/youcompleteme_test.py`：新增单测。

---

## Task 1: 参数化 `_FindCompilationDatabaseDir` 与连接文件 helper

纯重构，行为不变。让 helper 接受显式 `compdb_dir`，补第一批单测。

**Files:**
- Modify: `python/ycm/youcompleteme.py:120-167`
- Test: `python/ycm/tests/youcompleteme_test.py`

**Interfaces:**
- Produces:
  - `_FindCompilationDatabaseDir(filepath=None) -> str|None`
  - `_ProjectDirName(compdb_dir) -> str`
  - `_ConnectionFilePath(compdb_dir) -> str`
  - `_ReadConnectionFile(compdb_dir) -> ConnectionInfo|None`
  - `_WriteConnectionFile(compdb_dir, info) -> None`
  - `_RemoveConnectionFile(compdb_dir) -> None`

- [ ] **Step 1: 写失败测试**

import 区补：

```python
import tempfile
import shutil
from pathlib import Path
```

并把 `from ycm.youcompleteme import YouCompleteMe` 扩展为：

```python
from ycm.youcompleteme import ( YouCompleteMe,
                                ConnectionInfo,
                                _FindCompilationDatabaseDir,
                                _ConnectionFilePath,
                                _ReadConnectionFile,
                                _WriteConnectionFile,
                                _RemoveConnectionFile )
```

新增测试类（文件末尾、`YouCompleteMeTest` 之外）：

```python
class CompilationDatabaseTest( TestCase ):
  def setUp( self ):
    self._tmp = tempfile.mkdtemp()

  def tearDown( self ):
    shutil.rmtree( self._tmp, ignore_errors = True )

  def _MakeCompdb( self, rel_dir ):
    directory = os.path.join( self._tmp, rel_dir )
    os.makedirs( directory, exist_ok = True )
    Path( directory, 'compile_commands.json' ).touch()
    return directory

  def test_FindCompilationDatabaseDir_FromFilepath( self ):
    compdb_dir = self._MakeCompdb( 'proj' )
    source = os.path.join( compdb_dir, 'src', 'main.cpp' )
    os.makedirs( os.path.dirname( source ), exist_ok = True )
    Path( source ).touch()
    assert_that( _FindCompilationDatabaseDir( source ),
                 equal_to( compdb_dir ) )

  def test_FindCompilationDatabaseDir_NoneFound( self ):
    assert_that( _FindCompilationDatabaseDir( self._tmp ),
                 equal_to( None ) )

  def test_ConnectionFile_RoundTrip( self ):
    compdb_dir = '/proj/a'
    info = ConnectionInfo( port = 4242, hmac_secret = b'\x01\x02',
                           pid = 42, stdout = '/tmp/o', stderr = '/tmp/e' )
    with patch( 'ycm.youcompleteme.CONNECTION_FILE_DIR', self._tmp ):
      _WriteConnectionFile( compdb_dir, info )
      assert_that( _ReadConnectionFile( compdb_dir ), equal_to( info ) )
      assert_that( _ConnectionFilePath( compdb_dir ),
                   equal_to( os.path.join( self._tmp, '_proj_a',
                                           'connection.json' ) ) )
      _RemoveConnectionFile( compdb_dir )
      assert_that( _ReadConnectionFile( compdb_dir ), equal_to( None ) )
```

- [ ] **Step 2: 运行确认失败**

Run: `python run_tests.py --skip-build ycm.tests.youcompleteme_test.CompilationDatabaseTest`
Expected: FAIL —— `TypeError`（`_FindCompilationDatabaseDir` 不接受参数）。

- [ ] **Step 3: 实现**

`youcompleteme.py:120-167` 改为：

```python
def _FindCompilationDatabaseDir( filepath = None ):
  directory = ( os.path.dirname( os.path.realpath( filepath ) )
                if filepath else os.path.realpath( os.getcwd() ) )
  while True:
    if os.path.isfile( os.path.join( directory, 'compile_commands.json' ) ):
      return directory
    parent = os.path.dirname( directory )
    if parent == directory:
      return None
    directory = parent


def _ProjectDirName( compdb_dir ):
  base = compdb_dir or os.path.realpath( os.getcwd() )
  return re.sub( r'[^A-Za-z0-9_]', '_', base.lstrip( '/' ) )


def _ConnectionFilePath( compdb_dir ):
  return os.path.join( CONNECTION_FILE_DIR, _ProjectDirName( compdb_dir ),
                       'connection.json' )


def _ReadConnectionFile( compdb_dir ):
  filepath = _ConnectionFilePath( compdb_dir )
  try:
    with open( filepath ) as f:
      data = json.load( f )
    data[ 'hmac_secret' ] = base64.b64decode( data[ 'hmac_secret' ] )
    return ConnectionInfo( **data )
  except ( OSError, KeyError, ValueError, TypeError ):
    return None


def _WriteConnectionFile( compdb_dir, info ):
  filepath = _ConnectionFilePath( compdb_dir )
  os.makedirs( os.path.dirname( filepath ), exist_ok = True )
  data = asdict( info )
  data[ 'hmac_secret' ] = utils.ToUnicode(
    base64.b64encode( data[ 'hmac_secret' ] ) )
  with open( filepath, 'w' ) as f:
    json.dump( data, f )


def _RemoveConnectionFile( compdb_dir ):
  filepath = _ConnectionFilePath( compdb_dir )
  try:
    os.remove( filepath )
  except OSError:
    pass
```

- [ ] **Step 4: 改 `_SetUpServer` 里的调用点**

`_ReadConnectionFile()` → `_ReadConnectionFile( compdb_dir )`、
`_WriteConnectionFile( ... )` → `_WriteConnectionFile( compdb_dir, ... )`、
`_ConnectionFilePath()` → `_ConnectionFilePath( compdb_dir )`，其中 `compdb_dir = _FindCompilationDatabaseDir()`。

> 注：`_ProjectDirName` 必须保留 `compdb_dir or cwd` 回退——`ServerPid` 与 `RestartServer` 在无 compdb
> 时会以 `None` 调用它（实现时发现去掉了回退会崩 `None.lstrip`）。

- [ ] **Step 5: 运行确认通过 + lint + 提交**

```bash
python run_tests.py --skip-build ycm.tests.youcompleteme_test.CompilationDatabaseTest ycm.tests.youcompleteme_test.YouCompleteMeTest
python -m flake8 python/ycm/youcompleteme.py python/ycm/tests/youcompleteme_test.py
git add python/ycm/youcompleteme.py python/ycm/tests/youcompleteme_test.py
git commit -m "refactor: parameterize compdb_dir through connection-file helpers"
```

---

## Task 2: 抽出 `_StartServer(compdb_dir, idle_suicide_seconds)`

把"启动全新 ycmd"那段从 `_SetUpServer` 抽成共享方法，行为不变。`_SetUpServer` 里的复用分支暂时保留原位。

**Files:**
- Modify: `python/ycm/youcompleteme.py:315-378`

**Interfaces:**
- Produces: `_StartServer(self, compdb_dir, idle_suicide_seconds=SERVER_IDLE_SUICIDE_SECONDS) -> ConnectionInfo|None`
  （`compdb_dir is not None` 时写连接文件并返回 `ConnectionInfo`；否则返回 `None`；无 python 解释器返回 `None`）

- [ ] **Step 1: 新增 `_StartServer`**

把 `_SetUpServer` 中 `hmac_secret = os.urandom(...)` 到 `_WriteConnectionFile(...)` 整段搬进新方法，
三处 `self._reuse_project` 换成 `compdb_dir is not None`，`--idle_suicide_seconds` 用参数：

```python
  def _StartServer( self, compdb_dir,
                    idle_suicide_seconds = SERVER_IDLE_SUICIDE_SECONDS ):
    hmac_secret = os.urandom( HMAC_SECRET_LENGTH )
    options_dict = dict( self._user_options )
    options_dict[ 'hmac_secret' ] = utils.ToUnicode(
      base64.b64encode( hmac_secret ) )
    options_dict[ 'server_keep_logfiles' ] = (
        self._user_options[ 'keep_logfiles' ]
        or compdb_dir is not None )

    with NamedTemporaryFile( delete = False, mode = 'w+' ) as options_file:
      json.dump( options_dict, options_file )

    server_port = utils.GetUnusedLocalhostPort()

    BaseRequest.server_location = 'http://127.0.0.1:' + str( server_port )
    BaseRequest.hmac_secret = hmac_secret

    try:
      python_interpreter = paths.PathToPythonInterpreter()
    except RuntimeError as error:
      error_message = (
        f"Unable to start the ycmd server. { str( error ).rstrip( '.' ) }. "
        "Correct the error then restart the server "
        "with ':YcmRestartServer'." )
      self._logger.exception( error_message )
      vimsupport.PostVimMessage( error_message )
      return None

    args = [ python_interpreter,
             paths.PathToServerScript(),
             f'--port={ server_port }',
             f'--options_file={ options_file.name }',
             f'--log={ self._user_options[ "log_level" ] }',
             f'--idle_suicide_seconds={ idle_suicide_seconds }' ]

    if compdb_dir is not None:
      log_dir = os.path.dirname( _ConnectionFilePath( compdb_dir ) )
      os.makedirs( log_dir, exist_ok = True )
      self._server_stdout = os.path.join(
        log_dir, f'ycmd_{ server_port }_stdout.log' )
      self._server_stderr = os.path.join(
        log_dir, f'ycmd_{ server_port }_stderr.log' )
    else:
      self._server_stdout = utils.CreateLogfile(
          SERVER_LOGFILE_FORMAT.format( port = server_port, std = 'stdout' ) )
      self._server_stderr = utils.CreateLogfile(
          SERVER_LOGFILE_FORMAT.format( port = server_port, std = 'stderr' ) )
    args.append( f'--stdout={ self._server_stdout }' )
    args.append( f'--stderr={ self._server_stderr }' )

    if ( self._user_options[ 'keep_logfiles' ]
         or compdb_dir is not None ):
      args.append( '--keep_logfiles' )

    self._server_popen = utils.SafePopen( args, stdin_windows = PIPE,
                                          stdout = PIPE, stderr = PIPE )
    self._reusing_server = False

    if compdb_dir is None:
      return None

    conn = ConnectionInfo( port = server_port,
                           hmac_secret = hmac_secret,
                           pid = self._server_popen.pid,
                           stdout = self._server_stdout,
                           stderr = self._server_stderr )
    _WriteConnectionFile( compdb_dir, conn )
    return conn
```

- [ ] **Step 2: `_SetUpServer` 复用分支末尾改为调 `_StartServer`**

原"启动全新 server"那段删除，复用失败后落到 `self._StartServer( _FindCompilationDatabaseDir() )`；
非复用路径 `self._StartServer( None )`。

- [ ] **Step 3: 运行确认 + lint + 提交**

```bash
python run_tests.py --skip-build ycm.tests.youcompleteme_test
python -m flake8 python/ycm/youcompleteme.py
git add python/ycm/youcompleteme.py
git commit -m "refactor: extract _StartServer from _SetUpServer"
```

---

## Task 3: policy 拆分（Strategy，行为不变）

引入 `ServerPolicy` / `IndividualServerPolicy` / `ReusableServerPolicy`，把 6 个生命周期分歧点委托出去，
`_reuse_project` 布尔彻底移除。同时新增 `g:ycm_reuse_max_project_servers` 选项与
`SECONDARY_IDLE_SUICIDE_SECONDS` 常量（本 Task 只用常量值 300，不实际用）。

**Files:**
- Modify: `python/ycm/youcompleteme.py:80-99,207-222,276-494,893-947`
- Modify: `plugin/youcompleteme.vim`（新增一个选项）

**Interfaces:**
- Produces:
  - `ServerPolicy`：`OnServerSetup(ycm)` / `OnBufferVisit(ycm)` / `OnBufferUnload(ycm, deleted_buffer_number)` /
    `OnVimLeave(ycm)` / `RestartServer(ycm)` / `ServerPid(ycm)`
  - `IndividualServerPolicy` / `ReusableServerPolicy`

- [ ] **Step 1: 新增常量与选项**

`youcompleteme.py:99` 后加：

```python
SECONDARY_IDLE_SUICIDE_SECONDS = 300  # 5 minutes, non-first projects
```

`plugin/youcompleteme.vim`（`reuse_max_memory_mb` 声明之后）加：

```vim
let g:ycm_reuse_max_project_servers =
      \ get( g:, 'ycm_reuse_max_project_servers', 5 )
```

- [ ] **Step 2: 新增 policy 类**

放在 `class YouCompleteMe:` 之前：

```python
class ServerPolicy:
  def OnServerSetup( self, ycm ):
    raise NotImplementedError

  def OnBufferVisit( self, ycm ):
    pass

  def OnBufferUnload( self, ycm, deleted_buffer_number ):
    raise NotImplementedError

  def OnVimLeave( self, ycm ):
    raise NotImplementedError

  def RestartServer( self, ycm ):
    raise NotImplementedError

  def ServerPid( self, ycm ):
    return -1


class IndividualServerPolicy( ServerPolicy ):
  def OnServerSetup( self, ycm ):
    ycm._StartServer( None )

  def OnBufferUnload( self, ycm, deleted_buffer_number ):
    SendEventNotificationAsync( 'BufferUnload', deleted_buffer_number )

  def OnVimLeave( self, ycm ):
    ycm._ShutdownServer()

  def RestartServer( self, ycm ):
    vimsupport.PostVimMessage( 'Restarting ycmd server...' )
    SendShutdownRequest()
    ycm._reusing_server = False
    ycm._SetUpServer()

  def ServerPid( self, ycm ):
    if ycm._server_popen:
      return ycm._server_popen.pid
    return -1


class ReusableServerPolicy( ServerPolicy ):
  def __init__( self ):
    self._active_compdb_dir = None

  def OnServerSetup( self, ycm ):
    compdb_dir = _FindCompilationDatabaseDir()
    if not compdb_dir:
      ycm._StartServer( None )
      return
    conn = _ReadConnectionFile( compdb_dir )
    if conn:
      location = f'http://127.0.0.1:{ conn.port }'
      if _CheckServerHealthy( location, conn.hmac_secret ):
        BaseRequest.server_location = location
        BaseRequest.hmac_secret = conn.hmac_secret
        ycm._server_popen = None
        ycm._server_stdout = conn.stdout
        ycm._server_stderr = conn.stderr
        ycm._reusing_server = True
        ycm._logger.info( 'Reusing existing ycmd server on port %d '
                          '(pid %d)', conn.port, conn.pid )
        self._active_compdb_dir = compdb_dir
        return
    ycm._StartServer( compdb_dir )
    self._active_compdb_dir = compdb_dir

  def OnBufferVisit( self, ycm ):
    filepath = vimsupport.GetCurrentBufferFilepath()
    if filepath in ycm._open_files_lru:
      ycm._open_files_lru.remove( filepath )
    ycm._open_files_lru.append( filepath )
    ycm._EvictOldOpenFiles()

  def OnBufferUnload( self, ycm, deleted_buffer_number ):
    try:
      filepath = vim.buffers[ deleted_buffer_number ].name
      if filepath in ycm._open_files_lru:
        ycm._open_files_lru.remove( filepath )
    except ( KeyError, ValueError ):
      pass

  def OnVimLeave( self, ycm ):
    if self._active_compdb_dir is None:
      ycm._ShutdownServer()

  def RestartServer( self, ycm ):
    vimsupport.PostVimMessage( 'Restarting ycmd server...' )
    SendShutdownRequest()
    _RemoveConnectionFile( _FindCompilationDatabaseDir() )
    ycm._reusing_server = False
    ycm._SetUpServer()

  def ServerPid( self, ycm ):
    if ycm._reusing_server:
      conn = _ReadConnectionFile( _FindCompilationDatabaseDir() )
      return conn.pid if conn else -1
    if ycm._server_popen:
      return ycm._server_popen.pid
    return -1
```

- [ ] **Step 3: 改造 `YouCompleteMe` 委托**

`__init__` 删掉 `self._reuse_project = False`；加 `self._server_policy = None`（占位，
`_SetUpServer` 会重建）。

`_SetUpServer` 尾部（`self._reuse_project = ...` 及之后的复用/启动段）替换为：

```python
    self._reusing_server = False
    self._server_policy = (
        ReusableServerPolicy()
        if self._user_options.get( 'reuse_project_ycmd_server' )
        else IndividualServerPolicy() )
    self._server_policy.OnServerSetup( self )
```

`OnBufferVisit` 尾部（原 `if self._reuse_project:` LRU 段）替换为：

```python
    self._server_policy.OnBufferVisit( self )
```

`OnBufferUnload` 改为：

```python
  def OnBufferUnload( self, deleted_buffer_number ):
    self._server_policy.OnBufferUnload( self, deleted_buffer_number )
```

`OnVimLeave` 改为：

```python
  def OnVimLeave( self ):
    self._server_policy.OnVimLeave( self )
    self._CleanLogfile()
```

`RestartServer` 改为：

```python
  def RestartServer( self ):
    self._server_policy.RestartServer( self )
```

`ServerPid` 改为：

```python
  def ServerPid( self ):
    return self._server_policy.ServerPid( self )
```

- [ ] **Step 4: 运行确认行为不变 + lint + 提交**

```bash
python run_tests.py --skip-build ycm.tests.youcompleteme_test
python -m flake8 python/ycm/youcompleteme.py
git add python/ycm/youcompleteme.py plugin/youcompleteme.vim
git commit -m "refactor: introduce ServerPolicy to replace reuse flag checks"
```

> 说明：fallback 关停已在本 Task 正确处理——未路由到任何项目（`_active_compdb_dir is None`）时
> `OnVimLeave` 关掉 fallback server（它无 connection.json、无法复用）；有 project server 时才全不关。
> 与旧行为一致，无行为差异。

---

## Task 4: 修 cwd —— `ReusableServerPolicy` 按文件路由（单连接）

让路由基于当前 buffer 文件而非启动 cwd。仍单活跃连接：compdb 变化时切换（复用/重启）到新项目，
旧 server 交给 idle_suicide。

**Files:**
- Modify: `python/ycm/youcompleteme.py`（`ReusableServerPolicy`）
- Test: `python/ycm/tests/youcompleteme_test.py`

**Interfaces:**
- Produces: `ReusableServerPolicy._active_compdb_dir`（当前路由到的 compdb_dir 或 None）

- [ ] **Step 1: 写失败测试**

```python
class BufferRoutingTest( TestCase ):
  def _MakeYcm( self ):
    ycm = YouCompleteMe.__new__( YouCompleteMe )
    ycm._logger = MagicMock()
    ycm._open_files_lru = []
    ycm._reusing_server = False
    ycm._server_popen = None
    return ycm

  def test_ReusablePolicy_OnBufferVisit_RoutesToNewCompdb( self ):
    ycm = self._MakeYcm()
    policy = ReusableServerPolicy()
    with patch( 'ycm.youcompleteme._FindCompilationDatabaseDir',
                return_value = '/proj/a' ), \
         patch.object( ycm, '_StartServer' ) as start, \
         patch.object( ycm, '_EvictOldOpenFiles' ), \
         patch( 'ycm.youcompleteme.vimsupport.GetCurrentBufferFilepath',
                return_value = '/proj/a/src/main.cpp' ):
      policy.OnBufferVisit( ycm )
      start.assert_called_once_with( '/proj/a' )
      assert_that( policy._active_compdb_dir, equal_to( '/proj/a' ) )

  def test_ReusablePolicy_OnBufferVisit_NoRouteWhenSameCompdb( self ):
    ycm = self._MakeYcm()
    policy = ReusableServerPolicy()
    policy._active_compdb_dir = '/proj/a'
    with patch( 'ycm.youcompleteme._FindCompilationDatabaseDir',
                return_value = '/proj/a' ), \
         patch.object( ycm, '_StartServer' ) as start, \
         patch.object( ycm, '_EvictOldOpenFiles' ), \
         patch( 'ycm.youcompleteme.vimsupport.GetCurrentBufferFilepath',
                return_value = '/proj/a/src/other.cpp' ):
      policy.OnBufferVisit( ycm )
      start.assert_not_called()
```

> 需 import `ReusableServerPolicy`。

- [ ] **Step 2: 运行确认失败**

Run: `python run_tests.py --skip-build ycm.tests.youcompleteme_test.BufferRoutingTest`
Expected: FAIL —— 无路由 / `_active_compdb_dir` 不存在。

- [ ] **Step 3: 实现**

`ReusableServerPolicy` 加 `__init__` 与路由：

```python
class ReusableServerPolicy( ServerPolicy ):
  def __init__( self ):
    self._active_compdb_dir = None

  def OnServerSetup( self, ycm ):
    compdb_dir = _FindCompilationDatabaseDir()
    if not compdb_dir:
      ycm._StartServer( None )
      return
    conn = _ReadConnectionFile( compdb_dir )
    if conn:
      location = f'http://127.0.0.1:{ conn.port }'
      if _CheckServerHealthy( location, conn.hmac_secret ):
        BaseRequest.server_location = location
        BaseRequest.hmac_secret = conn.hmac_secret
        ycm._server_popen = None
        ycm._server_stdout = conn.stdout
        ycm._server_stderr = conn.stderr
        ycm._reusing_server = True
        ycm._logger.info( 'Reusing existing ycmd server on port %d '
                          '(pid %d)', conn.port, conn.pid )
        self._active_compdb_dir = compdb_dir
        return
    ycm._StartServer( compdb_dir )
    self._active_compdb_dir = compdb_dir

  def OnBufferVisit( self, ycm ):
    filepath = vimsupport.GetCurrentBufferFilepath()
    compdb_dir = _FindCompilationDatabaseDir( filepath )
    if compdb_dir and compdb_dir != self._active_compdb_dir:
      conn = _ReadConnectionFile( compdb_dir )
      if conn and _CheckServerHealthy(
          f'http://127.0.0.1:{ conn.port }', conn.hmac_secret ):
        BaseRequest.server_location = f'http://127.0.0.1:{ conn.port }'
        BaseRequest.hmac_secret = conn.hmac_secret
        ycm._server_popen = None
        ycm._server_stdout = conn.stdout
        ycm._server_stderr = conn.stderr
        ycm._reusing_server = True
      else:
        ycm._StartServer( compdb_dir )
      self._active_compdb_dir = compdb_dir

    if filepath in ycm._open_files_lru:
      ycm._open_files_lru.remove( filepath )
    ycm._open_files_lru.append( filepath )
    ycm._EvictOldOpenFiles()
```

> 说明：Task 4 里"切走就丢"旧 server（不显式关，交给 idle_suicide）。Task 5 会用 dict 记住它以便切回复用。
>
> 注：本 Task 的 `BufferRoutingTest` 在 Task 5 重写——Task 5 把路由收敛到 `_EnsureProjectServer`，原测试
> patch 的 `ycm._EvictOldOpenFiles`（已移入 policy）与 `_StartServer( '/proj/a' )`（改为两参）都不再成立。

- [ ] **Step 4: 运行确认 + lint + 提交**

```bash
python run_tests.py --skip-build ycm.tests.youcompleteme_test.BufferRoutingTest ycm.tests.youcompleteme_test.YouCompleteMeTest
python -m flake8 python/ycm/youcompleteme.py python/ycm/tests/youcompleteme_test.py
git add python/ycm/youcompleteme.py python/ycm/tests/youcompleteme_test.py
git commit -m "fix: route ycmd by buffer filepath instead of startup cwd"
```

---

## Task 5: 多连接并存 + per-project LRU + cap 软淘汰 + idle_suicide 分级

核心改动，全部落在 `ReusableServerPolicy`。用字典记各项目连接、切换复用；per-project LRU；超上限软淘汰；
按是否首个项目分级 idle_suicide。

**Files:**
- Modify: `python/ycm/youcompleteme.py`（`ReusableServerPolicy`）
- Test: `python/ycm/tests/youcompleteme_test.py`

**Interfaces:**
- Produces:
  - `ReusableServerPolicy._ycmd_servers: dict[str, ConnectionInfo]`
  - `ReusableServerPolicy._open_files_lru: dict[str, list[str]]`（per-project）
  - `ReusableServerPolicy._seen_projects: set[str]`（用于 idle_suicide 分级）
  - `ReusableServerPolicy._EnsureProjectServer(ycm, compdb_dir)`

- [ ] **Step 1: 写失败测试**

```python
class MultiServerTest( TestCase ):
  def _MakeYcm( self ):
    ycm = YouCompleteMe.__new__( YouCompleteMe )
    ycm._logger = MagicMock()
    ycm._reusing_server = False
    ycm._server_popen = None
    ycm._user_options = { 'reuse_max_open_files': 20,
                          'reuse_max_memory_mb': 0,
                          'reuse_max_project_servers': 5 }
    return ycm

  def test_EnsureProjectServer_ReusesKnownHealthy( self ):
    ycm = self._MakeYcm()
    policy = ReusableServerPolicy()
    policy._ycmd_servers[ '/proj/a' ] = ConnectionInfo(
        port = 1, hmac_secret = b'x', pid = 10 )
    with patch( 'ycm.youcompleteme._CheckServerHealthy',
                return_value = True ), \
         patch.object( ycm, '_StartServer' ) as start:
      policy._EnsureProjectServer( ycm, '/proj/a' )
      start.assert_not_called()
      assert_that( ycm._reusing_server, equal_to( True ) )

  def test_EnsureProjectServer_StartsWhenMissing( self ):
    ycm = self._MakeYcm()
    policy = ReusableServerPolicy()
    conn = ConnectionInfo( port = 2, hmac_secret = b'y', pid = 20 )
    with patch.object( ycm, '_StartServer', return_value = conn ) as start:
      policy._EnsureProjectServer( ycm, '/proj/b' )
      start.assert_called_once()
      assert_that( policy._ycmd_servers[ '/proj/b' ], equal_to( conn ) )

  def test_EnsureProjectServer_SoftEvictsWhenOverCap( self ):
    ycm = self._MakeYcm()
    policy = ReusableServerPolicy()
    for i in range( 5 ):
      policy._ycmd_servers[ f'/proj/{ i }' ] = ConnectionInfo(
          port = 10 + i, hmac_secret = b'z', pid = 30 + i )
    policy._open_files_lru = { f'/proj/{ i }': [ f'/proj/{ i }/f.cc' ]
                               for i in range( 5 ) }
    conn = ConnectionInfo( port = 99, hmac_secret = b'w', pid = 99 )
    with patch.object( ycm, '_StartServer', return_value = conn ), \
         patch( 'ycm.youcompleteme._RemoveConnectionFile' ) as remove:
      policy._EnsureProjectServer( ycm, '/proj/new' )
      remove.assert_called_once()
      assert_that( len( policy._ycmd_servers ), equal_to( 5 ) )

  def test_EnsureProjectServer_FirstProjectGetsLongIdleSuicide( self ):
    ycm = self._MakeYcm()
    policy = ReusableServerPolicy()
    conn = ConnectionInfo( port = 3, hmac_secret = b'x', pid = 3 )
    with patch.object( ycm, '_StartServer', return_value = conn ) as start:
      policy._EnsureProjectServer( ycm, '/proj/a' )
      first = start.call_args[ 0 ][ 1 ]
      policy._EnsureProjectServer( ycm, '/proj/b' )
      second = start.call_args[ 0 ][ 1 ]
    assert_that( first, equal_to( SERVER_IDLE_SUICIDE_SECONDS ) )
    assert_that( second, equal_to( SECONDARY_IDLE_SUICIDE_SECONDS ) )

  def test_OnVimLeave_ShutsDownFallbackWhenNoProject( self ):
    ycm = self._MakeYcm()
    policy = ReusableServerPolicy()  # _active_compdb_dir is None
    with patch.object( ycm, '_ShutdownServer' ) as shutdown:
      policy.OnVimLeave( ycm )
      shutdown.assert_called_once()

  def test_OnVimLeave_KeepsProjectServers( self ):
    ycm = self._MakeYcm()
    policy = ReusableServerPolicy()
    policy._active_compdb_dir = '/proj/a'
    with patch.object( ycm, '_ShutdownServer' ) as shutdown:
      policy.OnVimLeave( ycm )
      shutdown.assert_not_called()
```

> 需 import `ReusableServerPolicy`、`SERVER_IDLE_SUICIDE_SECONDS`、`SECONDARY_IDLE_SUICIDE_SECONDS`。

- [ ] **Step 2: 运行确认失败**

Run: `python run_tests.py --skip-build ycm.tests.youcompleteme_test.MultiServerTest`
Expected: FAIL —— `_EnsureProjectServer` 不存在。

- [ ] **Step 3: 实现 `_EnsureProjectServer` + cap 软淘汰 + 分级**

`ReusableServerPolicy` 扩展：

```python
  def __init__( self ):
    self._active_compdb_dir = None
    self._ycmd_servers = {}
    self._open_files_lru = {}
    self._seen_projects = set()

  def _EnsureProjectServer( self, ycm, compdb_dir ):
    if compdb_dir == self._active_compdb_dir:
      return

    conn = self._ycmd_servers.get( compdb_dir )
    if not conn:
      conn = _ReadConnectionFile( compdb_dir )
    if conn and _CheckServerHealthy( f'http://127.0.0.1:{ conn.port }',
                                     conn.hmac_secret ):
      self._ycmd_servers[ compdb_dir ] = conn
      self._SetActive( ycm, compdb_dir, conn, reused = True )
      self._seen_projects.add( compdb_dir )
      return

    if compdb_dir not in self._ycmd_servers:
      if len( self._ycmd_servers ) >= self._MaxServers( ycm ):
        self._EvictOneProject( ycm )
      idle_suicide_seconds = (
          SERVER_IDLE_SUICIDE_SECONDS if not self._seen_projects
          else SECONDARY_IDLE_SUICIDE_SECONDS )
      conn = ycm._StartServer( compdb_dir, idle_suicide_seconds )
      if conn is not None:
        self._ycmd_servers[ compdb_dir ] = conn
        self._SetActive( ycm, compdb_dir, conn, reused = False )
        self._seen_projects.add( compdb_dir )

  def _SetActive( self, ycm, compdb_dir, conn, reused ):
    self._active_compdb_dir = compdb_dir
    BaseRequest.server_location = f'http://127.0.0.1:{ conn.port }'
    BaseRequest.hmac_secret = conn.hmac_secret
    ycm._reusing_server = reused
    if reused:
      ycm._server_popen = None
    ycm._server_stdout = conn.stdout
    ycm._server_stderr = conn.stderr

  def _MaxServers( self, ycm ):
    return ycm._user_options.get( 'reuse_max_project_servers', 5 )

  def _EvictOneProject( self, ycm ):
    if not self._ycmd_servers:
      return
    victim = min( self._ycmd_servers,
                  key = lambda d: len( self._open_files_lru.get( d, [] ) ) )
    self._ycmd_servers.pop( victim )
    self._open_files_lru.pop( victim, None )
    _RemoveConnectionFile( victim )
    ycm._logger.info( 'Soft-evicting ycmd server for project %s', victim )
```

- [ ] **Step 4: 重写 `OnBufferVisit` / `OnServerSetup` / `ServerPid` / `RestartServer`**

`OnBufferVisit` 改为按项目维护 LRU：

```python
  def OnBufferVisit( self, ycm ):
    filepath = vimsupport.GetCurrentBufferFilepath()
    compdb_dir = _FindCompilationDatabaseDir( filepath )
    if not compdb_dir:
      return
    self._EnsureProjectServer( ycm, compdb_dir )
    lru = self._open_files_lru.setdefault( compdb_dir, [] )
    if filepath in lru:
      lru.remove( filepath )
    lru.append( filepath )
    self._EvictOldOpenFiles( ycm, compdb_dir )
```

`OnServerSetup` 复用走 `_EnsureProjectServer`：

```python
  def OnServerSetup( self, ycm ):
    compdb_dir = _FindCompilationDatabaseDir()
    if compdb_dir:
      self._EnsureProjectServer( ycm, compdb_dir )
    else:
      ycm._StartServer( None )
```

`_EvictOldOpenFiles` 移到 policy（per-project）：

```python
  def _EvictOldOpenFiles( self, ycm, compdb_dir ):
    lru = self._open_files_lru[ compdb_dir ]
    max_files = ycm._user_options.get( 'reuse_max_open_files', 20 )
    max_memory = ycm._user_options.get( 'reuse_max_memory_mb', 0 )

    def _ShouldEvict():
      if len( lru ) <= 1:
        return False
      if len( lru ) > max_files:
        return True
      if max_memory > 0:
        ycmd_pid = ycm.ServerPid()
        if ycmd_pid > 0 and _GetChildProcessRssMb( ycmd_pid ) > max_memory:
          return True
      return False

    while _ShouldEvict():
      evicted = lru.pop( 0 )
      request_data = BuildRequestData()
      request_data[ 'filepath' ] = evicted
      request_data[ 'event_name' ] = 'BufferUnload'
      BaseRequest().PostDataToHandler( request_data, 'event_notification' )
```

`ServerPid` 改为读 `_active_compdb_dir`：

```python
  def ServerPid( self, ycm ):
    if ycm._server_popen:
      return ycm._server_popen.pid
    if ycm._reusing_server and self._active_compdb_dir:
      conn = self._ycmd_servers.get( self._active_compdb_dir )
      if conn:
        return conn.pid
    return -1
```

`RestartServer` 只重启当前 buffer 的：

```python
  def RestartServer( self, ycm ):
    vimsupport.PostVimMessage( 'Restarting ycmd server...' )
    SendShutdownRequest()
    if self._active_compdb_dir:
      _RemoveConnectionFile( self._active_compdb_dir )
      self._ycmd_servers.pop( self._active_compdb_dir, None )
      self._open_files_lru.pop( self._active_compdb_dir, None )
      self._EnsureProjectServer( ycm, self._active_compdb_dir )
    else:
      ycm._reusing_server = False
      ycm._SetUpServer()
```

> `YouCompleteMe._EvictOldOpenFiles` 与 `YouCompleteMe._open_files_lru`（全局 list）已删除——移入 policy 后成死代码。
> `ReusableServerPolicy.OnBufferUnload` 同步改为按项目从 per-project LRU 移除（用
> `_FindCompilationDatabaseDir( filepath )` 定位项目）。`BufferRoutingTest` 重写为测 `OnBufferVisit` →
> `_EnsureProjectServer` 接线 + fallback（见 Task 4 注）。

- [ ] **Step 5: 运行确认失败→实现→通过 + 全量回归 + lint + 提交**

```bash
python run_tests.py --skip-build ycm.tests.youcompleteme_test.MultiServerTest
python run_tests.py --skip-build ycm.tests.youcompleteme_test
python -m flake8 python/ycm/youcompleteme.py python/ycm/tests/youcompleteme_test.py
git add python/ycm/youcompleteme.py python/ycm/tests/youcompleteme_test.py
git commit -m "feat: keep multiple per-project ycmd servers with soft eviction"
```

---

## Task 6: 手动集成验证（真实多项目）

- [ ] **Step 1: 准备两个项目**

```bash
mkdir -p /tmp/ycm-multi/a/src /tmp/ycm-multi/b/src
echo '[{"directory":"/tmp/ycm-multi/a","command":"cc -c main.cpp","file":"src/main.cpp"}]' \
  > /tmp/ycm-multi/a/compile_commands.json
echo '[{"directory":"/tmp/ycm-multi/b","command":"cc -c main.cpp","file":"src/main.cpp"}]' \
  > /tmp/ycm-multi/b/compile_commands.json
printf 'int main(){}\n' > /tmp/ycm-multi/a/src/main.cpp
printf 'int main(){}\n' > /tmp/ycm-multi/b/src/main.cpp
```

- [ ] **Step 2: 父目录开 vim（复现原 cwd 问题）**

`cd /tmp/ycm-multi && vim`，`:set` 确认 `g:ycm_reuse_project_ycmd_server=1`。

- [ ] **Step 3: 打开 A，确认路由/复用**

`:e a/src/main.cpp`，`:YcmDebugInfo` 记端口；确认 `~/.cache/ycmd/_tmp_ycm-multi_a/connection.json` 存在。

- [ ] **Step 4: 切到 B，确认第二个 ycmd**

`:e b/src/main.cpp`，端口应不同于 A；B 的 connection.json 存在；`ps` 见两个 clangd。

- [ ] **Step 5: 切回 A，确认复用不重启**

`:e a/src/main.cpp`，端口回到 Step 3 记的值。

- [ ] **Step 6: 散文件 fallback**

`:e /tmp/foo.rs`，不报错、不新建项目 ycmd，保持当前连接。

- [ ] **Step 7: `:YcmRestartServer` 只重启当前 buffer 的**

在 B 上 `:YcmRestartServer`，A 端口不变、B 端口变。

- [ ] **Step 8: 退出 vim，确认全不关**

`:qa` 后 `ps` 仍见两个 clangd（idle_suicide 回收）。

- [ ] **Step 9: idle_suicide 分级抽查**

首个项目（A）server 的启动参数应含 `--idle_suicide_seconds=1800`；B 的应含 `=300`
（`ps aux | grep ycmd` 或直接看 `~/.cache/ycmd/_tmp_ycm-multi_b/ycmd_*_stdout.log`）。

---

## Self-Review 记录

- **Spec 覆盖**：§3.1 数据结构→Task 5；§3.2 路由→Task 4/5；§3.3 fallback→Task 4/5；§3.4 启动/复用/退出→Task 3/4/5；
  §4 决策 1 全不关→Task 3/5；决策 2 cap 软淘汰→Task 5；决策 3 idle_suicide 分级→Task 5；决策 4 per-project LRU→Task 5；
  决策 8 只重启当前→Task 5；决策 9 policy 拆分→Task 3；§5 改动范围→全程遵守（新增 plugin 选项已在文件清单注明）。
- **类型一致性**：`_StartServer(compdb_dir, idle_suicide_seconds)` / `_EnsureProjectServer(ycm, compdb_dir)` /
  `_FindCompilationDatabaseDir(filepath)` / `_SetActive(ycm, compdb_dir, conn, reused)` / `_EvictOldOpenFiles(ycm, compdb_dir)`
  签名跨 task 一致。
- **无占位符**：所有新代码在对应 step 给出完整实现。

---

## 实现偏差记录（回写）

实现时发现的计划疏漏，已按实际实现回写上方各 Task：

1. **`OnBufferUnload` 漏列**（Task 3）：原计划只列 5 个分歧点，但 `OnBufferUnload` 也 `if self._reuse_project`。
   已补进 `ServerPolicy` 接口 + 两套 policy + `YouCompleteMe` 委托，Task 3 的分歧点由 5 变 6。
2. **`_ProjectDirName` 需保留 `or cwd` 回退**（Task 1）：计划版本去掉了回退，`ServerPid`/`RestartServer`
   在无 compdb 时以 `None` 调用会崩 `None.lstrip`。已改回。
3. **`BufferRoutingTest` 重写**（Task 5）：原为 Task 4 内联路由写的，Task 5 把路由收敛到
   `_EnsureProjectServer` 后失效（patch 的 `ycm._EvictOldOpenFiles` 已移除、`_StartServer` 变两参）。
   已改写为测 `OnBufferVisit` → `_EnsureProjectServer` 接线 + 无 compdb fallback。
4. **死代码删除**（Task 5）：`YouCompleteMe._EvictOldOpenFiles` 与 `YouCompleteMe._open_files_lru`
   （全局 list）移入 policy 后成死代码，已删除；`OnBufferUnload` 同步改为按项目从 per-project LRU 移除。

---

## 已知问题（已解决）

1. **clangd workspace 状态不随文件切换更新**（2026-09-24 发现，已由第 6 条解决）：原 `Project Directory` /
   `Open Workspaces` 显示 cwd / 文件目录，现通过 `clangd_project_directory` 透传工程根，显示为 marker 目录。

---

## 后续改动（已实现，回写）

5. **`.ycm_project.json` 工程根标记**：`_FindCompilationDatabaseDir` 改名 `_FindProjectRoot`，逻辑改为
   沿父目录链先找 `.ycm_project.json`（工程标记文件，内容暂不解析），找不到再回退 `compile_commands.json`。
   变量 `compdb_dir` / `_active_compdb_dir` 同步改名 `project_root` / `_active_project_root`（全文 55 处）。
   解决 build 目录（`build_debug` / `build_relwithdebinfo`）各带一份 compile_commands.json 被当成
   独立工程、重复起 ycmd 的问题。新增 `test_FindProjectRoot_FromMarker` 单测。
6. **`.ycm_project.json` 的 `compilation_database` 透传**：客户端解析 marker 的 `compilation_database`
   字段（目录，或 compile_commands.json 文件路径，相对 marker 目录解析），归一成目录后作为
   `clangd_compilation_database_dir` 传给 ycmd，ycmd `GetClangdCommand` 加 `--compile-commands-dir=<目录>`；
   marker 目录作为 `clangd_project_directory` 传给 ycmd，`GetProjectDirectory` 用作 LSP `Project Directory`。
   ycmd 侧改动在 submodule（`clangd_completer.py` + `default_settings.json`）。解决"clangd 用错
   compile_commands.json"与"Project Directory 显示 cwd"两个问题。新增客户端单测
   `test_ReadProjectMarker` / `test_GetCompilationDatabaseDir` 等 5 个。
7. **文件级 LRU / 内存上限移到 ycmd 侧**（设计修正，2026-09-24）：
   - **问题**：`_EvictOpenFiles` + `reuse_max_open_files` / `reuse_max_memory_mb` 是客户端（per-vim）
     的 LRU/RSS 控制，管不住共享 ycmd 的总量。
   - **ycmd 侧（submodule）**：
     - `default_settings.json` 新增 `lsp_max_open_files`(20) / `lsp_max_memory_mb`(0)，及
       `clangd_max_open_files` / `clangd_max_memory_mb`（空串 = 继承 lsp 值，供 clangd 单独调）。
     - `LanguageServerCompleter.GetMaxOpenFiles()` / `GetMaxMemoryMb()` 读 `lsp_*`；
       `ClangdCompleter` 覆盖为 `clangd_*` 非空则用、否则回退基类。
     - `ServerFileState` 加 `last_used`；`ServerReset` 加 `_file_access_counter`；
       `_RefreshFileContentsUnderLock` 每次 bump `last_used`；`_ServerProcessRssMb()` 读
       `/proc/<pid>/statm`；`_EvictFilesUnderLock()` 在 `_UpdateServerWithFileContents` 末尾按
       `max_open_files` / `max_memory` 逐出 LRU 最久未用的文件（`_PurgeFileFromServer` → `didClose`）。
     - 单测：`language_server_completer_test` 的 GetMaxFiles/Evict 系列 + `clangd/utilities_test`
       的 clangd override 系列。
   - **客户端（YCM）**：删 `_EvictOpenFiles` / `_GetChildProcessRssMb` /
     `reuse_max_open_files` / `reuse_max_memory_mb` 选项与 `g:ycm_reuse_max_*` 声明、`file-lru` 子命令。
     **保留** per-project `_open_files_lru` 计数，仅供 `_EvictOneProject` 淘汰与 `YcmDebugInfo projects`
     的 `files=N` 展示；文件级**驱逐动作**已完全移到 ycmd（客户端不再发 `BufferUnload`）。
     `reuse_max_project_servers`（项目级上限）保留。

     **为什么两侧各留一层 LRU（不重叠）：**
     - **ycmd 的文件级 LRU** 管「单个服务器内部该 `didClose` 哪些文件」。clangd 的 AST + 动态索引内存、
       以及 `didOpen` 文件状态都在 ycmd 服务器侧，且一个 ycmd 会被多个 vim 客户端共享——只有服务器侧
       看得到**文件总量**（客户端是 per-vim 的，管不住 N×20）。所以内存上限只能放 ycmd。
     - **YCM 的项目级计数** 管「该淘汰哪个项目（=哪个 ycmd server）」。**项目**是客户端路由出来的概念
       （`project_root` → server 连接），ycmd 服务器**不知道项目**（它只看到一堆文件）；「哪个项目的
       buffer 最少、最像临时打开」这个归属信息只有客户端有。所以项目级淘汰必须在客户端做。
     - 一句话：**文件归 ycmd（它拥有文件状态与内存）、项目归 YCM（它拥有项目路由与 buffer 归属）**。
       两者不重叠：ycmd 不感知项目，YCM 也不再直接发 `didClose`。

---
