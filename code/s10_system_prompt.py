#!/usr/bin/env python3
"""Day 10: System Prompt.

Day 09 的 Agent 已经有了持久化 Memory。本章继续整理 System Prompt：
把身份、行为规则、工具规则、Memory、Skill 等上下文拆成有优先级的 section，
每轮按预算组装成最终提示词。

这个版本故意保持简单：

    prompt sections -> priority sort -> budget trim -> final system prompt

System Prompt 不再是一段越写越长的字符串，而是 Harness 每轮动态组装出的控制面。
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal, TypedDict

try:
    import readline  # type: ignore

    readline.parse_and_bind("set bind-tty-special-chars off")
    readline.parse_and_bind("set input-meta on")
    readline.parse_and_bind("set output-meta on")
    readline.parse_and_bind("set convert-meta off")
    readline.parse_and_bind("set enable-meta-keybindings on")
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
MODEL = os.environ.get("MODEL_ID")
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))

BASE_IDENTITY = f"You are a coding agent working in {WORKDIR}."

CORE_BEHAVIOR = """Use the available tools to solve the user's task.
For multi-step work, maintain a visible task list with todo_write.
When you need focused research, use task to launch a subagent.
Prefer small, reversible edits and explain important trade-offs.
The todo list must have at most one in_progress item."""

TOOL_POLICY = """Tool rules:
- Use read_file before editing unfamiliar files.
- Use write_file or edit_file only for workspace files.
- Use list_skills and read_skill when a relevant skill may help.
- Use remember to persist stable user preferences, project conventions, or important facts.
- Do not store secrets in Memory."""

PROMPT_BUDGET_CHARS = 18_000

PermissionDecision = Literal["allow", "deny", "ask"]
HookEvent = Literal["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]
HookCallback = Callable[..., str | None]
TodoStatus = Literal["pending", "in_progress", "completed"]
TodoPriority = Literal["high", "medium", "low"]


class PromptSection(TypedDict):
    """System Prompt 的一个可组装片段。"""

    name: str
    priority: int
    content: str
    budget: int


class TodoItem(TypedDict):
    """TodoWrite 维护的最小任务结构。"""

    content: str
    status: TodoStatus
    priority: TodoPriority


# TodoWrite 在 Day 10 仍然只做当前会话计划；长期事实交给 Memory。
TODOS: list[TodoItem] = []



class Skill(TypedDict):
    """从 SKILL.md 解析出来的最小技能元数据。"""

    name: str
    description: str
    path: str
    content: str
    keywords: list[str]


SKILLS_DIR = WORKDIR / "skills"
LOADED_SKILLS: dict[str, Skill] = {}


def parse_simple_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """解析最小 frontmatter：只支持 key: value。"""
    if not text.startswith("---\n"):
        return {}, text

    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text

    metadata: dict[str, str] = {}
    raw_metadata = text[4:end]
    for line in raw_metadata.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip().lower()] = value.strip().strip("\"'")

    return metadata, text[end + 5 :].lstrip()


def first_heading_or_paragraph(markdown: str) -> str:
    """从普通 Markdown 中提取一句简短描述。"""
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()
        return stripped[:200]
    return "No description provided."


def skill_name_from_path(path: Path) -> str:
    """优先使用技能目录名，根目录 SKILL.md 则用文件名。"""
    if path.parent != SKILLS_DIR:
        return path.parent.name
    return path.stem.lower()


def parse_skill_file(path: Path) -> Skill:
    """读取并解析一个 SKILL.md。"""
    content = path.read_text(encoding="utf-8", errors="replace")
    metadata, body = parse_simple_frontmatter(content)

    name = metadata.get("name") or skill_name_from_path(path)
    description = metadata.get("description") or first_heading_or_paragraph(body)
    words = set(name.lower().replace("-", " ").split())
    words.update(description.lower().replace("-", " ").split())

    return {
        "name": name.strip(),
        "description": description.strip(),
        "path": str(path.relative_to(WORKDIR)),
        "content": content,
        "keywords": sorted(word.strip(".,:;()[]{}") for word in words if word.strip()),
    }


def discover_skills(root: Path = SKILLS_DIR) -> dict[str, Skill]:
    """发现工作区内的 skills/**/SKILL.md；目录不存在时返回空字典。"""
    if not root.exists() or not root.is_dir():
        return {}

    discovered: dict[str, Skill] = {}
    for path in sorted(root.glob("**/SKILL.md")):
        try:
            resolved = path.resolve()
            if resolved != WORKDIR and WORKDIR not in resolved.parents:
                continue
            skill = parse_skill_file(resolved)
        except Exception:
            continue
        discovered[skill["name"]] = skill
    return discovered


def score_skill(query: str, skill: Skill) -> int:
    """用简单关键词计分选择技能；第 07 天 不引入 embeddings。"""
    query_words = {
        word.strip(".,:;()[]{}!?，。！？、").lower()
        for word in query.replace("-", " ").split()
        if word.strip()
    }
    if not query_words:
        return 0

    haystack = " ".join(
        [
            skill["name"],
            skill["description"],
            " ".join(skill["keywords"]),
            skill["content"][:1000],
        ]
    ).lower()
    return sum(1 for word in query_words if word and word in haystack)


def select_skills(query: str, skills: dict[str, Skill], limit: int = 3) -> list[Skill]:
    """从已发现技能中选出和当前用户输入最相关的几个。"""
    scored = [(score_skill(query, skill), skill["name"], skill) for skill in skills.values()]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [skill for score, _name, skill in scored if score > 0][:limit]


def format_skill_summaries(active_skills: list[Skill]) -> str:
    """只把技能摘要注入 system prompt，不把完整 SKILL.md 全塞进去。"""
    if not active_skills:
        return ""

    lines = [
        "Relevant skills are available for this turn:",
        "Use read_skill(name) if you need the full instructions before applying a skill.",
    ]
    for skill in active_skills:
        lines.append(f"- {skill['name']}: {skill['description']} ({skill['path']})")
    return "\n".join(lines)


MEMORY_DIR = WORKDIR / ".agent_memory"
PROJECT_MEMORY_FILE = MEMORY_DIR / "project.md"
MAX_MEMORY_CHARS = 12_000


def ensure_memory_file() -> None:
    """确保 Memory 目录和项目记忆文件存在。"""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    if not PROJECT_MEMORY_FILE.exists():
        PROJECT_MEMORY_FILE.write_text(
            "# Project Memory\n\n"
            "Stable facts remembered by the local coding agent.\n\n",
            encoding="utf-8",
        )


def read_project_memory() -> str:
    """读取项目级长期记忆；文件不存在时返回空字符串。"""
    if not PROJECT_MEMORY_FILE.exists():
        return ""
    return PROJECT_MEMORY_FILE.read_text(encoding="utf-8", errors="replace")[:MAX_MEMORY_CHARS]


def build_memory_block() -> str:
    """把持久化 Memory 转成 system prompt 可注入的短块。"""
    memory = read_project_memory().strip()
    if not memory:
        return ""
    return (
        "Persistent project memory is available below. Treat it as user/project context, "
        "but prefer newer explicit user instructions if there is a conflict.\n"
        f"{memory}"
    )


def run_read_memory() -> str:
    """工具：读取当前持久化 Memory。"""
    memory = read_project_memory().strip()
    if not memory:
        return "No project memory has been saved yet."
    return memory


def run_remember(text: str) -> str:
    """工具：把稳定事实追加到项目级 Memory 文件。"""
    if not isinstance(text, str) or not text.strip():
        return "Error: text must be a non-empty string."

    cleaned = " ".join(text.strip().split())
    if len(cleaned) > 500:
        return "Error: memory text should be concise and under 500 characters."

    ensure_memory_file()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with PROJECT_MEMORY_FILE.open("a", encoding="utf-8") as file:
        file.write(f"- {timestamp}: {cleaned}\n")
    return f"Remembered in {PROJECT_MEMORY_FILE.relative_to(WORKDIR)}: {cleaned}"


def trim_to_budget(content: str, budget: int) -> str:
    """按字符预算裁剪 section，保留开头并明确标注截断。"""
    if budget <= 0 or len(content) <= budget:
        return content
    marker = "\n... (section truncated by prompt budget)"
    if budget <= len(marker):
        return content[:budget]
    return content[: budget - len(marker)].rstrip() + marker


def make_section(name: str, priority: int, content: str, budget: int) -> PromptSection | None:
    """创建一个非空 prompt section。"""
    cleaned = content.strip()
    if not cleaned:
        return None
    return {
        "name": name,
        "priority": priority,
        "content": cleaned,
        "budget": budget,
    }


def build_prompt_sections(active_skills: list[Skill]) -> list[PromptSection]:
    """收集本轮 system prompt 需要的全部 section。"""
    raw_sections = [
        make_section("identity", 10, BASE_IDENTITY, 1_000),
        make_section("core_behavior", 20, CORE_BEHAVIOR, 3_000),
        make_section("tool_policy", 30, TOOL_POLICY, 3_000),
        make_section("persistent_memory", 40, build_memory_block(), 5_000),
        make_section("active_skills", 50, format_skill_summaries(active_skills), 5_000),
    ]
    return [section for section in raw_sections if section is not None]


def render_prompt_sections(sections: list[PromptSection]) -> str:
    """按优先级和预算把 sections 渲染成最终 system prompt。"""
    rendered: list[str] = []
    used = 0
    for section in sorted(sections, key=lambda item: item["priority"]):
        remaining = PROMPT_BUDGET_CHARS - used
        if remaining <= 0:
            break
        budget = min(section["budget"], remaining)
        content = trim_to_budget(section["content"], budget)
        rendered.append(f"## {section['name']}\n{content}")
        used += len(content)
    return "\n\n".join(rendered)


def build_system_prompt(active_skills: list[Skill]) -> str:
    """把身份、规则、Memory、Skill 组装成最终 system prompt。"""
    return render_prompt_sections(build_prompt_sections(active_skills))


def run_show_system_prompt() -> str:
    """工具：查看当前不含本轮 Skill 匹配的 system prompt。"""
    return build_system_prompt([])[:50_000]


def build_subagent_system_prompt(active_skills: list[Skill]) -> str:
    """子 Agent 也可以看到技能摘要，但仍然只能使用只读工具。"""
    skill_block = format_skill_summaries(active_skills)
    if not skill_block:
        return SUBAGENT_SYSTEM
    return f"{SUBAGENT_SYSTEM}\n\n{skill_block}"


def refresh_skills() -> dict[str, Skill]:
    """重新扫描工作区技能目录，并更新内存态缓存。"""
    LOADED_SKILLS.clear()
    LOADED_SKILLS.update(discover_skills())
    return LOADED_SKILLS


def run_list_skills() -> str:
    """列出当前工作区发现的技能。"""
    skills = refresh_skills()
    if not skills:
        return "No skills found. Create skills/<name>/SKILL.md to add one."

    lines = ["Available skills:"]
    for skill in sorted(skills.values(), key=lambda item: item["name"]):
        lines.append(f"- {skill['name']}: {skill['description']} ({skill['path']})")
    return "\n".join(lines)


def run_read_skill(name: str) -> str:
    """读取一个技能的完整说明。"""
    if not isinstance(name, str) or not name.strip():
        return "Error: name must be a non-empty string."

    skills = refresh_skills()
    skill = skills.get(name.strip())
    if skill is None:
        lowered = name.strip().lower()
        for candidate in skills.values():
            if candidate["name"].lower() == lowered:
                skill = candidate
                break

    if skill is None:
        return f"Error: skill {name!r} not found."

    return (
        f"# Skill: {skill['name']}\n"
        f"Description: {skill['description']}\n"
        f"Path: {skill['path']}\n\n"
        f"{skill['content']}"
    )[:50_000]


COMPACT_TRIGGER_CHARS = 30_000
COMPACT_KEEP_RECENT = 6
MAX_COMPACT_INPUT_CHARS = 40_000
MAX_COMPACT_SUMMARY_CHARS = 8_000
COMPACT_SUMMARY_PREFIX = "[compact summary]"


def stringify_for_context(value: Any) -> str:
    """递归把 message/content/tool_result 转成可估算的文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        parts: list[str] = []
        for key, item in value.items():
            parts.append(f"{key}: {stringify_for_context(item)}")
        return "\n".join(parts)
    if isinstance(value, list):
        return "\n".join(stringify_for_context(item) for item in value)

    block_type = getattr(value, "type", None)
    if block_type == "text":
        return str(getattr(value, "text", ""))
    if block_type == "tool_use":
        return f"tool_use {getattr(value, 'name', '')}: {getattr(value, 'input', '')}"
    return str(value)


