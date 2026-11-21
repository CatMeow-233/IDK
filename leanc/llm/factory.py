"""Provider selection by name (``--provider`` / ``leanc.yaml``)."""

from __future__ import annotations

from pathlib import Path

from leanc.foundation.errors import ProviderError
from leanc.llm.base import LLMProvider
from leanc.llm.mock import MockProvider
from leanc.llm.openai_compat import OpenAICompatProvider

_NAMES = ("mock", "openai", "openai-compatible")


def available_providers() -> list[str]:
    """Accepted provider names (``openai`` is an alias of ``openai-compatible``)."""
    return list(_NAMES)


def get_provider(
    name: str,
    *,
    project_root: Path,
    mock_dir: Path | None = None,
    transcript_dir: Path | None = None,
) -> LLMProvider:
    """Instantiate a provider.

    * ``mock``: fixtures from ``mock_dir`` (relative paths are taken relative
      to ``project_root``; default ``<project_root>/mock``).
    * ``openai`` / ``openai-compatible``: configured from the environment (see
      :meth:`OpenAICompatProvider.from_env`); request/response transcripts
      (redacted) go to ``transcript_dir`` when given.
    """
    key = (name or "").strip().lower()
    if key == "mock":
        root = Path(project_root)
        d = Path(mock_dir) if mock_dir is not None else root / "mock"
        if not d.is_absolute():
            d = root / d
        return MockProvider(d)
    if key in ("openai", "openai-compatible"):
        return OpenAICompatProvider.from_env(transcript_dir=transcript_dir)
    raise ProviderError(f"unknown LLM provider {name!r}; choose one of: {', '.join(_NAMES)}")


__all__ = ["available_providers", "get_provider"]
