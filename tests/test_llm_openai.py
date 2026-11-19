"""OpenAICompatProvider against a threaded local fake ``/chat/completions``
server, plus the prompts it sends.  No external network: the server listens on
127.0.0.1 and the provider bypasses the environment's proxies (proxy="none")."""

from __future__ import annotations

import json
import logging
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from leanc.core.errors import ProviderError
from leanc.core.models import (
    CounterexampleInput,
    Diagnostic,
    FailureReport,
    Location,
    StageResult,
    VerificationResult,
)
from leanc.core.status import Outcome, Status
from leanc.llm.base import CodeGenRequest, RepairRequest, SpecDraftRequest
from leanc.llm.openai_compat import OpenAICompatProvider
from leanc.llm.prompts import (
    FEW_SHOT_SPEC_YAML,
    PromptLimits,
    build_codegen_prompt,
    build_repair_prompt,
    build_spec_draft_prompt,
    spec_schema_summary,
    truncate,
)
from leanc.specs.loader import load_spec, parse_spec_text, spec_to_yaml

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "examples" / "safe_array_get"
KEY = "sk-test-" + "Zq9x" * 8


# ------------------------------------------------------------------ fake server
@dataclass
class Reply:
    status: int = 200
    body: Any = None  # dict -> JSON, str/bytes -> raw
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0


def chat(content: Any, finish: str = "stop", **message: Any) -> dict:
    return {
        "id": "cmpl-1",
        "object": "chat.completion",
        "model": "test-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content, **message}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class FakeLLM:
    def __init__(self) -> None:
        self.script: list[Reply] = []
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def push(self, *replies: Reply) -> None:
        self.script.extend(replies)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server API
        fake: FakeLLM = self.server.fake  # type: ignore[attr-defined]
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        with fake.lock:
            fake.requests.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "content_type": self.headers.get("Content-Type"),
                    "raw": raw.decode("utf-8"),
                    "body": json.loads(raw),
                }
            )
            reply = fake.script.pop(0) if fake.script else Reply(500, {"error": {"message": "script exhausted"}})
        if reply.delay:
            time.sleep(reply.delay)
        body = reply.body
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
        elif isinstance(body, str):
            data = body.encode()
        else:
            data = body or b""
        try:
            self.send_response(reply.status)
            self.send_header("Content-Type", "application/json")
            for k, v in reply.headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):  # client gave up (timeout test)
            pass

    def log_message(self, *args: Any) -> None:  # keep test output clean
        pass


@pytest.fixture
def fake():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    server.fake = FakeLLM()  # type: ignore[attr-defined]
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    t.start()
    server.fake.url = f"http://127.0.0.1:{server.server_address[1]}"  # type: ignore[attr-defined]
    try:
        yield server.fake  # type: ignore[attr-defined]
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def make(fake, sleeps, tmp_path):
    def _make(**kw: Any) -> OpenAICompatProvider:
        kw.setdefault("transcript_dir", tmp_path / "llm")
        kw.setdefault("proxy", "none")
        kw.setdefault("sleep", sleeps.append)
        return OpenAICompatProvider(fake.url + "/v1", kw.pop("api_key", KEY), kw.pop("model", "test-model"), **kw)

    return _make


@pytest.fixture
def spec():
    return load_spec(EXAMPLE / "spec.yaml")


def codegen_req(spec) -> CodeGenRequest:
    return CodeGenRequest(
        spec=spec,
        spec_yaml=spec_to_yaml(spec),
        headers={"include/safe_array.h": (EXAMPLE / "include/safe_array.h").read_text()},
        targets=["src/safe_array.c"],
    )


def failure() -> FailureReport:
    return FailureReport(
        id="cbmc:safe_array_get.pointer_dereference",
        kind="counterexample",
        tool="cbmc",
        property_id="safe_array_get.pointer_dereference.5",
        description="dereference failure: pointer outside object bounds",
        location=Location(file="src/safe_array.c", line=9, function="safe_array_get"),
        inputs=[CounterexampleInput(name="len", value="3"), CounterexampleInput(name="idx", value="3")],
    )