def estimate_context_chars(messages: list[dict[str, Any]]) -> int:
    """用字符数近似估算当前 conversation 的上下文压力。"""
    return sum(len(stringify_for_context(message)) for message in messages)


def extract_message_text(message: dict[str, Any]) -> str:
    """把一条 message 转成压缩模型可读的文本。"""
    role = message.get("role", "unknown")
    content = stringify_for_context(message.get("content", ""))
    return f"## {role}\n{content}".strip()


def build_compact_prompt(old_messages: list[dict[str, Any]]) -> str:
    """构造压缩提示，要求模型输出当前会话的结构化摘要。"""
    chunks: list[str] = []
    used = 0
    for message in old_messages:
        text = extract_message_text(message)
        remaining = MAX_COMPACT_INPUT_CHARS - used
        if remaining <= 0:
            break
        if len(text) > remaining:
            text = text[:remaining] + "\n... (truncated for compact input)"
        chunks.append(text)
        used += len(text)

    transcript = "\n\n".join(chunks)
    return (
        "Summarize the following earlier conversation for a coding agent that will continue "
        "the same session. Keep facts that affect future actions.\n\n"
        "Include these sections:\n"
        "- User goal\n"
        "- Current plan / todo state\n"
        "- Important facts discovered\n"
        "- Files touched or read\n"
        "- Decisions made\n"
        "- Open questions / next steps\n\n"
        "Earlier conversation:\n"
        f"{transcript}"
    )


