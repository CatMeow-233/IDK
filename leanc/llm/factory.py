"""Provider selection by name (``--provider`` / ``leanc.yaml``)."""

from __future__ import annotations

from pathlib import Path

from leanc.foundation.errors import ProviderError
from leanc.llm.base import LLMProvider
from leanc.llm.mock import MockProvider
from leanc.llm.openai_compat import OpenAICompatProvider

MOCK_PROVIDER = "mock"
OPENAI_COMPATIBLE_ALIASES = ("openai", "openai-compatible")
DEFAULT_MOCK_DIR = "mock"

_NAMES = (MOCK_PROVIDER, *OPENAI_COMPATIBLE_ALIASES)


def available_providers() -> list[str]:
    """Accepted provider names (``openai`` is an alias of ``openai-compatible``)."""
    return list(_NAMES)


def _mock_fixture_dir(project_root: Path, mock_dir: Path | None) -> Path:
    root = Path(project_root)
    fixture_dir = Path(mock_dir) if mock_dir is not None else root / DEFAULT_MOCK_DIR
    if not fixture_dir.is_absolute():
        fixture_dir = root / fixture_dir
    return fixture_dir


def get_provider(
    name: str,
    *,
    project_root: Path,
    mock_dir: Path | None = None,
    transcript_dir: Path | None = None,
) -> LLMProvider:
    """Instantiate a provider: mock fixtures (relative to project_root) or an OpenAI-compatible endpoint from env."""
    key = (name or "").strip().lower()
    if key == MOCK_PROVIDER:
        return MockProvider(_mock_fixture_dir(project_root, mock_dir))
    if key in OPENAI_COMPATIBLE_ALIASES:
        return OpenAICompatProvider.from_env(transcript_dir=transcript_dir)
    raise ProviderError(f"unknown LLM provider {name!r}; choose one of: {', '.join(_NAMES)}")


__all__ = ["available_providers", "get_provider"]