def repair_req(spec, **over: Any) -> RepairRequest:
    kw: dict[str, Any] = dict(
        requirement=spec.requirement,
        spec=spec,
        spec_yaml=spec_to_yaml(spec),
        round=1,
        sources={"src/safe_array.c": (EXAMPLE / "src/safe_array.c").read_text().replace("idx >= len", "idx > len")},
        headers={"include/safe_array.h": (EXAMPLE / "include/safe_array.h").read_text()},
        harness=(EXAMPLE / "harness/safe_array_get_harness.c").read_text(),
        tests=(EXAMPLE / "tests/test_safe_array_get.c").read_text(),
        diagnostics=[Diagnostic(tool="clang", severity="warning", message="unused variable 'x'")],
        failures=[failure()],
        history=[{"round": 0, "note": "earlier attempt reverted"}],
    )
    kw.update(over)
    return RepairRequest(**kw)


def vresult() -> VerificationResult:
    return VerificationResult(
        run_id="r1",
        status=Status.COUNTEREXAMPLE_FOUND,
        stages=[StageResult(stage="cbmc", outcome=Outcome.FAIL, failures=[failure()])],
    )


GOOD_FILES = {"files": {"src/safe_array.c": "#include \"safe_array.h\"\nint safe_array_get(const int *a, size_t n, size_t i, int *o) { return 0; }\n"}, "notes": "ok"}


# ------------------------------------------------------------------ request shape
def test_generate_code_request_body_and_headers(fake, make, spec):
    fake.push(Reply(body=chat(json.dumps(GOOD_FILES))))
    prov = make()
    out = prov.generate_code(codegen_req(spec))
    assert out.files == GOOD_FILES["files"] and out.notes == "ok"
    assert json.loads(out.raw_response) == GOOD_FILES
    (req,) = fake.requests
    assert req["path"] == "/v1/chat/completions"
    assert req["authorization"] == f"Bearer {KEY}"
    assert req["content_type"] == "application/json"
    body = req["body"]
    assert body["model"] == "test-model" and body["temperature"] == 0.0
    assert "response_format" not in body and "max_tokens" not in body
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user"]
    system, user = (m["content"] for m in body["messages"])
    assert "C17" in system and "__CPROVER_" in system and '"files"' in system and '"notes"' in system
    assert "src/safe_array.c" in user and spec.target.signature in user
    assert "SAFE_ARRAY_ERR_RANGE" in user  # protected header content is shown
    assert prov.info.model_dump() == {"name": "openai-compatible", "model": "test-model", "real_llm": True}


def test_json_mode_temperature_and_max_tokens(fake, make, spec):
    fake.push(Reply(body=chat(json.dumps(GOOD_FILES))), Reply(body=chat("plain explanation")))
    prov = make(json_mode=True, temperature=None, max_tokens=512)
    prov.generate_code(codegen_req(spec))
    assert prov.explain_failure(vresult()) == "plain explanation"
    gen, expl = (r["body"] for r in fake.requests)
    assert gen["response_format"] == {"type": "json_object"} and gen["max_tokens"] == 512
    assert "temperature" not in gen
    assert "response_format" not in expl  # free-text answers are not forced into JSON


def test_repair_prompt_states_hard_rules_and_parses_answer(fake, make, spec):
    answer = "Here is the fix:\n```json\n" + json.dumps(
        {"root_cause": "off by one", "rationale": "use >=", "files": {"src/safe_array.c": "int x;\n"}}
    ) + "\n```"
    fake.push(Reply(body=chat(answer)))
    prop = make().propose_patch(repair_req(spec))
    assert prop.root_cause == "off by one" and prop.files == {"src/safe_array.c": "int x;\n"} and prop.patch is None
    system, user = (m["content"] for m in fake.requests[0]["body"]["messages"])
    for phrase in (
        "NEVER modify protected files",
        "__CPROVER_",
        "LEANC_",
        "assume",
        "NEVER delete, weaken or disable assertions",
        "return a constant",
        "swallow",
        "MINIMAL change",
        "ROOT CAUSE",
        '"root_cause"',
        '"rationale"',
        '"files"',
        "ONLY a JSON object",
    ):
        assert phrase in system, phrase
    for path in ("harness/safe_array_get_harness.c", "include/safe_array.h", "tests/test_safe_array_get.c"):
        assert path in user
    assert "idx=3" in user and "len=3" in user and "unused variable" in user and "earlier attempt reverted" in user
    assert re.search(r"^\s*>\s*9 \| ", user, re.M)  # numbered excerpt around the counterexample location
    assert "Repair round 1" in user


