# 第 14 章：Cron Scheduler

## 本章解决什么问题？

Day 13 让 Task 可以放到后台线程里跑。主 Agent 不必一直等待子 Agent 调查完成，而是先拿到 `task_id`，之后用 `task_list`、`task_read`、`task_wait` 查看状态。

但这仍然是“用户主动触发”的任务模型。真实 Agent 如果要做长期代理，还需要另一类能力：**按时间自动触发任务**。

比如：

- 每隔 10 分钟检查一次测试日志。
- 每小时总结一次后台任务状态。
- 每天扫描一次项目 TODO。
- 在长时间重构期间周期性运行只读巡检。

这些任务不应该靠用户一遍遍输入，也不应该写成阻塞循环卡住主 Agent。所以 Day 14 我在 Background Tasks 之上增加一个最小 Cron Scheduler。

> Cron Scheduler 把“用户主动发起任务”扩展成“Harness 按时间触发任务”：到期后自动创建后台 Task，并把触发记录保存在 CronJob 里。

## 核心概念

Day 14 的流程是：

```text
cron_add(interval_seconds)
  -> CronJob
  -> scheduler loop / cron_tick
  -> trigger background Task
  -> task_list / task_read
```

这里有三个关键概念。

第一，CronJob。

周期任务不是 Task 本身，而是“生成 Task 的规则”。它记录间隔、下一次运行时间、运行次数、最大运行次数，以及每次触发出来的 `task_id`。

第二，Scheduler Loop。

进程内有一个轻量后台线程，定期扫描到期的 CronJob。发现到期任务后，就调用 Day 13 的后台 Task 机制。

第三，手动 tick。

为了测试和调试，我没有只依赖后台线程，还加了 `cron_tick`。它会手动扫描一次到期任务，便于验证调度逻辑。

## 我的实现

完整实现见：`code/s14_cron_scheduler.py`

Day 14 继续基于 Day 13，所以 Background Tasks、Task System、Error Recovery、System Prompt、Memory、Skill、Compact 等机制都保留。新增内容集中在 CronJob、调度线程和 cron 工具。

### CronJob

先定义周期任务状态和记录：

```python
CronStatus = Literal["enabled", "paused"]

class CronJob(TypedDict):
    """Cron Scheduler 维护的周期任务记录。"""

    id: str
    description: str
    prompt: str
    interval_seconds: float
    status: CronStatus
    created_at: str
    next_run_at: float
    last_run_at: float | None
    run_count: int
    max_runs: int | None
    task_ids: list[str]
```

这里最重要的是区分两个 ID：

- `cron_id`：周期任务本身，比如 `cron-0001`。
- `task_id`：某次触发产生的后台任务，比如 `task-0003`。

一个 CronJob 可以触发多个 Task。

### 全局状态

Cron Scheduler 有自己的状态和锁：

```python
CRON_JOBS: dict[str, CronJob] = {}
CRON_LOCK = threading.RLock()
NEXT_CRON_ID = 1
CRON_SCHEDULER_THREAD: threading.Thread | None = None
CRON_SCHEDULER_STOP = threading.Event()
```

我没有把它混进 `TASKS`，因为它们是两层抽象：

- `TASKS` 记录一次执行。
- `CRON_JOBS` 记录周期规则。

### 创建 CronJob

创建周期任务的函数是 `new_cron_job()`：

```python
def new_cron_job(
    description: str,
    prompt: str,
    interval_seconds: float,
    max_runs: int | None = None,
) -> CronJob:
    """创建一个周期任务记录。"""
    global NEXT_CRON_ID
    cron_id = f"cron-{NEXT_CRON_ID:04d}"
    NEXT_CRON_ID += 1
    now = time.time()
    job: CronJob = {
        "id": cron_id,
        "description": description.strip(),
        "prompt": prompt.strip(),
        "interval_seconds": interval_seconds,
        "status": "enabled",
        "created_at": format_timestamp(now),
        "next_run_at": now + interval_seconds,
        "last_run_at": None,
        "run_count": 0,
        "max_runs": max_runs,
        "task_ids": [],
    }
    with CRON_LOCK:
        CRON_JOBS[cron_id] = job
    return job
```

它不会立刻运行任务，而是计算下一次运行时间。

### 触发到期任务

核心逻辑是 `trigger_cron_job()`：

```python
def trigger_cron_job(job: CronJob, now: float | None = None) -> str:
    """触发一个周期任务：创建后台 Task，并更新下一次运行时间。"""
    current = time.time() if now is None else now
    record = new_task_record(
        f"cron {job['id']}: {job['description']}",
        job["prompt"],
        background=True,
    )
    thread = threading.Thread(
        target=run_background_task,
        args=(record,),
        name=f"cron-{job['id']}-{record['id']}",
        daemon=True,
    )
    with TASK_LOCK:
        TASK_THREADS[record["id"]] = thread
    with CRON_LOCK:
        job["task_ids"].append(record["id"])
        job["last_run_at"] = current
        job["run_count"] += 1
        if job["max_runs"] is not None and job["run_count"] >= job["max_runs"]:
            job["status"] = "paused"
        job["next_run_at"] = current + job["interval_seconds"]
    thread.start()
    return f"Triggered {job['id']} -> {record['id']}"
```

