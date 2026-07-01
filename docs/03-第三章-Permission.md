# 第 03 章：Permission

Day 02 我给 Agent 加了多个工具：`bash`、`read_file`、`write_file`、`edit_file`。

这时 Agent 已经不只是“会说话”了，它开始能动手改文件、执行命令。

但也正因为它能动手，新的问题出现了：

> 模型可以提出动作，但谁决定这个动作能不能真的执行？

这一章，我给 Harness 加第一道真正的边界：Permission。

---

## 本章解决什么问题？

Day 02 的工具分发层解决的是：

```text
模型想调用哪个工具？
这个工具应该交给哪个 handler 执行？
执行结果怎么回填给模型？
```

但它还没有认真解决：

```text
这个工具调用是否应该被执行？
```

比如用户说：

```text
帮我清理一下这个项目
```

模型可能会选择执行：

```bash
rm -rf /
```

或者在 Windows 上执行某些批量删除命令。即使模型不是故意作恶，也可能因为理解错任务、上下文不足、命令写错，导致不可逆的破坏。

所以安全不能靠“相信模型会乖”。

安全必须变成 Harness 的代码边界：

```text
tool_use -> permission check -> tool handler -> tool_result
```

模型负责提出动作，Harness 负责决定动作能不能落地。

---

## 核心概念

Permission 的核心不是“让模型自己判断危险不危险”。

Permission 的核心是：

> 在工具真正执行前，由 Harness 插入一道决策门。

这一章我只实现三个决策：

```text
allow：直接允许
ask：问用户
 deny：直接拒绝
```

放到 Agent Loop 里，就是这样：

```text
assistant returns tool_use
        |
        v
permission_decision(tool_name, input)
        |
        +-- allow -> 执行 handler
        |
        +-- ask   -> 询问用户，用户同意才执行
        |
        +-- deny  -> 不执行，返回 Permission denied
        v
tool_result 回填给模型
```

这里有几个关键概念：

- `tool_use`：模型提出的动作请求。
- `permission check`：Harness 在执行前做的权限判断。
- `allow`：当前策略认为可以直接执行。
- `ask`：当前策略认为需要用户确认。
- `deny`：当前策略认为绝对不能执行。
- `tool_result`：即使被拒绝，也要把拒绝结果回填给模型。

这一点很重要：

> 拒绝工具调用，不等于让工具调用消失。

如果 Harness 静默跳过工具调用，模型就不知道发生了什么。正确做法是返回一个 `tool_result`：

```text
Permission denied: ...
```

这样模型才能调整下一步行为。

---

## Permission 和 safe_path 的区别

Day 02 我已经写了 `safe_path()`：

```python
def safe_path(path: str) -> Path:
    resolved = (WORKDIR / path).resolve()
    if resolved != WORKDIR and WORKDIR not in resolved.parents:
        raise ValueError(f"Path escapes workspace: {path}")
    return resolved
```

它解决的是：

```text
文件路径不能逃出当前工作目录。
```

但它不是完整权限系统。

原因很简单：

1. `safe_path()` 只管文件路径，不管 `bash`。
2. `safe_path()` 只能说“路径是否合法”，不能说“这个动作是否应该执行”。
3. 在 workspace 内删除重要文件，路径也是合法的，但动作仍然可能危险。

所以我对二者的理解是：

```text
safe_path：路径边界
Permission：执行边界
```

路径边界是必要的，但不够。

---

## 我的实现

完整实现见：`code/s03_permission.py`

这一章没有重写 Agent Loop，也没有重写 Tool Use。

我只是把 Day 02 里的“危险命令 demo guard”从 `run_bash()` 里拿出来，变成一个独立的权限管线。

核心类型很简单：

```python
PermissionDecision = Literal["allow", "deny", "ask"]
```

然后定义两组规则。

第一组是永远拒绝：

```python
DENIED_BASH_FRAGMENTS = [
    "rm -rf /",
    "sudo ",
    "shutdown",
    "reboot",
    "> /dev/",
    "format ",
    "mkfs",
    "dd if=",
    "del /f /s /q c:\\",
]
```

第二组是需要询问：

```python
ASK_BASH_FRAGMENTS = [
    "rm ",
    "del ",
    "rmdir ",
    "move ",
    "copy ",
    "git reset",
    "git clean",
    "pip install",
    "npm install",
]
```

核心判断函数是：

```python
def permission_decision(
    tool_name: str,
    tool_input: dict[str, Any],
) -> tuple[PermissionDecision, str]:
    reason = deny_reason(tool_name, tool_input)
    if reason:
        return "deny", reason

    if tool_name in {"write_file", "edit_file"}:
        return "ask", f"{tool_name} modifies files in the workspace."

    if tool_name == "bash":
        command = str(tool_input.get("command", "")).lower()
        for fragment in ASK_BASH_FRAGMENTS:
            if fragment in command:
                return "ask", f"bash command may change files or environment: {fragment!r}"

    return "allow", "safe by current Day 03 policy"
```

