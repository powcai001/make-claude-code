#!/usr/bin/env python3
"""Day 17: Autonomous Agents.

Day 16 的 Agent 已经能按团队协议稳定协作。本章继续增加 Autonomous Agents：
把目标、预算、停止条件和阶段性执行记录放进 Harness，让 Agent 可以围绕一个目标
自主推进多个 step，而不是每一步都等待用户明确下达下一条指令。

这个版本故意保持保守：默认只执行一个可审计 step，长程自主运行必须有 max_steps、
时间预算和可查看的运行记录。

    autonomous_start -> AutonomyRun -> autonomous_step -> task/team tools -> stop condition

Autonomous Agents 的重点不是“无限自动干活”，而是让自主循环有边界、有记录、可暂停、可恢复。
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
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
When you need focused research, use task to launch a tracked subagent task.
For long-running research, set run_in_background=true and poll task_list or task_read later.
Use cron_add only for recurring, bounded, user-approved checks.
Use team_run when a problem benefits from multiple specialized perspectives.
Prefer built-in team protocols (plan, debate, redteam) over ad-hoc member lists when the workflow is well-known.
Use autonomous_start only for bounded goals with clear stop conditions and budgets.
Prefer small, reversible edits and explain important trade-offs.
When a tool returns an error, inspect the recovery hint before retrying.
The todo list must have at most one in_progress item."""

TOOL_POLICY = """Tool rules:
- Use read_file before editing unfamiliar files.
- Use write_file or edit_file only for workspace files.
- Use list_skills and read_skill when a relevant skill may help.
- Use remember to persist stable user preferences, project conventions, or important facts.
- Do not store secrets in Memory.
- Do not repeat the same failing tool call without changing inputs.
- Keep scheduled cron jobs bounded and easy to inspect.
- Keep agent teams small and give each member a distinct role.
- Reuse team protocols so the same kind of problem always runs the same way.
- Keep autonomous runs bounded by max_steps, deadline, and explicit success criteria."""

PROMPT_BUDGET_CHARS = 18_000