def compact_messages(messages: list[dict[str, Any]], system_prompt: str) -> bool:
    """把旧消息压缩成一条摘要消息，并保留最近窗口。"""
    if len(messages) <= COMPACT_KEEP_RECENT + 1:
        return False

    old_messages = messages[:-COMPACT_KEEP_RECENT]
    recent_messages = messages[-COMPACT_KEEP_RECENT:]
    prompt = build_compact_prompt(old_messages)

    response = client.messages.create(
        model=MODEL,
        system=system_prompt,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000,
    )
    summary = extract_text(response.content)[:MAX_COMPACT_SUMMARY_CHARS]
    if not summary:
        summary = "Compaction ran, but the summary model returned no text."

    messages[:] = [
        {
            "role": "user",
            "content": f"{COMPACT_SUMMARY_PREFIX}\n{summary}",
        },
        *recent_messages,
    ]
    print(
        f"\033[90m[compact] replaced {len(old_messages)} old messages with summary, "
        f"kept {len(recent_messages)} recent messages\033[0m"
    )
    return True


def maybe_compact_messages(messages: list[dict[str, Any]], system_prompt: str) -> bool:
    """在模型调用前按阈值自动压缩上下文。"""
    size = estimate_context_chars(messages)
    if size < COMPACT_TRIGGER_CHARS:
        return False

    print(f"\033[90m[compact] context {size} chars, compacting older messages...\033[0m")
    return compact_messages(messages, system_prompt)


