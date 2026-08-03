"""Mechanical verification of tests — generated or hand-written.

Three orthogonal gates: it compiles, it passes against correct code, and it FAILS against
deliberately broken code. The third is the one that matters, and the only one that catches
a test which is green in CI while asserting nothing.
"""

from .harness import language_of, verify_test
from .models import VERDICTS, TestOutcome, verdict_for
from .mutation import Mutant, apply_mutant, count_assertions, mutants_for, supported
from .sandbox import DockerSandbox, RunResult, Sandbox
from .symbols import SymbolIssue, SymbolReport, check_python_test

__all__ = [
    "VERDICTS", "DockerSandbox", "Mutant", "RunResult", "Sandbox", "TestOutcome",
    "apply_mutant", "count_assertions", "language_of", "mutants_for", "supported",
    "SymbolIssue", "SymbolReport", "check_python_test", "verdict_for", "verify_test",
]
