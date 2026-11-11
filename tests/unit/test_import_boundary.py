"""Import boundary checks for the zero-consumer rule (R-12.7)."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LINT_IMPORTS = Path(sys.executable).parent / "lint-imports"


def _run_lint_imports(cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no untrusted input
        [str(LINT_IMPORTS)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_core_packages_do_not_import_consumer_modules() -> None:
    """R-12.7: the repository's import contract holds with no forbidden imports."""

    result = _run_lint_imports(REPO_ROOT, os.environ.copy())

    assert result.returncode == 0, result.stdout + result.stderr


def test_forbidden_import_in_scratch_copy_fails(tmp_path: Path) -> None:
    """R-12.7: a core package importing a consumer module breaks the contract."""

    shutil.copytree(
        REPO_ROOT / "src" / "invoicebot",
        tmp_path / "invoicebot",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copy2(REPO_ROOT / ".importlinter", tmp_path / ".importlinter")
    queue_init = tmp_path / "invoicebot" / "queue" / "__init__.py"
    queue_init.write_text(
        queue_init.read_text(encoding="utf-8") + "\nimport invoicebot.api\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(tmp_path) if not existing else os.pathsep.join([str(tmp_path), existing])
    result = _run_lint_imports(tmp_path, env)
    output = result.stdout + result.stderr

    assert result.returncode != 0, output
    # The scratch copy must be the package under analysis, not the editable install.
    assert "invoicebot.queue" in output, output
