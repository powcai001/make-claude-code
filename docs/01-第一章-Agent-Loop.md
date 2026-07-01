# 第 01 章：Agent Loop

> 📦 **本项目源码**：https://github.com/powcai001/make-claude-code
> 📄 **本章代码**：[`code/s01_agent_loop.py`](https://github.com/powcai001/make-claude-code/blob/main/code/s01_agent_loop.py)


## 本章解决什么问题？

如果只有一个大模型，它本质上只能做一件事：根据上下文预测下一段文本。

它可以告诉你“应该运行测试”，但它自己不会真的打开终端；它可以猜测某个文件哪里有 bug，但它自己不会真的读取文件；它可以写出一段修复方案，但它自己不会真的把代码改到仓库里。

所以，Claude Code 这类工具的第一层关键能力，不是“模型更聪明”，而是给模型外面套了一层 Harness：

```text
模型负责判断下一步做什么
Harness 负责把这一步真的执行掉
执行结果再回到模型上下文里
```

这就是 Agent Loop。

没有 Agent Loop，人类需要在模型和真实世界之间来回搬运信息：复制命令、运行命令、复制报错、再问模型。Agent Loop 把这件事自动化了：模型可以请求工具，Harness 执行工具，然后把工具结果作为新的上下文喂回模型。

本章我只做一件事：实现一个最小 Agent，它只有一个 `bash` 工具，但已经具备 Agent 的核心形态。

## 核心概念

一句话概括：

```text
一个工具 + 一个循环 = 一个 Agent
```

这不是完整的 Claude Code，但它已经包含了 Claude Code 类 Agent 的最小闭环：

```text
User Task
   |
   v
Messages + Tools  --->  Model
                           |
                           | tool_use
                           v
Harness executes tool
                           |
                           | tool_result
                           v
Messages append result ---> Model again
                           |
                           | end_turn
                           v
Done
```

几个核心概念：

- `messages`：对话历史，也是 Agent 的短期状态。模型每次判断下一步，都依赖这份上下文。
- `tools`：Harness 暴露给模型的外部能力。本章只有一个工具：`bash`。
- `tool_use`：模型不是直接执行命令，而是向 Harness 发出“我要调用某个工具”的请求。
- `tool_result`：Harness 执行工具后，把结果包装成消息，再交还给模型。
- `stop_reason`：循环是否结束的信号。如果模型还在请求工具，就继续；如果模型停止请求工具，就结束。

从 Harness Engineering 的角度看，Agent Loop 是最底层的运行时结构。后面的权限、Hooks、Todo、上下文压缩、多 Agent 协作，本质上都不是替代这个循环，而是围绕这个循环继续加控制层。

## 我的实现

完整实现见：`code/s01_agent_loop.py`

核心代码可以压缩成三部分。

第一部分：定义一个工具。

```python
TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command in the current working directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"}
            },
            "required": ["command"],
        },
    }
]
```

这里要注意：模型看到的不是 Python 函数，而是一个工具描述。模型会根据 `name`、`description` 和 `input_schema` 决定什么时候调用它，以及给它传什么参数。

第二部分：Harness 真正执行命令。

```python
def run_bash(command: str) -> str:
    result = subprocess.run(
        command,
        shell=True,
        cwd=os.getcwd(),
        capture_output=True,
        text=True,
        timeout=120,
    )

    output = (result.stdout + result.stderr).strip()
    if not output:
        output = f"(no output, exit code {result.returncode})"
    elif result.returncode != 0:
        output = f"(exit code {result.returncode})\n{output}"

    return output[:50_000]
```

模型只负责提出 `tool_use`，真正运行命令的是 Harness。

第三部分：最核心的循环。

```python
def agent_loop(messages: list[dict[str, Any]]) -> None:
    while True:
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            print_assistant_text(response.content)
            return

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            command = block.input["command"]
            output = run_bash(command)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                }
            )

        messages.append({"role": "user", "content": tool_results})
```

这段代码里最重要的是顺序：

1. 把当前 `messages` 和 `tools` 发给模型。
2. 把模型回复追加到 `messages`。
3. 如果模型没有请求工具，结束。
4. 如果模型请求工具，Harness 执行工具。
5. 把工具结果作为 `tool_result` 追加回 `messages`。
6. 回到第 1 步。

这就是一个最小 Agent 的心跳。

运行方式：

```bash
python code/s01_agent_loop.py
```

然后输入一个任务，例如：

```text
在当前目录创建 hello.py，内容是打印 hello agent，然后运行它
```

如果环境变量、依赖和模型配置正确，模型会先请求执行 shell 命令，Harness 执行后把结果返回给模型，模型再决定下一步。

## 我踩的坑

### 1. 忘记把 assistant response 放回 messages

一开始很容易写成：模型请求工具，我执行工具，然后只把工具结果发回去。

问题是，模型下一轮需要知道自己刚才发起的是哪个 `tool_use`。如果不把 assistant response 追加进 `messages`，后面的 `tool_result` 就失去了对应关系。

正确顺序是：

```text
assistant tool_use -> user tool_result
```

而不是只有：

```text
user tool_result
```

### 2. 把 tool_result 的 role 写错

在 Anthropic Messages API 里，工具结果是以 `role: "user"` 的消息放回去的，内容块类型是 `tool_result`。

也就是说，不是：

```python
{"role": "tool", "content": output}
```

而是：

```python
{
    "role": "user",
    "content": [
        {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": output,
        }
    ],
}
```

这个结构不对，循环就接不上。

### 3. 只处理了第一个 tool_use

模型一次回复里可能有多个内容块，其中也可能有多个 `tool_use`。

所以代码不要假设 `response.content[0]` 一定就是工具调用，而应该遍历所有 block：

```python
for block in response.content:
    if block.type == "tool_use":
        ...
```

### 4. 误以为危险命令黑名单等于权限系统

本章代码里有一个非常轻量的危险命令拦截，比如拦截 `rm -rf /`、`shutdown` 等。

但这不是安全沙箱，也不是完整权限系统。它只是为了防止 Day 01 示例太危险。

真正的权限边界至少要考虑：

- 用户确认；
- 命令分类；
- 工作目录限制；
- 文件读写范围；
- 网络访问；
- 超时和资源限制；
- 审计日志。

这些会放到后面的 Permission 章节。

### 5. 没有停止条件就会变成死循环

Agent Loop 必须检查 `stop_reason`。

如果模型已经不再请求工具，Harness 就应该停下来，把最终文本输出给用户。否则模型可能一直被迫进入下一轮，造成无意义循环。

## 对应真实 Claude Code 的哪里

本章对应的是 Claude Code 类系统最核心的运行时循环：

```text
model call -> tool use -> tool execution -> tool result -> model call
```

真实 Claude Code 肯定比本章复杂得多，它不只有一个 `bash` 工具，还会有文件读写、搜索、任务列表、子 Agent、MCP、Hooks、权限系统、上下文压缩、后台任务等机制。

但这些机制的底层位置都可以放回这个循环里理解：

- 工具系统：决定模型能请求哪些外部动作。
- 权限系统：决定某个工具请求能不能执行。
- Hooks：在工具执行前后插入额外逻辑。
- Todo：把长任务状态显式化。
- Context Compact：当 `messages` 太长时压缩上下文。
- SubAgent：把一部分任务交给另一个循环。

所以 Day 01 的目标不是复刻完整 Claude Code，而是先抓住它最小的骨架。

## 小结

本章我实现了一个只有 `bash` 工具的最小 Agent Loop。

它证明了一件事：Agent 并不神秘。模型本身不是 Agent，模型外面的 Harness 循环，才让模型拥有了“观察世界、采取行动、根据结果继续行动”的能力。

可以把本章记成一句话：

```text
Model 负责决定下一步，Harness 负责执行下一步，Agent Loop 负责让它们不断闭环。
```

从下一章开始，我会继续在这个循环上加能力：先把“工具调用”这件事讲得更细。
