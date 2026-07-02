# 第 02 章：Tool Use

> 📦 **本项目源码**：https://github.com/powcai001/make-claude-code
> 📄 **本章代码**：[`code/s02_tool_use.py`](https://github.com/powcai001/make-claude-code/blob/main/code/s02_tool_use.py)


## 本章解决什么问题？

第一章 里，我已经实现了一个最小 Agent Loop。模型可以请求 `bash`，Harness 可以执行命令，再把结果回填给模型。

这已经能跑，但它有一个明显问题：所有事情都压在 `bash` 这一个工具上。

为什么不能只用 `bash`？根因在于：**`bash` 把"做什么"和"怎么做"混在了一起。**

理想情况下，模型只需要表达意图——"读这个文件"、"改这一行"。但通过 `bash`，模型不得不同时承担和任务无关的负担：shell 语法怎么写、引号怎么转义、跨平台命令差异。更麻烦的是，Harness 只看到一串字符串命令，根本看不出这条命令到底是在读、在写、还是在做危险操作——它没法据此做权限判断、审计或拦截。

举个例子，同样是"读文件"，模型可能写出：

```bash
cat README.md
```

写文件，模型可能写出：

```bash
cat > hello.py <<'EOF'
print("hello")
EOF
```

编辑文件，模型可能写出 `sed`、`perl`、PowerShell 或一长串重定向命令。这些命令不是不能用，而是每一类都带来同一组问题：

- 引号和换行容易转义错；
- Windows / Linux / macOS 的命令不一样；
- 文件路径里有空格就容易出问题；
- 模型要同时思考"我要做什么"和"shell 语法怎么写"；
- Harness 很难知道这条命令到底是在读、写、编辑，还是做危险操作。

所以第二章要解决的问题是：把外部能力从一个粗糙的 `bash`，拆成更明确的结构化工具——让模型只表达"做什么"，把"怎么做"交回给 Harness。

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

第一章 的循环不需要重写。变化只发生在工具层：

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

我故意让 `edit_file` 只做“精确字符串替换”，不做模糊匹配或正则替换。这一章先保证每次编辑结果可预测：宁可让模型没匹配上、回头重读文件再试一次，也不要让 handler 自己猜位置、把文件改坏。模糊匹配、智能定位这些优化是后面的事。

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

第一章 的循环只需要改一个小地方：从“只会执行 bash”，变成“根据工具名找 handler”。

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

这一章的坑，抓住几件事就够了：

- **不要把每个工具写进循环。** 一开始容易写成 `if block.name == "bash" ... elif "read_file" ...`，工具一多循环就乱。正确做法是 dispatch map：循环只负责通用流程，工具细节都放进各自的 handler。
- **不要直接相信模型传来的路径。** `Path(path).read_text()` 会让模型读到工作区外的文件。文件工具必须先走 `safe_path()`，给工具层加上工作区边界。
- **edit_file 不要做太聪明。** 模糊匹配、正则、自动定位行号都会让结果不可控。这一章只认字面精确匹配，找不到就报错让模型重读。
- **schema 不等于 Python 函数签名。** 模型看到的不是 `def run_write(path, content)`，而是工具 schema。字段名、描述、required 列表才是模型和 Harness 之间的接口文档，要写清楚。
- **safe_path 不是权限系统。** 它只回答“路径有没有越出工作目录”，回答不了“这个文件能不能覆盖”“写 `.env` 要不要确认”“删文件行不行”。这些是下一章 Permission 的事。

## 小结

第一章我实现了 Agent Loop；第二章在这个循环上加了工具分发层：把粗糙的 `bash`，拆成 `read_file` / `write_file` / `edit_file` 这些结构化工具。

这一章的核心是：工具不是随便给模型一个函数，工具是一份契约。

```text
schema        模型看到的契约（叫什么、要什么参数、什么时候用）
handler       Harness 真正执行的动作
dispatch map  把两者路由起来的表
```

有了这层之后，Agent 不再只能靠 `bash` 粗暴地碰世界，而是用更明确、更可控的方式读写文件。

真实 Claude Code 的工具层当然复杂得多：它有 Bash、Read、Write、Edit、Grep、Glob、TodoWrite、SubAgent、MCP 等一堆工具，还会处理权限确认、调用审计、输出截断、错误恢复、Hooks。但这些都是叠在这套 `schema + handler + dispatch map` 之上的机制，没有改变工具层的本质：

```text
模型发出结构化 tool_use，Harness 按工具名分发执行，再把结果作为 tool_result 回填。
```

下一章要解决的问题是：既然模型已经能读写和执行命令，那哪些动作应该允许？哪些动作必须先问用户？这就是 Permission。