# 永远拒绝：命中后不询问，直接返回 Permission denied。
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

# 需要询问：不是必然危险，但会修改环境或依赖。
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

# Hook 注册表：事件名 -> 回调列表。
HOOKS: dict[HookEvent, list[HookCallback]] = {
    "UserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
}


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


def safe_path(path: str) -> Path:
    """把模型传来的路径限制在当前工作目录内。"""
    resolved = (WORKDIR / path).resolve()
    if resolved != WORKDIR and WORKDIR not in resolved.parents:
        raise ValueError(f"Path escapes workspace: {path}")
    return resolved


def run_bash(command: str) -> str:
    """执行 shell 命令。权限判断不写在这里，而是作为 PreToolUse hook。"""
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 120 seconds."
    except OSError as exc:
        return f"Error: {exc}"

    output = (result.stdout + result.stderr).strip()
    if not output:
        output = f"(no output, exit code {result.returncode})"
    elif result.returncode != 0:
        output = f"(exit code {result.returncode})\n{output}"

    return output[:50_000]


def run_read(path: str, limit: int | None = None) -> str:
    """读取文件，并带上行号返回，方便模型后续精确编辑。"""
    try:
        file_path = safe_path(path)
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"Error: {exc}"

    lines = text.splitlines()
    if limit is not None and limit > 0 and len(lines) > limit:
        shown = lines[:limit]
        shown.append(f"... ({len(lines) - limit} more lines)")
    else:
        shown = lines

    numbered = [f"{index + 1:>4} | {line}" for index, line in enumerate(shown)]
    return "\n".join(numbered)[:50_000] if numbered else "(empty file)"


