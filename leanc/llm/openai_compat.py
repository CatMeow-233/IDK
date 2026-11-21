from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

from leanc import __version__
from leanc.foundation.errors import JSON_LOAD_ERRORS, ProviderError
from leanc.foundation.models import (
    FailureReport,
    ProviderInfo,
    VerificationResult,
    utcnow,
)
from leanc.foundation.redact import REDACTED, redact
from leanc.foundation.text import utf8_safe
from leanc.llm.base import (
    CodeGenRequest,
    GeneratedCode,
    LLMProvider,
    RepairProposal,
    RepairRequest,
    SpecDraft,
    SpecDraftRequest,
)
from leanc.llm.parsing import (
    excerpt,
    parse_code_response,
    parse_repair_response,
    parse_spec_draft_response,
)
from leanc.llm.prompts.analysis import build_analyze_prompt, build_explain_prompt
from leanc.llm.prompts.codegen import build_codegen_prompt
from leanc.llm.prompts.repair import build_repair_prompt
from leanc.llm.prompts.spec_draft import build_spec_draft_prompt
from leanc.llm.prompts.text import DEFAULT_LIMITS, Prompt, PromptLimits
from leanc.specs.model import Spec

log = logging.getLogger("leanc.llm")

DEFAULT_BASE_URL = "https://api.openai.com/v1"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_ERROR_BODY_BYTES = 64 * 1024
ERROR_DETAIL_CHARS = 300
MAX_RETRIES_LIMIT = 10
RETRYABLE_STATUS = frozenset({408, 409, 429})
AUTH_FAILURE_STATUS = frozenset({401, 403})
NOT_FOUND_STATUS = 404
FIRST_SERVER_ERROR_STATUS = 500
FINISH_REASON_CUT_OFF = "length"
MIN_KEY_CHARS_SCRUBBED_VERBATIM = 4

_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
_UNSET_TEMPERATURE_VALUES = ("default", "none")
_PROXY_MODES = ("env", "none")
_DELAY_SECONDS_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_CUT_OFF_ANSWER_HINT = " (finish_reason=length: the answer was cut off; raise LEANC_LLM_MAX_TOKENS or reduce context)"


class _RedirectsBecomeHTTPErrors(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _Retryable(Exception):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    text = value.strip()
    if not _DELAY_SECONDS_RE.fullmatch(text):
        return None
    seconds = float(text)
    return seconds if seconds >= 0 else None


def _is_transient_status(status: int) -> bool:
    return status in RETRYABLE_STATUS or status >= FIRST_SERVER_ERROR_STATUS


def _is_redirect_status(status: int) -> bool:
    return 300 <= status < 400


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read(MAX_ERROR_BODY_BYTES).decode("utf-8", "replace")
    except (OSError, http.client.HTTPException):
        return ""
    finally:
        exc.close()


def _error_message(body: str) -> str:
    try:
        data = json.loads(body)
    except JSON_LOAD_ERRORS:
        return body
    if not isinstance(data, dict):
        return body
    err = data.get("error", data)
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or err)
    return str(err)


def _text_of_content_parts(parts: list) -> str:
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text")


def _env_reader(env: Mapping[str, str]) -> Callable[[str], str]:
    def get(name: str) -> str:
        return (env.get(name) or "").strip()

    return get


def _env_timeout(raw: str) -> float:
    message = f"LEANC_LLM_TIMEOUT must be a positive number of seconds, got {raw!r}"
    try:
        timeout = float(raw)
    except ValueError:
        raise ProviderError(message) from None
    if not timeout > 0:
        raise ProviderError(message)
    return timeout


def _env_temperature(raw: str) -> float | None:
    if raw.lower() in _UNSET_TEMPERATURE_VALUES:
        return None
    try:
        return float(raw)
    except ValueError:
        raise ProviderError(f"LEANC_LLM_TEMPERATURE must be a number or 'default', got {raw!r}") from None


