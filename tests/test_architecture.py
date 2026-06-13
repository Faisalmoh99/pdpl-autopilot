"""Architectural fitness function — the AI/deterministic boundary is enforced.

CLAUDE.md core decision principle:

    AI reads / suggests / explains. Deterministic logic decides / scores /
    classifies. A compliance decision must NEVER reach the user directly from
    an AI output.

This test makes that principle mechanically true. It runs `lint-imports`
against the `.importlinter` contract and asserts a clean exit. The contract
forbids the deterministic core (`services/decision`, `services/checks`,
`services/scoring`) from importing — directly or transitively — the reserved
AI namespace `pdpl.ai` or any LLM SDK (anthropic, openai, google genai, ...).

If a future change makes the decision path depend on an AI module, grimp sees
the import statically and `lint-imports` exits non-zero, so this test FAILS —
the same way every other invariant in this project fails the suite. The
boundary is no longer a convention; it is a gate.

Unlike the rest of the suite this test touches no database — it is pure
static import-graph analysis and runs offline.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Repo root holds `.importlinter`; `lint-imports` must run from there.
_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_deterministic_core_does_not_import_the_ai_layer() -> None:
    # Invoke import-linter's click command in a subprocess rather than relying
    # on the `lint-imports` console script being on PATH. The command reads
    # `.importlinter` from the working directory and exits non-zero on any
    # contract violation.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from importlinter.cli import lint_imports_command; "
            "lint_imports_command()",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    # On any contract violation lint-imports exits non-zero and prints which
    # forbidden import was found and through what chain. Surface that output.
    assert result.returncode == 0, (
        "import-linter contract violated — the deterministic core reached the "
        "AI layer or an LLM SDK:\n\n"
        f"{result.stdout}\n{result.stderr}"
    )
