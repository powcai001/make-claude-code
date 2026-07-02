# 第 03 章：Permission

> 📦 **本项目源码**：https://github.com/powcai001/make-claude-code
> 📄 **本章代码**：[`code/s03_permission.py`](https://github.com/powcai001/make-claude-code/blob/main/code/s03_permission.py)


第二章 我给 Agent 加了多个工具：`bash`、`read_file`、`write_file`、`edit_file`。

这时 Agent 已经不只是“会说话”了，它开始能动手改文件、执行命令。

但也正因为它能动手，新的问题出现了：

> 模型可以提出动作，但谁决定这个动作能不能真的执行？

这一章，我给 Harness 加第一道真正的边界：Permission。

---

## 本章解决什么问题？

第二章 的工具分发层解决的是：

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

第二章 我已经写了 `safe_path()`：

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

我只是把 第二章 里的“危险命令 demo guard”从 `run_bash()` 里拿出来，变成一个独立的权限管线。

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

    return "allow", "safe by current chapter 3 policy"
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

运行方式：

```bash
python code/s03_permission.py
```

试一个会触发权限拦截的任务：

```text
帮我把当前目录下所有文件都删掉
```

你会看到类似这样的交互：

```text
> bash: {'command': 'rm -rf *'}
⚠ Permission required: bash
命令可能删除文件: 'rm '
Allow? [y/N]
```

- 如果输入 `N`（或直接回车），Harness 返回 `Permission denied`，模型会看到被拒绝、调整策略。
- 如果是 `rm -rf /` 这种命中黑名单的命令，连询问都没有，直接 `⛔ Permission denied`。
- `read_file` 这类只读操作默认直接放行，不会打断你。

这就是这一章想让用户"感受到"的东西：**模型可以提出动作，但能不能落地，是 Harness 说了算。**

## 我踩的坑

这一章的坑，抓住几件事就够了：

- **权限逻辑不要塞进 handler。** 第二章里我把危险命令判断写在 `run_bash()` 里，结果是每个工具各管各的权限，规则散得到处都是。正确做法是把权限判断提到 handler 执行前，统一一道门。
- **拒绝工具后必须回填 `tool_result`。** 不能 `continue` 跳过——模型发起的每个 `tool_use` 都要有一个对应的 `tool_result`，否则消息链断裂，模型不知道发生了什么。拒绝也是一种结果。
- **不要把所有写操作都直接 deny。** 写文件全 deny，Agent 就没法完成任何编码任务。教学版的合理策略是：读 allow、写 ask、明显危险 deny，刚好展示三种决策。
- **子串匹配挡不住组合命令。** `DENIED_BASH_FRAGMENTS` 是简单子串匹配，`rm -rf /` 能挡住，但 `echo hi && rm -rf foo` 这种用 `&&` 拼起来的命令，子串匹配不一定命中。真实 Claude Code 会把组合命令按 `&&` `;` `|` 拆成子命令逐条匹配——这是后面优化权限规则时要补的点，第三章先知道有这个坑就行。
- **deny 是单调的。** 一旦某个环节决定 deny，后续环节不能翻转成 allow。这是权限系统最重要的安全不变式：在纵深防御里，内层不该有能力削弱外层的保护。理解了这一点，后面加 Hooks 时就不会犯"用 hook 去解锁被全局规则禁止的操作"这种错。

## 小结

第一章我得到的是：`一个工具 + 一个循环 = 一个 Agent`。  
第二章我得到的是：`加工具不是改循环，而是加 schema、加 handler、注册 dispatch map`。  
第三章我得到的是：

```text
模型可以提出动作，但 Harness 决定动作能不能执行。
```

Permission 不是工具的一部分，而是工具执行前的一道边界。

```text
tool_use -> permission check -> handler -> tool_result
```

真实 Claude Code 的权限系统比这复杂得多。它有五种权限模式（default / plan / acceptEdits / auto / bypassPermissions），从最保守到最激进渐变；权限判断被抽象成一个可插拔的函数接口 `f(tool, input, context) -> allow/deny/ask`，在 REPL、SDK、远程环境里有不同实现；还会用 tree-sitter 把 bash 命令解析成 AST 逐条评估安全性。但这些都是叠在"工具执行前先过一道权限门"这个核心结构之上的，没有改变 Permission 的本质。

下一章要回答的问题是：如果权限检查是工具执行前的固定边界，那更通用的"执行前/执行后扩展点"是什么？答案就是 Hooks。
