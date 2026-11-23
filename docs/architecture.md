# Architecture

This page shows how the bot fits together, drawn from the code. It is a view of
[the design spec](../specs/03-design.md), not a second source of truth: when the
two disagree, the spec decides and the drawing is fixed (D161).
`tests/unit/test_docs.py` fails when a package or an invoice state is missing
from the diagrams.

Business rules and the user messages are in
[spec 06](../specs/06-business-rules.md), and how to run the live host is in
[the deploy guide](../deploy/README.md).

## Components

One Python process (FastAPI + asyncio) and Postgres. Arrows point from the
caller to what it uses. Solid arrows are imports, which the contracts in
`.importlinter` keep pointing inward; dotted arrows are runtime calls through a
port (`Handler`, `Transport`) that `app.py` wires at startup.

```mermaid
flowchart LR
    meta(["WhatsApp Cloud API"])

    subgraph edge["Edge"]
        api["api: WhatsApp webhook"]
        channels["channels: Channel port, WhatsApp adapter"]
    end

    subgraph core["Core"]
        queue["queue: jobs, Worker, OutboxSender"]
        pipeline["pipeline: job steps, replies, commands"]
        routing["routing: buyer → flow"]
        rules["rules: BR-* catalog, es-PE texts"]
        extract["extract: QR, OCR, PDF, XML"]
        store[("store: Postgres SQL functions")]
    end

    subgraph adapters["Adapters"]
        fetchers["fetchers: qr_url, portal, sunat_sol, mail inbox"]
        sinks["sinks: OneDrive, Excel, mail, Drive, Sheets, laptop job"]
        sunat["sunat: validez API"]
        github["github: App token, issues"]
        auth["auth: Microsoft and Google tokens"]
        net["net: outbound URL guard"]
    end

    subgraph runtime["Runtime"]
        config["config: typed YAML config"]
        ops["ops: daily jobs, backups, padrón"]
        gaps["gaps: capability gaps"]
        wiring["wiring: builds each chat channel for app.py"]
    end

    helpers[["helper subprocesses: extract, browser (sandbox.py)"]]
    events["events (v1.1)"]

    meta -- webhook --> api
    api --> pipeline
    api --> channels
    api --> store
    queue -. runs jobs .-> pipeline
    pipeline --> routing
    pipeline --> rules
    pipeline --> extract
    pipeline --> fetchers
    pipeline --> sinks
    pipeline --> sunat
    pipeline --> gaps
    pipeline --> github
    pipeline --> store
    pipeline --> queue
    queue -. outbox .-> channels
    queue -. outbox .-> github
    channels --> meta
    extract -. heavy work .-> helpers
    fetchers -. browser .-> helpers
    fetchers --> net
    fetchers --> auth
    sinks --> auth
    ops --> pipeline
    wiring --> channels
    wiring --> api
    ops --> sinks
    ops --> store
    routing --> config
    config --> rules
```

| Package    | Job                                                                                    |
| ---------- | -------------------------------------------------------------------------------------- |
| `api`      | WhatsApp webhook routes: handshake, signature check, intake (R-1.2); REST/MCP in v1.1. |
| `channels` | Channel adapters and the canonical channel port (PT-1).                                |
| `queue`    | Durable jobs with leases and backoff, the `Worker`, the `OutboxSender` (S1.4).         |
| `pipeline` | The job steps, chat replies and commands that move an invoice through its states.      |
| `routing`  | Picks the flow for an invoice from its buyer and the configured rules (R-3.1).         |
| `rules`    | Business-rule catalog and es-PE user messages (spec 06).                               |
| `extract`  | Pure reading and checking of invoice data: QR payloads, images, PDF and XML.           |
| `store`    | Postgres: the connection pool and one module of SQL functions per table group.         |
| `fetchers` | Get an invoice's official PDF and XML (R-5.1).                                         |
| `sinks`    | Where a filed invoice goes: OneDrive folder, Excel, mail, Drive, Sheets.               |
| `sunat`    | The SUNAT validez API client.                                                          |
| `github`   | GitHub writes for capability gaps: the App token, the issue text, the transport.       |
| `auth`     | Sign-in and token caches for the accounts the sinks use (R-7.9).                       |
| `net`      | Outbound request guard: pure URL and address checks (R-5.10).                          |
| `config`   | Typed runtime configuration (R-9.6).                                                   |
| `ops`      | Scheduled operations: the daily task loop, backups, housekeeping, padrón (R-9.11).     |
| `gaps`     | Deduplicated missing capabilities that need a repository change (spec 06 §3).          |
| `events`   | Signed event webhooks to subscribers; a placeholder until v1.1.                        |

