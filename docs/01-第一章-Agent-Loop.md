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

## 真实运行：State 是怎么长出来的

看完上面的代码，最应该盯住的是 `messages`。在 `learn-claude-code` 的第一章里，最小 Agent 的状态就是一个**累积式消息列表**：用户任务进去，assistant 的 `tool_use` 进去，工具执行后的 `tool_result` 也进去。下一轮模型调用时，Harness 把这份列表整体再发给模型。

也就是说，在第一章这个最小版本里，可以先把 State 理解成：

```text
State ≈ messages（累积式消息列表）
```

它不是模型自己的记忆，而是 Harness 在本地维护、每轮都重新发给模型的上下文。

比如用户输入：

```text
在当前目录创建 hello.py，内容是打印 hello agent，然后运行它
```

真实运行时，模型可能先返回一个 `tool_use`，Harness 执行命令：

```text
$ echo 'print("hello agent")' > hello.py && python hello.py
(no output, exit code 0)
```

这时 `messages` 大概长这样：

```python
[
    {"role": "user", "content": "创建 hello.py 并运行"},

    {"role": "assistant", "content": [
        {"type": "tool_use", "id": "call_00", "name": "bash",
         "input": {"command": "echo 'print("hello agent")' > hello.py"}}
    ]},

    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "call_00",
         "content": "(no output, exit code 0)"}
    ]},
]
```

这里要分清两件事：工具确实改变了外部世界，比如当前目录里可能多了一个 `hello.py`；但模型不会自动知道这个变化。模型下一轮能看到的，仍然只有 Harness 写回 `messages` 的内容。以后如果加入 Todo、Memory、Task、文件系统索引、队列等机制，State 会变得更丰富；但第一章先抓住最小形态：**messages 是 Agent Loop 的短期状态**。

这就是 Agent Loop 的价值：模型不是一次性给答案，而是在循环里不断接收工具结果、修正动作、继续推进。

## 我踩的坑

这一章的坑不需要记很多，抓住三件事就够了：

- **工具调用不是函数调用，而是 messages 里的配对关系。** 先有 `assistant(tool_use)`，再有 `user(tool_result)`。中间那条 assistant 消息不能省，否则 tool_result 找不到对应的 tool_use。
- **Anthropic 的 `tool_result` 是放在 `role: "user"` 里的。** 我一开始受 OpenAI API 影响写成 `role: "tool"`，结果结构不对。不同 API 的 messages 形状不能混着套。
- **demo 输出不完美，不代表 loop 错了。** 我真实跑的时候第一轮命令没有得到预期输出，但模型可以根据 tool_result 继续尝试。这正是 loop 的意义：观察结果，然后修正动作。

本章代码里还有一点危险命令拦截，但那只是 demo guard，不是权限系统。真正的权限边界会放到后面的 Permission 章节。

## 小结

本章我实现了一个只有 `bash` 工具的最小 Agent Loop。

它证明了一件事：Agent 并不神秘。模型本身不是 Agent，模型外面的 Harness 循环，才让模型拥有了“观察世界、采取行动、根据结果继续行动”的能力。

第一章最重要的结论，是这条公式：

```text
Agent = Loop + Model + Tool + State
```

- `Model`：负责判断下一步。
- `Tool`：负责连接真实世界。
- `State`：负责保存模型下一轮还能看到什么；在第一章里先表现为 `messages`，后面会扩展到 Todo、Memory、Task 等更持久的状态。
- `Loop`：负责把模型、工具和状态反复串起来。

真实 Claude Code 当然复杂得多：它不只有 `bash`，还会有文件读写、搜索、Todo、权限、Hooks、上下文压缩、SubAgent、MCP 等机制。但这些机制都不是替代 Agent Loop，而是在这个最小循环上继续加控制层。

所以第一章的目标不是复刻完整 Claude Code，而是先抓住它最小的骨架。少了任何一个要素，都只是普通聊天或普通脚本；四个要素合在一起，才出现了最小 Agent。

从下一章开始，我会继续在这个循环上加能力：先把“工具调用”这件事讲得更细。
