# 第 13 章：Background Tasks

> 📦 **本项目源码**：https://github.com/powcai001/make-claude-code
> 📄 **本章代码**：[`code/s13_background_tasks.py`](https://github.com/powcai001/make-claude-code/blob/main/code/s13_background_tasks.py)


## 本章解决什么问题？

第 12 章 把 `task` 从一次性子 Agent 调用升级成了 Task System：每个子任务都有 `task_id`、状态、轮次、结果和错误记录。

但 第 12 章 仍然有一个明显限制：任务是同步执行的。主 Agent 一调用 `task`，就会阻塞在那里，直到子 Agent 完成调查并返回报告。

这对短任务没问题，但真实使用 Agent 时，经常会遇到更长的调查：

- 扫一遍大型代码库。
- 让子 Agent 比较多个实现方案。
- 跑一个耗时验证。
- 后台观察某个日志或命令结果。

如果主 Agent 必须一直等，交互体验会很差。用户也不能在等待期间继续问问题、调整计划或查看其它状态。

所以 第 13 章 我在 第 12 章 的 Task System 上继续加一层：Background Tasks。

> Background Tasks 让子 Agent 可以在后台线程中运行，主 Agent 立即拿到 `task_id`，之后通过 `task_list`、`task_read`、`task_wait` 查询状态和结果。

## 核心概念

第 13 章 的流程是：

```text
task(run_in_background=True)
  -> create TaskRecord
  -> start daemon thread
  -> return task_id immediately
  -> task_list / task_read / task_wait
```

这里有三个关键概念。

第一，后台运行。

`task` 新增 `run_in_background` 参数。为 `true` 时，Harness 不再等待子 Agent 完成，而是启动一个后台线程，然后马上把 `task_id` 返回给主 Agent。

第二，线程安全状态。

既然后台线程会更新任务状态，主线程也会读取任务状态，就必须加锁。我用 `threading.RLock()` 保护 `TASKS` 和任务记录更新。

第三，等待工具。

后台任务不代表永远不等。有时候主 Agent 需要“等它最多 30 秒，如果完成就读结果，没完成就返回当前状态”。所以我加了 `task_wait`。

## 我的实现

完整实现见：`code/s13_background_tasks.py`

第 13 章 继续基于 第 12 章，所以 Task System、Error Recovery、System Prompt、Memory、Skill、Compact 等机制都保留。新增内容集中在后台线程和状态查询。

### TaskRecord 增加 background 字段

第 12 章 的 `TaskRecord` 已经记录了任务生命周期。第 13 章 我只加了一个字段：

```python
class TaskRecord(TypedDict):
    """Task System 维护的子任务生命周期记录。"""

    id: str
    description: str
    prompt: str
    status: TaskStatus
    created_at: str
    started_at: str | None
    finished_at: str | None
    turns: int
    result: str
    error: str | None
    background: bool
```

这个字段用来区分同步任务和后台任务。`format_task_record()` 也会显示它：

```python
f"background: {record.get('background', False)}"
```

### 全局线程表和锁

后台任务需要保存线程对象，也需要保护共享状态：

```python
TASKS: dict[str, TaskRecord] = {}
TASK_THREADS: dict[str, threading.Thread] = {}
TASK_LOCK = threading.RLock()
NEXT_TASK_ID = 1
```

这里我没有引入复杂队列，只用 Python 标准库线程。因为本章目标不是做生产级调度器，而是把“任务可以在后台运行”这个抽象打通。

创建任务时写入 `TASKS`：

```python
def new_task_record(description: str, prompt: str, background: bool = False) -> TaskRecord:
    ...
    record: TaskRecord = {
        "id": task_id,
        "description": description.strip(),
        "prompt": prompt.strip(),
        "status": "queued",
        "created_at": now,
        "started_at": None,
        "finished_at": None,
        "turns": 0,
        "result": "",
        "error": None,
        "background": background,
    }
    with TASK_LOCK:
        TASKS[task_id] = record
    return record
```

更新任务完成状态时也加锁：

```python
def finish_task(record: TaskRecord, status: TaskStatus, result: str, error: str | None = None) -> None:
    """更新子任务最终状态。"""
    with TASK_LOCK:
        record["status"] = status
        record["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        record["result"] = result[:50_000]
        record["error"] = error
```

### 后台线程入口

后台任务的线程入口很小：

```python
def run_background_task(record: TaskRecord) -> None:
    """后台线程入口：运行子 Agent 并把最终状态写回 TaskRecord。"""
    try:
        run_subagent(record)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        finish_task(record, "failed", error, error=error)
```

它不返回结果给主 Agent，而是把结果写回 `TaskRecord`。这就是后台任务和同步任务的关键差异。

### task 支持 run_in_background

`run_task()` 新增第三个参数：

```python
def run_task(description: str, prompt: str, run_in_background: bool = False) -> str:
    """主 Agent 通过 task 工具启动一个可同步或后台运行的子 Agent。"""
```

如果是后台任务，就创建线程并立即返回：

```python
record = new_task_record(description, prompt, background=bool(run_in_background))
if run_in_background:
    thread = threading.Thread(
        target=run_background_task,
        args=(record,),
        name=f"background-{record['id']}",
        daemon=True,
    )
    with TASK_LOCK:
        TASK_THREADS[record["id"]] = thread
    thread.start()
    return (
        f"Started background task {record['id']} for {description!r}.\n"
        f"Use task_list to poll status, task_read with task_id={record['id']!r} "
        "to inspect details, or task_wait to block until it finishes."
    )
```

如果不是后台任务，就沿用 第 12 章 的同步执行逻辑。

### task_wait

第 12 章 已经有 `task_list` 和 `task_read`。第 13 章 新增 `task_wait`：

```python
def run_task_wait(task_id: str, timeout_seconds: float = 30.0) -> str:
    """工具：等待一个后台任务结束，或在超时后返回当前状态。"""
    with TASK_LOCK:
        record = TASKS.get(task_id)
        thread = TASK_THREADS.get(task_id)
    if record is None:
        return f"Error: task {task_id!r} not found."
    if thread is None:
        return format_task_record(record, include_prompt=True)

    timeout = max(0.0, min(float(timeout_seconds), 300.0))
    thread.join(timeout=timeout)
    if thread.is_alive():
        return f"Task {task_id} is still running after waiting {timeout:g}s.\n\n{format_task_record(record)}"
    return format_task_record(record, include_prompt=True)
```

它做了两件事：

1. 如果任务还在跑，最多等 `timeout_seconds` 秒。
2. 如果超时仍未完成，就返回当前状态，而不是一直卡死。

我把等待时间限制在最多 300 秒，避免模型传一个离谱的超长等待值。

### 工具 schema

`task` 工具增加了 `run_in_background`：

```python
"run_in_background": {
    "type": "boolean",
    "description": "If true, start the task in a background thread and return immediately.",
}
```

同时注册 `task_wait`，并让 Subagent 也能查看任务状态：

```python
"task_wait": lambda **kwargs: run_task_wait(kwargs["task_id"], kwargs.get("timeout_seconds", 30.0)),
```

这样主 Agent 可以：

1. 启动后台任务。
2. 继续做其它事。
3. 稍后 `task_list` 看状态。
4. 必要时 `task_wait` 等结果。

## 我踩的坑

### 坑 1：后台任务不能直接返回报告

同步 `task` 的返回值就是报告，但后台任务启动后报告还不存在。

所以后台模式必须改变返回语义：返回的不是最终结果，而是 `task_id` 和后续查询方式。

这也是为什么 第 12 章 先做 `TaskRecord` 很重要。如果没有任务记录，后台任务启动后就没有地方保存结果。

### 坑 2：共享状态必须加锁

一开始我想直接让线程写 `TASKS`。但主线程可能同时调用 `task_list` 或 `task_read`。

虽然 CPython 的某些字典操作是原子的，但任务记录是多字段更新：`status`、`finished_at`、`result`、`error`。如果不加锁，读取方可能看到半更新状态。

所以 第 13 章 我加了 `TASK_LOCK`，在创建、读取、更新任务时都使用它。

### 坑 3：后台任务不能无限等待

如果 `task_wait` 没有超时，模型可能让 Harness 永远卡住。这样就违背了 Background Tasks 的初衷。

所以 `task_wait` 有默认 30 秒等待，并把最大等待限制在 300 秒。超时后返回“仍在运行”的状态，让模型决定下一步。

## 小结

第 13 章 的关键词是：**非阻塞执行**。

**对照真实 Claude Code**：真实 Claude Code / Codex 类工具里，Background Tasks 通常体现在几类能力里：

- Bash 工具的后台运行模式。
- 长任务启动后返回 task id。
- 后续查询输出、状态或等待完成。
- 后台任务的日志、取消、清理和超时控制。
- 主 Agent 不必阻塞在长时间工具或子 Agent 调查上。

我这个最小实现对应的是：

```text
run_in_background -> thread -> TaskRecord -> task_list/task_read/task_wait
```

和真实系统相比，它还很简单：

- 使用本地线程，而不是进程池、任务队列或事件循环。
- 没有取消任务。
- 没有实时日志流。
- 没有后台任务数量限制。
- 没有进程级隔离。
- 没有持久化任务状态。

但核心思想已经出现：

> 后台任务的本质，是把“启动执行”和“读取结果”拆开，让 Harness 管理中间状态。


第 12 章 让子任务可跟踪；第 13 章 让子任务可以不阻塞主 Agent。

有了 `run_in_background` 和 `task_wait`，Task System 开始具备真实 Harness 的味道：主 Agent 可以启动长任务、继续交互、按需查询状态，再在合适的时候读取结果。
