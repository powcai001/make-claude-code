# 第 04 章：Hooks

> 📦 **本项目源码**：https://github.com/powcai001/make-claude-code
> 📄 **本章代码**：[`code/s04_hooks.py`](https://github.com/powcai001/make-claude-code/blob/main/code/s04_hooks.py)


## 本章解决什么问题？

Day 03 我已经给 Agent 加上了 Permission：模型可以提出 `tool_use`，但真正执行之前，Harness 会先判断 `allow / ask / deny`。

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

所以 Day 04 要解决的问题是：**如何让 Agent Loop 保持稳定，同时允许 Harness 在关键生命周期点挂载额外行为？**

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

Day 03 的流程是：

```text
tool_use -> permission check -> tool handler -> tool_result
```

Day 04 的流程是：

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

Day 03 的 `check_permission()` 保留不动，只是换一个调用位置。

```python
def permission_hook(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """把 Day 03 的权限检查挂到 PreToolUse 生命周期。"""
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

## 我踩的坑

### 坑 1：Hook 不是把 if-else 换个地方堆起来

一开始很容易把 Hooks 写成另一种形式的 if-else：

```python
if event == "PreToolUse":
    check_permission(...)
elif event == "PostToolUse":
    log_output(...)
```

这样写表面上叫 hook，本质上还是把扩展逻辑写死在调度函数里。

真正的 Hook 应该是：

```python
HOOKS[event].append(callback)
```

核心代码只负责注册和触发，不知道每个 callback 具体做什么。

### 坑 2：PreToolUse 阻断后仍然要返回 tool_result

如果权限拒绝了某个工具调用，不能只是跳过。

因为从模型视角看，它已经发出了一个 `tool_use`。Harness 必须给这个 `tool_use_id` 对应的 `tool_result`，否则消息链就断了。

所以我这里的逻辑是：

```python
if blocked is not None:
    output = blocked
```

也就是说，被 Hook 阻断的工具调用，也会生成一个正常的 `tool_result`，内容是阻断原因。

### 坑 3：Hook 顺序很重要

同一个事件可以注册多个 hook，所以顺序会影响结果。

本章里：

```python
register_hook("PreToolUse", log_hook)
register_hook("PreToolUse", permission_hook)
```

这表示先记录工具调用，再做权限判断。

如果反过来，权限 hook 一旦返回字符串中断流程，后面的日志 hook 就不会执行。

这就是 Hook 系统必须明确的语义：

- 是否按顺序执行？
- 是否允许中断？
- 中断后后面的 hook 还跑不跑？

我的最小实现选择：**按注册顺序执行，遇到非 `None` 返回值就停止。**

### 坑 4：Stop hook 可能制造无限循环

`Stop` hook 很强大，因为它发生在 Agent 准备退出前。

我这里留了一个能力：如果 `Stop` hook 返回字符串，就把它作为新的 user message 加回去，让 Agent 继续跑：

```python
forced = trigger_hooks("Stop", messages)
if forced:
    messages.append({"role": "user", "content": forced})
    continue
```

这可以用来做“退出前自动总结”“发现结果不完整就继续追问”等能力。

但它也有风险：如果某个 Stop hook 每次都返回字符串，就会让 Agent 永远停不下来。

所以本章的 `summary_hook` 只打印状态，返回 `None`。

## 对应真实 Claude Code 的哪里

真实 Claude Code 里的 Hooks 比我这里复杂得多，但抽象方向是一样的：

```text
在 Agent 生命周期的关键点，允许 Harness 插入额外行为。
```

真实系统里的 Hook 通常会更结构化：

- 有更多事件类型。
- 有更明确的输入输出协议。
- 有配置来源，比如项目配置、用户配置、组织策略。
- 有更严格的错误处理和超时控制。
- Hook 结果不是简单的 `str | None`，而可能是结构化对象。

我这里故意做得很小：

```python
HookCallback = Callable[..., str | None]
```

因为本章不是要复刻完整 Claude Code，而是先抓住最核心的工程抽象：

> Agent Loop 不应该知道所有扩展逻辑，它只应该暴露生命周期事件。

Day 03 的 Permission 是一个很好的例子。

在 Day 03，它是 Agent Loop 里的显式步骤：

```text
tool_use -> permission check -> tool handler
```

到了 Day 04，它变成一个 `PreToolUse` hook：

```text
tool_use -> PreToolUse(permission_hook) -> tool handler
```

这就是 Harness Engineering 的味道：能力越来越多，但核心循环不要越来越乱。

## 小结

本章实现了一个最小 Hooks 系统。

它只做了三件事：

1. 定义生命周期事件。
2. 提供 `register_hook()` 和 `trigger_hooks()`。
3. 在 Agent Loop 的关键位置触发事件。

Hooks 的核心思想是：

```text
核心循环保持稳定，扩展逻辑挂在生命周期事件上。
```

到这里，我的 Agent 已经从：

```text
一个工具 + 一个循环
```

逐步变成了：

```text
Agent Loop + Tool System + Permission Gate + Lifecycle Hooks
```

这也更接近真实 Claude Code 的 Harness：模型只是一部分，真正让它像 Agent 一样工作的，是围绕模型的这套工程系统。
