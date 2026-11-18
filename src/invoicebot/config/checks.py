"""Config consistency rules run by ``scripts/check_config.py`` (R-11.6).

Each rule returns a :class:`Problem`. A blocking problem fails CI; the others
are only reported, so ``main`` never freezes a deploy (R-11.6).
"""

from __future__ import annotations

import string
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import combinations

from invoicebot.config.models import (
    Config,
    ExcelTemplateSink,
    GoogleDriveSink,
    GoogleSheetsSink,
    MailerSink,
    OneDriveFolderSink,
    Portal,
    Sink,
    Store,
    StoreDirectory,
)
from invoicebot.rules import RULES
from invoicebot.sunat_origins import SUNAT_SOL

PORTAL_ADAPTER_PACKAGE = "invoicebot.fetchers.portals."
MANUAL_PORTAL = "manual"
_PYTHON_ADAPTER_PREFIX = "python:"
_GENERIC_FORM_ADAPTER = "generic_form"
_URL_TEMPLATE_MARKER = "{"

#: The names a flow B template may read (BR-D02); ``profile`` stands for ``profile.attr``.
_FLOW_TEMPLATE_NAMES = frozenset(
    {
        "fecha",
        "seq",
        "comercio",
        "serie",
        "numero",
        "ruc_emisor",
        "razon_social",
        "base",
        "igv",
        "service_charge",
        "total",
        "motivo",
        "descripcion",
        "profile",
    }
)
_DESCRIPTION_FORBIDDEN_NAMES = frozenset({"motivo", "descripcion"})
_DESCRIPTION_TEMPLATE_NAMES = _FLOW_TEMPLATE_NAMES - _DESCRIPTION_FORBIDDEN_NAMES
_FORMATTER = string.Formatter()

_FLOW_TEMPLATE_CODE = "FLOW_TEMPLATE_UNKNOWN_NAME"
_FLOW_DESCRIPTION_CODE = "FLOW_DESCRIPTION_TEMPLATE"
_FLOW_FOLDER_CODE = "FLOW_FOLDER_TEMPLATE"
_FLOW_ATTACH_CODE = "FLOW_MAIL_ATTACH"
_FLOW_LABEL_CODE = "FLOW_LABEL_LENGTH"
_STORE_COMERCIO_CODE = "STORE_COMERCIO_LENGTH"
_SOL_DISABLED_CODE = "SOL_DISABLED"
_SHEETS_LINK_WITHOUT_DRIVE_CODE = "SHEETS_LINK_WITHOUT_DRIVE"


@dataclass(frozen=True)
class Problem:
    """One failed check, printed as a single line by the CLI."""

    code: str
    path: str
    message: str
    blocking: bool


def run_checks(cfg: Config, directory: StoreDirectory, *, changed_portals: frozenset[str]) -> list[Problem]:
    """Return every problem found in ``cfg`` and ``directory`` (R-11.6, R-5.8, D43).

    Portal problems point at ``portals/<key>.yaml:<field>`` and store problems
    at ``stores/<ruc>.yaml:<field>``. A blocking problem fails CI.
    ``changed_portals`` holds the keys of the portal files a pull request adds or
    changes; only those block while todo/broken. On ``main`` it is empty.
    """
    problems: list[Problem] = []
    for key, portal in directory.portals.items():
        problems.extend(_portal_problems(key, portal, changed=key in changed_portals))
    for ruc, store in directory.stores.items():
        problems.extend(_store_problems(ruc, store, directory.portals))
    problems.extend(_sol_problems(cfg, directory))
    problems.extend(_llm_fallback_problems(cfg))
    problems.extend(_routing_problems(cfg))
    return problems


def flow_problems(
    cfg: Config, *, field_names: Callable[[str], frozenset[str]], drive_links: frozenset[str]
) -> list[Problem]:
    """Return every problem in a flow's sink templates (BR-D02, R-7.1, D144).

    ``field_names`` is the safe template parser (``invoicebot.sinks.expr.field_names``) and
    ``drive_links`` the link names a Drive sink writes (``invoicebot.sinks.drive_files``),
    both passed in because ``config`` may not import the sinks. Each problem's path names
    the flow, the sink index and the field, such as ``flows.work.sinks[0].folder``.
    """
    problems: list[Problem] = []
    for flow_name, flow in cfg.flows.items():
        has_drive = any(isinstance(sink, GoogleDriveSink) for sink in flow.sinks)
        missing_links = frozenset() if has_drive else drive_links
        for index, sink in enumerate(flow.sinks):
            problems.extend(_sink_problems(flow_name, index, sink, field_names, missing_links=missing_links))
        problems.extend(_mail_attach_problems(flow_name, flow.sinks))
    return problems