PermissionDecision = Literal["allow", "deny", "ask"]
HookEvent = Literal["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]
HookCallback = Callable[..., str | None]
TodoStatus = Literal["pending", "in_progress", "completed"]
TodoPriority = Literal["high", "medium", "low"]
TaskStatus = Literal["queued", "running", "completed", "failed"]
CronStatus = Literal["enabled", "paused"]
ProtocolName = Literal["plan", "debate", "redteam", "custom"]
AutonomyStatus = Literal["running", "paused", "completed", "failed"]
ErrorKind = Literal["permission", "validation", "not_found", "timeout", "transient", "unknown"]


class PromptSection(TypedDict):
    """System Prompt 的一个可组装片段。"""

    name: str
    priority: int
    content: str
    budget: int


class ErrorRecord(TypedDict):
    """最近一次工具或模型错误的结构化记录。"""

    kind: ErrorKind
    where: str
    message: str
    hint: str


class TodoItem(TypedDict):
    """TodoWrite 维护的最小任务结构。"""

    content: str
    status: TodoStatus
    priority: TodoPriority


class TaskRecord(TypedDict):
    """Task System 维护的子任务生命周期记录。"""

    id: str
    description: str
    prompt: str
    status: TaskStatus
    created_at: str
    started_at: str | None
    finished_at: str | None
    turns: int
    result: str
    error: str | None
    background: bool


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


class TeamMember(TypedDict):
    """Agent Team 中一个带角色的成员。"""

    name: str
    role: str
    prompt: str


class ProtocolStage(TypedDict):
    """Team Protocol 中的一个阶段。"""

    name: str
    role: str
    prompt: str


class TeamProtocol(TypedDict):
    """可复用的团队协作协议模板。"""

    name: ProtocolName
    description: str
    stages: list[ProtocolStage]


class TeamRun(TypedDict):
    """一次团队协作运行记录。"""

    id: str
    objective: str
    protocol: ProtocolName
    members: list[TeamMember]
    status: TaskStatus
    created_at: str
    finished_at: str | None
    task_ids: list[str]
    report: str
    error: str | None


class AutonomyRun(TypedDict):
    """一次有边界的自主运行记录。"""

    id: str
    goal: str
    success_criteria: str
    status: AutonomyStatus
    created_at: str
    updated_at: str
    max_steps: int
    step_count: int
    deadline_at: float | None
    notes: list[str]
    task_ids: list[str]
    team_ids: list[str]
    final_report: str
    error: str | None


# TodoWrite 在 Day 17 仍然只做当前会话计划；长期事实交给 Memory。
TODOS: list[TodoItem] = []
ERROR_HISTORY: list[ErrorRecord] = []
MAX_ERROR_HISTORY = 20
TASKS: dict[str, TaskRecord] = {}
TASK_THREADS: dict[str, threading.Thread] = {}
TASK_LOCK = threading.RLock()
NEXT_TASK_ID = 1
CRON_JOBS: dict[str, CronJob] = {}
CRON_LOCK = threading.RLock()
NEXT_CRON_ID = 1
CRON_SCHEDULER_THREAD: threading.Thread | None = None
CRON_SCHEDULER_STOP = threading.Event()
TEAM_RUNS: dict[str, TeamRun] = {}
TEAM_LOCK = threading.RLock()
NEXT_TEAM_ID = 1
TEAM_PROTOCOLS: dict[str, TeamProtocol] = {}
AUTONOMY_RUNS: dict[str, AutonomyRun] = {}
AUTONOMY_LOCK = threading.RLock()
NEXT_AUTONOMY_ID = 1



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

    response = call_model_with_retries(
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


def classify_error(message: str) -> ErrorKind:
    """把非结构化错误文本粗略分类，供恢复策略使用。"""
    lowered = message.lower()
    if "permission denied" in lowered or "user rejected" in lowered:
        return "permission"
    if "must be" in lowered or "required" in lowered or "invalid" in lowered:
        return "validation"
    if "not found" in lowered or "no such file" in lowered:
        return "not_found"
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if "rate limit" in lowered or "overloaded" in lowered or "temporarily" in lowered:
        return "transient"
    return "unknown"


def recovery_hint(kind: ErrorKind, where: str, message: str) -> str:
    """根据错误类型生成给模型看的下一步建议。"""
    if kind == "permission":
        return "Do not retry the same action. Explain why it was blocked or ask the user for a safer alternative."
    if kind == "validation":
        return "Fix the tool input schema or missing arguments before retrying."
    if kind == "not_found":
        return "Check the path/name with a read-only tool before trying again."
    if kind == "timeout":
        return "Retry with a narrower command, smaller input, or split the work into steps."
    if kind == "transient":
        return "The operation may succeed later. Retry only if the action is safe and bounded."
    return "Inspect the error, change strategy, and avoid repeating the exact same failing call."


def record_error(where: str, message: str) -> ErrorRecord:
    """记录最近错误，并返回结构化错误对象。"""
    kind = classify_error(message)
    record: ErrorRecord = {
        "kind": kind,
        "where": where,
        "message": message[:2_000],
        "hint": recovery_hint(kind, where, message),
    }
    ERROR_HISTORY.append(record)
    del ERROR_HISTORY[:-MAX_ERROR_HISTORY]
    return record


def format_error_record(record: ErrorRecord) -> str:
    """把错误记录渲染成工具结果可读的恢复提示。"""
    return (
        f"[error_recovery]\n"
        f"kind: {record['kind']}\n"
        f"where: {record['where']}\n"
        f"hint: {record['hint']}"
    )


def attach_recovery_hint(where: str, output: str) -> str:
    """当工具结果看起来是错误时，追加恢复建议。"""
    stripped = output.strip()
    lowered = stripped.lower()
    is_error = (
        lowered.startswith("error:")
        or lowered.startswith("permission denied")
        or lowered.startswith("(exit code")
    )
    if not is_error:
        return output
    record = record_error(where, output)
    return f"{output}\n\n{format_error_record(record)}"


def run_show_errors() -> str:
    """工具：查看最近错误和恢复建议。"""
    if not ERROR_HISTORY:
        return "No errors recorded yet."
    lines: list[str] = ["Recent errors:"]
    for index, record in enumerate(ERROR_HISTORY, start=1):
        lines.append(
            f"{index}. [{record['kind']}] {record['where']}: "
            f"{record['message'][:160]}\n   hint: {record['hint']}"
        )
    return "\n".join(lines)


def call_model_with_retries(**kwargs: Any) -> Any:
    """调用模型 API；对短暂错误做有限重试，对最终失败给出结构化记录。"""
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            return client.messages.create(**kwargs)
        except Exception as exc:
            last_error = exc
            message = f"{type(exc).__name__}: {exc}"
            kind = classify_error(message)
            if kind != "transient" and attempt == 1:
                break
            if attempt < 3:
                time.sleep(0.5 * attempt)

    message = f"{type(last_error).__name__}: {last_error}" if last_error else "unknown model error"
    record = record_error("model_api", message)
    raise RuntimeError(format_error_record(record)) from last_error


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

    return "allow", "safe by current Day 17 policy"


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


def new_task_record(description: str, prompt: str, background: bool = False) -> TaskRecord:
    """创建一个带唯一 ID 的子任务记录。"""
    global NEXT_TASK_ID
    task_id = f"task-{NEXT_TASK_ID:04d}"
    NEXT_TASK_ID += 1
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    record: TaskRecord = {
        "id": task_id,
        "description": description.strip(),
        "prompt": prompt.strip(),
        "status": "queued",
        "created_at": now,
        "started_at": None,
        "finished_at": None,
        "turns": 0,
        "result": "",
        "error": None,
        "background": background,
    }
    with TASK_LOCK:
        TASKS[task_id] = record
    return record


def finish_task(record: TaskRecord, status: TaskStatus, result: str, error: str | None = None) -> None:
    """更新子任务最终状态。"""
    with TASK_LOCK:
        record["status"] = status
        record["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        record["result"] = result[:50_000]
        record["error"] = error


def format_task_record(record: TaskRecord, include_prompt: bool = False) -> str:
    """把子任务记录渲染成人类和模型都容易读的文本。"""
    lines = [
        f"{record['id']} [{record['status']}] {record['description']}",
        f"created: {record['created_at']}",
        f"started: {record['started_at'] or '-'}",
        f"finished: {record['finished_at'] or '-'}",
        f"turns: {record['turns']}",
        f"background: {record.get('background', False)}",
    ]
    if include_prompt:
        lines.append(f"prompt:\n{record['prompt']}")
    if record["error"]:
        lines.append(f"error:\n{record['error']}")
    if record["result"]:
        lines.append(f"result:\n{record['result'][:4_000]}")
    return "\n".join(lines)


def run_task_list() -> str:
    """工具：列出当前会话内的子任务状态。"""
    with TASK_LOCK:
        if not TASKS:
            return "No subagent tasks have been created yet."
        records = list(TASKS.values())
    return "\n\n".join(format_task_record(record) for record in records)


def run_task_read(task_id: str) -> str:
    """工具：读取某个子任务的完整记录。"""
    with TASK_LOCK:
        record = TASKS.get(task_id)
    if record is None:
        return f"Error: task {task_id!r} not found."
    return format_task_record(record, include_prompt=True)


def run_subagent(record: TaskRecord, max_turns: int = 6) -> str:
    """启动一个独立上下文的子 Agent，并把生命周期写入 TaskRecord。"""
    description = record["description"]
    prompt = record["prompt"]
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description must be a non-empty string")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    if not MODEL:
        raise RuntimeError("MODEL_ID is not set")

    with TASK_LOCK:
        record["status"] = "running"
        record["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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
        with TASK_LOCK:
            record["turns"] = turn
        response = call_model_with_retries(
            model=MODEL,
            system=build_subagent_system_prompt(select_skills(prompt, LOADED_SKILLS)),
            messages=messages,
            tools=SUBAGENT_TOOLS,
            max_tokens=4000,
        )
        messages.append({"role": "assistant", "content": response.content})
        last_text = extract_text(response.content)

        if response.stop_reason != "tool_use":
            result = (last_text or "Subagent finished without text output.")[:50_000]
            finish_task(record, "completed", result)
            return result

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            handler = SUBAGENT_TOOL_HANDLERS.get(block.name)
            if handler is None:
                output = attach_recovery_hint(
                    f"subagent.{block.name}",
                    f"Error: subagent cannot use tool {block.name!r}.",
                )
            else:
                print(f"\033[35m[subagent:{description}] > {block.name}: {block.input}\033[0m")
                blocked = trigger_hooks("PreToolUse", block.name, block.input)
                if blocked is not None:
                    output = attach_recovery_hint(f"subagent.{block.name}", blocked)
                else:
                    try:
                        output = handler(**block.input)
                    except Exception as exc:
                        output = f"Error: {exc}"
                    output = attach_recovery_hint(f"subagent.{block.name}", output)
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
    result = (
        f"Error: subagent reached max_turns={max_turns} before finishing.\n\n"
        f"Last partial output:\n{suffix}"
    )[:50_000]
    finish_task(record, "failed", result, error=f"max_turns={max_turns}")
    return result


def run_background_task(record: TaskRecord) -> None:
    """后台线程入口：运行子 Agent 并把最终状态写回 TaskRecord。"""
    try:
        run_subagent(record)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        finish_task(record, "failed", error, error=error)


def run_task(description: str, prompt: str, run_in_background: bool = False) -> str:
    """主 Agent 通过 task 工具启动一个可同步或后台运行的子 Agent。"""
    if not isinstance(description, str) or not description.strip():
        return "Error: description must be a non-empty string."
    if not isinstance(prompt, str) or not prompt.strip():
        return "Error: prompt must be a non-empty string."

    record = new_task_record(description, prompt, background=bool(run_in_background))
    if run_in_background:
        thread = threading.Thread(
            target=run_background_task,
            args=(record,),
            name=f"background-{record['id']}",
            daemon=True,
        )
        with TASK_LOCK:
            TASK_THREADS[record["id"]] = thread
        thread.start()
        return (
            f"Started background task {record['id']} for {description!r}.\n"
            f"Use task_list to poll status, task_read with task_id={record['id']!r} "
            "to inspect details, or task_wait to block until it finishes."
        )

    print(f"\033[35m[task:start] {record['id']} {description}\033[0m")
    try:
        report = run_subagent(record)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        finish_task(record, "failed", error, error=error)
        print(f"\033[35m[task:failed] {record['id']}\033[0m")
        return attach_recovery_hint("task", f"Error: task {record['id']} failed: {error}")

    print(f"\033[35m[task:done] {record['id']}\033[0m")
    return (
        f"Task {record['id']} completed for {description!r}.\n"
        f"Use task_read with task_id={record['id']!r} to inspect the full record.\n\n"
        f"Subagent report:\n{report}"
    )


def run_task_wait(task_id: str, timeout_seconds: float = 30.0) -> str:
    """工具：等待一个后台任务结束，或在超时后返回当前状态。"""
    with TASK_LOCK:
        record = TASKS.get(task_id)
        thread = TASK_THREADS.get(task_id)
    if record is None:
        return f"Error: task {task_id!r} not found."
    if thread is None:
        return format_task_record(record, include_prompt=True)

    timeout = max(0.0, min(float(timeout_seconds), 300.0))
    thread.join(timeout=timeout)
    if thread.is_alive():
        return f"Task {task_id} is still running after waiting {timeout:g}s.\n\n{format_task_record(record)}"
    return format_task_record(record, include_prompt=True)


def format_timestamp(timestamp: float | None) -> str:
    """把 Unix timestamp 渲染成易读时间。"""
    if timestamp is None:
        return "-"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


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


def format_cron_job(job: CronJob) -> str:
    """把 CronJob 渲染成可读文本。"""
    max_runs = job["max_runs"] if job["max_runs"] is not None else "unlimited"
    return "\n".join(
        [
            f"{job['id']} [{job['status']}] {job['description']}",
            f"interval_seconds: {job['interval_seconds']:g}",
            f"created: {job['created_at']}",
            f"last_run: {format_timestamp(job['last_run_at'])}",
            f"next_run: {format_timestamp(job['next_run_at'])}",
            f"run_count: {job['run_count']}/{max_runs}",
            f"task_ids: {', '.join(job['task_ids']) or '-'}",
        ]
    )


def run_cron_list() -> str:
    """工具：列出所有周期任务。"""
    with CRON_LOCK:
        if not CRON_JOBS:
            return "No cron jobs have been scheduled yet."
        jobs = list(CRON_JOBS.values())
    return "\n\n".join(format_cron_job(job) for job in jobs)


def run_cron_read(cron_id: str) -> str:
    """工具：读取一个周期任务详情。"""
    with CRON_LOCK:
        job = CRON_JOBS.get(cron_id)
    if job is None:
        return f"Error: cron job {cron_id!r} not found."
    return f"{format_cron_job(job)}\n\nprompt:\n{job['prompt']}"


def due_cron_jobs(now: float | None = None) -> list[CronJob]:
    """取出当前到期且可运行的周期任务。"""
    current = time.time() if now is None else now
    with CRON_LOCK:
        return [
            job
            for job in CRON_JOBS.values()
            if job["status"] == "enabled"
            and job["next_run_at"] <= current
            and (job["max_runs"] is None or job["run_count"] < job["max_runs"])
        ]


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


def run_cron_tick() -> str:
    """工具：手动触发一次调度扫描，运行所有到期 cron。"""
    jobs = due_cron_jobs()
    if not jobs:
        return "No cron jobs are due."
    return "\n".join(trigger_cron_job(job) for job in jobs)


def cron_scheduler_loop(poll_seconds: float = 1.0) -> None:
    """后台调度循环：定期扫描到期任务。"""
    while not CRON_SCHEDULER_STOP.is_set():
        for job in due_cron_jobs():
            try:
                trigger_cron_job(job)
            except Exception as exc:
                record_error("cron_scheduler", f"Error: {exc}")
        CRON_SCHEDULER_STOP.wait(poll_seconds)


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


def run_cron_add(
    description: str,
    prompt: str,
    interval_seconds: float,
    max_runs: int | None = None,
) -> str:
    """工具：登记一个周期性后台任务。"""
    if not isinstance(description, str) or not description.strip():
        return "Error: description must be a non-empty string."
    if not isinstance(prompt, str) or not prompt.strip():
        return "Error: prompt must be a non-empty string."
    try:
        interval = float(interval_seconds)
    except (TypeError, ValueError):
        return "Error: interval_seconds must be a number."
    if interval < 5:
        return "Error: interval_seconds must be at least 5 seconds."
    runs = None if max_runs is None else int(max_runs)
    if runs is not None and runs <= 0:
        return "Error: max_runs must be positive when provided."

    job = new_cron_job(description, prompt, interval, runs)
    ensure_cron_scheduler_started()
    return f"Scheduled {job['id']} every {interval:g}s. Next run: {format_timestamp(job['next_run_at'])}"


def run_cron_pause(cron_id: str) -> str:
    """工具：暂停一个周期任务。"""
    with CRON_LOCK:
        job = CRON_JOBS.get(cron_id)
        if job is None:
            return f"Error: cron job {cron_id!r} not found."
        job["status"] = "paused"
    return f"Paused {cron_id}."


def run_cron_resume(cron_id: str) -> str:
    """工具：恢复一个周期任务，并从现在重新计算下一次运行时间。"""
    with CRON_LOCK:
        job = CRON_JOBS.get(cron_id)
        if job is None:
            return f"Error: cron job {cron_id!r} not found."
        job["status"] = "enabled"
        job["next_run_at"] = time.time() + job["interval_seconds"]
    ensure_cron_scheduler_started()
    return f"Resumed {cron_id}. Next run: {format_timestamp(job['next_run_at'])}"


def run_cron_remove(cron_id: str) -> str:
    """工具：移除一个周期任务。"""
    with CRON_LOCK:
        removed = CRON_JOBS.pop(cron_id, None)
    if removed is None:
        return f"Error: cron job {cron_id!r} not found."
    return f"Removed {cron_id}."


def default_team_members() -> list[TeamMember]:
    """返回默认三人小队：研究、审查、测试。"""
    return [
        {
            "name": "researcher",
            "role": "Researcher",
            "prompt": "Find relevant files, concepts, and implementation details. Focus on evidence and citations.",
        },
        {
            "name": "reviewer",
            "role": "Reviewer",
            "prompt": "Review risks, edge cases, missing requirements, and possible regressions.",
        },
        {
            "name": "tester",
            "role": "Tester",
            "prompt": "Suggest validation steps, lightweight tests, and observable success criteria.",
        },
    ]


def builtin_team_protocols() -> dict[str, TeamProtocol]:
    """返回内置团队协议模板。"""
    plan_protocol: TeamProtocol = {
        "name": "plan",
        "description": "Plan a change: research, design a plan, then review the plan for risks.",
        "stages": [
            {"name": "research", "role": "Researcher", "prompt": "Map the relevant files and current behavior with evidence."},
            {"name": "plan", "role": "Planner", "prompt": "Propose a concrete, step-by-step plan referencing the research."},
            {"name": "review", "role": "Reviewer", "prompt": "Stress-test the plan: edge cases, regressions, and rollback notes."},
        ],
    }
    debate_protocol: TeamProtocol = {
        "name": "debate",
        "description": "Debate an approach: propose, challenge, then synthesize.",
        "stages": [
            {"name": "propose", "role": "Proposer", "prompt": "Argue for a specific approach with concrete reasons."},
            {"name": "challenge", "role": "Challenger", "prompt": "Attack the proposal with counterexamples, risks, and alternatives."},
            {"name": "synthesize", "role": "Synthesizer", "prompt": "Merge the strongest points into a balanced recommendation."},
        ],
    }
    redteam_protocol: TeamProtocol = {
        "name": "redteam",
        "description": "Red-team a design: build a threat model, find failures, then mitigate.",
        "stages": [
            {"name": "threat_model", "role": "Threat Modeler", "prompt": "List abuse cases and attacker goals for the target."},
            {"name": "attack", "role": "Attacker", "prompt": "Describe concrete ways the target could fail or be abused."},
            {"name": "mitigate", "role": "Defender", "prompt": "Propose bounded mitigations and verification steps for each failure."},
        ],
    }
    return {
        plan_protocol["name"]: plan_protocol,
        debate_protocol["name"]: debate_protocol,
        redteam_protocol["name"]: redteam_protocol,
    }


def resolve_protocol(name: str | None) -> tuple[ProtocolName, list[TeamMember]]:
    """根据协议名解析出协议标签和阶段成员。"""
    if not TEAM_PROTOCOLS:
        TEAM_PROTOCOLS.update(builtin_team_protocols())
    if name and name in TEAM_PROTOCOLS:
        protocol = TEAM_PROTOCOLS[name]
        members = [
            {"name": stage["name"], "role": stage["role"], "prompt": stage["prompt"]}
            for stage in protocol["stages"]
        ]
        label: ProtocolName = "custom" if name not in {"plan", "debate", "redteam"} else name  # type: ignore[assignment]
        return label, members
    return "custom", default_team_members()


def normalize_team_members(members: list[dict[str, Any]] | None) -> list[TeamMember]:
    """把用户传入的 team members 规范成小而明确的角色列表。"""
    if not members:
        return default_team_members()
    normalized: list[TeamMember] = []
    for index, member in enumerate(members[:5], start=1):
        name = str(member.get("name") or f"member-{index}").strip()
        role = str(member.get("role") or name).strip()
        prompt = str(member.get("prompt") or f"Work as {role}.").strip()
        if not name or not role or not prompt:
            continue
        normalized.append({"name": name, "role": role, "prompt": prompt})
    return normalized or default_team_members()


def new_team_run(objective: str, members: list[TeamMember], protocol: ProtocolName = "custom") -> TeamRun:
    """创建一次团队运行记录。"""
    global NEXT_TEAM_ID
    team_id = f"team-{NEXT_TEAM_ID:04d}"
    NEXT_TEAM_ID += 1
    run: TeamRun = {
        "id": team_id,
        "objective": objective.strip(),
        "protocol": protocol,
        "members": members,
        "status": "running",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": None,
        "task_ids": [],
        "report": "",
        "error": None,
    }
    with TEAM_LOCK:
        TEAM_RUNS[team_id] = run
    return run


def format_team_run(run: TeamRun, include_report: bool = True) -> str:
    """把团队运行记录渲染成可读文本。"""
    lines = [
        f"{run['id']} [{run['status']}] {run['objective']}",
        f"protocol: {run['protocol']}",
        f"created: {run['created_at']}",
        f"finished: {run['finished_at'] or '-'}",
        f"members: {', '.join(member['name'] for member in run['members'])}",
        f"task_ids: {', '.join(run['task_ids']) or '-'}",
    ]
    if run["error"]:
        lines.append(f"error:\n{run['error']}")
    if include_report and run["report"]:
        lines.append(f"report:\n{run['report'][:8_000]}")
    return "\n".join(lines)


def aggregate_team_report(run: TeamRun, member_outputs: list[tuple[TeamMember, str, str]]) -> str:
    """把多个成员的结果汇总成团队报告。"""
    lines = [
        f"# Team report: {run['objective']}",
        "",
        "## Members",
    ]
    for member, task_id, report in member_outputs:
        lines.extend(
            [
                f"- {member['name']} ({member['role']}): {task_id}",
            ]
        )
    lines.append("")
    lines.append("## Findings")
    for member, task_id, report in member_outputs:
        lines.extend(
            [
                f"### {member['name']} ({member['role']}) — {task_id}",
                report.strip() or "No report.",
                "",
            ]
        )
    return "\n".join(lines).strip()


def run_team(objective: str, members: list[dict[str, Any]] | None = None, protocol: str | None = None) -> str:
    """工具：按协议顺序运行多个带角色的子 Agent，并汇总团队报告。"""
    if not isinstance(objective, str) or not objective.strip():
        return "Error: objective must be a non-empty string."
    protocol_label, team_members = resolve_protocol(protocol if not members else None)
    if members:
        team_members = normalize_team_members(members)
        protocol_label = "custom"
    run = new_team_run(objective, team_members, protocol=protocol_label)
    outputs: list[tuple[TeamMember, str, str]] = []

    try:
        for index, member in enumerate(team_members):
            member_prompt = (
                f"Team objective:\n{objective.strip()}\n\n"
                f"Protocol: {run['protocol']}\n"
                f"Stage {index + 1}/{len(team_members)} — {member['name']} ({member['role']})\n"
                f"Stage instructions:\n{member['prompt']}\n\n"
                "Return a concise stage-specific report."
            )
            record = new_task_record(
                f"team {run['id']} / {member['name']}: {objective[:80]}",
                member_prompt,
                background=False,
            )
            with TEAM_LOCK:
                run["task_ids"].append(record["id"])
            report = run_subagent(record)
            outputs.append((member, record["id"], report))

        final_report = aggregate_team_report(run, outputs)
        with TEAM_LOCK:
            run["status"] = "completed"
            run["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            run["report"] = final_report
        return f"Team {run['id']} ({run['protocol']}) completed.\n\n{final_report}"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        with TEAM_LOCK:
            run["status"] = "failed"
            run["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            run["error"] = error
            if outputs:
                run["report"] = aggregate_team_report(run, outputs)
        return attach_recovery_hint("team_run", f"Error: team {run['id']} failed: {error}")


def run_team_list() -> str:
    """工具：列出当前会话内的团队运行。"""
    with TEAM_LOCK:
        if not TEAM_RUNS:
            return "No team runs have been created yet."
        runs = list(TEAM_RUNS.values())
    return "\n\n".join(format_team_run(run, include_report=False) for run in runs)


def run_team_read(team_id: str) -> str:
    """工具：读取一次团队运行的完整记录。"""
    with TEAM_LOCK:
        run = TEAM_RUNS.get(team_id)
    if run is None:
        return f"Error: team run {team_id!r} not found."
    return format_team_run(run, include_report=True)


def run_protocol_list() -> str:
    """工具：列出可用的团队协议模板。"""
    protocols = list(TEAM_PROTOCOLS.values())
    if not protocols:
        return "No team protocols registered."
    lines = ["Available team protocols:"]
    for protocol in protocols:
        roles = ", ".join(stage["role"] for stage in protocol["stages"])
        lines.append(f"- {protocol['name']}: {protocol['description']} (stages: {roles})")
    return "\n".join(lines)


def new_autonomy_run(
    goal: str,
    success_criteria: str,
    max_steps: int = 3,
    deadline_seconds: float | None = None,
) -> AutonomyRun:
    """创建一次有边界的自主运行。"""
    global NEXT_AUTONOMY_ID
    run_id = f"auto-{NEXT_AUTONOMY_ID:04d}"
    NEXT_AUTONOMY_ID += 1
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    deadline_at = time.time() + deadline_seconds if deadline_seconds else None
    run: AutonomyRun = {
        "id": run_id,
        "goal": goal.strip(),
        "success_criteria": success_criteria.strip(),
        "status": "running",
        "created_at": now_text,
        "updated_at": now_text,
        "max_steps": max_steps,
        "step_count": 0,
        "deadline_at": deadline_at,
        "notes": [],
        "task_ids": [],
        "team_ids": [],
        "final_report": "",
        "error": None,
    }
    with AUTONOMY_LOCK:
        AUTONOMY_RUNS[run_id] = run
    return run


def format_autonomy_run(run: AutonomyRun) -> str:
    """把自主运行记录渲染成可读文本。"""
    deadline = format_timestamp(run["deadline_at"])
    lines = [
        f"{run['id']} [{run['status']}] {run['goal']}",
        f"created: {run['created_at']}",
        f"updated: {run['updated_at']}",
        f"steps: {run['step_count']}/{run['max_steps']}",
        f"deadline: {deadline}",
        f"success_criteria: {run['success_criteria']}",
        f"team_ids: {', '.join(run['team_ids']) or '-'}",
        f"task_ids: {', '.join(run['task_ids']) or '-'}",
    ]
    if run["notes"]:
        lines.append("notes:\n" + "\n".join(f"- {note}" for note in run["notes"][-10:]))
    if run["error"]:
        lines.append(f"error:\n{run['error']}")
    if run["final_report"]:
        lines.append(f"final_report:\n{run['final_report'][:8_000]}")
    return "\n".join(lines)


def autonomy_should_stop(run: AutonomyRun) -> str | None:
    """检查自主运行是否已经触达停止条件。"""
    if run["status"] != "running":
        return f"run is {run['status']}"
    if run["step_count"] >= run["max_steps"]:
        return "max_steps reached"
    if run["deadline_at"] is not None and time.time() >= run["deadline_at"]:
        return "deadline reached"
    return None


def run_autonomous_step(run: AutonomyRun) -> str:
    """执行一个保守的自主 step：用 plan 协议生成下一步报告。"""
    stop_reason = autonomy_should_stop(run)
    if stop_reason:
        run["status"] = "completed" if stop_reason == "max_steps reached" else run["status"]
        return f"Autonomy {run['id']} stopped: {stop_reason}."

    run["step_count"] += 1
    run["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    objective = (
        f"Autonomous goal:\n{run['goal']}\n\n"
        f"Success criteria:\n{run['success_criteria']}\n\n"
        f"Step {run['step_count']} of {run['max_steps']}: decide and document the next bounded action."
    )
    report = run_team(objective, protocol="plan")
    with TEAM_LOCK:
        latest_team_id = next(reversed(TEAM_RUNS)) if TEAM_RUNS else None
    if latest_team_id:
        run["team_ids"].append(latest_team_id)
        run["notes"].append(f"step {run['step_count']}: created {latest_team_id}")
    run["final_report"] = report[:50_000]
    if run["step_count"] >= run["max_steps"]:
        run["status"] = "completed"
    return f"Autonomy {run['id']} completed step {run['step_count']}.\n\n{report}"


def run_autonomous_start(
    goal: str,
    success_criteria: str,
    max_steps: int = 3,
    deadline_seconds: float | None = None,
    run_first_step: bool = True,
) -> str:
    """工具：启动一次有边界的自主运行。"""
    if not isinstance(goal, str) or not goal.strip():
        return "Error: goal must be a non-empty string."
    if not isinstance(success_criteria, str) or not success_criteria.strip():
        return "Error: success_criteria must be a non-empty string."
    steps = max(1, min(int(max_steps), 10))
    deadline = None if deadline_seconds is None else max(5.0, min(float(deadline_seconds), 3600.0))
    run = new_autonomy_run(goal, success_criteria, steps, deadline)
    if run_first_step:
        step_output = run_autonomous_step(run)
        return f"Started {run['id']} and ran first step.\n\n{step_output}"
    return f"Started {run['id']}. Use autonomous_step with run_id={run['id']!r} to continue."


def run_autonomous_step_tool(run_id: str) -> str:
    """工具：推进一次自主运行的一个 step。"""
    with AUTONOMY_LOCK:
        run = AUTONOMY_RUNS.get(run_id)
    if run is None:
        return f"Error: autonomous run {run_id!r} not found."
    return run_autonomous_step(run)


def run_autonomous_list() -> str:
    """工具：列出自主运行。"""
    with AUTONOMY_LOCK:
        if not AUTONOMY_RUNS:
            return "No autonomous runs have been created yet."
        runs = list(AUTONOMY_RUNS.values())
    return "\n\n".join(format_autonomy_run(run) for run in runs)


def run_autonomous_read(run_id: str) -> str:
    """工具：读取一次自主运行详情。"""
    with AUTONOMY_LOCK:
        run = AUTONOMY_RUNS.get(run_id)
    if run is None:
        return f"Error: autonomous run {run_id!r} not found."
    return format_autonomy_run(run)


def run_autonomous_pause(run_id: str) -> str:
    """工具：暂停自主运行。"""
    with AUTONOMY_LOCK:
        run = AUTONOMY_RUNS.get(run_id)
        if run is None:
            return f"Error: autonomous run {run_id!r} not found."
        run["status"] = "paused"
        run["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"Paused {run_id}."


def run_autonomous_resume(run_id: str) -> str:
    """工具：恢复自主运行。"""
    with AUTONOMY_LOCK:
        run = AUTONOMY_RUNS.get(run_id)
        if run is None:
            return f"Error: autonomous run {run_id!r} not found."
        run["status"] = "running"
        run["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"Resumed {run_id}."


SUBAGENT_TOOL_HANDLERS: dict[str, ToolHandler] = {
    "bash": lambda **kwargs: run_bash(kwargs["command"]),
    "read_file": lambda **kwargs: run_read(kwargs["path"], kwargs.get("limit")),
    "list_skills": lambda **kwargs: run_list_skills(),
    "read_skill": lambda **kwargs: run_read_skill(kwargs["name"]),
    "read_memory": lambda **kwargs: run_read_memory(),
    "show_system_prompt": lambda **kwargs: run_show_system_prompt(),
    "show_errors": lambda **kwargs: run_show_errors(),
    "task_list": lambda **kwargs: run_task_list(),
    "task_read": lambda **kwargs: run_task_read(kwargs["task_id"]),
    "task_wait": lambda **kwargs: run_task_wait(kwargs["task_id"], kwargs.get("timeout_seconds", 30.0)),
    "cron_add": lambda **kwargs: run_cron_add(
        kwargs["description"],
        kwargs["prompt"],
        kwargs["interval_seconds"],
        kwargs.get("max_runs"),
    ),
    "cron_list": lambda **kwargs: run_cron_list(),
    "cron_read": lambda **kwargs: run_cron_read(kwargs["cron_id"]),
    "cron_pause": lambda **kwargs: run_cron_pause(kwargs["cron_id"]),
    "cron_resume": lambda **kwargs: run_cron_resume(kwargs["cron_id"]),
    "cron_remove": lambda **kwargs: run_cron_remove(kwargs["cron_id"]),
    "cron_tick": lambda **kwargs: run_cron_tick(),
    "team_list": lambda **kwargs: run_team_list(),
    "team_read": lambda **kwargs: run_team_read(kwargs["team_id"]),
    "team_run": lambda **kwargs: run_team(kwargs["objective"], kwargs.get("members"), kwargs.get("protocol")),
    "protocol_list": lambda **kwargs: run_protocol_list(),
    "autonomous_list": lambda **kwargs: run_autonomous_list(),
    "autonomous_read": lambda **kwargs: run_autonomous_read(kwargs["run_id"]),
    "autonomous_start": lambda **kwargs: run_autonomous_start(
        kwargs["goal"],
        kwargs["success_criteria"],
        kwargs.get("max_steps", 3),
        kwargs.get("deadline_seconds"),
        kwargs.get("run_first_step", True),
    ),
    "autonomous_step": lambda **kwargs: run_autonomous_step_tool(kwargs["run_id"]),
    "autonomous_pause": lambda **kwargs: run_autonomous_pause(kwargs["run_id"]),
    "autonomous_resume": lambda **kwargs: run_autonomous_resume(kwargs["run_id"]),
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
    "task": lambda **kwargs: run_task(kwargs["description"], kwargs["prompt"], kwargs.get("run_in_background", False)),
    "list_skills": lambda **kwargs: run_list_skills(),
    "read_skill": lambda **kwargs: run_read_skill(kwargs["name"]),
    "read_memory": lambda **kwargs: run_read_memory(),
    "remember": lambda **kwargs: run_remember(kwargs["text"]),
    "show_system_prompt": lambda **kwargs: run_show_system_prompt(),
    "show_errors": lambda **kwargs: run_show_errors(),
    "task_list": lambda **kwargs: run_task_list(),
    "task_read": lambda **kwargs: run_task_read(kwargs["task_id"]),
    "task_wait": lambda **kwargs: run_task_wait(kwargs["task_id"], kwargs.get("timeout_seconds", 30.0)),
    "cron_add": lambda **kwargs: run_cron_add(
        kwargs["description"],
        kwargs["prompt"],
        kwargs["interval_seconds"],
        kwargs.get("max_runs"),
    ),
    "cron_list": lambda **kwargs: run_cron_list(),
    "cron_read": lambda **kwargs: run_cron_read(kwargs["cron_id"]),
    "cron_pause": lambda **kwargs: run_cron_pause(kwargs["cron_id"]),
    "cron_resume": lambda **kwargs: run_cron_resume(kwargs["cron_id"]),
    "cron_remove": lambda **kwargs: run_cron_remove(kwargs["cron_id"]),
    "cron_tick": lambda **kwargs: run_cron_tick(),
    "team_list": lambda **kwargs: run_team_list(),
    "team_read": lambda **kwargs: run_team_read(kwargs["team_id"]),
    "team_run": lambda **kwargs: run_team(kwargs["objective"], kwargs.get("members"), kwargs.get("protocol")),
    "protocol_list": lambda **kwargs: run_protocol_list(),
    "autonomous_list": lambda **kwargs: run_autonomous_list(),
    "autonomous_read": lambda **kwargs: run_autonomous_read(kwargs["run_id"]),
    "autonomous_start": lambda **kwargs: run_autonomous_start(
        kwargs["goal"],
        kwargs["success_criteria"],
        kwargs.get("max_steps", 3),
        kwargs.get("deadline_seconds"),
        kwargs.get("run_first_step", True),
    ),
    "autonomous_step": lambda **kwargs: run_autonomous_step_tool(kwargs["run_id"]),
    "autonomous_pause": lambda **kwargs: run_autonomous_pause(kwargs["run_id"]),
    "autonomous_resume": lambda **kwargs: run_autonomous_resume(kwargs["run_id"]),
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
        "name": "show_errors",
        "description": "Show recent tool/model errors with recovery hints.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "task_list",
        "description": "List tracked subagent tasks from this session with status and short results.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "task_read",
        "description": "Read one tracked subagent task by task_id, including prompt and result.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task id returned by task, for example task-0001."}
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "task_wait",
        "description": "Wait for a background subagent task to finish, then return its record.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task id returned by task."},
                "timeout_seconds": {"type": "number", "description": "Maximum seconds to wait, capped at 300."},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "cron_add",
        "description": "Schedule a recurring background task that starts a subagent task every interval_seconds.",
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Short recurring job description."},
                "prompt": {"type": "string", "description": "Detailed prompt for each triggered subagent task."},
                "interval_seconds": {"type": "number", "description": "Seconds between runs; must be at least 5."},
                "max_runs": {"type": "integer", "description": "Optional maximum number of runs before pausing."},
            },
            "required": ["description", "prompt", "interval_seconds"],
        },
    },
    {
        "name": "cron_list",
        "description": "List scheduled cron jobs and their latest triggered task ids.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "cron_read",
        "description": "Read one cron job by cron_id, including prompt and triggered task ids.",
        "input_schema": {
            "type": "object",
            "properties": {"cron_id": {"type": "string", "description": "Cron job id, for example cron-0001."}},
            "required": ["cron_id"],
        },
    },
    {
        "name": "cron_pause",
        "description": "Pause a scheduled cron job so it no longer triggers new tasks.",
        "input_schema": {
            "type": "object",
            "properties": {"cron_id": {"type": "string", "description": "Cron job id to pause."}},
            "required": ["cron_id"],
        },
    },
    {
        "name": "cron_resume",
        "description": "Resume a paused cron job and recalculate its next run time.",
        "input_schema": {
            "type": "object",
            "properties": {"cron_id": {"type": "string", "description": "Cron job id to resume."}},
            "required": ["cron_id"],
        },
    },
    {
        "name": "cron_remove",
        "description": "Remove a scheduled cron job from this session.",
        "input_schema": {
            "type": "object",
            "properties": {"cron_id": {"type": "string", "description": "Cron job id to remove."}},
            "required": ["cron_id"],
        },
    },
    {
        "name": "cron_tick",
        "description": "Manually run one scheduler scan and trigger any due cron jobs.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "team_list",
        "description": "List agent team runs from this session with members and task ids.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "team_read",
        "description": "Read one agent team run by team_id, including its aggregated report.",
        "input_schema": {
            "type": "object",
            "properties": {"team_id": {"type": "string", "description": "Team run id, for example team-0001."}},
            "required": ["team_id"],
        },
    },
    {
        "name": "team_run",
        "description": "Run a small team of specialized subagents and aggregate their reports.",
        "input_schema": {
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "Shared objective for the team."},
                "protocol": {
                    "type": "string",
                    "description": "Optional built-in protocol: plan, debate, or redteam. Ignored when members is provided.",
                },
                "members": {
                    "type": "array",
                    "description": "Optional custom team members; overrides protocol when provided.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "role": {"type": "string"},
                            "prompt": {"type": "string"},
                        },
                    },
                },
            },
            "required": ["objective"],
        },
    },
    {
        "name": "protocol_list",
        "description": "List available team protocol templates (plan, debate, redteam).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "autonomous_start",
        "description": "Start a bounded autonomous run with goal, success criteria, max_steps, and optional deadline.",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "The objective the autonomous agent should pursue."},
                "success_criteria": {"type": "string", "description": "Observable criteria for stopping successfully."},
                "max_steps": {"type": "integer", "description": "Maximum autonomous steps, capped at 10."},
                "deadline_seconds": {"type": "number", "description": "Optional time budget in seconds, capped at 3600."},
                "run_first_step": {"type": "boolean", "description": "If true, immediately run the first autonomous step."},
            },
            "required": ["goal", "success_criteria"],
        },
    },
    {
        "name": "autonomous_step",
        "description": "Advance one existing autonomous run by one bounded step.",
        "input_schema": {
            "type": "object",
            "properties": {"run_id": {"type": "string", "description": "Autonomous run id, for example auto-0001."}},
            "required": ["run_id"],
        },
    },
    {
        "name": "autonomous_list",
        "description": "List autonomous runs with status, budgets, and linked task/team ids.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "autonomous_read",
        "description": "Read one autonomous run by run_id.",
        "input_schema": {
            "type": "object",
            "properties": {"run_id": {"type": "string", "description": "Autonomous run id."}},
            "required": ["run_id"],
        },
    },
    {
        "name": "autonomous_pause",
        "description": "Pause an autonomous run.",
        "input_schema": {
            "type": "object",
            "properties": {"run_id": {"type": "string", "description": "Autonomous run id."}},
            "required": ["run_id"],
        },
    },
    {
        "name": "autonomous_resume",
        "description": "Resume a paused autonomous run.",
        "input_schema": {
            "type": "object",
            "properties": {"run_id": {"type": "string", "description": "Autonomous run id."}},
            "required": ["run_id"],
        },
    },

    {
        "name": "task",
        "description": (
            "Launch a tracked focused subagent task for research or analysis. "
            "The subagent has its own short conversation; the Task System records id, status, turns, and result."
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
                "run_in_background": {
                    "type": "boolean",
                    "description": "If true, start the task in a background thread and return immediately.",
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
    tool for tool in TOOLS if tool["name"] in {"bash", "read_file", "list_skills", "read_skill", "read_memory", "show_system_prompt", "show_errors", "task_list", "task_read", "task_wait", "cron_list", "cron_read", "team_list", "team_read", "protocol_list", "autonomous_list", "autonomous_read"}
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
        response = call_model_with_retries(
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
                output = attach_recovery_hint(block.name, f"Error: unknown tool {block.name!r}.")
            else:
                print(f"\033[33m> {block.name}: {block.input}\033[0m")
                blocked = trigger_hooks("PreToolUse", block.name, block.input)
                if blocked is not None:
                    output = attach_recovery_hint(block.name, blocked)
                else:
                    try:
                        output = handler(**block.input)
                    except Exception as exc:
                        output = f"Error: {exc}"
                    output = attach_recovery_hint(block.name, output)
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
    """启动一个支持 Autonomous Agents 的最小 Agent。"""
    ensure_memory_file()
    ensure_cron_scheduler_started()
    TEAM_PROTOCOLS.update(builtin_team_protocols())
    print("Day 17 Autonomous Agents. 输入任务开始，输入 q / exit / 空行退出。")
    history: list[dict[str, Any]] = []

    while True:
        try:
            query = input("\033[36ms17 >> \033[0m")
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
