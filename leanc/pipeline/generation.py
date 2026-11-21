from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from leanc.foundation.errors import YAML_LOAD_ERRORS, LeanCError, ProviderError, SpecError
from leanc.foundation.hashing import sha256_text
from leanc.foundation.models import GenerationRecord, PolicyDecision
from leanc.foundation.redact import redact
from leanc.foundation.workspace import Workspace
from leanc.lang.c_lexer import CSource
from leanc.llm.base import CodeGenRequest, SpecDraftRequest
from leanc.llm.parsing import require_valid_unicode
from leanc.repair.policy import check_generated
from leanc.rules.definitions import find_function_definitions
from leanc.specs.approval import check_approval
from leanc.specs.loader import parse_spec_text, spec_to_yaml
from leanc.specs.model import Spec

SPEC_UNAPPROVED_PREFIX = "SPEC_UNAPPROVED"
DRAFT_ORIGIN = "ai-draft"
DRAFT_NOTE = "UNTRUSTED AI DRAFT: review spec, harness and tests, then run `leanc spec approve`"
DRAFT_SPEC_HEADER = (
    "# UNTRUSTED AI-GENERATED SPEC DRAFT (provenance.origin: ai-draft).\n"
    "# It has no effect until a human reviews it (and the harness/tests) and runs `leanc spec approve`.\n"
)
DRAFT_HARNESS_HEADER = "/* UNTRUSTED AI DRAFT harness: review before approval. */\n"
DRAFT_TESTS_HEADER = "/* UNTRUSTED AI DRAFT tests: review before approval. */\n"
DRAFT_YAML_WIDTH = 100
SAFE_SPEC_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"

_SAFE_ID = re.compile(SAFE_SPEC_ID_PATTERN)


class GenerationRejected(LeanCError):
    def __init__(self, message: str, decision: PolicyDecision | None = None):
        super().__init__(message)
        self.decision = decision


def _is_unchanged_echo(spec: Spec, ws: Workspace, rel: str, content: str) -> bool:
    if rel not in spec.sources.editable or not ws.exists(rel):
        return False
    try:
        current = ws.read_text(rel)
    except LeanCError:
        return False
    return current == content


def _drop_unchanged_echoes(spec: Spec, ws: Workspace, files: dict[str, str], targets: list[str]) -> list[str]:
    notes: list[str] = []
    for rel in sorted(set(files) - set(targets)):
        if _is_unchanged_echo(spec, ws, rel, files[rel]):
            del files[rel]
            notes.append(f"ignored unchanged echo of kept file {rel}")
    return notes


def _accept_target_defined_in_kept_file(spec: Spec, ws: Workspace, files: dict[str, str], decision: PolicyDecision) -> PolicyDecision:
    kept = {p: ws.read_text(p) for p in spec.sources.editable if p not in files and ws.exists(p)}
    if not kept or not any(v.rule == "MISSING_TARGET" for v in decision.violations):
        return decision
    if not any(find_function_definitions(CSource(text), spec.target.function, rel) for rel, text in kept.items()):
        return decision
    remaining = [v for v in decision.violations if v.rule != "MISSING_TARGET"]
    return PolicyDecision(allowed=not remaining, violations=remaining)


def _policy_rejection(decision: PolicyDecision) -> GenerationRejected:
    lines = [f"{v.rule}: {v.message}" + (f" ({v.file}:{v.line})" if v.file else "") for v in decision.violations]
    return GenerationRejected("generated code rejected by policy:\n  " + "\n  ".join(lines), decision)


def _write_generated(ws: Workspace, files: dict[str, str], run_dir: Path) -> list[str]:
    notes: list[str] = []
    generated_dir = run_dir / "generated"
    backup_dir = run_dir / "backup" / "before-generate"
    for rel, content in files.items():
        if ws.exists(rel):
            notes.append(_back_up_user_code(ws, rel, backup_dir))
        ws.write_text(rel, content)
        dest = generated_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    return notes


def _back_up_user_code(ws: Workspace, rel: str, backup_dir: Path) -> str:
    backup = backup_dir / rel
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(ws.read_bytes(rel))
    return f"previous {rel} backed up to {backup}"