def test_analyze_and_explain_return_stripped_text(fake, make, spec):
    fake.push(Reply(body=chat("  idx == len reads past the end.  \n")), Reply(body=chat("Status means a bug.")))
    prov = make(json_mode=True)
    assert prov.analyze_counterexample(failure(), {"src/safe_array.c": "a\nb\n"}, spec) == "idx == len reads past the end."
    assert prov.explain_failure(vresult()) == "Status means a bug."
    analyze_user = fake.requests[0]["body"]["messages"][1]["content"]
    assert "cbmc:safe_array_get.pointer_dereference" in analyze_user
    explain_sys = fake.requests[1]["body"]["messages"][0]["content"]
    assert "VERIFIED_BOUNDED" in explain_sys and "not a proof" in explain_sys


def test_spec_draft_round_trip(fake, make):
    ans = {"spec_yaml": FEW_SHOT_SPEC_YAML, "harness_c": "int main(void) { return 0; }", "tests_c": None, "notes": "n"}
    fake.push(Reply(body=chat(json.dumps(ans))))
    d = make().generate_spec_draft(SpecDraftRequest(requirement="clamp an int", spec_id="clamp", title="Clamp"))
    assert d.spec_yaml == FEW_SHOT_SPEC_YAML and d.harness_c.endswith("\n") and d.tests_c is None
    system, user = (m["content"] for m in fake.requests[0]["body"]["messages"])
    assert "UNTRUSTED" in system and "leanc spec approve" in system
    assert "verification: object (required)" in system  # schema summary derived from the model
    assert '"spec_yaml"' in system and '"harness_c"' in system and '"tests_c"' in system
    assert "spec_id: safe-array-get" in user and "clamp an int" in user and "UNTRUSTED" in user


def test_content_parts_form_is_accepted(fake, make, spec):
    parts = [{"type": "text", "text": json.dumps(GOOD_FILES)[:20]}, {"type": "text", "text": json.dumps(GOOD_FILES)[20:]}]
    fake.push(Reply(body=chat(parts)))
    assert make().generate_code(codegen_req(spec)).files == GOOD_FILES["files"]


# ------------------------------------------------------------------ retries
def test_retry_500_then_200(fake, make, spec, sleeps):
    fake.push(Reply(500, {"error": {"message": "boom"}}), Reply(body=chat(json.dumps(GOOD_FILES))))
    out = make(backoff_s=0.5).generate_code(codegen_req(spec))
    assert out.files == GOOD_FILES["files"]
    assert len(fake.requests) == 2 and sleeps == [0.5]
    assert fake.requests[0]["raw"] == fake.requests[1]["raw"]  # identical retry


def test_retry_after_header_is_honoured_and_capped(fake, make, spec, sleeps):
    fake.push(
        Reply(429, {"error": {"message": "rate limited"}}, headers={"Retry-After": "2"}),
        Reply(429, {"error": {"message": "rate limited"}}, headers={"Retry-After": "999"}),
        Reply(body=chat(json.dumps(GOOD_FILES))),
    )
    make(max_backoff_s=5).generate_code(codegen_req(spec))
    assert sleeps == [2.0, 5.0]


def test_retries_exhausted_with_exponential_backoff(fake, make, spec, sleeps):
    fake.push(*[Reply(503, {"error": {"message": "overloaded"}})] * 4)
    with pytest.raises(ProviderError, match=r"after 4 attempt\(s\): HTTP 503: overloaded"):
        make(max_retries=3).generate_code(codegen_req(spec))
    assert sleeps == [1.0, 2.0, 4.0] and len(fake.requests) == 4


def test_connection_refused_is_retried_then_reported(sleeps, tmp_path, spec):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    prov = OpenAICompatProvider(f"http://127.0.0.1:{port}/v1", KEY, "m", max_retries=2, proxy="none", sleep=sleeps.append)
    with pytest.raises(ProviderError, match="connection error") as ei:
        prov.generate_code(codegen_req(spec))
    assert "after 3 attempt(s)" in str(ei.value) and len(sleeps) == 2


