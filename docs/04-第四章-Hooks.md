# 第 04 章：Hooks

> 📦 **本项目源码**：https://github.com/powcai001/make-claude-code
> 📄 **本章代码**：[`code/s04_hooks.py`](https://github.com/powcai001/make-claude-code/blob/main/code/s04_hooks.py)


## 本章解决什么问题？

第 03 章 我已经给 Agent 加上了 Permission：模型可以提出 `tool_use`，但真正执行之前，Harness 会先判断 `allow / ask / deny`。

问题是：如果继续往 Agent 里加能力，会发生什么？

比如我想记录每次工具调用、统计工具输出长度、在用户输入进入模型前做预处理、在 Agent 停止前做收尾。如果这些逻辑都直接写进 `agent_loop()`，循环很快就会变成一坨：

```text
调用模型
检查 stop_reason
判断工具
打印日志
检查权限
执行工具
截断输出
统计 token
写审计日志
停止前总结
...
```

这样写当然能跑，但它有一个工程问题：核心循环和扩展逻辑耦合在一起了。

所以 第 04 章 要解决的问题是：**如何让 Agent Loop 保持稳定，同时允许 Harness 在关键生命周期点挂载额外行为？**

答案就是 Hooks。

## 核心概念

Hooks 可以理解成 Agent Loop 暴露出来的生命周期插槽。

核心循环只负责在关键位置喊一声：

> 现在用户输入提交了。  
> 现在工具准备执行了。  
> 现在工具执行完了。  
> 现在 Agent 准备停止了。

至于谁要在这些位置做事，由 hook 注册表决定。

本章我实现了四个事件：

- `UserPromptSubmit`：用户输入进入模型前触发。
- `PreToolUse`：工具执行前触发。
- `PostToolUse`：工具执行后触发。
- `Stop`：Agent Loop 准备退出前触发。

流程变成这样：

```text
UserPromptSubmit
      |
      v
LLM response
      |
      +-- no tool_use --> Stop hooks --> exit
      |
      v
tool_use
      |
      v
PreToolUse hooks
      |
      +-- blocked --> tool_result
      |
      v
tool handler
      |
      v
PostToolUse hooks
      |
      v
tool_result -> messages
```

第 03 章 的流程是：

```text
tool_use -> permission check -> tool handler -> tool_result
```

第 04 章 的流程是：

```text
tool_use -> PreToolUse hooks -> tool handler -> PostToolUse hooks -> tool_result
```

注意：Permission 没有消失，只是从“写死在循环里的一段代码”，变成了挂在 `PreToolUse` 上的一个 hook。

## 我的实现

完整实现见：`code/s04_hooks.py`

### 1. Hook 注册表

我先定义事件类型、回调类型和一个全局注册表：

```python
HookEvent = Literal["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]
HookCallback = Callable[..., str | None]

HOOKS: dict[HookEvent, list[HookCallback]] = {
    "UserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
}
```

这里的设计非常简单：

- 一个事件名，对应一组回调函数。
- 同一个事件可以注册多个 hook。
- hook 按注册顺序执行。

然后提供两个函数：

```python
def register_hook(event: HookEvent, callback: HookCallback) -> None:
    """注册一个 hook 回调。"""
    HOOKS[event].append(callback)


def trigger_hooks(event: HookEvent, *args: Any) -> str | None:
    """触发生命周期事件；返回非 None 表示中断默认流程。"""
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return str(result)
    return None
```

这里我给 hook 定了一个最小协议：

- 返回 `None`：继续默认流程。
- 返回字符串：中断默认流程。

比如 `PreToolUse` hook 返回字符串，就表示工具不要执行了，直接把这个字符串作为 `tool_result` 返回给模型。

### 2. 把 Permission 变成 PreToolUse hook

第 03 章 的 `check_permission()` 保留不动，只是换一个调用位置。

```python
def permission_hook(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """把 第 03 章 的权限检查挂到 PreToolUse 生命周期。"""
    allowed, reason = check_permission(tool_name, tool_input)
    if allowed:
        return None
    return f"Permission denied: {reason}"
```

这段代码的意思是：

- 如果权限通过，返回 `None`，工具继续执行。
- 如果权限拒绝，返回 `Permission denied: ...`，工具不执行。

这样一来，Agent Loop 不需要知道“权限检查”这个具体概念，它只知道：

```python
blocked = trigger_hooks("PreToolUse", block.name, block.input)
```

如果有 hook 阻断，就不执行工具。

### 3. Agent Loop 只触发事件

工具执行部分变成这样：

```python
blocked = trigger_hooks("PreToolUse", block.name, block.input)
if blocked is not None:
    output = blocked
else:
    try:
        output = handler(**block.input)
    except Exception as exc:
        output = f"Error: {exc}"
    trigger_hooks("PostToolUse", block.name, block.input, output)
```

这一段体现了 Hooks 的核心价值：

Agent Loop 不再关心具体扩展逻辑。

它不关心这里挂的是权限、日志、审计、缓存还是输出压缩；它只负责在工具执行前后触发事件。

### 4. 注册 hooks

本章注册了几个最小 hook：

```python
register_hook("UserPromptSubmit", user_prompt_log_hook)
register_hook("PreToolUse", log_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)
```

其中：

- `user_prompt_log_hook`：记录用户输入进入模型前的事件。
- `log_hook`：记录工具调用。
- `permission_hook`：执行权限检查。
- `large_output_hook`：观察工具输出长度。
- `summary_hook`：在 Agent 停止前打印消息数量。

这里特意让 `log_hook` 注册在 `permission_hook` 前面。

原因是：即使一个工具调用后来被权限拒绝，我也希望先看到它尝试调用了什么。

运行方式：

```bash
python code/s04_hooks.py
```

试一个普通任务：

```text
读取 README.md 的前几行
```

你会在终端里看到类似这样的 hook 输出：

```text
[hook:UserPromptSubmit] 读取 README.md 的前几行
[hook:PreToolUse] read_file: {'path': 'README.md', 'limit': 20}
[hook:Stop] conversation has 3 messages
```

如果任务涉及写文件，例如：

```text
创建 hook_demo.txt，内容是 hello hooks
```

你会同时看到 `PreToolUse` 日志和 Permission 弹窗。也就是说，模型只是请求 `write_file`，但权限检查、日志记录、输出观察这些动作，都是 Hook 在生命周期点上自动触发的。

这就是这一章想让用户"感受到"的东西：**Hook 不是让模型记得做某件事，而是在 Harness 的固定位置自动执行某件事。**

## 我踩的坑

这一章的坑，抓住几件事就够了：

- **Hook 不是换个地方写 if-else。** 如果 `trigger_hooks()` 里还是写满 `if event == ...`，那只是把复杂度挪了位置。真正的 Hook 是注册回调，核心循环只负责触发事件。
- **PreToolUse 阻断后也要返回 tool_result。** 模型发起了 `tool_use`，就必须拿到对应结果。被 hook 拦住的工具调用，也要把拦截原因作为 `tool_result` 回填。
- **Hook 顺序要有明确语义。** 本章选择按注册顺序执行，遇到非 `None` 返回值就停止。顺序会影响结果：先日志再权限，和先权限再日志，用户看到的行为不一样。
- **Stop hook 最容易写成死循环。** 如果 Stop hook 每次都返回新消息，Agent 就永远停不下来。本章的 `summary_hook` 只打印状态、返回 `None`，就是为了避免这个问题。

## 小结

本章实现了一个最小 Hooks 系统：定义生命周期事件，提供 `register_hook()` / `trigger_hooks()`，然后在 Agent Loop 的关键位置触发事件。

Hooks 的核心思想是：

```text
核心循环保持稳定，扩展逻辑挂在生命周期事件上。
```

第 03 章的 Permission 是一个很好的例子：之前它是工具执行前的一段固定逻辑；到了第 04 章，它变成了挂在 `PreToolUse` 上的 `permission_hook`。这就是 Harness Engineering 的味道：能力越来越多，但核心循环不要越来越乱。

真实 Claude Code 的 Hook 系统比这里复杂得多。它有更多事件，比如 `SessionStart`、`PreCompact`、`PostToolUseFailure`、`PermissionRequest`；也不一定是某个事件触发全部 hook，而是会经过 matcher / 条件过滤；hook 来源也可能来自 settings、plugin、skill、session、function hook。真实系统把 Hook 当成一套外接神经系统，用来接入安全策略、审计日志、自动验证、失败恢复和项目规则。

但无论规模多大，核心都还是这一句：**不要把所有扩展逻辑塞进 Agent Loop；把它们挂在生命周期事件上。**

下一章要解决的问题是：Agent 开始能做多步任务之后，如何让它把计划显式写出来，让用户和 Harness 都看得见？这就是 TodoWrite。