Two rules shape the arrows:

- **The outbox.** Nothing sends to a channel or to GitHub directly. A state
  change, its next job and its outgoing message are written in one transaction;
  the `OutboxSender` sends later and retries.
- **Helpers.** OCR, PDF rendering and the Playwright browser run as short-lived
  subprocesses (`helper.py`, `sandbox.py`), one at a time (the `HEAVY`
  semaphore), so the main process stays small. Portal traffic from the browser
  goes through `GuardProxy`, which applies the `net` checks.

## Deploy topology (local MVP host)

The MVP runs on the owner's Ubuntu machine (D141) as the rootless Podman
Compose project `invoicebot-live` (D148). Meta reaches it through Tailscale
Funnel (D146); the helpers run in `unshare` namespaces inside the app
container (D147).

```mermaid
flowchart LR
    meta(["Meta WhatsApp Cloud API"])
    uptime(["uptime monitor"])

    subgraph host["Ubuntu host"]
        funnel["Tailscale Funnel"]
        subgraph live["Compose project invoicebot-live"]
            app["app container: FastAPI + Worker + OutboxSender"]
            helpers[["helpers in unshare namespaces"]]
            pg[("postgres container")]
        end
        files[("~/.local/share/invoicebot: config, secrets.age, db password")]
    end

    subgraph cloud["Outbound services"]
        metagraph["Meta Graph API"]
        ms["Microsoft Graph: OneDrive, Excel, Outlook mail"]
        google["Google: Drive, Sheets, Gmail"]
        portals["store portals"]
        sunatapi["SUNAT: validez API, padrón"]
        gh["GitHub App: ops repo issues"]
    end

    laptop["owner's laptop: laptop agent + Outlook classic"]

    meta -- webhook --> funnel --> app
    uptime -- /health --> funnel
    app --> pg
    app --> helpers
    app --- files
    app --> metagraph
    app --> ms
    app --> google
    helpers -- via GuardProxy --> portals
    app --> sunatapi
    app --> gh
    app -- nightly age backup --> ms
    ms -- _outbox jobs and receipts --> laptop
```

- The laptop agent never talks to the bot directly. The bot drops signed
  `laptop_outlook` jobs in the personal OneDrive `_outbox/`; the agent sends
  them through Outlook classic and writes signed receipts back (R-7.14).
- Backups are `age`-encrypted before they leave the host (D119).
- `/health` answers 200 or, with Postgres down, 503 (R-9.14, R-9.15).

## Invoice state machine

Every transition is one Postgres transaction: the state, the rule ID that caused
it, the next job and the outgoing message (spec 03 §4). The states are in
`src/invoicebot/states.py`; the moves below are the ones the code makes, and
the two the spec describes but the code doesn't make yet are marked.

```mermaid
stateDiagram-v2
    [*] --> RECEIVED: photo or PDF arrives
    RECEIVED --> EXTRACT_FAILED: no photo or PDF to read (BR-E01)
    RECEIVED --> EXTRACTING: extract job
    RECEIVED --> CANCELLED: unsupported media (BR-E07)
    EXTRACTING --> ROUTING: fields read
    EXTRACTING --> EXTRACT_FAILED: unreadable or bad RUC (BR-E01)
    EXTRACTING --> CANCELLED: unsupported type or too many pages (BR-E07)
    EXTRACT_FAILED --> EXTRACTING: better photo (spec only, not built yet)
    EXTRACT_FAILED --> ROUTING: campo=valor edit
    ROUTING --> VALIDATING: flow decided
    ROUTING --> CONFIRMING: needs a question (BR-R03, BR-E03, BR-D06, confirm_mode)
    ROUTING --> CANCELLED: rejected or duplicate (BR-R07, BR-E04)
    CONFIRMING --> ROUTING: answer or edit
    VALIDATING --> FETCHING_DOCS: valid, outage or validation off
    VALIDATING --> CONFIRM_INVALID: estadoCp 2 or 4 (BR-V02, BR-V06)
    CONFIRM_INVALID --> FETCHING_DOCS: user continues
    FETCHING_DOCS --> DELIVERING: documents fetched
    FETCHING_DOCS --> WAITING_CAPTCHA: captcha (not built yet, D99)
    WAITING_CAPTCHA --> FETCHING_DOCS: answered or timed out
    FETCHING_DOCS --> MANUAL_REQUIRED: fetcher chain exhausted (BR-F07)
    MANUAL_REQUIRED --> DELIVERING: user uploads the files, or template fixed
    MANUAL_REQUIRED --> FETCHING_DOCS: gap fixed or /reprocesar
    DELIVERING --> PENDING_AUTH: token expired (BR-D01)
    PENDING_AUTH --> DELIVERING: account signed in again
    DELIVERING --> MANUAL_REQUIRED: Excel template invalid (BR-D02)
    DELIVERING --> DONE: flow A and personal_dni
    DELIVERING --> BATCHED: flow B, added to its batch
    BATCHED --> DONE: batch sent (R-7.8)
    BATCHED --> DELIVERING: /reprocesar after a failed batch, or an edit
    FAILED --> FETCHING_DOCS: /reprocesar
    EXTRACT_FAILED --> PENDING_USER: no answer (BR-S08)
    CONFIRMING --> PENDING_USER: no answer (BR-S08)
    CONFIRM_INVALID --> PENDING_USER: no answer (BR-S08)
    PENDING_USER --> CONFIRMING: user answers
    PENDING_USER --> EXTRACT_FAILED: user answers
    PENDING_USER --> CONFIRM_INVALID: user answers
    DONE --> [*]
    CANCELLED --> [*]
    FAILED --> [*]
```