def test_timeout_is_retried_and_reported(fake, make, spec, sleeps):
    fake.push(Reply(body=chat("{}"), delay=1.5), Reply(body=chat("{}"), delay=1.5))
    t0 = time.monotonic()
    with pytest.raises(ProviderError, match=r"timed out after 0\.3s") as ei:
        make(timeout_s=0.3, max_retries=1).generate_code(codegen_req(spec))
    assert time.monotonic() - t0 < 3.0
    assert "after 2 attempt(s)" in str(ei.value) and len(fake.requests) == 2 and len(sleeps) == 1


# ------------------------------------------------------------------ errors and secrets
def test_401_is_not_retried_and_never_leaks_the_key(fake, make, spec, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    fake.push(Reply(401, {"error": {"message": f"Incorrect API key provided: {KEY}", "code": "invalid_api_key"}}))
    prov = make()
    with pytest.raises(ProviderError) as ei:
        prov.generate_code(codegen_req(spec))
    msg = str(ei.value)
    assert "HTTP 401" in msg and "LEANC_API_KEY" in msg and "[REDACTED]" in msg
    assert KEY not in msg and KEY not in repr(ei.value)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__
    assert len(fake.requests) == 1
    assert KEY not in caplog.text
    transcript = prov.last_transcript.read_text()
    assert KEY not in transcript and json.loads(transcript)["error"].startswith("LLM request failed: HTTP 401")
    assert KEY not in repr(prov)


@pytest.mark.parametrize(
    "reply, match",
    [
        (Reply(body=chat("I'd rather explain in prose.")), "malformed model answer: no JSON object"),
        (Reply(body=chat(json.dumps({"notes": "no files"}))), "missing required key"),
        (Reply(body="<html>gateway</html>"), "non-JSON body"),
        (Reply(body={"choices": []}), "no choices"),
        (Reply(body={"error": {"message": "model overloaded"}}), "returned an error: model overloaded"),
        (Reply(body=chat(None, refusal="I can't help with that")), "refused"),
        (Reply(body=chat(None)), "no content"),
        (Reply(body={"choices": [{"message": "x"}]}), "no message"),
        (Reply(body=chat('{"files": {"src/a.c": "int', finish="length")), "finish_reason=length"),
        (Reply(404, {"error": {"message": "model not found"}}), "check LEANC_BASE_URL"),
    ],
)
def test_malformed_or_failed_responses_raise(fake, make, spec, reply, match):
    fake.push(reply)
    with pytest.raises(ProviderError, match=match):
        make(max_retries=0).generate_code(codegen_req(spec))
    assert len(fake.requests) == 1


def test_redirects_are_not_followed(fake, make, spec):
    fake.push(Reply(302, b"", headers={"Location": fake.url + "/elsewhere"}), Reply(body=chat(json.dumps(GOOD_FILES))))
    with pytest.raises(ProviderError, match="redirect"):
        make().generate_code(codegen_req(spec))
    assert len(fake.requests) == 1  # Authorization was not re-sent anywhere


def test_transcripts_are_written_and_redacted(fake, make, spec, tmp_path, monkeypatch):
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "tok-0123456789abcdef")
    fake.push(Reply(body=chat(json.dumps(GOOD_FILES))), Reply(body=chat(f"explained; key was {KEY}")))
    prov = make()
    req = codegen_req(spec)
    req.spec_yaml += f"\n# leaked: {KEY} and tok-0123456789abcdef\n"
    prov.generate_code(req)
    assert "explained; key was [REDACTED]" == prov.explain_failure(vresult())
    sent = fake.requests[0]["raw"]
    assert KEY not in sent and "tok-0123456789abcdef" not in sent and "[REDACTED]" in sent
    files = sorted((tmp_path / "llm").iterdir())
    assert [f.name for f in files] == ["001-generate.json", "002-explain.json"]
    for f in files:
        text = f.read_text()
        assert KEY not in text and "tok-0123456789abcdef" not in text
        rec = json.loads(text)
        assert rec["model"] == "test-model" and rec["request"]["messages"][0]["role"] == "system"
        assert rec["attempts"][-1]["status"] == 200 and "duration_s" in rec
    gen = json.loads(files[0].read_text())
    assert json.loads(gen["response"]["content"]) == GOOD_FILES
    assert "Authorization" not in files[0].read_text() and "Bearer" not in files[0].read_text()


