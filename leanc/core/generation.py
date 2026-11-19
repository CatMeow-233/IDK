"""Code Generation Engine and Specification Engine (draft side).

* :func:`generate_sources` asks a provider to implement the *approved* spec and
  writes the result into the spec's editable source files only, after the
  generated-code policy check.
* :func:`draft_spec` asks a provider to turn a natural-language requirement
  into a *candidate* spec (+ optional harness / test drafts).  Drafts are
  written under ``drafts/<spec_id>/`` and marked ``provenance.origin: ai-draft``;
  they are never trusted until a human runs ``leanc spec approve``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from leanc.core.errors import LeanCError, ProviderError, SpecError
from leanc.core.hashing import sha256_text
from leanc.core.models import GenerationRecord, PolicyDecision
from leanc.core.redact import redact
from leanc.core.workspace import Workspace
from leanc.specs.approval import check_approval
from leanc.specs.loader import parse_spec_text, spec_to_yaml
from leanc.specs.model import Spec


class GenerationRejected(LeanCError):
    def __init__(self, message: str, decision: PolicyDecision | None = None):
        super().__init__(message)
        self.decision = decision


def generate_sources(
    spec: Spec,
    project_root: Path,
    provider,
    *,
    run_dir: Path,
    force: bool = False,
) -> GenerationRecord:
    """Generate the editable sources of an approved spec.

    Existing editable files are kept unless ``force`` is true.  Raises
    :class:`GenerationRejected` if the spec is not approved or the generated
    code violates the policy.
    """
    from leanc.llm.base import CodeGenRequest
    from leanc.repair.policy import check_generated

    ws = Workspace(project_root)
    approval = check_approval(spec, ws)
    if not approval.approved:
        raise GenerationRejected(
            "SPEC_UNAPPROVED: refusing to generate code for an unapproved spec: " + "; ".join(approval.reasons)
        )
    targets = [p for p in spec.sources.editable if force or not ws.exists(p)]
    if not targets:
        return GenerationRecord(provider=provider.info, files={}, note="all editable sources already exist (use --force to regenerate)")
    headers = {h: ws.read_text(h) for h in spec.sources.headers}
    req = CodeGenRequest(spec=spec, spec_yaml=spec_to_yaml(spec), headers=headers, targets=targets)
    out = provider.generate_code(req)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "generation-response.txt").write_text(redact(out.raw_response or ""), encoding="utf-8")
    files = {k: v for k, v in out.files.items()}
    unexpected = sorted(set(files) - set(targets))
    decision = check_generated(spec, files)
    if unexpected:
        raise GenerationRejected(f"provider returned files that were not requested: {unexpected}", decision)
    if not decision.allowed:
        lines = [f"{v.rule}: {v.message}" for v in decision.violations]
        raise GenerationRejected("generated code rejected by policy:\n  " + "\n  ".join(lines), decision)
    missing = sorted(set(targets) - set(files))
    if missing:
        raise GenerationRejected(f"provider did not generate: {missing}", decision)
    gen_dir = run_dir / "generated"
    for rel, content in files.items():
        ws.write_text(rel, content)
        dest = gen_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    return GenerationRecord(
        provider=provider.info,
        files={rel: sha256_text(c) for rel, c in files.items()},
        note=out.notes or "",
    )


_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def draft_spec(
    requirement: str,
    spec_id: str,
    project_root: Path,
    provider,
    *,
    title: str | None = None,
    force: bool = False,
) -> dict[str, str]:
    """Produce an UNTRUSTED spec draft.  Returns rel paths written."""
    from leanc.llm.base import SpecDraftRequest

    if not _SAFE_ID.match(spec_id):
        raise SpecError("spec id must match ^[a-z0-9][a-z0-9._-]{0,63}$")
    ws = Workspace(project_root)
    draft = provider.generate_spec_draft(SpecDraftRequest(requirement=requirement, spec_id=spec_id, title=title))
    try:
        data = yaml.safe_load(draft.spec_yaml)
    except yaml.YAMLError as exc:
        raise ProviderError(f"provider returned unparsable spec YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ProviderError("provider returned a spec that is not a mapping")
    # The draft is untrusted no matter what the model claims.
    data["spec_id"] = spec_id
    data.setdefault("version", 1)
    data["requirement"] = requirement
    data["provenance"] = {
        "origin": "ai-draft",
        "generator": provider.info.name + (f":{provider.info.model}" if provider.info.model else ""),
        "note": "UNTRUSTED AI DRAFT: review spec, harness and tests, then run `leanc spec approve`",
    }
    base = f"drafts/{spec_id}"
    written: dict[str, str] = {}
    paths = {
        "spec": f"{base}/spec.yaml",
        "harness": f"{base}/harness_draft.c",
        "tests": f"{base}/tests_draft.c",
        "notes": f"{base}/NOTES.md",
    }
    if not force:
        for p in paths.values():
            if ws.exists(p):
                raise LeanCError(f"{p} already exists (use --force to overwrite)")
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    header = (
        "# UNTRUSTED AI-GENERATED SPEC DRAFT (provenance.origin: ai-draft).\n"
        "# It has no effect until a human reviews it (and the harness/tests) and runs `leanc spec approve`.\n"
    )
    ws.write_text(paths["spec"], header + text)
    written["spec"] = paths["spec"]
    validation = "ok"
    try:
        parse_spec_text(text, source=paths["spec"])
    except SpecError as exc:
        validation = str(exc)
    if draft.harness_c:
        ws.write_text(paths["harness"], "/* UNTRUSTED AI DRAFT harness: review before approval. */\n" + draft.harness_c)
        written["harness"] = paths["harness"]
    if draft.tests_c:
        ws.write_text(paths["tests"], "/* UNTRUSTED AI DRAFT tests: review before approval. */\n" + draft.tests_c)
        written["tests"] = paths["tests"]
    notes = (
        f"# Draft for `{spec_id}` (UNTRUSTED)\n\n"
        f"Provider: {provider.info.name} (real LLM: {provider.info.real_llm})\n\n"
        f"Schema validation: {validation}\n\n"
        "Before approving:\n\n"
        "1. Move/rename the draft files to their final locations and fix the paths in the spec.\n"
        "2. Review every pre/postcondition against the original requirement.\n"
        "3. Review the harness: are the LEANC_ASSUME constraints too strong? Are there LEANC_REACH probes?\n"
        "4. Review the trusted tests and their expected results.\n"
        "5. Run `leanc spec approve <spec.yaml> --by <your name>`.\n\n"
        f"Provider notes:\n\n{redact(draft.notes or '(none)')}\n"
    )
    ws.write_text(paths["notes"], notes)
    written["notes"] = paths["notes"]
    written["validation"] = validation
    (ws.root / base / "draft-meta.json").write_text(
        json.dumps({"provider": provider.info.model_dump(), "validation": validation}, indent=2), encoding="utf-8"
    )
    return written
