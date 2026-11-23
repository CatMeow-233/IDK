from __future__ import annotations

import logging
from pathlib import Path

import pytest

from leanc.foundation.redact import REDACTED
from leanc.pipeline.commands.console import LOGGER_NAME, setup_logging

GATE_LOGGER_NAME = f"{LOGGER_NAME}.controller"
SECRET_SHAPED_TOKEN = "sk-" + "A" * 24
SECRET_ENV_VALUE = "approval-key-0123456789"
TOKEN_CONTEXT = "token "
EXCEPTION_CONTEXT = "tool printed "


@pytest.fixture
def leanc_logger_restored_afterwards():
    logger = logging.getLogger(LOGGER_NAME)
    handlers_before = list(logger.handlers)
    level_before = logger.level
    yield
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    for handler in handlers_before:
        logger.addHandler(handler)
    logger.setLevel(level_before)


def _log_a_secret_as_argument_and_in_exception_text(run_log: Path, monkeypatch) -> None:
    monkeypatch.setenv("LEANC_APPROVAL_KEY", SECRET_ENV_VALUE)
    setup_logging(run_log, verbose=True)
    log = logging.getLogger(GATE_LOGGER_NAME)
    log.info("gate compile: FAIL first: %s", f"src/safe_array.c:14:2: {TOKEN_CONTEXT}{SECRET_SHAPED_TOKEN}")
    log.error("tool failed", exc_info=RuntimeError(f"{EXCEPTION_CONTEXT}{SECRET_ENV_VALUE}"))


def _assert_every_secret_is_masked(logged: str) -> None:
    assert SECRET_SHAPED_TOKEN not in logged
    assert SECRET_ENV_VALUE not in logged
    assert f"{TOKEN_CONTEXT}{REDACTED}" in logged
    assert f"{EXCEPTION_CONTEXT}{REDACTED}" in logged


def test_run_log_file_masks_secrets(tmp_path, monkeypatch, leanc_logger_restored_afterwards):
    run_log = tmp_path / "leanc.log"
    _log_a_secret_as_argument_and_in_exception_text(run_log, monkeypatch)
    _assert_every_secret_is_masked(run_log.read_text(encoding="utf-8"))


def test_stderr_log_masks_secrets(tmp_path, monkeypatch, capsys, leanc_logger_restored_afterwards):
    _log_a_secret_as_argument_and_in_exception_text(tmp_path / "leanc.log", monkeypatch)
    _assert_every_secret_is_masked(capsys.readouterr().err)
