"""Typed pydantic models for the runtime configuration (R-9.6).

Every model forbids unknown keys so a typo is caught at load time rather than
silently ignored.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Owner(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ruc: str
    profile: str
    timezone: str
    language: str


class Work(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ruc: str


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    whatsapp: str | None = None
    telegram_user_id: int | None = None
    preferred_channel: Literal["whatsapp", "telegram"] | None = None
    flows: list[str]
    can_send_batch: bool
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


class TelegramChannel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["telegram"]
    enabled: bool


type Channel = Annotated[WhatsAppChannel | TelegramChannel, Field(discriminator="kind")]


class ValidezApi(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class Sol(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class Padron(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_cron: str


class Sunat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    validez_api: ValidezApi
    sol: Sol
    padron: Padron


class Ocr(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engine: str
    lang: str
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
    helper_timeout_s: int = Field(gt=0)


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


class GoogleDriveSink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["google_drive"]
    root_folder_id: str
    path: str
    filename: str
    upload: list[str]


class GoogleSheetsSink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["google_sheets"]
    spreadsheet_id: str
    tab: str
    upsert_key: str
    columns: dict[str, str]


class OneDriveSink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["onedrive"]
    account: str
    path: str
    filename: str
    upload: list[str]


class ExcelTemplateSink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["excel_template"]
    template: str
    template_sha256: str
    allow_macros: bool
    output: str
    sheet: str
    cells: dict[str, str]


class MailerTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    modes: list[Literal["on_command", "per_invoice", "scheduled"]]
    confirm_before_send: bool
    min_items: int


class MailerSink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["mailer"]
    provider: Literal["graph", "gmail"]
    batch_aware: bool
    trigger: MailerTrigger
    to: list[str]
    cc: list[str]
    subject: str
    body_template: str
    attach: list[str]
    zip_if_over_mb: int


type Sink = Annotated[
    GoogleDriveSink | GoogleSheetsSink | OneDriveSink | ExcelTemplateSink | MailerSink,
    Field(discriminator="kind"),
]


class Flow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    accepted_tipos: list[str]
    fetchers: list[str]
    required_docs: list[str]
    deliver_without_docs: bool
    sinks: list[Sink]


class EmailInbox(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["gmail", "graph"]
    retry_schedule: list[str]


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


class Retrieval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email_inbox: EmailInbox
    captcha: Captcha
    paid_api: PaidApi


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
    admins: list[str]
    teach_via_chat: bool
    realert_days: int
    broken_after: int
    digest_kinds: list[str]
    digest_cron: str


class BackupTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    path: str


class Backup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cron: str
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


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: Owner
    work: Work
    profiles: dict[str, Profile]
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
    def _routing_rules_name_known_flows(self) -> Self:
        """R-3.1: a routing rule may only send invoices to a configured flow."""
        for index, rule in enumerate(self.routing.rules):
            if rule.flow not in self.flows:
                raise ValueError(f"routing.rules[{index}] ({rule.id}) names unknown flow '{rule.flow}'")
        return self


class PortalMatch(BaseModel):
    """Issuer match rules for a portal registry entry (R-5.8)."""

    model_config = ConfigDict(extra="forbid")

    ruc: list[str] = Field(default_factory=list)
    url_contains: list[str] = Field(default_factory=list)


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


class Portal(BaseModel):
    """One entry of the portal registry in ``config/portals.yaml`` (R-5.8)."""

    model_config = ConfigDict(extra="forbid")

    key: str
    status: Literal["active", "todo", "broken"] = "active"
    match: PortalMatch = Field(default_factory=PortalMatch)
    adapter: str
    url: str | None = None
    fields: dict[str, str | dict[str, str]] = Field(default_factory=dict)
    captcha: PortalCaptcha | None = None
    submit: str | None = None
    downloads: PortalDownloads | None = None