def label_problems(cfg: Config, directory: StoreDirectory, *, max_chars: int) -> list[Problem]:
    """Return every flow label or store comercio that cannot be a button title (BR-R03, BR-R09).

    Flow labels and store comercios become WhatsApp button titles, so each is 1 to
    ``max_chars`` characters. The cap is passed in because ``config`` may not import
    the channels. Each problem points at its flow or store field, such as
    ``flows.work.label`` or ``stores/<ruc>.yaml:brands[0].comercio``.
    """
    problems: list[Problem] = []
    for flow_name, flow in cfg.flows.items():
        problem = _label_problem(
            flow.label,
            path=f"flows.{flow_name}.label",
            code=_FLOW_LABEL_CODE,
            subject=f"flow '{flow_name}' label",
            max_chars=max_chars,
        )
        if problem is not None:
            problems.append(problem)
    for ruc, store in directory.stores.items():
        for index, brand in enumerate(store.brands):
            problem = _label_problem(
                brand.comercio,
                path=f"stores/{ruc}.yaml:brands[{index}].comercio",
                code=_STORE_COMERCIO_CODE,
                subject=f"stores/{ruc}.yaml comercio '{brand.comercio}'",
                max_chars=max_chars,
            )
            if problem is not None:
                problems.append(problem)
    return problems


def _label_problem(value: str, *, path: str, code: str, subject: str, max_chars: int) -> Problem | None:
    """BR-R03, BR-R09: a button title is 1 to ``max_chars`` characters."""
    if 1 <= len(value) <= max_chars:
        return None
    return Problem(
        code=code,
        path=path,
        message=(
            f"{subject} is {len(value)} characters; it becomes a WhatsApp button title, "
            f"which must be 1-{max_chars} (BR-R03, BR-R09)"
        ),
        blocking=True,
    )


def _sink_problems(
    flow_name: str,
    index: int,
    sink: Sink,
    field_names: Callable[[str], frozenset[str]],
    *,
    missing_links: frozenset[str],
) -> list[Problem]:
    if isinstance(sink, OneDriveFolderSink):
        return _folder_sink_problems(flow_name, index, sink, field_names)
    if isinstance(sink, ExcelTemplateSink):
        return _excel_sink_problems(flow_name, index, sink, field_names)
    if isinstance(sink, MailerSink):
        return _mailer_sink_problems(flow_name, index, sink, field_names)
    if isinstance(sink, GoogleSheetsSink):
        return _sheets_sink_problems(flow_name, index, sink, field_names, missing_links=missing_links)
    return []


def _sheets_sink_problems(
    flow_name: str,
    index: int,
    sink: GoogleSheetsSink,
    field_names: Callable[[str], frozenset[str]],
    *,
    missing_links: frozenset[str],
) -> list[Problem]:
    """D144: a Sheets column names a Drive link only when the flow has a Drive sink.

    ``missing_links`` holds the Drive link names this flow cannot fill: all of them
    without a Drive sink, none with one.
    """
    problems: list[Problem] = []
    for header, template in sink.columns.items():
        try:
            names = field_names(template)
        except ValueError:
            # shortcut: a malformed column template is not this check's concern (D144).
            continue
        links = names & missing_links
        if links:
            problems.append(
                _problem(
                    flow_name,
                    index,
                    f"columns.{header}",
                    code=_SHEETS_LINK_WITHOUT_DRIVE_CODE,
                    reason=(f"uses {', '.join(sorted(links))} but the flow has no google_drive sink (D144)"),
                )
            )
    return problems


def _folder_sink_problems(
    flow_name: str, index: int, sink: OneDriveFolderSink, field_names: Callable[[str], frozenset[str]]
) -> list[Problem]:
    """BR-D02, R-7.1: the folder, file names and folder layout of an OneDrive sink."""
    problems: list[Problem] = []
    folder_failure = _template_failure(sink.folder, _FLOW_TEMPLATE_NAMES, field_names)
    if folder_failure is not None:
        problems.append(_problem(flow_name, index, "folder", code=_FLOW_TEMPLATE_CODE, reason=folder_failure))
    for field, template in (("files.pdf", sink.files.pdf), ("files.xml", sink.files.xml)):
        failure = _template_failure(template, _FLOW_TEMPLATE_NAMES, field_names)
        if failure is not None:
            problems.append(_problem(flow_name, index, field, code=_FLOW_TEMPLATE_CODE, reason=failure))
    # A malformed folder already has its name problem above; parsing it again is pointless.
    if folder_failure is None:
        folder_problem = _folder_layout_problem(flow_name, index, sink.folder)
        if folder_problem is not None:
            problems.append(folder_problem)
    return problems