def test_transcript_numbering_skips_existing_files(fake, make, spec, tmp_path):
    (tmp_path / "llm").mkdir()
    (tmp_path / "llm" / "001-generate.json").write_text("{}")
    fake.push(Reply(body=chat(json.dumps(GOOD_FILES))))
    prov = make()
    prov.generate_code(codegen_req(spec))
    assert prov.last_transcript.name == "002-generate.json"
    assert (tmp_path / "llm" / "001-generate.json").read_text() == "{}"


# ------------------------------------------------------------------ configuration
def test_from_env_errors_are_actionable():
    with pytest.raises(ProviderError, match="LEANC_MODEL is not set"):
        OpenAICompatProvider.from_env(environ={})
    with pytest.raises(ProviderError, match="LEANC_API_KEY"):
        OpenAICompatProvider.from_env(environ={"LEANC_MODEL": "m"})
    for bad in ("abc", "0", "-3"):
        with pytest.raises(ProviderError, match="LEANC_LLM_TIMEOUT"):
            OpenAICompatProvider.from_env(environ={"LEANC_MODEL": "m", "LEANC_API_KEY": KEY, "LEANC_LLM_TIMEOUT": bad})
    with pytest.raises(ProviderError, match="LEANC_LLM_MAX_RETRIES"):
        OpenAICompatProvider.from_env(environ={"LEANC_MODEL": "m", "LEANC_API_KEY": KEY, "LEANC_LLM_MAX_RETRIES": "x"})
    with pytest.raises(ProviderError, match="LEANC_LLM_PROXY"):
        OpenAICompatProvider.from_env(environ={"LEANC_MODEL": "m", "LEANC_API_KEY": KEY, "LEANC_LLM_PROXY": "socks"})


def test_from_env_values():
    p = OpenAICompatProvider.from_env(
        environ={"LEANC_MODEL": " m1 ", "OPENAI_API_KEY": "sk-openai-fallback-123456", "LEANC_LLM_TIMEOUT": "7.5",
                 "LEANC_LLM_JSON_MODE": "1", "LEANC_LLM_MAX_TOKENS": "100", "LEANC_LLM_TEMPERATURE": "default"}
    )
    assert p.base_url == "https://api.openai.com/v1" and p.model == "m1" and p.timeout_s == 7.5
    assert p.json_mode is True and p.max_tokens == 100 and p.temperature is None and p.max_retries == 3
    assert p._headers()["Authorization"] == "Bearer sk-openai-fallback-123456"
    q = OpenAICompatProvider.from_env(environ={"LEANC_MODEL": "m", "LEANC_API_KEY": KEY, "OPENAI_API_KEY": "other-key-123"})
    assert q._headers()["Authorization"] == f"Bearer {KEY}" and q.json_mode is False and q.timeout_s == 120


def test_from_env_reads_os_environ_and_talks_to_local_server(fake, monkeypatch):
    monkeypatch.setenv("LEANC_MODEL", "env-model")
    monkeypatch.setenv("LEANC_API_KEY", KEY)
    monkeypatch.setenv("LEANC_BASE_URL", fake.url + "/v1/")
    monkeypatch.setenv("LEANC_LLM_PROXY", "none")
    monkeypatch.delenv("LEANC_LLM_TIMEOUT", raising=False)
    fake.push(Reply(body=chat("hello")))
    p = OpenAICompatProvider.from_env()
    assert p.explain_failure(vresult()) == "hello"
    assert fake.requests[0]["path"] == "/v1/chat/completions" and fake.requests[0]["body"]["model"] == "env-model"
    assert fake.requests[0]["authorization"] == f"Bearer {KEY}"


def test_keyless_local_server_sends_no_authorization(fake):
    fake.push(Reply(body=chat("ok")))
    p = OpenAICompatProvider.from_env(environ={"LEANC_MODEL": "local", "LEANC_BASE_URL": fake.url + "/v1", "LEANC_LLM_PROXY": "none"})
    assert p.explain_failure(vresult()) == "ok"
    assert fake.requests[0]["authorization"] is None