def _optional_settings_from_env(get: Callable[[str], str]) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if get("LEANC_LLM_TIMEOUT"):
        kwargs["timeout_s"] = _env_timeout(get("LEANC_LLM_TIMEOUT"))
    if get("LEANC_LLM_MAX_RETRIES"):
        raw = get("LEANC_LLM_MAX_RETRIES")
        if not raw.isdigit() or int(raw) > MAX_RETRIES_LIMIT:
            raise ProviderError(f"LEANC_LLM_MAX_RETRIES must be an integer in 0..{MAX_RETRIES_LIMIT}, got {raw!r}")
        kwargs["max_retries"] = int(raw)
    if get("LEANC_LLM_MAX_TOKENS"):
        raw = get("LEANC_LLM_MAX_TOKENS")
        if not raw.isdigit() or int(raw) <= 0:
            raise ProviderError(f"LEANC_LLM_MAX_TOKENS must be a positive integer, got {raw!r}")
        kwargs["max_tokens"] = int(raw)
    if get("LEANC_LLM_TEMPERATURE"):
        kwargs["temperature"] = _env_temperature(get("LEANC_LLM_TEMPERATURE"))
    if get("LEANC_LLM_PROXY"):
        raw = get("LEANC_LLM_PROXY").lower()
        if raw not in _PROXY_MODES:
            raise ProviderError(f"LEANC_LLM_PROXY must be 'env' or 'none', got {raw!r}")
        kwargs["proxy"] = raw
    kwargs["json_mode"] = get("LEANC_LLM_JSON_MODE").lower() in _TRUTHY_ENV_VALUES
    kwargs["allow_insecure_http"] = get("LEANC_ALLOW_INSECURE_HTTP").lower() in _TRUTHY_ENV_VALUES
    return kwargs


def _is_default_openai_host(base_url: str) -> bool:
    host = urllib.parse.urlsplit(base_url).hostname or ""
    return host == urllib.parse.urlsplit(DEFAULT_BASE_URL).hostname


