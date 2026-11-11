"""Config consistency rules run by ``scripts/check_config.py`` (R-11.6).

Each rule returns a :class:`Problem`. A blocking problem fails CI; the others
are only reported, so ``main`` never freezes a deploy (R-11.6).
"""

from __future__ import annotations

from dataclasses import dataclass

from invoicebot.config.models import Config, Portal

PORTAL_ADAPTER_PACKAGE = "invoicebot.fetchers.portals."
_PYTHON_ADAPTER_PREFIX = "python:"


@dataclass(frozen=True)
class Problem:
    """One failed check, printed as a single line by the CLI."""

    code: str
    path: str
    message: str
    blocking: bool


def run_checks(cfg: Config, portals: list[Portal], *, pr_mode: bool) -> list[Problem]:
    """Return every problem found in ``cfg`` and ``portals`` (R-11.6, R-5.8, D43)."""
    problems: list[Problem] = []
    for index, portal in enumerate(portals):
        problems.extend(_portal_problems(index, portal, pr_mode=pr_mode))
    problems.extend(_llm_fallback_problems(cfg))
    return problems


def _portal_problems(index: int, portal: Portal, *, pr_mode: bool) -> list[Problem]:
    problems: list[Problem] = []
    if portal.status != "active":
        problems.append(
            Problem(
                code="PORTAL_NOT_ACTIVE",
                path=f"portals[{index}].status",
                message=f"portal '{portal.key}' has status '{portal.status}'; only active portals are used",
                blocking=pr_mode,
            )
        )
    adapter_problem = _adapter_problem(index, portal)
    if adapter_problem is not None:
        problems.append(adapter_problem)
    return problems


def _adapter_problem(index: int, portal: Portal) -> Problem | None:
    path = f"portals[{index}].adapter"
    adapter = portal.adapter
    if adapter == "generic_form":
        return None
    if not adapter.startswith(_PYTHON_ADAPTER_PREFIX):
        return Problem(
            code="PORTAL_ADAPTER_UNKNOWN",
            path=path,
            message=(
                f"portal '{portal.key}' adapter '{adapter}' is neither "
                "'generic_form' nor 'python:<module>:<Class>'"
            ),
            blocking=True,
        )
    # "python:<module>:<Class>", with an optional space after "python:".
    module = adapter[len(_PYTHON_ADAPTER_PREFIX) :].strip().split(":", 1)[0].strip()
    if not module.startswith(PORTAL_ADAPTER_PACKAGE):
        return Problem(
            code="PORTAL_ADAPTER_OUTSIDE_PACKAGE",
            path=path,
            message=(
                f"portal '{portal.key}' adapter module '{module}' is outside "
                f"{PORTAL_ADAPTER_PACKAGE}* (R-5.8)"
            ),
            blocking=True,
        )
    return None


def _llm_fallback_problems(cfg: Config) -> list[Problem]:
    # D43: work invoices never go to DeepSeek.
    if cfg.llm_fallback.provider == "deepseek" and cfg.llm_fallback.flows.get("work_microsoft", False):
        return [
            Problem(
                code="LLM_WORK_FLOW_DEEPSEEK",
                path="llm_fallback.flows.work_microsoft",
                message="work_microsoft must never use the DeepSeek LLM fallback (D43)",
                blocking=True,
            )
        ]
    return []
