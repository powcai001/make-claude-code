# 第 12 章：Task System

> 📦 **本项目源码**：https://github.com/powcai001/make-claude-code
> 📄 **本章代码**：[`code/s12_task_system.py`](https://github.com/powcai001/make-claude-code/blob/main/code/s12_task_system.py)


## 本章解决什么问题？

第 06 章 我已经做过一个 `task` 工具：主 Agent 可以把一段研究任务交给子 Agent，子 Agent 用自己的上下文调查完，再把报告返回给主 Agent。

这个版本能跑，但还很粗糙。它本质上只是：

```text
task(description, prompt) -> run_subagent() -> report string
```

一旦任务多起来，就会出现几个问题：

1. 主 Agent 只拿到一段报告，不知道这个任务的生命周期。
2. 子任务失败后，很难追踪失败发生在哪个任务里。
3. 多个子任务之间没有 ID，后续无法引用“刚才那个任务”。
4. 用户和模型都看不到任务列表，只能依赖聊天历史。

真实 Agent 里的 Task 不只是“再开一个模型调用”。它更像一个由 Harness 管理的小型任务系统：创建任务、分配上下文、限制工具、记录状态、保存结果，再把结果交还给主 Agent。

所以 第 12 章 我把 Subagent 调用升级成一个最小 Task System。

> Task System 管理子 Agent 的生命周期，让任务从一次性函数调用变成可跟踪、可查看、可复盘的结构化对象。

## 核心概念

第 12 章 的流程是：

```text
task tool call
  -> new TaskRecord
  -> run isolated subagent
  -> update status / turns / result
  -> task_list / task_read
```

这里有三个关键概念。

第一，TaskRecord。

每个子任务都有自己的 ID、状态、创建时间、开始时间、结束时间、轮次、结果和错误。

第二，生命周期。

任务不再只有“返回字符串”这一种状态，而是会经历：

```text
queued -> running -> completed / failed
```

第三，可观察性。

我新增了 `task_list` 和 `task_read` 两个工具，让主 Agent 或用户可以查看当前会话内所有子任务，以及某个任务的完整记录。

## 我的实现

完整实现见：`code/s12_task_system.py`

第 12 章 继续基于 第 11 章，所以 Error Recovery、System Prompt、Memory、Skill、Compact 等机制都保留。新增内容集中在任务记录和任务查询。

### TaskRecord

首先定义任务状态和任务记录：

```python
TaskStatus = Literal["queued", "running", "completed", "failed"]

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
```

然后用两个全局变量保存当前会话内的任务：

```python
TASKS: dict[str, TaskRecord] = {}
NEXT_TASK_ID = 1
```

这个实现没有做持久化，也没有并发队列。它的目标是先把抽象跑通：每个子 Agent 调用都必须有一个可追踪的记录。

### 创建任务

`new_task_record()` 负责创建任务 ID 和初始状态：

```python
def new_task_record(description: str, prompt: str) -> TaskRecord:
    """创建一个带唯一 ID 的子任务记录。"""
    global NEXT_TASK_ID
    task_id = f"task-{NEXT_TASK_ID:04d}"
    NEXT_TASK_ID += 1
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
    }
    TASKS[task_id] = record
    return record
```

这里的 ID 很简单：`task-0001`、`task-0002`。真实系统可能用 UUID 或数据库主键，但对最小实现来说，可读的顺序 ID 更方便调试。

### 运行任务

第 11 章 里 `run_subagent()` 接收的是 `description` 和 `prompt`。

第 12 章 改成接收一个 `TaskRecord`：

```python
def run_subagent(record: TaskRecord, max_turns: int = 6) -> str:
    """启动一个独立上下文的子 Agent，并把生命周期写入 TaskRecord。"""
    description = record["description"]
    prompt = record["prompt"]
    ...
    record["status"] = "running"
    record["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
```

每轮模型调用时更新轮次：

```python
for turn in range(1, max_turns + 1):
    record["turns"] = turn
    response = call_model_with_retries(...)
```

正常结束时写入结果：

```python
if response.stop_reason != "tool_use":
    result = (last_text or "Subagent finished without text output.")[:50_000]
    finish_task(record, "completed", result)
    return result
```

超过最大轮次时标记失败：

```python
result = (
    f"Error: subagent reached max_turns={max_turns} before finishing.\n\n"
    f"Last partial output:\n{suffix}"
)[:50_000]
finish_task(record, "failed", result, error=f"max_turns={max_turns}")
return result
```

### task 工具

主 Agent 调用 `task` 时，现在会先创建记录，再运行子 Agent：

```python
def run_task(description: str, prompt: str) -> str:
    """主 Agent 通过 task 工具启动一个受 Task System 跟踪的子 Agent。"""
    if not isinstance(description, str) or not description.strip():
        return "Error: description must be a non-empty string."
    if not isinstance(prompt, str) or not prompt.strip():
        return "Error: prompt must be a non-empty string."

    record = new_task_record(description, prompt)
    print(f"\033[35m[task:start] {record['id']} {description}\033[0m")
    try:
        report = run_subagent(record)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        finish_task(record, "failed", error, error=error)
        print(f"\033[35m[task:failed] {record['id']}\033[0m")
        return attach_recovery_hint("task", f"Error: task {record['id']} failed: {error}")

    print(f"\033[35m[task:done] {record['id']}\033[0m")
    return (
        f"Task {record['id']} completed for {description!r}.\n"
        f"Use task_read with task_id={record['id']!r} to inspect the full record.\n\n"
        f"Subagent report:\n{report}"
    )
```

注意这里和 第 11 章 的 Error Recovery 接上了：如果子任务抛异常，会被标记为 `failed`，并通过 `attach_recovery_hint()` 返回恢复建议。

### task_list 和 task_read

为了让任务系统可观察，我加了两个工具。

`task_list`：

```python
def run_task_list() -> str:
    """工具：列出当前会话内的子任务状态。"""
    if not TASKS:
        return "No subagent tasks have been created yet."
    return "\n\n".join(format_task_record(record) for record in TASKS.values())
```

`task_read`：

```python
def run_task_read(task_id: str) -> str:
    """工具：读取某个子任务的完整记录。"""
    record = TASKS.get(task_id)
    if record is None:
        return f"Error: task {task_id!r} not found."
    return format_task_record(record, include_prompt=True)
```

它们都被注册进主 Agent 和 Subagent 可用的工具里。这样主 Agent 可以在后续轮次里说：“读取 `task-0001` 的完整调查结果”，而不是只能在聊天历史里翻找。

## 我踩的坑

### 坑 1：一开始把 Task 和 Todo 混在一起

TodoWrite 和 Task System 都叫“任务”，但它们不是一回事。

- Todo 是主 Agent 对当前工作流的计划。
- Task 是 Harness 启动的子 Agent 执行单元。

Todo 可以写“调查路由实现”；Task 则是真正启动一个隔离上下文去调查路由实现。

如果把它们混在一起，Todo 列表会被子任务生命周期污染，Task 结果也会缺少执行细节。所以 第 12 章 我单独引入 `TASKS`，不复用 `TODOS`。

### 坑 2：只返回报告不够

第 06 章 的 `task` 工具只返回子 Agent 报告。

但真实使用时，主 Agent 经常需要知道：这个报告来自哪个任务？跑了几轮？有没有失败？能不能稍后再看？

所以 第 12 章 的 `run_task()` 返回里明确包含任务 ID，并提示可以用 `task_read` 查看完整记录。

### 坑 3：先做同步生命周期，不急着做并发

看到 Task System 很容易想直接实现后台任务、并发调度、取消任务、任务队列。

但这会一下子引入很多复杂度：线程安全、输出合并、权限交互、取消语义、错误传播。

所以我先保持同步执行，只把生命周期记录打通。这样下一步要加后台运行时，已经有 `TaskRecord` 这个稳定抽象可以承载状态变化。

## 小结

第 12 章 的关键词是：**任务生命周期**。

**对照真实 Claude Code**：真实 Claude Code / Codex 类工具里的 Task System 通常对应这些能力：

- 启动一个独立上下文的子 Agent。
- 给子 Agent 限定工具和权限。
- 主 Agent 只接收最终报告，而不是完整内部对话。
- Harness 记录任务状态、日志、错误和结果。
- 未来可能支持后台运行、取消、并发、进度事件。

我这个最小实现对应的是：

```text
Task tool -> isolated subagent -> tracked TaskRecord -> task_list/task_read
```

和真实系统相比，它还很简单：

- 仍然是同步执行，没有后台任务。
- 任务记录只保存在内存里，没有持久化。
- 没有取消任务和进度事件。
- 没有任务优先级和队列调度。
- 没有并发安全处理。

但核心思想已经出现：

> Task 不是普通函数调用，而是由 Harness 管理的可追踪执行单元。


Subagent 解决“让另一个模型上下文去调查问题”；Task System 解决“如何管理这些调查任务”。

有了 `TaskRecord`、`task_list` 和 `task_read`，子 Agent 的执行结果不再只是一次性字符串，而是可以被主 Agent 和用户反复查看、引用和复盘的结构化记录。