def _folder_layout_problem(flow_name: str, index: int, template: str) -> Problem | None:
    """R-7.1: the folder name carries the year and month and the claimed sequence."""
    try:
        parsed = list(_FORMATTER.parse(template))
    except ValueError:
        return None  # the name check already reported the malformed template
    names = {name for _, name, _, _ in parsed if name is not None}
    date_specs = [spec or "" for _, name, spec, _ in parsed if name == "fecha"]
    has_year = any("%Y" in spec for spec in date_specs)
    has_month = any("%m" in spec for spec in date_specs)
    if "seq" in names and has_year and has_month:
        return None
    return _problem(
        flow_name,
        index,
        "folder",
        code=_FLOW_FOLDER_CODE,
        reason="the folder template must use {seq} and {fecha} with %Y and %m (R-7.1)",
    )


def _excel_sink_problems(
    flow_name: str, index: int, sink: ExcelTemplateSink, field_names: Callable[[str], frozenset[str]]
) -> list[Problem]:
    return [
        *_cell_problems(flow_name, index, sink, field_names),
        *_default_problems(flow_name, index, sink, field_names),
    ]


def _cell_problems(
    flow_name: str, index: int, sink: ExcelTemplateSink, field_names: Callable[[str], frozenset[str]]
) -> list[Problem]:
    """BR-D02: every ``cells`` value only reads a known name."""
    problems: list[Problem] = []
    for cell, template in sink.cells.items():
        failure = _template_failure(template, _FLOW_TEMPLATE_NAMES, field_names)
        if failure is not None:
            problems.append(
                _problem(flow_name, index, f"cells.{cell}", code=_FLOW_TEMPLATE_CODE, reason=failure)
            )
    return problems


def _default_problems(
    flow_name: str, index: int, sink: ExcelTemplateSink, field_names: Callable[[str], frozenset[str]]
) -> list[Problem]:
    """BR-D02: the defaults are safe, and ``descripcion`` cannot read the overrides."""
    problems: list[Problem] = []
    motivo_failure = _template_failure(sink.defaults.motivo, _FLOW_TEMPLATE_NAMES, field_names)
    if motivo_failure is not None:
        problems.append(
            _problem(flow_name, index, "defaults.motivo", code=_FLOW_TEMPLATE_CODE, reason=motivo_failure)
        )
    descripcion_failure = _template_failure(
        sink.defaults.descripcion, _DESCRIPTION_TEMPLATE_NAMES, field_names
    )
    if descripcion_failure is not None:
        problems.append(
            _problem(
                flow_name,
                index,
                "defaults.descripcion",
                code=_FLOW_DESCRIPTION_CODE,
                reason=(
                    f"{descripcion_failure}; defaults.descripcion may not use "
                    "{motivo} or {descripcion} (BR-D02)"
                ),
            )
        )
    return problems


def _mailer_sink_problems(
    flow_name: str, index: int, sink: MailerSink, field_names: Callable[[str], frozenset[str]]
) -> list[Problem]:
    """BR-D02: the mail subject and body only read a known name."""
    problems: list[Problem] = []
    for field, template in (("subject", sink.subject), ("body", sink.body)):
        failure = _template_failure(template, _FLOW_TEMPLATE_NAMES, field_names)
        if failure is not None:
            problems.append(_problem(flow_name, index, field, code=_FLOW_TEMPLATE_CODE, reason=failure))
    return problems


def _mail_attach_problems(flow_name: str, sinks: Sequence[Sink]) -> list[Problem]:
    """BR-D02: a mailer may attach the Excel only when the flow builds one."""
    if any(isinstance(sink, ExcelTemplateSink) for sink in sinks):
        return []
    return [
        _problem(
            flow_name,
            index,
            "attach",
            code=_FLOW_ATTACH_CODE,
            reason="attach names 'excel_template' but the flow has no excel_template sink (BR-D02)",
        )
        for index, sink in enumerate(sinks)
        if isinstance(sink, MailerSink) and "excel_template" in sink.attach
    ]