def run_write(path: str, content: str) -> str:
    """写入文件；父目录不存在时自动创建。"""
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
    except Exception as exc:
        return f"Error: {exc}"

    return f"Wrote {len(content)} characters to {path}."


def run_edit(path: str, old_text: str, new_text: str) -> str:
    """对文件做一次精确字符串替换。"""
    try:
        file_path = safe_path(path)
        content = file_path.read_text(encoding="utf-8", errors="replace")
        if old_text not in content:
            return f"Error: old_text not found in {path}."

        updated = content.replace(old_text, new_text, 1)
        file_path.write_text(updated, encoding="utf-8")
    except Exception as exc:
        return f"Error: {exc}"

    return f"Edited {path}."


def normalize_todo(raw: Any, index: int) -> TodoItem:
    """校验模型传来的单个 todo，并转成内部结构。"""
    if not isinstance(raw, dict):
        raise ValueError(f"todos[{index}] must be an object")

    content = raw.get("content")
    status = raw.get("status")
    priority = raw.get("priority")

    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"todos[{index}].content must be a non-empty string")
    if status not in {"pending", "in_progress", "completed"}:
        raise ValueError(
            f"todos[{index}].status must be pending, in_progress, or completed"
        )
    if priority not in {"high", "medium", "low"}:
        raise ValueError(f"todos[{index}].priority must be high, medium, or low")

    return {
        "content": content.strip(),
        "status": status,
        "priority": priority,
    }


def format_todos(todos: list[TodoItem]) -> str:
    """把当前 todo list 格式化成人和模型都容易读的摘要。"""
    if not todos:
        return "Todo list is empty."

    icons = {
        "pending": "□",
        "in_progress": "▶",
        "completed": "✓",
    }
    lines = ["Current todo list:"]
    for index, todo in enumerate(todos, start=1):
        icon = icons[todo["status"]]
        lines.append(
            f"{index}. {icon} [{todo['priority']}] {todo['content']} ({todo['status']})"
        )
    return "\n".join(lines)


def run_todo_write(todos: list[Any]) -> str:
    """整体替换当前任务列表，并返回新的可见状态。"""
    if not isinstance(todos, list):
        return "Error: todos must be a list."

    try:
        normalized = [normalize_todo(raw, index) for index, raw in enumerate(todos)]
    except ValueError as exc:
        return f"Error: {exc}"

    in_progress_count = sum(1 for todo in normalized if todo["status"] == "in_progress")
    if in_progress_count > 1:
        return "Error: todo list can have at most one in_progress item."

    TODOS.clear()
    TODOS.extend(normalized)
    return format_todos(TODOS)