这段代码复用了 Day 13 的后台任务机制：Cron 不直接跑模型，而是创建一个后台 Task。

这样 Task System 仍然是所有子 Agent 执行的统一入口。

### 调度循环

后台调度线程很小：

```python
def cron_scheduler_loop(poll_seconds: float = 1.0) -> None:
    """后台调度循环：定期扫描到期任务。"""
    while not CRON_SCHEDULER_STOP.is_set():
        for job in due_cron_jobs():
            try:
                trigger_cron_job(job)
            except Exception as exc:
                record_error("cron_scheduler", f"Error: {exc}")
        CRON_SCHEDULER_STOP.wait(poll_seconds)
```

它每秒扫描一次。真实系统会有更精细的调度器、持久化存储和恢复机制；这里先保持最小实现。

启动函数是：

```python
def ensure_cron_scheduler_started() -> None:
    """确保进程内 cron 调度线程已经启动。"""
    global CRON_SCHEDULER_THREAD
    if CRON_SCHEDULER_THREAD and CRON_SCHEDULER_THREAD.is_alive():
        return
    CRON_SCHEDULER_STOP.clear()
    CRON_SCHEDULER_THREAD = threading.Thread(
        target=cron_scheduler_loop,
        name="cron-scheduler",
        daemon=True,
    )
    CRON_SCHEDULER_THREAD.start()
```

Agent 启动时会调用它，`cron_add` / `cron_resume` 也会确保调度线程已启动。

### cron 工具

Day 14 新增了一组工具：

- `cron_add`：登记周期任务。
- `cron_list`：列出所有 CronJob。
- `cron_read`：读取某个 CronJob 详情。
- `cron_pause`：暂停周期任务。
- `cron_resume`：恢复周期任务。
- `cron_remove`：移除周期任务。
- `cron_tick`：手动扫描一次到期任务。

其中 `cron_add` 做了几个限制：

```python
if interval < 5:
    return "Error: interval_seconds must be at least 5 seconds."
runs = None if max_runs is None else int(max_runs)
if runs is not None and runs <= 0:
    return "Error: max_runs must be positive when provided."
```

我故意不允许 1 秒甚至 0 秒的 interval，因为那很容易制造忙等和任务风暴。

## 我踩的坑

### 坑 1：CronJob 不能等同于 Task

一开始我想直接给 Task 加一个 `interval_seconds` 字段。

后来发现这会混淆两个概念：

- Task 是一次执行。
- CronJob 是执行规则。

如果把它们混在一起，一个周期任务运行 10 次，到底是一个 Task 还是 10 个 Task？结果应该存在哪里？错误算哪一次？

所以 Day 14 单独引入 `CronJob`，并让它记录触发出来的 `task_ids`。

### 坑 2：调度必须有下限

如果允许 `interval_seconds=0` 或 `1`，模型可能不小心创建一个高频任务，后台线程不断启动子 Agent。

所以我给 interval 加了最小 5 秒限制，并在 system prompt 里强调：

```text
Use cron_add only for recurring, bounded, user-approved checks.
```

真实系统里还会有权限确认、配额、最大并发数和资源限制。

### 坑 3：自动触发要复用 Task System

Cron 到期后最简单的做法是直接调用模型。

但这会绕过 Day 12/13 已经做好的任务记录、后台线程、错误查看等机制。

所以我让 `trigger_cron_job()` 创建后台 `TaskRecord`，再用 `run_background_task()` 执行。这样无论任务来自用户手动 `task`，还是来自 cron 触发，都能用同一套 `task_list` / `task_read` 查看。

## 对应真实 Claude Code 的哪里

真实 Claude Code / Codex 类工具不一定直接叫 Cron Scheduler，但会有类似的调度能力：

- 后台任务的定期检查。
- 长时间运行任务的轮询和刷新。
- 定时 compact、定时总结、定时状态同步。
- 外部事件或时间事件触发 Agent 行动。
- 任务队列、调度器、重试和暂停恢复机制。

我这个最小实现对应的是：

```text
scheduled rule -> due scan -> background Task -> tracked result
```

和真实系统相比，它还很简单：

- 只支持固定间隔，不支持 cron 表达式。
- 只保存在内存里，进程退出后丢失。
- 没有取消正在运行的 task。
- 没有任务并发上限。
- 没有权限弹窗和资源配额。
- 没有 missed-run 恢复。

但核心思想已经出现：

> Scheduler 不应该绕过 Agent Harness，而应该把到期事件转成标准 Task，让已有的任务、错误和可观察性机制继续生效。

## 小结

Day 14 的关键词是：**时间触发**。

Day 13 解决的是“任务能不能后台跑”；Day 14 解决的是“任务能不能按时间自动发起”。

有了 `CronJob`、`cron_add`、`cron_tick` 和调度线程，Agent Harness 开始具备长期代理的雏形：它不仅能响应用户输入，也能在时间到达时主动创建后台任务。