Not drawn, to keep the picture readable:

- **Cancel:** `/cancelar`, or the [Cancelar] button on a question, moves any state in `CANCELLABLE` (every state before
  `DELIVERING`, plus `PENDING_AUTH`) to `CANCELLED`.
- **Edit:** a `campo=valor` edit in any `EDITABLE` state goes back to
  `ROUTING`; a `PENDING_USER` invoice is resumed first (D94, D97).
- **Dead job:** a job that fails 5 times or kills the worker twice moves its
  invoice to `FAILED` (BR-S03).
- **Waiting:** `WAITING_FOR_USER` is `CONFIRMING`, `EXTRACT_FAILED` and
  `CONFIRM_INVALID`; `PENDING_USER` stores which one it left in
  `waiting_state` and returns to it.
- **XML:** an XML only attaches to an open invoice (R-1.5); alone it is
  refused with BR-E07 and creates nothing.
- **Today** a new photo after `EXTRACT_FAILED` starts a new invoice; S4.5
  owns the move back to `EXTRACTING`.
- **Terminal:** `DONE`, `CANCELLED` and `FAILED`; only `FAILED` can be
  reprocessed. Payment is not a state: `/pagado` links a payment to a `DONE`
  flow B invoice (R-7.12).

## Flow A (RK EXPORT, Google)

The owner's company invoices. Only the PDF is required (D142); the result goes
to Google Drive and a Sheets row.

```mermaid
sequenceDiagram
    actor User
    participant WA as WhatsApp
    participant Bot as app (Worker)
    participant SUNAT
    participant Src as qr_url, portal or Gmail inbox
    participant Google as Drive and Sheets

    User->>WA: photo of the invoice
    WA->>Bot: webhook
    Bot-->>User: acknowledgement
    Bot->>Bot: extract (QR, OCR or PDF), route by buyer RUC
    opt validation on
        Bot->>SUNAT: validez check
    end
    Bot-->>User: summary with the flow
    Bot->>Src: fetch the official PDF
    Src-->>Bot: PDF
    Bot->>Google: upload to the flow's folder, upsert the Sheets row
    Bot-->>User: done, with the links (BR-D16)
```

## Flow B (CIGNIUM, Microsoft)

The owner's employer reimbursements. The PDF and the XML are required; the
result is a OneDrive folder, a row in the Excel template and a mail to HR.

```mermaid
sequenceDiagram
    actor User
    participant WA as WhatsApp
    participant Bot as app (Worker)
    participant Src as qr_url, portal or Outlook inbox
    participant MS as OneDrive and Excel
    participant Mail as Graph mail or laptop agent

    User->>WA: photo of the invoice
    WA->>Bot: webhook
    Bot-->>User: acknowledgement
    Bot->>Bot: extract, route by buyer RUC, check the reimbursement cap
    Bot-->>User: summary with the flow
    Bot->>Src: fetch the PDF and XML
    alt documents found
        Src-->>Bot: PDF and XML
    else chain exhausted (BR-F07)
        Bot-->>User: asks for the files
        User->>Bot: sends them
    end
    Bot->>MS: create the invoice folder, fill the Excel template
    Bot->>Mail: send the batch to HR (Graph, or a signed laptop job)
    Mail-->>Bot: sent, or laptop receipt
    Bot-->>User: done (BATCHED to DONE)
    User->>Bot: /pagado later links the payment (R-7.12)
```

A third flow, `personal_dni`, takes boletas issued to the owner's DNI: it
fetches nothing and writes only a Sheets row (D143, D144).
