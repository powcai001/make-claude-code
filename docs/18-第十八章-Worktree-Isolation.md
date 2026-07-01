# 第 18 章：Worktree Isolation

> 📦 **本项目源码**：https://github.com/powcai001/make-claude-code
> 📄 **本章代码**：[`code/s18_worktree_isolation.py`](https://github.com/powcai001/make-claude-code/blob/main/code/s18_worktree_isolation.py)


## 本章解决什么问题？

前面 17 天的 Agent 几乎所有工具都跑在同一个 `WORKDIR` 里。`bash`、`write_file`、`edit_file` 直接改主工作区。

这对简单任务没问题，但当你想让 Agent 做一些“有风险”或“试探性”的事情时就会出问题：

- 让它试一个重构方案，但又怕把当前文件改乱。
- 让它在一个隔离分支里跑实验，跑完再决定要不要合并。
- 让一个 subagent 在独立目录里验证想法，不污染主仓库。

如果所有改动都直接落到主工作区，用户就得时刻盯着、随时回滚。这违背了“先在沙盒里试一下”的工程直觉。

所以 Day 18 我实现一个最小版 Worktree Isolation：

> 让 Harness 可以在 `.agent_worktrees/` 下创建隔离工作目录，task / subagent 可以指定在其中运行，bash 的 cwd 临时切到隔离目录，结束后再恢复主工作区。

## 核心概念

Day 18 的流程是：

```text
worktree_create -> WorktreeRecord(wt-0001)
  -> task(worktree_id="wt-0001")
  -> CwdStack 切到隔离目录
  -> subagent 运行
  -> 结束后恢复主工作区
  -> worktree_remove 清理
```

这里有三个关键概念。

第一，WorktreeRecord。

每个隔离目录都有 id、路径、状态、是否 git worktree、base_ref 和关联的 task_ids。

第二，CwdStack。

这是本章最关键的工程点。Agent 全局只有一个 `WORKDIR`，但我希望隔离任务运行时，`bash` 的 cwd 临时变成 worktree 路径。我实现了一个线程局部的 cwd 栈，进入 worktree 时压栈，结束时弹栈，默认仍然是主工作区。

第三，关联但不强行切换。

`worktree_id` 是可选的。不传时，task 行为和以前完全一致；传了才会进入隔离模式。这让 worktree 是“按需使用”的能力，而不是默认行为。

## 我的实现

完整实现见：`code/s18_worktree_isolation.py`

Day 18 继续基于 Day 17，所以 Autonomous Agents、Team Protocols、Agent Teams、Cron Scheduler、Background Tasks、Task System、Error Recovery、System Prompt、Memory、Skill、Compact 等机制都保留。新增内容集中在 worktree 记录、线程局部 cwd 和 worktree 工具。

### WorktreeRecord

先定义状态和记录：

```python
WorktreeStatus = Literal["open", "removed"]

class WorktreeRecord(TypedDict):
    """一个隔离工作目录的记录。"""

    id: str
    path: str
    status: WorktreeStatus
    created_at: str
    is_git_worktree: bool
    base_ref: str | None
    task_ids: list[str]
```

全局状态放在自己的字典和锁里：

```python
WORKTREES: dict[str, WorktreeRecord] = {}
WORKTREE_LOCK = threading.RLock()
NEXT_WORKTREE_ID = 1
WORKTREES_ROOT = WORKDIR / ".agent_worktrees"
```

### 创建隔离目录

`new_worktree_record()` 创建目录并记录：

```python
def new_worktree_record(use_git: bool = False, base_ref: str | None = None) -> WorktreeRecord:
    ...
    target = WORKTREES_ROOT / wt_id
    if use_git:
        ref = base_ref or detect_base_ref() or "HEAD"
        try:
            subprocess.run(
                ["git", "worktree", "add", "-b", f"agent/{wt_id}", str(target), ref],
                ...
                check=True,
            )
            is_git = True
            chosen_ref = ref
        except (FileNotFoundError, subprocess.SubprocessError):
            target.mkdir(parents=True, exist_ok=True)
    else:
        target.mkdir(parents=True, exist_ok=True)
```

我故意做成“优雅降级”：如果指定了 `use_git` 但当前不是 git 仓库，就退回普通目录。这样 worktree 在任何项目里都能用。

### 线程局部 CwdStack

这是本章最关键的工程点：

```python
class CwdStack:
    """线程局部的 cwd 栈：子 Agent 进入 worktree 时压栈，结束时弹栈。"""

    def __init__(self) -> None:
        self._local = threading.local()

    def current(self) -> Path:
        stack = self._stack()
        return stack[-1] if stack else WORKDIR

    @contextmanager
    def use(self, path: Path):
        """临时切换当前线程的工作目录到 path。"""
        self._stack().append(path)
        try:
            yield
        finally:
            self._stack().pop()


_CWD_STACK = CwdStack()
```

`run_bash` 改为从 `_CWD_STACK.current()` 取 cwd：

```python
def run_bash(command: str) -> str:
    cwd = _CWD_STACK.current()
    result = subprocess.run(command, shell=True, cwd=cwd, ...)
```

为什么用线程局部？因为 Day 13 之后有后台任务，多个 subagent 可能并发跑。如果用全局变量切换 cwd，一个任务会影响另一个。线程局部保证每个任务在自己的线程里看到自己的 cwd。

### task 支持 worktree

`run_task` 新增 `worktree_id` 参数：

```python
def run_task(description: str, prompt: str, run_in_background: bool = False, worktree_id: str | None = None) -> str:
```

它做三件事：

1. 校验 worktree 存在且是 `open`。
2. 在 subagent prompt 里告诉它运行在隔离目录。
3. 用 `_CWD_STACK.use(path)` 包裹 `run_subagent`，让 bash 在隔离目录执行。

同步任务：

```python
cwd_ctx = _CWD_STACK.use(Path(worktree_path)) if worktree_path else contextlib.nullcontext()
try:
    with cwd_ctx:
        report = run_subagent(record)
```

后台任务也通过 `run_background_task(record, worktree_path)` 接收 worktree 路径，在新线程里压栈：

```python
def run_background_task(record: TaskRecord, worktree_path: str | None = None) -> None:
    cwd_ctx = _CWD_STACK.use(Path(worktree_path)) if worktree_path else contextlib.nullcontext()
    try:
        with cwd_ctx:
            run_subagent(record)
```

这样无论同步还是后台，worktree 内的任务都会在隔离目录里运行。

### 安全清理

`worktree_remove` 会做几层保护：

```python
def run_worktree_remove(wt_id: str) -> str:
    ...
    target = Path(record["path"])
    if record["is_git_worktree"]:
        subprocess.run(["git", "worktree", "remove", "--force", str(target)], ...)
    if target.exists() and is_inside_workspace(target):
        shutil.rmtree(target, ignore_errors=True)
```

`is_inside_workspace()` 保证只能删 `.agent_worktrees/` 下的目录，避免 worktree 路径逃逸到工作区外造成误删。

### worktree 工具

Day 18 新增四个工具：

- `worktree_create`：创建隔离目录。
- `worktree_list`：列出所有 worktree。
- `worktree_read`：读取某个 worktree 详情。
- `worktree_remove`：移除并清理。

同时 `task` 工具的 schema 加了 `worktree_id`：

```python
"worktree_id": {
    "type": "string",
    "description": "Optional isolated worktree id; the subagent runs with cwd inside that worktree.",
}
```

## 我踩的坑

### 坑 1：不能直接改全局 WORKDIR

一开始我想：进入 worktree 时直接 `WORKDIR = worktree_path`。

但全局变量在多线程下会互相串。后台任务 A 进了 worktree，把 WORKDIR 改了，主循环里的任务 B 就会跑错目录。

所以必须用线程局部栈。`_CWD_STACK` 让每个线程独立，默认回退到主 WORKDIR。

### 坑 2：后台任务的 cwd 不能在主线程压栈

后台任务跑在另一个线程里。如果在主线程压栈，新线程的 thread-local 栈是空的。

所以我把 `worktree_path` 作为参数传给 `run_background_task`，让它在自己的线程里压栈。

### 坑 3：清理必须限制在工作区内

`shutil.rmtree` 很危险。如果 worktree 路径被构造得很奇怪，可能删错地方。

所以我加了 `is_inside_workspace()` 校验，只有路径确实落在 `WORKDIR/.agent_worktrees/` 下才允许删。

## 对应真实 Claude Code 的哪里

真实 Claude Code / Codex 类工具里，Worktree Isolation 通常体现在：

- `git worktree` 作为实验分支的载体。
- 子 Agent 或长任务在独立目录运行，避免污染主仓库。
- 实验结束后合并、丢弃或清理。
- Bash 工具按当前 worktree 切换 cwd。
- 隔离目录的权限、清理和生命周期管理。

我这个最小实现对应的是：

```text
worktree_create -> task(worktree_id) -> thread-local cwd -> worktree_remove
```

和真实系统相比，它还很简单：

- 隔离只覆盖 bash 的 cwd，write_file/edit_file 仍以主工作区为根。
- 没有 worktree 之间的文件合并或 diff。
- 没有自动清理和过期回收。
- git worktree 是可选的，非 git 项目只能用普通目录。
- 没有把 worktree 状态持久化。

但核心思想已经出现：

> 隔离执行的本质，是让一段任务运行在一个受控的、可丢弃的工作目录里，而不是直接改主工作区。

## 小结

Day 18 的关键词是：**隔离执行**。

通过 `WorktreeRecord`、`CwdStack`、`worktree_create` 和 `task(worktree_id=...)`，Agent Harness 开始支持“先在沙盒里试一下”。这为后面更安全的自主重构、并行实验和回滚式工作流打下了基础。