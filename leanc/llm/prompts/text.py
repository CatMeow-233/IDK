from __future__ import annotations

import re
from dataclasses import dataclass

from leanc.foundation.redact import redact

CONTEXT_FILES_SHARING_ONE_BUDGET = 4
MIN_FENCE_TICKS = 3
EXCERPT_RADIUS_LINES = 4

_FENCE_LANGUAGES: tuple[tuple[tuple[str, ...], str], ...] = (
    ((".c", ".h"), "c"),
    ((".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"), "cpp"),
    ((".yaml", ".yml"), "yaml"),
    ((".json",), "json"),
)


@dataclass(frozen=True)
class PromptLimits:
    max_requirement_chars: int = 8_000
    max_spec_chars: int = 30_000
    max_file_chars: int = 40_000
    max_total_source_chars: int = 120_000
    max_context_file_chars: int = 20_000
    max_diagnostics: int = 30
    max_failures: int = 8
    max_trace_steps: int = 40
    max_inputs: int = 40
    max_history: int = 8
    max_item_chars: int = 2_000
    max_prompt_chars: int = 400_000

    @property
    def max_total_context_chars(self) -> int:
        return self.max_context_file_chars * CONTEXT_FILES_SHARING_ONE_BUDGET


DEFAULT_LIMITS = PromptLimits()


@dataclass(frozen=True)
class Prompt:
    system: str
    user: str

    def messages(self) -> list[dict[str, str]]:
        return [{"role": "system", "content": self.system}, {"role": "user", "content": self.user}]

    @property
    def size(self) -> int:
        return len(self.system) + len(self.user)


def _head_length(limit: int) -> int:
    return (limit * 2) // 3


def truncate(text: str | None, limit: int, *, label: str = "text") -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 0:
        return f"[{label} omitted: {len(text)} characters]"
    head_len = _head_length(limit)
    tail_len = limit - head_len
    omitted = len(text) - head_len - tail_len
    tail = text[len(text) - tail_len :] if tail_len > 0 else ""
    return f"{text[:head_len]}\n... [{label} truncated: {omitted} characters omitted] ...\n{tail}"


def fence(content: str, lang: str = "") -> str:
    longest = max((len(run) for run in re.findall(r"`+", content or "")), default=0)
    ticks = "`" * max(MIN_FENCE_TICKS, longest + 1)
    body = content if (content or "").endswith("\n") else (content or "") + "\n"
    return f"{ticks}{lang}\n{body}{ticks}"


def fence_language(path: str) -> str:
    for suffixes, language in _FENCE_LANGUAGES:
        if path.endswith(suffixes):
            return language
    return ""


def finish_prompt(system: str, user: str, limits: PromptLimits) -> Prompt:
    capped_user = truncate(user.strip() + "\n", limits.max_prompt_chars, label="prompt")
    return Prompt(system=redact(system.strip() + "\n"), user=redact(capped_user))


def numbered_excerpt(content: str, line: int, radius: int = EXCERPT_RADIUS_LINES) -> str:
    lines = content.splitlines()
    if not lines or line < 1 or line > len(lines):
        return ""
    first, last = max(1, line - radius), min(len(lines), line + radius)
    width = len(str(last))
    out = []
    for number in range(first, last + 1):
        mark = ">" if number == line else " "
        out.append(f"{mark}{number:>{width}} | {lines[number - 1]}")
    return "\n".join(out)


def render_files(files: dict[str, str], per_file: int, total: int, *, label: str) -> tuple[str, list[str]]:
    out: list[str] = []
    truncated: list[str] = []
    budget = total
    for path in sorted(files):
        content = files[path] or ""
        limit = max(0, min(per_file, budget))
        shown = truncate(content, limit, label=f"{label} {path}")
        if shown != content:
            truncated.append(path)
        budget -= min(len(content), limit)
        out.append(f"### {path}\n{fence(shown, fence_language(path))}")
    return ("\n\n".join(out) if out else "(none)"), truncated
