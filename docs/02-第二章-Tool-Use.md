# 第 02 章：Tool Use

> 📦 **本项目源码**：https://github.com/powcai001/make-claude-code
> 📄 **本章代码**：[`code/s02_tool_use.py`](https://github.com/powcai001/make-claude-code/blob/main/code/s02_tool_use.py)


## 本章解决什么问题？

Day 01 里，我已经实现了一个最小 Agent Loop。模型可以请求 `bash`，Harness 可以执行命令，再把结果回填给模型。

这已经能跑，但它有一个明显问题：所有事情都压在 `bash` 这一个工具上。

比如读文件，模型可能会写：

```bash
cat README.md
```

写文件，模型可能会写：

```bash
cat > hello.py <<'EOF'
print("hello")
EOF
```

编辑文件，模型可能会写 `sed`、`perl`、PowerShell 或一长串重定向命令。

这些命令不是不能用，而是不稳定：

- 引号和换行容易转义错；
- Windows / Linux / macOS 的命令不一样；
- 文件路径里有空格就容易出问题；
- 模型要同时思考“我要做什么”和“shell 语法怎么写”；
- Harness 很难知道这条命令到底是在读、写、编辑，还是做危险操作。

所以 Day 02 要解决的问题是：把外部能力从一个粗糙的 `bash`，拆成更明确的结构化工具。

这一章我新增三个文件工具：

- `read_file`：读文件；
- `write_file`：写文件；
- `edit_file`：精确替换文件里的文本。

再加上原来的 `bash`，就形成了一个最小但更清晰的工具层。

## 核心概念

本章最重要的一句话是：

```text
加工具不是改循环，而是加 schema、加 handler、注册 dispatch map。
```

Day 01 的循环不需要重写。变化只发生在工具层：

```text
User Task
   |
   v
Messages + Tools  --->  Model
                           |
                           | tool_use(name, input)
                           v
                 TOOL_HANDLERS[name](**input)
                           |
                           | tool_result
                           v
Messages append result ---> Model again
```

几个关键概念：

- `input_schema`：模型能看到的工具契约。它告诉模型这个工具叫什么、需要哪些参数、参数是什么类型。
- `handler`：Harness 里真正执行动作的 Python 函数。模型看不到它的实现。
- `dispatch map`：工具名到 handler 的映射，例如 `"read_file" -> run_read`。
- `safe_path`：路径安全辅助函数，防止模型传入 `../../secret.txt` 这类逃逸工作区的路径。
- `tool_result`：handler 执行后的结果，会被包装成消息重新交给模型。

这里有一个很关键的分工：

```text
模型负责选择工具和填写参数
Harness 负责校验参数、执行工具、返回结果
```

这就是 Tool Use 的核心。

## 我的实现

完整实现见：`code/s02_tool_use.py`

### 1. 用 safe_path 限制文件路径

文件工具不能直接相信模型传来的路径。模型可能传：

```text
../../.ssh/id_rsa
```

所以我先把路径解析成绝对路径，再检查它是否还在当前工作目录内：

```python
WORKDIR = Path.cwd()


def safe_path(path: str) -> Path:
    resolved = (WORKDIR / path).resolve()
    if resolved != WORKDIR and WORKDIR not in resolved.parents:
        raise ValueError(f"Path escapes workspace: {path}")
    return resolved
```

这不是完整权限系统，但至少让文件工具有了工作区边界。

### 2. 实现三个文件 handler

读文件：

```python
def run_read(path: str, limit: int | None = None) -> str:
    file_path = safe_path(path)
    text = file_path.read_text(encoding="utf-8", errors="replace")

    lines = text.splitlines()
    if limit is not None and limit > 0 and len(lines) > limit:
        shown = lines[:limit]
        shown.append(f"... ({len(lines) - limit} more lines)")
    else:
        shown = lines

    numbered = [f"{index + 1:>4} | {line}" for index, line in enumerate(shown)]
    return "\n".join(numbered)[:50_000] if numbered else "(empty file)"
```

这里我给读取结果加了行号。这样模型后续要编辑文件时，更容易定位内容。

写文件：

```python
def run_write(path: str, content: str) -> str:
    file_path = safe_path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} characters to {path}."
```

编辑文件：

```python
def run_edit(path: str, old_text: str, new_text: str) -> str:
    file_path = safe_path(path)
    content = file_path.read_text(encoding="utf-8", errors="replace")
    if old_text not in content:
        return f"Error: old_text not found in {path}."

    updated = content.replace(old_text, new_text, 1)
    file_path.write_text(updated, encoding="utf-8")
    return f"Edited {path}."
```

我故意让 `edit_file` 只做“精确字符串替换”，而不是做模糊匹配或正则替换。原因是 Day 02 先追求确定性。

### 3. 注册 dispatch map

真正让多个工具变得干净的，是这个映射表：

```python
TOOL_HANDLERS = {
    "bash": lambda **kwargs: run_bash(kwargs["command"]),
    "read_file": lambda **kwargs: run_read(kwargs["path"], kwargs.get("limit")),
    "write_file": lambda **kwargs: run_write(kwargs["path"], kwargs["content"]),
    "edit_file": lambda **kwargs: run_edit(
        kwargs["path"],
        kwargs["old_text"],
        kwargs["new_text"],
    ),
}
```

有了它之后，Agent Loop 不需要知道每个工具的细节。

### 4. 循环里只做通用分发

Day 01 的循环只需要改一个小地方：从“只会执行 bash”，变成“根据工具名找 handler”。

```python
for block in response.content:
    if block.type != "tool_use":
        continue

    handler = TOOL_HANDLERS.get(block.name)
    if handler is None:
        output = f"Error: unknown tool {block.name!r}."
    else:
        output = handler(**block.input)

    tool_results.append(
        {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": output,
        }
    )
```

这就是本章的关键：

```text
Agent Loop 不关心工具细节，只关心 tool_use 和 tool_result。
```

运行方式：

```bash
python code/s02_tool_use.py
```

可以试这样的任务：

```text
创建 hello.py，写一个 greet(name) 函数，然后运行它
```

理想情况下，模型会用 `write_file` 写文件，再用 `bash` 运行文件。

## 我踩的坑

### 1. 误以为加工具要重写 Agent Loop

一开始很容易把每个工具都写进循环里：

```python
if block.name == "bash":
    ...
elif block.name == "read_file":
    ...
elif block.name == "write_file":
    ...
```

这样能跑，但工具一多，循环会越来越乱。

更好的方式是 dispatch map：

```python
handler = TOOL_HANDLERS.get(block.name)
output = handler(**block.input)
```

这样循环只负责通用流程，工具细节都放在 handler 里。

### 2. 直接相信模型传来的 path

模型给出的路径不一定安全，也不一定符合预期。

如果直接：

```python
Path(path).read_text()
```

那模型就可能读取工作区外的文件。

所以文件工具必须先走 `safe_path()`。这一步不是为了让系统“绝对安全”，而是为了给工具层加上最基本的边界。

### 3. 把 edit_file 做得太聪明

编辑文件时，我很容易想做模糊匹配、正则替换、自动定位行号。

但越聪明，越不可控。比如模型给了一个相似片段，handler 猜错位置，就可能改坏文件。

所以 Day 02 的 `edit_file` 只做一件事：

```text
找到 old_text 的第一次精确匹配，然后替换成 new_text。
```

找不到就报错，让模型重新读取文件、重新给出更准确的片段。

### 4. 混淆 JSON schema 和 Python 函数签名

模型看到的不是：

```python
def run_write(path: str, content: str) -> str:
    ...
```

模型看到的是工具 schema：

```json
{
  "name": "write_file",
  "input_schema": {
    "properties": {
      "path": {"type": "string"},
      "content": {"type": "string"}
    }
  }
}
```

所以 schema 的字段名、描述、required 列表都很重要。它们就是模型和 Harness 之间的接口文档。

### 5. 把路径限制误认为权限系统

`safe_path()` 只能说明：文件工具不要越过工作目录。

它不能回答这些问题：

- 这个文件能不能被覆盖？
- 这个命令是否需要用户确认？
- 写入 `.env` 是否危险？
- 删除文件是否允许？
- 网络命令是否允许？

这些是权限系统的问题，不是 Day 02 的问题。下一章才会正式处理 Permission。

## 对应真实 Claude Code 的哪里

本章对应 Claude Code 类系统里的工具层。

真实 Claude Code 里会有很多工具，例如：

- Bash；
- Read；
- Write；
- Edit；
- Grep；
- Glob；
- TodoWrite；
- Task / SubAgent；
- MCP 工具。

这些工具背后都有类似的结构：

```text
工具描述给模型看
工具实现由 Harness 控制
工具结果重新进入上下文
```

真实系统当然更复杂。它会处理：

- 权限确认；
- 工具调用审计；
- 输出截断；
- 错误恢复；
- 后台任务；
- Hooks；
- 更严格的文件安全策略；
- 更丰富的工具元数据。

但 Day 02 已经抓住了工具层最重要的机制：

```text
模型发出结构化 tool_use，Harness 根据工具名分发执行，再把结果作为 tool_result 回填。
```

## 小结

Day 01 我实现了 Agent Loop；Day 02 我在这个循环上加了工具分发层。

最重要的收获是：工具不是随便给模型一个函数。工具是一份契约：

```text
schema 是模型看到的契约
handler 是 Harness 执行的实现
dispatch map 是二者之间的路由表
```

有了这层之后，Agent 就不再只能通过 `bash` 粗暴地接触世界，而是可以用更明确、更可控的方式读文件、写文件、编辑文件。

下一章要解决的问题是：既然模型已经能读写和执行命令，那哪些动作应该允许？哪些动作必须先问用户？这就是 Permission。
