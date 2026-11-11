# 05 · Open questions and decisions

## Decided (Q&A on 2026-10-13 and 2026-10-23)
| # | Decision |
|---|---|
| D1 | Country: **Peru (SUNAT)** |
| D2 | Channel: **official WhatsApp Cloud API**, with Telegram as an optional fallback |
| D3 | Users: several allowlisted people, **< 200 invoices per month** |
| D4 | ~~Ask in chat~~ → **revised 2026-10-23:** the flow is picked **automatically from the buyer RUC** (owner → A, work → B). A menu appears only when nothing matches (BR-R03) |
| D5 | Flow A: buyer is the owner's company RUC (20…); **SUNAT is the fallback** (owner has SOL) |
| D6 | Flow B: buyer is the employer's RUC; **no SUNAT access** → portals, then email, then manual |
| D7 | Hosting: **1 GB RAM VPS**, highly optimized (see constitution P2) |
| D8 | Build style: **code first + n8n**. Revised by the 1 GB constraint: n8n is an optional sink (Q1) |
| D9 | Flow B email: **all three trigger modes** (per invoice, on command, scheduled), configurable and combinable |
| D10 | Flow B Excel: **existing template, update specific cells** |
| D11 | When every fallback fails → **MANUAL_REQUIRED** and the user is told what is missing |
| D12 | Unknown, DNI or empty buyer → **ask then, and only then**, with buttons Mi empresa / Trabajo / Cancelar |
| D13 | Missing capability → **GitHub issue + draft PR + red CI** (spec 06 §3) |
| D14 | Unknown store portal → the bot **asks the user for the link** (once per issuer) and puts it into the draft PR |
| D15 | Admin alerts also go to **WhatsApp** (in addition to GitHub's own notifications) |
| D16 | GitHub repo is **private**. The real config (RUCs, phone numbers) lives only on the VPS, never in git (Q12) |
| D17 | Bot GitHub credential: **fine-grained personal access token**, this repo only (Issues, PRs, Contents); expiry warning 14 days before (Q13) |
| D18 | Admin alerts go **only to the owner's number** (Q14) |
| D19 | Gap PRs are completed by the **owner**; tagging `@claude` on the issue is optional help (Q15) |
| D20 | The VPS **pulls `main` every 10 min** and hot-reloads the config, so merged fixes go live and stuck invoices resume automatically (Q16) |
| D21 | `confirm_mode: auto`: no tap needed when everything reads cleanly; Editar/Cancelar stay available (Q17) |
| D22 | ~~Jarvis as the platform with invoices as its module~~ → **superseded by D39** the same day |
| D23 | ~~4 GB host~~ → **moved to Jarvis.** The bot alone targets ≤ 2 GB (constitution P1) |
| D24 | LLM in the invoice pipeline: **fallback only, off by default**. QR → XML → OCR stay first; a vision LLM (provider-neutral port, DeepSeek by default) runs only when they fail, tagged `source=llm` with low confidence, and the user always confirms |
| D25 | ~~Non-owner senders get intake only~~ → **moved to Jarvis.** In the bot, every allowlisted sender only does invoice intake anyway |
| D26 | ~~Jarvis built in this repo~~ → **superseded by D39** |
| D27 | Repo renamed to **`mk-invoice-bot`**. It stays **public during development**; making it private is the **last step** of the plan, on the owner's word. Until then no real RUCs, phone numbers, personal data or secrets in git. Amends D16 |
| D28–D30 | Foundation review, Pydantic AI, DeepSeek as the agent LLM → **moved to Jarvis** (see the jarvis repo). Spec 07 stays here for the patterns that also apply to the bot |
| D31 | Development runs in **local Docker Compose** with free tools (e.g. a Cloudflare quick tunnel for the Meta webhook). The VPS is chosen later; nothing may assume a provider |
| D32 | MVP = **full invoice spec, flows A and B**, delivered through internal checkpoints (foundation → flow A → flow B) |
| D33 | Background jobs and scheduling: **Postgres only** (SKIP LOCKED queue). No Redis |
| D34–D36 | Workstation connector, dashboard, LLM observability → **moved to Jarvis** |
| D37 | Deploys: **CI-gated**. Only green commits deploy, with migrations, a health check and automatic rollback. Config hot-reload stays. Supersedes D20 |
| D38 | Foundation: **clean code, patterns only** (spec 07). PT-1, PT-2, PT-4 and PT-8 apply to the bot |
| D39 | **Two repos, contract only (2026-11-10).** mk-invoice-bot is standalone; Jarvis is a separate repo. They integrate only through the bot's REST API, MCP server and signed event webhooks. No shared code and no shared database; the bot never depends on Jarvis (constitution P10) |
| D40 | Bot database: **Postgres** (replaces SQLite in P2 and spec 03) |
| D41 | Channels: the bot owns the **WhatsApp** number and also supports **Telegram** (R-1.7 is raised from C to S). Jarvis uses its own Telegram bot |

## Open (needed before or during Phase 0)
Until they are answered, work proceeds with the default in the last column. Q2–Q5 still need real input (accounts, recipients, template file, inbox) before E11 and S9.5.
| # | Question | Why it matters | Default if unanswered |
|---|---|---|---|
| Q1 | OK with n8n as an *optional* add-on (off on the 1 GB host), with the core in Python? Or do you prefer a 2 GB host (~US$4–6/mo) so n8n runs alongside? | RAM budget | Core in Python, n8n off |
| Q2 | Microsoft accounts: is your personal account outlook.com/hotmail (MSA)? Does your work tenant allow consenting to a third-party app? Should the bot write to your **personal** OneDrive and share that folder with your work account, or should the work account share a folder with your personal one? | Determines auth and folder location | Personal MSA OneDrive, folder shared with work |
| Q3 | Email for flow B: which provider sends it (Outlook personal through Graph, or Gmail), and who receives it (to and cc)? | Mailer config | Graph from the personal account |
| Q4 | Can you share the **Excel template** (cells to fill, sheet name, .xlsx or .xlsm, formulas, and whether a batch means one file per invoice or one file with many)? | Cell mapping; batch format | One file per invoice |
| Q5 | Which email do you give restaurants and stores for the electronic invoice? Can the bot read that inbox (Gmail or Outlook)? | The `email_inbox` fetcher is the strongest free fallback, especially for flow B | Gmail, read-only scope |
| Q6 | Google Sheet columns and Drive folder structure for flow A: keep the proposed defaults, or do you have an existing sheet? | Sheets mapping | Proposed defaults |
| Q7 | Required docs per flow: should flow B still deliver if only the PDF *or* only the XML is found? | Delivery rule | Both are required for B, XML for A |
| Q8 | Do you also need **boletas** (B-series, buyer DNI) or only **facturas** (F-series)? | Boletas often lack a buyer RUC; SOL won't list them | Facturas first, boletas best-effort |
| Q9 | Any currency other than PEN (USD invoices)? | Parsing and Sheets formatting | PEN, USD read from XML when present |
| Q10 | Comfortable creating a **secondary SOL user** with query-only permissions for the bot? | Security of the `sunat_sol` fetcher | Yes |
| Q11 | Do you already have a VPS provider or a domain for Cloudflare Tunnel? | Deploy | Local Docker for now (D31); later any ≤ 2 GB VPS, plus a free `trycloudflare` URL for dev |
