"""Lean C: a spec-first, verifier-checked code generation pipeline for C17.

Correctness is never judged by the model that generated the code: it is judged by
independent tools (Clang, Clang Static Analyzer, sanitizers, CBMC) against a
human-approved, hash-locked specification and harness.
"""

__version__ = "0.1.0"
