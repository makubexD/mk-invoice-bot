"""Typed pydantic models for the runtime configuration (R-9.6).

Every model forbids unknown keys so a typo is caught at load time rather than
silently ignored.
"""

from __future__ import annotations

import re
from datetime import time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    PlainSerializer,
    PositiveInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from invoicebot.sunat_origins import is_sunat_origin

#: D143: the owner's DNI, eight digits, used by BR-R10 to route boletas made out to it.
DNI_PATTERN = re.compile(r"^[0-9]{8}$")


class Owner(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ruc: str
    profile: str
    timezone: str
    language: str
    #: D143: optional; when set, a boleta made out to this DNI goes to the personal flow.
    dni: str | None = None

    @field_validator("dni")
    @classmethod
    def _dni_is_eight_digits(cls, value: str | None) -> str | None:
        """D143: the owner's DNI is optional, and when set it is exactly eight digits."""
        if value is not None and DNI_PATTERN.fullmatch(value) is None:
            raise ValueError("owner.dni must be 8 digits")
        return value


class Work(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ruc: str


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    legal_name: str
    whatsapp: str | None = None
    telegram_user_id: int | None = None
    preferred_channel: Literal["whatsapp", "telegram"] | None = None
    flows: list[str]
    admin: bool = False


class ChannelTemplates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    completion: str
    manual_required: str
    resume_notice: str
    pending_user_reminder: str
    batch_preview: str
    admin_alert: str


class WhatsAppChannel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["whatsapp_cloud"]
    phone_number_id: str
    graph_api_version: str
    templates: ChannelTemplates
    template_language: str = Field(default="es_PE", pattern=r"^[a-z]{2}(_[A-Z]{2})?$")


class TelegramChannel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["telegram"]
    enabled: bool


type Channel = Annotated[WhatsAppChannel | TelegramChannel, Field(discriminator="kind")]


class ValidezApi(BaseModel):
    """The SUNAT validez API: the endpoints to call and how long to wait (R-4.1)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    token_url: str = Field(
        default="https://api-seguridad.sunat.gob.pe/v1/clientesextranet/{client_id}/oauth2/token/",
        pattern=r"^https://",
    )
    api_url: str = Field(
        default="https://api.sunat.gob.pe/v1/contribuyente/contribuyentes/{ruc}/validarcomprobante",
        pattern=r"^https://",
    )
    timeout_s: float = Field(10, gt=0)

    @model_validator(mode="after")
    def _urls_carry_their_placeholders(self) -> Self:
        """R-4.1: a URL that lost its placeholder cannot name the client or the RUC."""
        if "{client_id}" not in self.token_url:
            raise ValueError(
                f"sunat.validez_api.token_url must contain '{{client_id}}', got {self.token_url!r}"
            )
        if "{ruc}" not in self.api_url:
            raise ValueError(f"sunat.validez_api.api_url must contain '{{ruc}}', got {self.api_url!r}")
        return self


class Sol(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


def _parse_refresh_cron(value: str) -> tuple[int, int, int]:
    """S12.4: parse ``M H * * D`` into ``(minute, hour, cron weekday)``.

    The refresh needs one fixed time on one weekday, so only plain integers are
    accepted; ranges, steps and lists are refused at load.
    """
    message = (
        "sunat.padron.refresh_cron must be 'M H * * D' with minute 0-59, "
        f"hour 0-23 and weekday 0-6 (Sunday 0), got {value!r}"
    )
    parts = value.split()
    if len(parts) != 5 or parts[2] != "*" or parts[3] != "*":
        raise ValueError(message)
    minute_text, hour_text, weekday_text = parts[0], parts[1], parts[4]
    if not all(re.fullmatch(r"[0-9]+", part) for part in (minute_text, hour_text, weekday_text)):
        raise ValueError(message)
    minute, hour, weekday = int(minute_text), int(hour_text), int(weekday_text)
    if not (0 <= minute <= 59 and 0 <= hour <= 23 and 0 <= weekday <= 6):
        raise ValueError(message)
    return minute, hour, weekday


class Padron(BaseModel):
    """S12.4: where the padrón ZIP comes from and when to refresh it."""

    model_config = ConfigDict(extra="forbid")

    refresh_cron: str
    url: HttpUrl = HttpUrl("https://www2.sunat.gob.pe/padron_reducido_ruc.zip")
    max_mb: int = Field(600, gt=0)

    @field_validator("refresh_cron")
    @classmethod
    def _is_a_weekly_time(cls, value: str) -> str:
        """S12.4: the refresh runs once a week at one fixed time, so only ``M H * * D`` loads."""
        _parse_refresh_cron(value)
        return value

    @property
    def at(self) -> time:
        """S12.4: the time of day the weekly refresh runs."""
        minute, hour, _ = _parse_refresh_cron(self.refresh_cron)
        return time(hour, minute)

    @property
    def weekday(self) -> int:
        """S12.4: the refresh weekday in Python's Monday-0 numbering (cron 0 is Sunday 6)."""
        _, _, cron_weekday = _parse_refresh_cron(self.refresh_cron)
        return (cron_weekday + 6) % 7


class Sunat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    validez_api: ValidezApi
    sol: Sol
    padron: Padron


class Ocr(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The helper always runs Tesseract, so any other engine is refused at load. A pattern
    # instead of ``Literal`` keeps the helper's string argument typed as ``str`` (strict mypy).
    engine: str = Field(pattern=r"^tesseract$")
    # Tesseract language codes: one or more codes joined with ``+``. The pattern refuses a
    # path (``../x``) and a leading dash (``-psm``) that would reach the argument list.
    lang: str = Field(pattern=r"^[A-Za-z_]+(\+[A-Za-z_]+)*$")
    psm: int = Field(ge=0, le=13)
    timeout_s: int = Field(gt=0)


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_image_px: int = Field(gt=0)
    ocr: Ocr
    review_threshold: float = Field(ge=0, le=1)
    max_invoice_age_days: int = Field(gt=0)
    # Written as strings in YAML so no float rounding reaches Decimal (R-2.4).
    igv_rates: list[Decimal] = Field(min_length=1)
    supported_tipos: list[str] = Field(min_length=1)
    helper_timeout_s: float = Field(gt=0)
    #: R-2.15: the non-space characters a page-1 text layer needs before it is read as text.
    pdf_min_text_chars: int = Field(40, ge=0)

    @model_validator(mode="after")
    def _ocr_finishes_before_helper_timeout(self) -> Self:
        """D81: OCR must finish before the helper timeout kills the whole helper."""
        if self.ocr.timeout_s >= self.helper_timeout_s:
            raise ValueError(
                f"extraction.ocr.timeout_s ({self.ocr.timeout_s}) must be less than "
                f"extraction.helper_timeout_s ({self.helper_timeout_s})"
            )
        return self


class LlmFallback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    max_image_px: int = Field(gt=0)
    monthly_cap_usd: float
    flows: dict[str, bool]


#: The reading fields a routing rule may match on (R-3.1); ``route`` compares each
#: ``when`` key against ``Reading.fields``.
type RoutingField = Literal["num_doc_adq", "tipo_doc_adq", "ruc_emisor", "tipo"]


class RoutingRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    when: dict[RoutingField, list[str]]
    flow: str


class Routing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm_mode: Literal["auto", "always"]
    rules: list[RoutingRule]
    unmatched: Literal["ask", "reject"]


_DELAY = re.compile(r"(\d+)([mhd])")
_MINUTES = {"m": 1, "h": 60, "d": 24 * 60}


def parse_delay(text: str) -> timedelta:
    """Parse a portal's interval such as ``90m``, ``6h`` or ``2d``."""
    match = _DELAY.fullmatch(text)
    if match is None:
        raise ValueError(f"invalid delay: {text!r}")
    amount, unit = match.groups()
    return timedelta(minutes=int(amount) * _MINUTES[unit])


def format_delay(delay: timedelta) -> str:
    """Write ``delay`` back in ``parse_delay``'s form, in the largest whole unit."""
    minutes = int(delay.total_seconds()) // 60
    for unit in ("d", "h"):
        if minutes % _MINUTES[unit] == 0:
            return f"{minutes // _MINUTES[unit]}{unit}"
    return f"{minutes}m"


def _delay(value: object) -> timedelta:
    """Refuse a non-string entry so pydantic reports a ``ValidationError``, then parse it."""
    if not isinstance(value, str):
        raise ValueError(f"delay must be a string such as '1h', got {value!r}")
    return parse_delay(value)


#: D65: one retry delay, written as ``<number><m|h|d>`` and dumped back as that text.
Delay = Annotated[timedelta, BeforeValidator(_delay), PlainSerializer(format_delay)]

_EMAIL_ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+$")

#: D138: an account name is a key, so it is lowercase letters, digits and underscores.
_ACCOUNT_NAME = re.compile(r"^[a-z0-9_]+$")


class Mail(BaseModel):
    """Flow mail switches; real sending only in production (R-7.7, D89)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    send_enabled: bool = False
    #: R-7.7: the owner's work mailbox the inbox_rule mode sends the mail to.
    owner_work_address: str | None = None
    #: R-7.8: how long a mail waits for the owner to confirm the receipt.
    confirm_timeout: Delay = timedelta(hours=24)
    #: R-7.8: how long a laptop job may stay unfinished before it is stale.
    agent_stale_after: Delay = timedelta(hours=4)

    @field_validator("owner_work_address")
    @classmethod
    def _owner_address_is_an_address(cls, value: str | None) -> str | None:
        """R-7.7: the owner's work mailbox is one plain address."""
        if value is not None and _EMAIL_ADDRESS.fullmatch(value) is None:
            raise ValueError(f"owner_work_address {value!r} is not an email address")
        return value


class Microsoft(BaseModel):
    """The Entra app the bot signs in with for Graph (R-7.9, D84)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # A public client's app id, so it is config and not a secret (D84).
    client_id: str = Field(
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )
    timeout_s: float = Field(gt=0)


class Google(BaseModel):
    """The OAuth client the bot signs in with for Drive and Gmail (R-6.5, D128, D129)."""

    model_config = ConfigDict(extra="forbid")

    # A desktop app's public client id, so it is config and not a secret (D128).
    client_id: str
    timeout_s: PositiveInt = 30
    #: D130: the most rows the sheets sink reads to find the invoice's key.
    max_rows: PositiveInt = 20000
    #: R-5.2: the most bytes one full-message read may stream before it is refused.
    max_message_bytes: PositiveInt = 8_388_608
    #: D138: the mailbox each account name is pinned to, checked by `auth-google`.
    accounts: dict[str, str] = Field(default_factory=dict)

    @field_validator("accounts")
    @classmethod
    def _accounts_map_names_to_addresses(cls, value: dict[str, str]) -> dict[str, str]:
        """R-6.5, D138: an account name is a key and its mailbox one lower-cased address."""
        addresses: dict[str, str] = {}
        for name, address in value.items():
            if _ACCOUNT_NAME.fullmatch(name) is None:
                raise ValueError(f"account name {name!r} is not lowercase letters, digits or '_'")
            address = address.lower()
            if _EMAIL_ADDRESS.fullmatch(address) is None:
                raise ValueError(f"account '{name}' is not an email address")
            addresses[name] = address
        return addresses


#: An id Google may return; anything else is refused before it is used.
GOOGLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{10,200}$")

#: The Drive id that names the user's own drive root rather than a folder.
GOOGLE_DRIVE_ROOT_ID = "root"


class GoogleDriveSink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["google_drive"]
    account: str = Field(pattern=r"^[a-z0-9_]+$")
    root_folder_id: str
    path: str
    filename: str
    upload: list[str]

    @field_validator("root_folder_id")
    @classmethod
    def _root_folder_id_is_a_google_id(cls, value: str) -> str:
        """R-6.1: a Drive folder id is one Google id, or the literal drive root."""
        if value != GOOGLE_DRIVE_ROOT_ID and GOOGLE_ID_PATTERN.fullmatch(value) is None:
            raise ValueError(f"root_folder_id {value!r} is not a Google Drive id")
        return value


#: D130: the sheets column that always holds the invoice's natural key.
SHEETS_KEY_COLUMN = "Clave"
#: The longest tab title Google Sheets accepts (R-6.2).
SHEETS_TAB_MAX_CHARS = 100


class GoogleSheetsSink(BaseModel):
    """A flow's Google Sheets sink: the row it appends (R-6.3, D130).

    With ``existing`` true, ``spreadsheet_id`` is the owner's own sheet, whose
    account needs the broader spreadsheets scope (R-6.5, D145).
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["google_sheets"]
    account: str = Field(pattern=r"^[a-z0-9_]+$")
    spreadsheet_id: str
    #: R-6.2: Sheets refuses an empty title, one over 100 characters, or one with ``[]*?:/\``.
    tab: str = Field(min_length=1, max_length=SHEETS_TAB_MAX_CHARS, pattern=r"^[^\[\]*?:/\\]+$")
    columns: dict[str, str] = Field(min_length=1)
    #: D145: true when the sheet is the owner's, not one ``google-setup`` creates.
    existing: bool = False

    @field_validator("spreadsheet_id")
    @classmethod
    def _spreadsheet_id_is_a_google_id(cls, value: str) -> str:
        """R-6.2: a spreadsheet id is one Google id."""
        if GOOGLE_ID_PATTERN.fullmatch(value) is None:
            raise ValueError(f"spreadsheet_id {value!r} is not a Google id")
        return value

    @field_validator("columns")
    @classmethod
    def _columns_skip_the_key_header(cls, value: dict[str, str]) -> dict[str, str]:
        """R-6.3, D130: column A is always the key, so config must not add it again."""
        if SHEETS_KEY_COLUMN in value:
            raise ValueError(f"columns must not contain the header {SHEETS_KEY_COLUMN!r}")
        return value


class SinkFiles(BaseModel):
    """File-name templates for the fetched documents in an invoice folder (R-7.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pdf: str
    xml: str


class OneDriveFolderSink(BaseModel):
    """One folder per invoice on the configured storage targets (R-7.1, R-7.2, R-7.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["onedrive_folder"]
    targets: list[Literal["onedrive_personal"]] = Field(min_length=1)
    root: str = Field(pattern=r"^/")
    folder: str
    files: SinkFiles
    manifest: str = ".invoicebot.json"


class ExcelDefaults(BaseModel):
    """Default reason and description when the sender gives none (R-7.4, D70)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    motivo: str
    descripcion: str


_CELL_REFERENCE = re.compile(r"^[A-Z]{1,3}[1-9][0-9]{0,6}$")


class ExcelTemplateSink(BaseModel):
    """The pinned Excel template and the cells patched into it (R-7.4, R-7.10, D71, D86)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["excel_template"]
    template: Path
    template_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    allow_macros: bool = False
    output: str
    sheet: str
    cells: dict[str, str]
    defaults: ExcelDefaults

    @field_validator("cells")
    @classmethod
    def _cell_keys_are_references(cls, value: dict[str, str]) -> dict[str, str]:
        """R-7.10: every key is an A1-style cell reference, so a bad mapping fails at load."""
        for key in value:
            if _CELL_REFERENCE.fullmatch(key) is None:
                raise ValueError(f"cell key {key!r} is not an A1-style cell reference")
        return value

    @model_validator(mode="after")
    def _xlsm_requires_allow_macros(self) -> Self:
        """R-7.10: a macro-enabled template is only used with an explicit opt-in."""
        if self.template.suffix.lower() == ".xlsm" and not self.allow_macros:
            raise ValueError(f"template {self.template} is macro-enabled; set allow_macros: true")
        return self


type MailMode = Literal["dry_run", "graph", "inbox_rule", "laptop_outlook"]


class MailerSink(BaseModel):
    """The invoice email: trigger, modes in order, account and templates (R-7.5 to R-7.7)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["mailer"]
    trigger: Literal["per_invoice"]
    modes: list[MailMode] = Field(min_length=1)
    account: Literal["personal"]
    #: R-7.7: the inbox_rule mode sends through the bot's own mailbox.
    inbox_rule_account: Literal["bot"] = "bot"
    to: list[str] = Field(min_length=1)
    cc: list[str] = Field(default_factory=list)
    subject: str
    body: str
    attach: list[Literal["excel_template", "pdf", "xml"]]

    @field_validator("modes")
    @classmethod
    def _modes_are_unique(cls, value: list[MailMode]) -> list[MailMode]:
        """R-7.7: each mail mode appears once, so its order is unambiguous."""
        if len(set(value)) != len(value):
            raise ValueError("mail modes must be unique")
        return value

    @field_validator("to")
    @classmethod
    def _recipients_are_addresses(cls, value: list[str]) -> list[str]:
        """R-7.6: a recipient is one plain address, so a mistyped one fails at load."""
        for address in value:
            if _EMAIL_ADDRESS.fullmatch(address) is None:
                raise ValueError(f"recipient {address!r} is not an email address")
        return value


type Sink = Annotated[
    GoogleDriveSink | GoogleSheetsSink | OneDriveFolderSink | ExcelTemplateSink | MailerSink,
    Field(discriminator="kind"),
]


class Reimbursement(BaseModel):
    """A flow's reimbursement settings; ``cap`` drives BR-D06 (R-7.13) and
    ``track_payments`` turns on ``/pagado`` payment tracking (R-7.12)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cap: Decimal = Field(gt=0)
    #: R-7.12: turns on /pagado payment tracking for the flow.
    track_payments: bool = False


class FlowInbox(BaseModel):
    """One flow's invoice mailbox: which provider, which account, how to retry (D98)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["gmail", "graph"]
    account: str = Field(pattern=r"^[a-z0-9_]+$")
    retry_schedule: tuple[Delay, ...]


#: The fetcher name that requires ``Flow.email_inbox`` settings (D98, R-5.2).
EMAIL_INBOX_FETCHER = "email_inbox"


class Flow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    accepted_tipos: list[str]
    fetchers: list[str]
    required_docs: list[str]
    #: R-1.5, D66: the file kinds a sender may upload by hand, in preference order.
    manual_uploads: list[Literal["pdf", "xml"]] = Field(default_factory=list)
    deliver_without_docs: bool
    reimbursement: Reimbursement | None = None
    email_inbox: FlowInbox | None = None
    sinks: list[Sink]

    @model_validator(mode="after")
    def _email_inbox_fetcher_needs_settings(self) -> Self:
        """D98, R-5.2: a flow that lists the email_inbox fetcher configures that inbox."""
        if EMAIL_INBOX_FETCHER in self.fetchers and self.email_inbox is None:
            raise ValueError(f"flow fetchers lists '{EMAIL_INBOX_FETCHER}' without flow.email_inbox settings")
        return self

    @model_validator(mode="after")
    def _google_drive_needs_sheets(self) -> Self:
        """D144: a drive sink renders links the sheets sink holds, so drive needs sheets.

        A sheets sink alone is allowed: the flow writes its row without uploading files.
        """
        kinds = {sink.kind for sink in self.sinks}
        if "google_drive" in kinds and "google_sheets" not in kinds:
            raise ValueError("a flow with a 'google_drive' sink needs a 'google_sheets' sink")
        return self


class PaidSolver(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    provider: str


class Captcha(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local_ocr_attempts: int
    ask_user_timeout_s: int
    paid_solver: PaidSolver


class PaidApi(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    provider: str


#: The worker's job lease; a portal fetch must end before it (R-5.1).
JOB_LEASE_S = 300

#: The margin a portal fetch keeps below the lease, so its result is recorded in time (R-5.1).
JOB_LEASE_MARGIN_S = 30


class Retrieval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Sized for a portal fetch, not for OCR; extraction.helper_timeout_s covers the OCR helper.
    portal_timeout_s: float = Field(gt=0)
    captcha: Captcha
    paid_api: PaidApi

    @model_validator(mode="after")
    def _portal_fetch_fits_in_job_lease(self) -> Self:
        """R-5.1: a portal fetch must end at least the margin before the job lease expires."""
        if self.portal_timeout_s + JOB_LEASE_MARGIN_S > JOB_LEASE_S:
            raise ValueError(
                f"retrieval.portal_timeout_s ({self.portal_timeout_s}) plus the margin "
                f"({JOB_LEASE_MARGIN_S}s) must not exceed the job lease ({JOB_LEASE_S}s)"
            )
        return self


class GapsGithub(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    repo: str
    app_id: int
    installation_id: int
    labels: list[str]
    daily_write_budget: int


class Gaps(BaseModel):
    model_config = ConfigDict(extra="forbid")

    github: GapsGithub
    teach_via_chat: bool
    realert_days: int
    broken_after: int
    digest_kinds: list[
        Literal["PORTAL_MISSING", "PORTAL_BROKEN", "TEMPLATE_INVALID", "PARSER_MISS", "UNROUTED_BUYER"]
    ]
    digest_at: time


class BackupTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    path: str


class Backup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: time
    max_mb: int = Field(200, gt=0)
    age_recipient: str
    target: BackupTarget


class RetentionDays(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages_payload: int
    outbox: int
    events: int
    jobs: int


class OutboxBacklog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_rows: int
    max_age_min: int


class SelfAlerts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outbox_backlog: OutboxBacklog
    disk_pct: int
    realert_hours: int = Field(6, gt=0)


class Ops(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backup: Backup
    retention_days: RetentionDays
    self_alerts: SelfAlerts


class MediaMaxBytes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image: int
    pdf: int
    xml: int
    zip: int


class MediaLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_megapixels: int = Field(gt=0)
    #: R-2.15: a PDF with more pages is refused before any page is rendered.
    max_pdf_pages: int = Field(5, gt=0)
    max_bytes: MediaMaxBytes
    zip_max_entries: int
    zip_max_ratio: int


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_queued_per_sender: int
    question_timeout_h: int
    media: MediaLimits
    heavy_concurrency: int
    browser_idle_close_s: int
    per_domain_min_interval_s: int


#: D93: a profile id is the natural key, so it cannot carry the ``|`` the key encodes.
PROFILE_ID_PATTERN = r"^[a-z0-9_-]+$"


def _lists_mail_mode(flow: Flow, mode: MailMode) -> bool:
    """True when ``flow`` has a mailer sink that lists ``mode``."""
    return any(isinstance(sink, MailerSink) and mode in sink.modes for sink in flow.sinks)


def _has_personal_onedrive_sink(flow: Flow) -> bool:
    """True when ``flow`` has an onedrive_folder sink targeting the personal drive."""
    return any(
        isinstance(sink, OneDriveFolderSink) and "onedrive_personal" in sink.targets for sink in flow.sinks
    )


def _google_accounts_in(flow: Flow) -> list[str]:
    """Return the accounts ``flow``'s Google sinks and gmail inbox name (R-6.5, D138)."""
    accounts = [sink.account for sink in flow.sinks if isinstance(sink, (GoogleDriveSink, GoogleSheetsSink))]
    inbox = flow.email_inbox
    if inbox is not None and inbox.provider == "gmail":
        accounts.append(inbox.account)
    return accounts


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: Owner
    work: Work
    env: Literal["development", "production"]
    mail: Mail
    microsoft: Microsoft
    google: Google | None = None
    profiles: dict[Annotated[str, StringConstraints(pattern=PROFILE_ID_PATTERN)], Profile]
    channels: list[Channel]
    sunat: Sunat
    extraction: Extraction
    llm_fallback: LlmFallback
    routing: Routing
    flows: dict[str, Flow]
    retrieval: Retrieval
    gaps: Gaps
    ops: Ops
    limits: Limits

    @model_validator(mode="after")
    def _accepted_tipos_are_supported(self) -> Self:
        """R-3.1: a flow may only accept tipos the extractor supports."""
        supported = set(self.extraction.supported_tipos)
        for name, flow in self.flows.items():
            unsupported = [tipo for tipo in flow.accepted_tipos if tipo not in supported]
            if unsupported:
                raise ValueError(
                    f"flow '{name}' accepts tipos {unsupported} outside "
                    f"extraction.supported_tipos ({sorted(supported)})"
                )
        return self

    @model_validator(mode="after")
    def _real_mail_needs_production(self) -> Self:
        """R-7.7: sending real mail is only allowed in production."""
        if self.mail.send_enabled and self.env != "production":
            raise ValueError(f"mail.send_enabled requires env: production, got {self.env!r}")
        return self

    @model_validator(mode="after")
    def _routing_rules_name_known_flows(self) -> Self:
        """R-3.1: a routing rule may only send invoices to a configured flow."""
        for index, rule in enumerate(self.routing.rules):
            if rule.flow not in self.flows:
                raise ValueError(f"routing.rules[{index}] ({rule.id}) names unknown flow '{rule.flow}'")
        return self

    @model_validator(mode="after")
    def _inbox_rule_needs_the_owner_address(self) -> Self:
        """R-7.7: a flow that sends through the inbox_rule mode names the owner's work mailbox."""
        for name, flow in self.flows.items():
            if _lists_mail_mode(flow, "inbox_rule") and self.mail.owner_work_address is None:
                raise ValueError(
                    f"flow '{name}' lists mail mode 'inbox_rule' without mail.owner_work_address"
                )
        return self

    @model_validator(mode="after")
    def _google_usage_needs_google_settings(self) -> Self:
        """R-6.5, D129, D138: a Google sink or gmail inbox needs its account in google.accounts."""
        for name, flow in self.flows.items():
            accounts = _google_accounts_in(flow)
            if not accounts:
                continue
            if self.google is None:
                raise ValueError(f"flow '{name}' has a Google sink or gmail inbox without google settings")
            for account in accounts:
                if account not in self.google.accounts:
                    raise ValueError(
                        f"flow '{name}' names Google account '{account}' missing from google.accounts"
                    )
        return self

    @model_validator(mode="after")
    def _laptop_outlook_needs_a_personal_onedrive(self) -> Self:
        """R-7.14: a flow that hands mail to the laptop agent files it on the personal OneDrive."""
        for name, flow in self.flows.items():
            if _lists_mail_mode(flow, "laptop_outlook") and not _has_personal_onedrive_sink(flow):
                raise ValueError(
                    f"flow '{name}' lists mail mode 'laptop_outlook' without an "
                    "'onedrive_folder' sink on 'onedrive_personal'"
                )
        return self


class Brand(BaseModel):
    """One comercio a store runs; ``serie_prefix`` picks its invoices apart (D47, R-5.8)."""

    model_config = ConfigDict(extra="forbid")

    comercio: str
    serie_prefix: list[str] = Field(default_factory=list)


class Store(BaseModel):
    """One issuer in ``config/stores/<ruc>.yaml``, keyed by its RUC (D47, R-5.8)."""

    model_config = ConfigDict(extra="forbid")

    ruc: str = Field(pattern=r"^\d{11}$")
    razon_social: str
    tipo: str
    brands: list[Brand] = Field(min_length=1)
    portal: str | None
    notes: str = ""


class SelectField(BaseModel):
    """A native ``<select>``: select the option the rendered template names (spec 03 §6)."""

    model_config = ConfigDict(extra="forbid")

    select: str


class PickField(BaseModel):
    """A custom dropdown: click the field, then the rendered option (spec 03 §6)."""

    model_config = ConfigDict(extra="forbid")

    pick: str


class CalendarSpec(BaseModel):
    """Selectors for a read-only date picker (spec 03 §6)."""

    model_config = ConfigDict(extra="forbid")

    cell: str
    prev: str


class CalendarField(BaseModel):
    """A date picker: step back with ``prev`` until ``cell`` shows, then click it (spec 03 §6)."""

    model_config = ConfigDict(extra="forbid")

    calendar: CalendarSpec


class PortalCaptcha(BaseModel):
    """Captcha selectors and solvers for a portal (spec 03 §6)."""

    model_config = ConfigDict(extra="forbid")

    image: str
    input: str
    solver: list[str]


class PortalDownloads(BaseModel):
    """Selectors that locate the downloadable documents (spec 03 §6)."""

    model_config = ConfigDict(extra="forbid")

    xml: str | None = None
    pdf: str | None = None


#: D65: a portal's default "not found yet" retry delays, in order.
DEFAULT_RETRY_SCHEDULE: tuple[timedelta, ...] = (
    timedelta(hours=1),
    timedelta(hours=6),
    timedelta(hours=24),
    timedelta(hours=72),
)


class Portal(BaseModel):
    """One portal site in ``config/portals/<key>.yaml`` (R-5.8, D82).

    The ``key`` is the file name, never a field of the file itself. A
    ``retry_schedule`` entry is a D65 delay such as ``1h``. ``captcha`` is
    ``"none"`` unless the site presents one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    adapter: str
    url: str | None = None
    status: Literal["active", "todo", "broken"] = "todo"
    fields: dict[str, str | SelectField | PickField | CalendarField] = Field(default_factory=dict)
    captcha: Literal["none", "image", "recaptcha"] | PortalCaptcha = "none"
    submit: str | None = None
    downloads: PortalDownloads | None = None
    not_found: str | None = None
    tipo_labels: dict[str, str] = Field(default_factory=dict)
    allow_http: bool = False
    retry_schedule: tuple[Delay, ...] = DEFAULT_RETRY_SCHEDULE
    notes: str = ""


class SolLogin(BaseModel):
    """The SOL sign-in form: the page and the selectors that receive the credentials (D134)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str
    ruc: str
    user: str
    password: str
    submit: str


class SunatSolSite(BaseModel):
    """The SOL site in ``config/sunat_sol.yaml``: sign-in and the invoice query (D134)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["active", "todo", "broken"]
    login: SolLogin
    query: Portal

    @model_validator(mode="after")
    def _login_url_is_a_sunat_origin(self) -> Self:
        """R-9.10: the credentials are typed only on a hard-coded SUNAT origin."""
        if not is_sunat_origin(self.login.url):
            raise ValueError(f"sunat_sol.login.url {self.login.url!r} is not a hard-coded SUNAT origin")
        return self


class StoreDirectory(BaseModel):
    """The two-folder store directory: stores by RUC and portals by key (D47, R-5.8).

    ``sunat_sol`` is the optional SOL site file beside them (D134).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    stores: dict[str, Store]
    portals: dict[str, Portal]
    sunat_sol: SunatSolSite | None = None