class OpenAICompatProvider(LLMProvider):
    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        timeout_s: float = 120,
        max_retries: int = 3,
        json_mode: bool = False,
        temperature: float | None = 0.0,
        transcript_dir: Path | None = None,
        proxy: Literal["env", "none"] = "env",
        *,
        max_tokens: int | None = None,
        backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
        allow_insecure_http: bool = False,
        limits: PromptLimits = DEFAULT_LIMITS,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._api_key = (api_key or "").strip()
        if not isinstance(model, str) or not model.strip():
            raise ProviderError("no model configured: set LEANC_MODEL to the model name your endpoint serves")
        self.model = model.strip()
        self.base_url = self._check_base_url(base_url, allow_insecure_http)
        if not (isinstance(timeout_s, (int, float)) and timeout_s > 0):
            raise ProviderError(f"LLM timeout must be a positive number of seconds, got {timeout_s!r}")
        if not (isinstance(max_retries, int) and 0 <= max_retries <= MAX_RETRIES_LIMIT):
            raise ProviderError(f"max_retries must be an integer in 0..{MAX_RETRIES_LIMIT}, got {max_retries!r}")
        if proxy not in _PROXY_MODES:
            raise ProviderError(f"proxy must be 'env' or 'none', got {proxy!r}")
        if max_tokens is not None and (not isinstance(max_tokens, int) or max_tokens <= 0):
            raise ProviderError(f"max_tokens must be a positive integer, got {max_tokens!r}")
        self.timeout_s = float(timeout_s)
        self.max_retries = max_retries
        self.json_mode = bool(json_mode)
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.transcript_dir = Path(transcript_dir) if transcript_dir is not None else None
        self.proxy = proxy
        self.backoff_s = max(0.0, float(backoff_s))
        self.max_backoff_s = max(0.0, float(max_backoff_s))
        self.limits = limits
        self._sleep = sleep
        self._seq = 0
        self.last_transcript: Path | None = None
        handlers: list[urllib.request.BaseHandler] = [_RedirectsBecomeHTTPErrors()]
        if proxy == "none":
            handlers.append(urllib.request.ProxyHandler({}))
        self._opener = urllib.request.build_opener(*handlers)
        self.info = ProviderInfo(name="openai-compatible", model=self.model, real_llm=True)

    def _check_base_url(self, base_url: str, allow_insecure_http: bool) -> str:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ProviderError("LLM base URL is empty (set LEANC_BASE_URL, e.g. https://api.openai.com/v1)")
        url = base_url.strip().rstrip("/")
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ProviderError(f"LLM base URL must be an http(s) URL, got {self._redact(url)!r}")
        if parts.username or parts.password:
            raise ProviderError("LLM base URL must not contain credentials; put the key in LEANC_API_KEY instead")
        if parts.query or parts.fragment:
            raise ProviderError("LLM base URL must not contain a query string or fragment")
        if parts.scheme == "http" and self._api_key and not allow_insecure_http and not _is_loopback(parts.hostname):
            raise ProviderError(
                f"refusing to send the API key over plain http to {parts.hostname}: use https, or set "
                "LEANC_ALLOW_INSECURE_HTTP=1 if this network path is trusted"
            )
        return url

    @classmethod
    def from_env(
        cls,
        *,
        transcript_dir: Path | None = None,
        environ: Mapping[str, str] | None = None,
        **overrides: Any,
    ) -> OpenAICompatProvider:
        get = _env_reader(os.environ if environ is None else environ)
        model = get("LEANC_MODEL")
        if not model:
            raise ProviderError(
                "LEANC_MODEL is not set: export LEANC_MODEL=<model name served by your endpoint> "
                "(plus LEANC_API_KEY and, for non-OpenAI endpoints, LEANC_BASE_URL), "
                "or use --provider mock for offline demos"
            )
        api_key = get("LEANC_API_KEY") or get("OPENAI_API_KEY")
        base_url = get("LEANC_BASE_URL") or DEFAULT_BASE_URL
        targets_openai = _is_default_openai_host(base_url)
        if not api_key and targets_openai:
            raise ProviderError(
                "no API key: export LEANC_API_KEY (or OPENAI_API_KEY) for the OpenAI API, or point "
                "LEANC_BASE_URL at a local OpenAI-compatible server that needs no key"
            )
        kwargs = _optional_settings_from_env(get)
        kwargs.update(overrides)
        return cls(base_url, api_key, model, transcript_dir=transcript_dir, **kwargs)

    def __repr__(self) -> str:
        return f"<OpenAICompatProvider base_url={self.base_url!r} model={self.model!r}>"

    def _redact(self, text: str) -> str:
        if not text:
            return text
        key = getattr(self, "_api_key", "")
        if key and len(key) >= MIN_KEY_CHARS_SCRUBBED_VERBATIM:
            text = text.replace(key, REDACTED)
        return redact(text, extra_secrets=[key] if key else None)

    def _next_transcript_path(self, purpose: str) -> Path:
        while True:
            self._seq += 1
            path = self.transcript_dir / f"{self._seq:03d}-{purpose}.json"
            if not path.exists():
                return path

    def _write_transcript(self, purpose: str, record: dict[str, Any]) -> None:
        if self.transcript_dir is None:
            return
        text = utf8_safe(self._redact(json.dumps(record, indent=2, ensure_ascii=False, default=str))) + "\n"
        try:
            self.transcript_dir.mkdir(parents=True, exist_ok=True)
            path = self._next_transcript_path(purpose)
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            log.warning("could not write LLM transcript: %s", exc)
            return
        self.last_transcript = path

    def _messages_without_own_key(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        return [{**m, "content": self._redact(m.get("content", ""))} for m in messages]

    def _request_body(self, messages: list[dict[str, str]], want_json: bool) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "messages": self._messages_without_own_key(messages)}
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.max_tokens is not None:
            body["max_tokens"] = self.max_tokens
        if want_json and self.json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    def _headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"leanc/{__version__}",
        }
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def _http_error(self, exc: urllib.error.HTTPError) -> _Retryable | ProviderError:
        status = exc.code
        detail = self._error_detail(_read_error_body(exc))
        if _is_transient_status(status):
            retry_after = _parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
            return _Retryable(f"HTTP {status}{detail}", retry_after=retry_after)
        if _is_redirect_status(status):
            location = exc.headers.get("Location", "?") if exc.headers else "?"
            return ProviderError(
                f"LLM endpoint answered with a redirect (HTTP {status} to {self._redact(location)}); redirects are not "
                "followed - set LEANC_BASE_URL to the final URL"
            )
        hint = ""
        if status in AUTH_FAILURE_STATUS:
            hint = " - check LEANC_API_KEY / OPENAI_API_KEY and that the key may use this model"
        elif status == NOT_FOUND_STATUS:
            hint = f" - check LEANC_BASE_URL ({self.base_url}) and LEANC_MODEL ({self.model})"
        return ProviderError(f"LLM request failed: HTTP {status}{detail}{hint}")

    def _connection_error(self, reason: object) -> _Retryable | ProviderError:
        if isinstance(reason, ssl.SSLCertVerificationError):
            return ProviderError(f"TLS certificate verification failed for {self.base_url}: {reason}")
        if isinstance(reason, TimeoutError):
            return _Retryable(f"timed out after {self.timeout_s:g}s")
        return _Retryable(f"connection error: {reason}")

    def _attempt(self, url: str, data: bytes) -> tuple[int, str]:
        req = urllib.request.Request(url, data=data, method="POST", headers=self._headers())
        try:
            with self._opener.open(req, timeout=self.timeout_s) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
                status = getattr(resp, "status", 200)
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from None
        except urllib.error.URLError as exc:
            raise self._connection_error(exc.reason) from None
        except TimeoutError:
            raise _Retryable(f"timed out after {self.timeout_s:g}s") from None
        except (http.client.HTTPException, ConnectionError) as exc:
            raise _Retryable(f"connection error: {type(exc).__name__}: {exc}") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderError(f"LLM response larger than {MAX_RESPONSE_BYTES} bytes; refusing to process it")
        return status, raw.decode("utf-8", "replace")

    def _error_detail(self, body: str) -> str:
        if not body:
            return ""
        return f": {excerpt(self._redact(_error_message(body)), ERROR_DETAIL_CHARS)}"

    def _retry_delay(self, attempt: int, retry_after: float | None) -> float:
        delay = retry_after if retry_after is not None else self.backoff_s * (2 ** (attempt - 1))
        return min(delay, self.max_backoff_s)

    def _send_with_retries(self, url: str, data: bytes, purpose: str, record: dict[str, Any]) -> tuple[str, str | None]:
        attempts = self.max_retries + 1
        last_error = ""
        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                status, text = self._attempt(url, data)
            except _Retryable as exc:
                last_error = self._redact(str(exc))
                record["attempts"].append(
                    {"attempt": attempt, "error": last_error, "duration_s": round(time.monotonic() - started, 3)}
                )
                if attempt == attempts:
                    break
                delay = self._retry_delay(attempt, exc.retry_after)
                log.info(
                    "LLM %s attempt %d/%d failed (%s); retrying in %.1fs", purpose, attempt, attempts, last_error, delay
                )
                if delay > 0:
                    self._sleep(delay)
                continue
            record["attempts"].append(
                {"attempt": attempt, "status": status, "duration_s": round(time.monotonic() - started, 3)}
            )
            record["response"] = {"status": status, "body": text}
            content, finish = self._extract_content(text)
            record["response"]["content"] = content
            record["response"]["finish_reason"] = finish
            return content, finish
        raise ProviderError(f"LLM request to {url} failed after {attempts} attempt(s): {last_error}")

    def _chat(self, prompt: Prompt, purpose: str, *, want_json: bool) -> tuple[str, str | None]:
        url = f"{self.base_url}/chat/completions"
        body = self._request_body(prompt.messages(), want_json)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        record: dict[str, Any] = {
            "provider": self.info.name,
            "model": self.model,
            "purpose": purpose,
            "url": url,
            "started_at": utcnow(),
            "request": body,
            "attempts": [],
        }
        started = time.monotonic()
        try:
            return self._send_with_retries(url, data, purpose, record)
        except ProviderError as exc:
            msg = self._redact(str(exc))
            record["error"] = msg
            raise ProviderError(msg) from None
        finally:
            record["duration_s"] = round(time.monotonic() - started, 3)
            self._write_transcript(purpose, record)

    def _extract_content(self, text: str) -> tuple[str, str | None]:
        try:
            payload = json.loads(text)
        except JSON_LOAD_ERRORS:
            raise ProviderError(f"LLM endpoint returned a non-JSON body: {excerpt(self._redact(text))!r}") from None
        if not isinstance(payload, dict):
            raise ProviderError("LLM endpoint returned JSON that is not an object")
        choices = payload.get("choices")
        if not choices:
            if payload.get("error"):
                raise ProviderError(f"LLM endpoint returned an error{self._error_detail(text)}")
            raise ProviderError("LLM response has no choices")
        if not isinstance(choices, list) or not isinstance(choices[0], dict):
            raise ProviderError("LLM response has a malformed 'choices' field")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ProviderError("LLM response choice has no message")
        content = message.get("content")
        if isinstance(content, list):
            content = _text_of_content_parts(content)
        if content is None or (isinstance(content, str) and not content.strip()):
            refusal = message.get("refusal")
            if refusal:
                raise ProviderError(f"model refused the request: {excerpt(self._redact(str(refusal)))}")
            raise ProviderError(f"LLM response message has no content (finish_reason={choice.get('finish_reason')})")
        if not isinstance(content, str):
            raise ProviderError(f"LLM response content is not text ({type(content).__name__})")
        finish = choice.get("finish_reason")
        return content, finish if isinstance(finish, str) else None

    def _complete(self, prompt: Prompt, purpose: str, parser: Callable[[str], Any]) -> Any:
        content, finish = self._chat(prompt, purpose, want_json=True)
        try:
            return parser(content)
        except ProviderError as exc:
            hint = _CUT_OFF_ANSWER_HINT if finish == FINISH_REASON_CUT_OFF else ""
            raise ProviderError(self._redact(f"{purpose}: malformed model answer: {exc}{hint}")) from None

    def generate_spec_draft(self, req: SpecDraftRequest) -> SpecDraft:
        draft = self._complete(build_spec_draft_prompt(req, self.limits), "spec-draft", parse_spec_draft_response)
        draft.raw_response = self._redact(draft.raw_response)
        return draft

    def generate_code(self, req: CodeGenRequest) -> GeneratedCode:
        out = self._complete(build_codegen_prompt(req, self.limits), "generate", parse_code_response)
        out.raw_response = self._redact(out.raw_response)
        return out

    def propose_patch(self, req: RepairRequest) -> RepairProposal:
        prop = self._complete(build_repair_prompt(req, self.limits), f"repair-round-{req.round}", parse_repair_response)
        prop.raw_response = self._redact(prop.raw_response)
        return prop

    def analyze_counterexample(self, failure: FailureReport, sources: dict[str, str], spec: Spec) -> str:
        content, _ = self._chat(build_analyze_prompt(failure, sources, spec, self.limits), "analyze", want_json=False)
        return self._redact(content.strip())

    def explain_failure(self, result: VerificationResult) -> str:
        content, _ = self._chat(build_explain_prompt(result, self.limits), "explain", want_json=False)
        return self._redact(content.strip())


__all__ = ["DEFAULT_BASE_URL", "OpenAICompatProvider"]