def deny_reason(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """返回必须直接拒绝的原因；没有命中则返回 None。"""
    if tool_name != "bash":
        return None

    command = str(tool_input.get("command", "")).lower()
    for fragment in DENIED_BASH_FRAGMENTS:
        if fragment in command:
            return f"bash command contains denied fragment: {fragment!r}"
    return None


def permission_decision(
    tool_name: str,
    tool_input: dict[str, Any],
) -> tuple[PermissionDecision, str]:
    """把一次工具调用分类成 allow / ask / deny。"""
    reason = deny_reason(tool_name, tool_input)
    if reason:
        return "deny", reason

    if tool_name in {"write_file", "edit_file", "remember"}:
        return "ask", f"{tool_name} modifies files in the workspace."

    if tool_name == "bash":
        command = str(tool_input.get("command", "")).lower()
        for fragment in ASK_BASH_FRAGMENTS:
            if fragment in command:
                return "ask", f"bash command may change files or environment: {fragment!r}"

    return "allow", "safe by current Day 10 policy"


def ask_user_permission(tool_name: str, tool_input: dict[str, Any], reason: str) -> bool:
    """把 ask 决策交给终端用户；默认拒绝。"""
    print(f"\n\033[33m⚠ Permission required: {tool_name}\033[0m")
    print(f"Reason: {reason}")
    print(f"Input: {tool_input}")
    choice = input("Allow? [y/N] ").strip().lower()
    return choice in {"y", "yes"}


def check_permission(tool_name: str, tool_input: dict[str, Any]) -> tuple[bool, str]:
    """工具执行前的统一权限入口。"""
    decision, reason = permission_decision(tool_name, tool_input)

    if decision == "allow":
        return True, reason

    if decision == "deny":
        print(f"\n\033[31m⛔ Permission denied: {reason}\033[0m")
        return False, reason

    if ask_user_permission(tool_name, tool_input, reason):
        return True, "approved by user"

    return False, f"user rejected permission request: {reason}"


ToolHandler = Callable[..., str]

SUBAGENT_SYSTEM = (
    f"You are a focused research subagent working in {WORKDIR}. "
    "Investigate the user's prompt using only the tools provided to you. "
    "Do not modify files. Do not make plans for the main agent. "
    "Return a concise report with file paths and line numbers when relevant."
)


def extract_text(content: Any) -> str:
    """从 assistant content blocks 中提取文本，供子 Agent 汇报给主 Agent。"""
    if isinstance(content, str):
        return content

    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


def run_subagent(description: str, prompt: str, max_turns: int = 6) -> str:
    """启动一个独立上下文的子 Agent，并返回它的最终调查报告。"""
    if not isinstance(description, str) or not description.strip():
        return "Error: description must be a non-empty string."
    if not isinstance(prompt, str) or not prompt.strip():
        return "Error: prompt must be a non-empty string."
    if not MODEL:
        return "Error: MODEL_ID is not set."

    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                f"Task description: {description.strip()}\n\n"
                f"Detailed prompt:\n{prompt.strip()}\n\n"
                "When you are done, return only the final report."
            ),
        }
    ]

    last_text = ""
    for turn in range(1, max_turns + 1):
        response = client.messages.create(
            model=MODEL,
            system=build_subagent_system_prompt(select_skills(prompt, LOADED_SKILLS)),
            messages=messages,
            tools=SUBAGENT_TOOLS,
            max_tokens=4000,
        )
        messages.append({"role": "assistant", "content": response.content})
        last_text = extract_text(response.content)

        if response.stop_reason != "tool_use":
            return (last_text or "Subagent finished without text output.")[:50_000]

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            handler = SUBAGENT_TOOL_HANDLERS.get(block.name)
            if handler is None:
                output = f"Error: subagent cannot use tool {block.name!r}."
            else:
                print(f"\033[35m[subagent:{description}] > {block.name}: {block.input}\033[0m")
                blocked = trigger_hooks("PreToolUse", block.name, block.input)
                if blocked is not None:
                    output = blocked
                else:
                    try:
                        output = handler(**block.input)
                    except Exception as exc:
                        output = f"Error: {exc}"
                    trigger_hooks("PostToolUse", block.name, block.input, output)
                print(output[:500])

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                }
            )

        messages.append({"role": "user", "content": tool_results})

    suffix = last_text or "No final report before max_turns."
    return (
        f"Error: subagent reached max_turns={max_turns} before finishing.\n\n"
        f"Last partial output:\n{suffix}"
    )[:50_000]


