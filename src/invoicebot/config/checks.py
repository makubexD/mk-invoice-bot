"""Config consistency rules run by ``scripts/check_config.py`` (R-11.6).

Each rule returns a :class:`Problem`. A blocking problem fails CI; the others
are only reported, so ``main`` never freezes a deploy (R-11.6).
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from invoicebot.config.models import Config, Portal, Store, StoreDirectory
from invoicebot.rules import RULES

PORTAL_ADAPTER_PACKAGE = "invoicebot.fetchers.portals."
MANUAL_PORTAL = "manual"
_PYTHON_ADAPTER_PREFIX = "python:"
_GENERIC_FORM_ADAPTER = "generic_form"
_URL_TEMPLATE_MARKER = "{"


@dataclass(frozen=True)
class Problem:
    """One failed check, printed as a single line by the CLI."""

    code: str
    path: str
    message: str
    blocking: bool


def run_checks(cfg: Config, directory: StoreDirectory, *, pr_mode: bool) -> list[Problem]:
    """Return every problem found in ``cfg`` and ``directory`` (R-11.6, R-5.8, D43).

    Portal problems point at ``portals/<key>.yaml:<field>`` and store problems
    at ``stores/<ruc>.yaml:<field>``. A blocking problem fails CI.
    """
    problems: list[Problem] = []
    for key, portal in directory.portals.items():
        problems.extend(_portal_problems(key, portal, pr_mode=pr_mode))
    for ruc, store in directory.stores.items():
        problems.extend(_store_problems(ruc, store, directory.portals))
    problems.extend(_llm_fallback_problems(cfg))
    problems.extend(_routing_problems(cfg))
    return problems


def _portal_problems(key: str, portal: Portal, *, pr_mode: bool) -> list[Problem]:
    problems: list[Problem] = []
    if portal.status != "active":
        problems.append(
            Problem(
                code="PORTAL_NOT_ACTIVE",
                path=f"portals/{key}.yaml:status",
                message=f"portal '{portal.key}' has status '{portal.status}'; only active portals are used",
                blocking=pr_mode,
            )
        )
    adapter_problem = _adapter_problem(key, portal)
    if adapter_problem is not None:
        problems.append(adapter_problem)
    template_problem = _url_template_problem(key, portal)
    if template_problem is not None:
        problems.append(template_problem)
    incomplete_problem = _form_incomplete_problem(key, portal)
    if incomplete_problem is not None:
        problems.append(incomplete_problem)
    return problems


def _url_template_problem(key: str, portal: Portal) -> Problem | None:
    """R-5.8: an active portal must carry a real URL, not a template the fetcher cannot fill."""
    # shortcut: URL templates such as {ruc_emisor} come with the first portal that needs one.
    if portal.status != "active" or portal.url is None or _URL_TEMPLATE_MARKER not in portal.url:
        return None
    return Problem(
        code="PORTAL_URL_TEMPLATE",
        path=f"portals/{key}.yaml:url",
        message=(
            f"portal '{portal.key}' is active but its url is a template; "
            "URL templates are not supported yet (R-5.8)"
        ),
        blocking=True,
    )


def _adapter_problem(key: str, portal: Portal) -> Problem | None:
    path = f"portals/{key}.yaml:adapter"
    adapter = portal.adapter
    if adapter == _GENERIC_FORM_ADAPTER:
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


def _form_incomplete_problem(key: str, portal: Portal) -> Problem | None:
    """R-5.8: an active generic_form portal must carry every form field it reads."""
    if portal.adapter != _GENERIC_FORM_ADAPTER or portal.status != "active":
        return None
    missing = _missing_form_fields(portal)
    if not missing:
        return None
    return Problem(
        code="PORTAL_FORM_INCOMPLETE",
        path=f"portals/{key}.yaml",
        message=f"portal '{portal.key}' is an active generic_form portal but lacks {', '.join(missing)}",
        blocking=True,
    )


def _missing_form_fields(portal: Portal) -> list[str]:
    return [
        name
        for name, value in (
            ("url", portal.url),
            ("submit", portal.submit),
            ("downloads", portal.downloads),
            ("not_found", portal.not_found),
        )
        if value is None
    ]


def _store_problems(ruc: str, store: Store, portals: dict[str, Portal]) -> list[Problem]:
    problems: list[Problem] = []
    if store.portal not in (None, MANUAL_PORTAL) and store.portal not in portals:
        problems.append(
            Problem(
                code="STORE_PORTAL_UNKNOWN",
                path=f"stores/{ruc}.yaml:portal",
                message=(
                    f"store {ruc} names portal '{store.portal}', "
                    f"but there is no portals/{store.portal}.yaml file (R-5.8)"
                ),
                blocking=True,
            )
        )
    problems.extend(_serie_prefix_problems(ruc, store))
    return problems


def _serie_prefix_problems(ruc: str, store: Store) -> list[Problem]:
    """R-5.8: series match by prefix, so prefixes of different brands may not overlap."""
    problems: list[Problem] = []
    for first, second in combinations(store.brands, 2):
        for first_prefix in first.serie_prefix:
            for second_prefix in second.serie_prefix:
                if _prefixes_overlap(first_prefix, second_prefix):
                    problems.append(
                        Problem(
                            code="STORE_SERIE_PREFIX_OVERLAP",
                            path=f"stores/{ruc}.yaml:serie_prefix",
                            message=(
                                f"store {ruc}: serie prefix '{first_prefix}' ({first.comercio}) "
                                f"overlaps '{second_prefix}' ({second.comercio})"
                            ),
                            blocking=True,
                        )
                    )
    return problems


def _prefixes_overlap(first: str, second: str) -> bool:
    return first.startswith(second) or second.startswith(first)


def _routing_problems(cfg: Config) -> list[Problem]:
    # R-11.1: the invoice records the id of the rule that routed it, so every
    # routing rule must have its row in the business-rule catalog (spec 06).
    problems: list[Problem] = []
    for index, rule in enumerate(cfg.routing.rules):
        if rule.id not in RULES:
            problems.append(
                Problem(
                    code="ROUTING_RULE_UNKNOWN",
                    path=f"routing.rules[{index}].id",
                    message=(
                        f"routing rule id '{rule.id}' is not in the business-rule catalog; "
                        "add its row to specs/06-business-rules.md first (R-11.1)"
                    ),
                    blocking=True,
                )
            )
    return problems


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