def generate_sources(
    spec: Spec,
    project_root: Path,
    provider,
    *,
    run_dir: Path,
    force: bool = False,
) -> GenerationRecord:
    ws = Workspace(project_root)
    approval = check_approval(spec, ws)
    if not approval.approved:
        raise GenerationRejected(
            f"{SPEC_UNAPPROVED_PREFIX}: refusing to generate code for an unapproved spec: " + "; ".join(approval.reasons)
        )
    targets = [p for p in spec.sources.editable if force or not ws.exists(p)]
    if not targets:
        return GenerationRecord(provider=provider.info, files={}, note="all editable sources already exist (use --force to regenerate)")
    headers = {h: ws.read_text(h) for h in spec.sources.headers}
    response = provider.generate_code(CodeGenRequest(spec=spec, spec_yaml=spec_to_yaml(spec), headers=headers, targets=targets))
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "generation-response.txt").write_text(redact(response.raw_response or ""), encoding="utf-8")
    files = dict(response.files)
    notes = _drop_unchanged_echoes(spec, ws, files, targets)
    unexpected = sorted(set(files) - set(targets))
    if unexpected:
        raise GenerationRejected(f"provider returned files that were not requested: {unexpected}")
    decision = _accept_target_defined_in_kept_file(spec, ws, files, check_generated(spec, files))
    if not decision.allowed:
        raise _policy_rejection(decision)
    missing = sorted(set(targets) - set(files))
    if missing:
        raise GenerationRejected(f"provider did not generate: {missing}", decision)
    notes.extend(_write_generated(ws, files, run_dir))
    if response.notes:
        notes.insert(0, response.notes)
    return GenerationRecord(
        provider=provider.info,
        files={rel: sha256_text(c) for rel, c in files.items()},
        note="; ".join(notes),
    )


def _untrusted_draft_data(draft_yaml: str, requirement: str, spec_id: str, provider) -> dict:
    try:
        data = yaml.safe_load(draft_yaml)
    except YAML_LOAD_ERRORS as exc:
        raise ProviderError(f"provider returned unparsable spec YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ProviderError("provider returned a spec that is not a mapping")
    require_valid_unicode(data, "provider spec YAML")
    data["spec_id"] = spec_id
    data.setdefault("version", 1)
    data["requirement"] = requirement
    data["provenance"] = {
        "origin": DRAFT_ORIGIN,
        "generator": provider.info.name + (f":{provider.info.model}" if provider.info.model else ""),
        "note": DRAFT_NOTE,
    }
    return data


def _draft_paths(spec_id: str) -> dict[str, str]:
    base = f"drafts/{spec_id}"
    return {
        "spec": f"{base}/spec.yaml",
        "harness": f"{base}/harness_draft.c",
        "tests": f"{base}/tests_draft.c",
        "notes": f"{base}/NOTES.md",
    }


def _schema_validation(text: str, source: str) -> str:
    try:
        parse_spec_text(text, source=source)
    except SpecError as exc:
        return str(exc)
    return "ok"


def _review_notes(spec_id: str, provider, validation: str, provider_notes: str | None) -> str:
    return (
        f"# Draft for `{spec_id}` (UNTRUSTED)\n\n"
        f"Provider: {provider.info.name} (real LLM: {provider.info.real_llm})\n\n"
        f"Schema validation: {validation}\n\n"
        "Before approving:\n\n"
        "1. Move/rename the draft files to their final locations and fix the paths in the spec.\n"
        "2. Review every pre/postcondition against the original requirement.\n"
        "3. Review the harness: are the LEANC_ASSUME constraints too strong? Are there LEANC_REACH probes?\n"
        "4. Review the trusted tests and their expected results.\n"
        "5. Run `leanc spec approve <spec.yaml> --by <your name>`.\n\n"
        f"Provider notes:\n\n{redact(provider_notes or '(none)')}\n"
    )


def draft_spec(
    requirement: str,
    spec_id: str,
    project_root: Path,
    provider,
    *,
    title: str | None = None,
    force: bool = False,
) -> dict[str, str]:
    if not _SAFE_ID.match(spec_id):
        raise SpecError(f"spec id must match {SAFE_SPEC_ID_PATTERN}")
    ws = Workspace(project_root)
    draft = provider.generate_spec_draft(SpecDraftRequest(requirement=requirement, spec_id=spec_id, title=title))
    data = _untrusted_draft_data(draft.spec_yaml, requirement, spec_id, provider)
    paths = _draft_paths(spec_id)
    if not force:
        for p in paths.values():
            if ws.exists(p):
                raise LeanCError(f"{p} already exists (use --force to overwrite)")
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=DRAFT_YAML_WIDTH)
    written: dict[str, str] = {}
    ws.write_text(paths["spec"], DRAFT_SPEC_HEADER + text)
    written["spec"] = paths["spec"]
    validation = _schema_validation(text, paths["spec"])
    if draft.harness_c:
        ws.write_text(paths["harness"], DRAFT_HARNESS_HEADER + draft.harness_c)
        written["harness"] = paths["harness"]
    if draft.tests_c:
        ws.write_text(paths["tests"], DRAFT_TESTS_HEADER + draft.tests_c)
        written["tests"] = paths["tests"]
    ws.write_text(paths["notes"], _review_notes(spec_id, provider, validation, draft.notes))
    written["notes"] = paths["notes"]
    written["validation"] = validation
    ws.write_text(
        f"drafts/{spec_id}/draft-meta.json",
        json.dumps({"provider": provider.info.model_dump(), "validation": validation}, indent=2),
    )
    return written