def run_task(description: str, prompt: str) -> str:
    """主 Agent 通过 task 工具启动子 Agent。"""
    print(f"\033[35m[subagent:start] {description}\033[0m")
    report = run_subagent(description, prompt)
    print(f"\033[35m[subagent:done] {description}\033[0m")
    return f"Subagent report for {description!r}:\n{report}"


SUBAGENT_TOOL_HANDLERS: dict[str, ToolHandler] = {
    "bash": lambda **kwargs: run_bash(kwargs["command"]),
    "read_file": lambda **kwargs: run_read(kwargs["path"], kwargs.get("limit")),
    "list_skills": lambda **kwargs: run_list_skills(),
    "read_skill": lambda **kwargs: run_read_skill(kwargs["name"]),
    "read_memory": lambda **kwargs: run_read_memory(),
    "show_system_prompt": lambda **kwargs: run_show_system_prompt(),
}


TOOL_HANDLERS: dict[str, ToolHandler] = {
    "bash": lambda **kwargs: run_bash(kwargs["command"]),
    "read_file": lambda **kwargs: run_read(kwargs["path"], kwargs.get("limit")),
    "write_file": lambda **kwargs: run_write(kwargs["path"], kwargs["content"]),
    "edit_file": lambda **kwargs: run_edit(
        kwargs["path"],
        kwargs["old_text"],
        kwargs["new_text"],
    ),
    "todo_write": lambda **kwargs: run_todo_write(kwargs["todos"]),
    "task": lambda **kwargs: run_task(kwargs["description"], kwargs["prompt"]),
    "list_skills": lambda **kwargs: run_list_skills(),
    "read_skill": lambda **kwargs: run_read_skill(kwargs["name"]),
    "read_memory": lambda **kwargs: run_read_memory(),
    "remember": lambda **kwargs: run_remember(kwargs["text"]),
    "show_system_prompt": lambda **kwargs: run_show_system_prompt(),
}

TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command in the current working directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run."}
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a UTF-8 text file from the workspace. Returns content with line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the workspace."},
                "limit": {"type": "integer", "description": "Optional maximum number of lines."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write UTF-8 text content to a file in the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the workspace."},
                "content": {"type": "string", "description": "Full file content to write."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace the first exact occurrence of old_text in a workspace file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the workspace."},
                "old_text": {"type": "string", "description": "Exact text to replace."},
                "new_text": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },

    {
        "name": "list_skills",
        "description": "List workspace skills discovered from skills/**/SKILL.md.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "read_skill",
        "description": "Read the full instructions for a discovered workspace skill by name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill name returned by list_skills or shown in the system prompt.",
                }
            },
            "required": ["name"],
        },
    },
    {
        "name": "read_memory",
        "description": "Read persistent project memory saved in .agent_memory/project.md.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "remember",
        "description": (
            "Persist a concise stable fact, user preference, or project convention "
            "to project memory for future turns and restarts. Do not store secrets."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Concise stable memory to append, under 500 characters.",
                }
            },
            "required": ["text"],
        },
    },
    {
        "name": "show_system_prompt",
        "description": "Show the currently assembled system prompt without turn-specific skill matches.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },

    {
        "name": "task",
        "description": (
            "Launch a focused subagent for research or analysis. "
            "The subagent has its own short conversation and returns a concise report."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Short description shown in logs.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Detailed instructions for the subagent.",
                },
            },
            "required": ["description", "prompt"],
        },
    },
    {
        "name": "todo_write",
        "description": (
            "Replace the visible task list for multi-step work. "
            "Use statuses pending, in_progress, completed and priorities high, medium, low. "
            "There must be at most one in_progress item."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "The full replacement todo list.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "Task description."},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                            },
                        },
                        "required": ["content", "status", "priority"],
                    },
                }
            },
            "required": ["todos"],
        },
    },
]