def _template_failure(
    template: str, allowed: frozenset[str], field_names: Callable[[str], frozenset[str]]
) -> str | None:
    """Return why ``template`` breaks BR-D02, or ``None`` when its names are safe."""
    try:
        names = field_names(template)
    except ValueError as exc:
        return f"template is not valid: {exc}"
    unknown = names - allowed
    if not unknown:
        return None
    return f"unknown name(s): {', '.join(sorted(unknown))} (BR-D02)"


def _problem(flow_name: str, index: int, field: str, *, code: str, reason: str) -> Problem:
    return Problem(
        code=code,
        path=f"flows.{flow_name}.sinks[{index}].{field}",
        message=f"flow '{flow_name}' sink {index} {field}: {reason}",
        blocking=True,
    )


def _portal_problems(
    key: str,
    portal: Portal,
    *,
    changed: bool,
    field_prefix: str | None = None,
) -> list[Problem]:
    """R-5.8: one portal's status, adapter, URL and form completeness.

    ``field_prefix`` is where the portal's fields live, ending in its separator,
    such as ``portals/<key>.yaml:`` or the SOL site's ``sunat_sol.yaml:query.``.
    """
    prefix = field_prefix or f"portals/{key}.yaml:"
    problems: list[Problem] = []
    if portal.status != "active":
        problems.append(
            Problem(
                code="PORTAL_NOT_ACTIVE",
                path=f"{prefix}status",
                message=f"portal '{portal.key}' has status '{portal.status}'; only active portals are used",
                blocking=changed,
            )
        )
    adapter_problem = _adapter_problem(portal, field_prefix=prefix)
    if adapter_problem is not None:
        problems.append(adapter_problem)
    template_problem = _url_template_problem(portal, field_prefix=prefix)
    if template_problem is not None:
        problems.append(template_problem)
    incomplete_problem = _form_incomplete_problem(portal, path=prefix[:-1])
    if incomplete_problem is not None:
        problems.append(incomplete_problem)
    return problems


def _sol_problems(cfg: Config, directory: StoreDirectory) -> list[Problem]:
    """R-5.8: the SOL site's query passes the portal checks, and a skipped chain step warns."""
    problems: list[Problem] = []
    site = directory.sunat_sol
    if cfg.sunat.sol.enabled and site is not None and site.status == "active":
        # The site's status governs the query; a portal file's default status would hide its checks.
        query = site.query.model_copy(update={"status": "active"})
        problems.extend(
            _portal_problems(
                SUNAT_SOL,
                query,
                changed=False,
                field_prefix="sunat_sol.yaml:query.",
            )
        )
    if not cfg.sunat.sol.enabled:
        problems.extend(_sol_disabled_problems(cfg))
    return problems


def _sol_disabled_problems(cfg: Config) -> list[Problem]:
    """R-5.8: a flow listing ``sunat_sol`` while SOL is off runs a chain that skips it."""
    return [
        Problem(
            code=_SOL_DISABLED_CODE,
            path=f"flows.{flow_name}.fetchers",
            message=(
                f"flow '{flow_name}' lists the '{SUNAT_SOL}' fetcher, "
                "but sunat.sol.enabled is false, so the chain skips it (R-5.8)"
            ),
            blocking=False,
        )
        for flow_name, flow in cfg.flows.items()
        if SUNAT_SOL in flow.fetchers
    ]


def _url_template_problem(portal: Portal, *, field_prefix: str) -> Problem | None:
    """R-5.8: an active portal must carry a real URL, not a template the fetcher cannot fill."""
    # shortcut: URL templates such as {ruc_emisor} come with the first portal that needs one.
    if portal.status != "active" or portal.url is None or _URL_TEMPLATE_MARKER not in portal.url:
        return None
    return Problem(
        code="PORTAL_URL_TEMPLATE",
        path=f"{field_prefix}url",
        message=(
            f"portal '{portal.key}' is active but its url is a template; "
            "URL templates are not supported yet (R-5.8)"
        ),
        blocking=True,
    )


def _adapter_problem(portal: Portal, *, field_prefix: str) -> Problem | None:
    path = f"{field_prefix}adapter"
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


def _form_incomplete_problem(portal: Portal, *, path: str) -> Problem | None:
    """R-5.8: an active generic_form portal must carry every form field it reads."""
    if portal.adapter != _GENERIC_FORM_ADAPTER or portal.status != "active":
        return None
    missing = _missing_form_fields(portal)
    if not missing:
        return None
    return Problem(
        code="PORTAL_FORM_INCOMPLETE",
        path=path,
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