def test_base_url_validation():
    with pytest.raises(ProviderError, match="plain http"):
        OpenAICompatProvider("http://llm.example.com/v1", KEY, "m")
    assert OpenAICompatProvider("http://llm.example.com/v1", KEY, "m", allow_insecure_http=True).base_url.startswith("http://")
    ok = OpenAICompatProvider.from_env(
        environ={"LEANC_MODEL": "m", "LEANC_API_KEY": KEY, "LEANC_BASE_URL": "http://llm.example.com/v1", "LEANC_ALLOW_INSECURE_HTTP": "1"}
    )
    assert ok.base_url == "http://llm.example.com/v1"
    assert OpenAICompatProvider("http://localhost:8000/v1", KEY, "m").base_url == "http://localhost:8000/v1"
    with pytest.raises(ProviderError, match="credentials"):
        OpenAICompatProvider("https://user:pw@llm.example.com/v1", KEY, "m")
    with pytest.raises(ProviderError, match="http\\(s\\) URL"):
        OpenAICompatProvider("ftp://llm.example.com", KEY, "m")
    with pytest.raises(ProviderError, match="no model"):
        OpenAICompatProvider("https://llm.example.com/v1", KEY, " ")
    with pytest.raises(ProviderError, match="timeout"):
        OpenAICompatProvider("https://llm.example.com/v1", KEY, "m", timeout_s=0)
    with pytest.raises(ProviderError, match="proxy"):
        OpenAICompatProvider("https://llm.example.com/v1", KEY, "m", proxy="socks")  # type: ignore[arg-type]


# ------------------------------------------------------------------ prompt construction
def test_schema_summary_is_derived_from_the_model():
    s = spec_schema_summary()
    for needle in ("spec_id: string (required)", "unwind: integer (>=1, <=100000) = 10", "harness: string (required)",
                   "editable: list of string (at least 1 item(s)) (required)", "origin: human | ai-draft",
                   "bounds, pointer, div-by-zero, signed-overflow, pointer-overflow, undefined-shift"):
        assert needle in s, needle


def test_few_shot_spec_is_a_valid_spec_matching_the_example():
    shot = parse_spec_text(FEW_SHOT_SPEC_YAML)
    example = load_spec(EXAMPLE / "spec.yaml")
    assert shot.target == example.target and shot.contract == example.contract
    assert shot.verification == example.verification and shot.sources == example.sources


def test_context_caps_and_patch_fallback(spec):
    limits = PromptLimits(max_file_chars=5_000, max_total_source_chars=6_000, max_failures=2, max_diagnostics=1)
    big = "/* filler */\n" * 2_000
    req = repair_req(
        spec,
        sources={"src/safe_array.c": big},
        failures=[failure()] * 5,
        diagnostics=[Diagnostic(tool="clang", message=f"w{i}") for i in range(4)],
    )
    p = build_repair_prompt(req, limits)
    assert len(p.user) < 60_000
    assert "truncated" in p.user and '"patch"' in p.user and "src/safe_array.c" in p.user
    assert "3 more failure(s) omitted" in p.user and "3 more diagnostic(s) omitted" in p.user
    tiny = build_codegen_prompt(codegen_req(spec), PromptLimits(max_prompt_chars=2_000))
    assert len(tiny.user) < 2_200 and "prompt truncated" in tiny.user
    assert truncate("q" * 100, 10).count("q") == 10 and truncate("q" * 10, 10) == "q" * 10


def test_prompts_are_redacted_and_deterministic(spec, monkeypatch):
    monkeypatch.setenv("LEANC_API_KEY", "sk-envsecret-ABCDEFGHIJKLMNOP")
    req = SpecDraftRequest(requirement="use key sk-envsecret-ABCDEFGHIJKLMNOP please", spec_id="x")
    p = build_spec_draft_prompt(req)
    assert "sk-envsecret" not in p.user and "[REDACTED]" in p.user
    assert p == build_spec_draft_prompt(req)
    assert build_repair_prompt(repair_req(spec)) == build_repair_prompt(repair_req(spec))