SUBAGENT_TOOLS = [
    tool for tool in TOOLS if tool["name"] in {"bash", "read_file", "list_skills", "read_skill", "read_memory", "show_system_prompt"}
]


def user_prompt_log_hook(query: str) -> str | None:
    """用户输入进入模型前，记录一次入口事件。"""
    print(f"\033[90m[hook:UserPromptSubmit] {query[:80]}\033[0m")
    return None


def log_hook(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """工具执行前记录日志。"""
    print(f"\033[90m[hook:PreToolUse] {tool_name}: {tool_input}\033[0m")
    return None


def permission_hook(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """把权限检查挂到 PreToolUse 生命周期。"""
    allowed, reason = check_permission(tool_name, tool_input)
    if allowed:
        return None
    return f"Permission denied: {reason}"


def large_output_hook(tool_name: str, tool_input: dict[str, Any], output: str) -> str | None:
    """工具执行后观察输出；这里先只做日志，不修改结果。"""
    if len(output) > 20_000:
        print(
            f"\033[90m[hook:PostToolUse] {tool_name} returned "
            f"{len(output)} characters; handler already truncated if needed.\033[0m"
        )
    return None


def todo_summary_hook(messages: list[dict[str, Any]]) -> str | None:
    """Agent 准备停止前打印当前 todo 状态，让计划对 Harness 可见。"""
    print(f"\033[90m[hook:Stop] conversation has {len(messages)} messages\033[0m")
    if TODOS:
        print("\033[90m[hook:Stop] todo state:\033[0m")
        print(format_todos(TODOS))
    return None


register_hook("UserPromptSubmit", user_prompt_log_hook)
register_hook("PreToolUse", log_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", todo_summary_hook)


def print_assistant_text(content: Any) -> None:
    """打印 assistant 回复里的文本块。"""
    if isinstance(content, str):
        print(content)
        return

    for block in content:
        if getattr(block, "type", None) == "text":
            print(block.text)


def agent_loop(messages: list[dict[str, Any]], system_prompt: str) -> None:
    """Agent Loop 接收本轮动态 system prompt，其余流程保持不变。"""
    if not MODEL:
        raise RuntimeError("请先在环境变量或 .env 中设置 MODEL_ID。")

    while True:
        maybe_compact_messages(messages, system_prompt)
        response = client.messages.create(
            model=MODEL,
            system=system_prompt,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            forced = trigger_hooks("Stop", messages)
            if forced:
                messages.append({"role": "user", "content": forced})
                continue

            print_assistant_text(response.content)
            return

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            handler = TOOL_HANDLERS.get(block.name)
            if handler is None:
                output = f"Error: unknown tool {block.name!r}."
            else:
                print(f"\033[33m> {block.name}: {block.input}\033[0m")
                blocked = trigger_hooks("PreToolUse", block.name, block.input)
                if blocked is not None:
                    output = blocked
                else:
                    try:
                        output = handler(**block.input)
                    except Exception as exc:
                        output = f"Error: {exc}"
                    trigger_hooks("PostToolUse", block.name, block.input, output)
                print(output[:500])

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                }
            )

        messages.append({"role": "user", "content": tool_results})


def main() -> None:
    """启动一个结构化组装 System Prompt 的最小 Agent。"""
    ensure_memory_file()
    print("Day 10 System Prompt. 输入任务开始，输入 q / exit / 空行退出。")
    history: list[dict[str, Any]] = []

    while True:
        try:
            query = input("\033[36ms10 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if query.strip().lower() in {"", "q", "exit"}:
            break

        trigger_hooks("UserPromptSubmit", query)
        skills = refresh_skills()
        active_skills = select_skills(query, skills)
        if active_skills:
            names = ", ".join(skill["name"] for skill in active_skills)
            print(f"[90m[skills] active: {names}[0m")
        history.append({"role": "user", "content": query})
        agent_loop(history, build_system_prompt(active_skills))


if __name__ == "__main__":
    main()