也就是说：

- `read_file` 默认允许。
- `write_file` / `edit_file` 默认询问。
- 普通 `bash` 默认允许。
- 命中高危片段的 `bash` 直接拒绝。
- 命中可能修改环境的 `bash` 询问用户。

真正的统一入口是：

```python
def check_permission(tool_name: str, tool_input: dict[str, Any]) -> tuple[bool, str]:
    decision, reason = permission_decision(tool_name, tool_input)

    if decision == "allow":
        return True, reason

    if decision == "deny":
        print(f"\n\033[31m⛔ Permission denied: {reason}\033[0m")
        return False, reason

    if ask_user_permission(tool_name, tool_input, reason):
        return True, "approved by user"

    return False, f"user rejected permission request: {reason}"
```

最后，把它插进工具执行前：

```python
handler = TOOL_HANDLERS.get(block.name)
if handler is None:
    output = f"Error: unknown tool {block.name!r}."
else:
    print(f"\033[33m> {block.name}: {block.input}\033[0m")
    allowed, reason = check_permission(block.name, block.input)
    if not allowed:
        output = f"Permission denied: {reason}"
    else:
        try:
            output = handler(**block.input)
        except Exception as exc:
            output = f"Error: {exc}"
```

注意这里的结构：

```text
先找 handler
再 check_permission
允许后才 handler(**block.input)
```

这就是本章最核心的一行边界。

---

## 我踩的坑

### 坑 1：把危险命令判断塞进 `run_bash()`

Day 02 里，我在 `run_bash()` 里写过一个 demo guard：

```python
if any(fragment in lowered for fragment in dangerous_fragments):
    return "Error: dangerous command blocked by the Day 02 demo guard."
```

这能挡住一点东西，但问题是：权限逻辑被塞进了具体工具 handler。

如果以后 `write_file`、`edit_file`、`delete_file`、`git_commit` 都有自己的权限逻辑，代码会散在每个 handler 里面。

Day 03 的修复方式是：

```text
权限逻辑从 handler 里拿出来，统一放到 handler 执行前。
```

handler 只负责做事。

Permission 负责决定能不能做。

---

### 坑 2：以为 `safe_path()` 就是权限系统

`safe_path()` 很重要，但它只能回答：

```text
这个路径有没有逃出 workspace？
```

它不能回答：

```text
这个文件该不该被覆盖？
这个命令该不该被执行？
这个操作要不要问用户？
```

所以它只是路径沙箱的一部分，不是完整权限边界。

---

### 坑 3：拒绝工具后没有返回 `tool_result`

一开始很容易写成：

```python
if not check_permission(block):
    continue
```

这样工具确实没有执行，但模型也不知道发生了什么。

正确做法是：

```python
output = f"Permission denied: {reason}"
tool_results.append({
    "type": "tool_result",
    "tool_use_id": block.id,
    "content": output,
})
```

只要模型发起了 `tool_use`，Harness 就应该给它一个对应的 `tool_result`。

---

### 坑 4：把所有写操作都直接拒绝

如果 `write_file` 和 `edit_file` 全部 `deny`，Agent 就无法完成大部分编码任务。

所以更合理的教学版策略是：

```text
读文件：allow
写文件：ask
明显危险：deny
```

这不是最安全的生产策略，但它很好地展示了 Permission 的基本形状。

---

## 对应真实 Claude Code 的哪里

真实 Claude Code 的权限系统比这里复杂很多。

我现在这个教学版只有三种结果：

```text
allow / ask / deny
```

真实系统里，权限判断会涉及更多层次：

- 工具参数验证。
- 工具自己的权限检查。
- 用户设置里的 allow / deny 规则。
- 项目设置里的规则。
- 命令行参数传入的授权规则。
- Hooks 对工具调用的拦截。
- 某些模式下的自动审批。
- 子 Agent 的权限冒泡。

也就是说，真实系统不是一个简单的 `if`。

它更像一条管线：

```text
validate input
-> pre tool hooks
-> tool permission check
-> user / project / policy rules
-> ask user if needed
-> execute tool
-> return result
```

但教学版先保留最小骨架：

```text
tool_use -> permission check -> handler -> tool_result
```

先理解这条线，后面再加 Hooks、配置、多 Agent，都不会乱。

---

## 小结

Day 01 我得到的是：

```text
一个工具 + 一个循环 = 一个 Agent
```

Day 02 我得到的是：

```text
加工具不是改循环，而是加 schema、加 handler、注册 dispatch map。
```

Day 03 我得到的是：

```text
模型可以提出动作，但 Harness 决定动作能不能执行。
```

Permission 不是工具的一部分。

Permission 是工具执行前的一道边界。

这一章之后，Agent 不再是“模型说执行就执行”，而是开始有了最基础的执行控制。

下一章 Day 04，我会继续把这个思路往前推进：

如果权限检查是工具执行前的固定边界，那么更通用的“执行前 / 执行后扩展点”是什么？

答案就是 Hooks。
