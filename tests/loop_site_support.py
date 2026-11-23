from __future__ import annotations

from loop_contract_support import contracts_text, inductive_spec, loop_entry

from leanc.rules.loop_contracts import LoopContract, parse_loop_contracts
from leanc.rules.loop_sites import LoopSiteMatch, SiteProblemKind, loop_sites

REL = "src/loops.c"
OTHER_REL = "src/other.c"


def preprocessed(body: str, rel: str = REL) -> str:
    return f'# 1 "{rel}"\n{body}'


def contract_at(line: int, file: str = REL, **overrides: object) -> LoopContract:
    entry = loop_entry(file=file, line=line, invariant=["i <= n"], assigns=["i"], decreases="n - i")
    entry.update(overrides)
    contracts = parse_loop_contracts(inductive_spec([REL, OTHER_REL]), contracts_text(entry))
    assert contracts.accepted, contracts.problems
    return contracts.loops[0]


def sites_of(body: str, *lines: int, source: str | None = None) -> LoopSiteMatch:
    return loop_sites(REL, preprocessed(body), body if source is None else source, [contract_at(n) for n in lines])


def problem_kinds(match: LoopSiteMatch) -> list[SiteProblemKind]:
    return [problem.kind for problem in match.problems]


def only_problem_kind(body: str, line: int, source: str | None = None) -> SiteProblemKind:
    match = sites_of(body, line, source=source)
    assert not match.matched and not match.accepted
    assert [problem.line for problem in match.problems] == [line]
    (kind,) = problem_kinds(match)
    return kind
