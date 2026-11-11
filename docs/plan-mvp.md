# Roadmap: mk-invoice-bot MVP (v1 = M1–M5)

Source of truth: `specs/` (constitution, requirements, design, backlog in `specs/04-tasks.md`). Each phase becomes its own step plan, `docs/plan-mvp-<phase>.md`, written when the previous phase finishes. A phase is done when its milestone demo in spec 04 passes.

| # | Phase (plan file) | Backlog | Milestone | Needs from the owner first |
|---|---|---|---|---|
| 1 | `foundation` | E1 (S1.1–S1.10, S5.1, S12.2) | part of M1 | Nothing |
| 2 | `whatsapp` | E2 (incl. S2.7–S2.9 window and templates) | part of M1 | S0.7: Meta app, dedicated number, utility templates submitted |
| 3 | `extraction` | E0 spikes S0.2, S0.3; E3 | part of M1 | S0.1: ≥ 30 synthetic or anonymised invoice photos + expected JSON |
| 4 | `routing` | E4, rest of E5 | M1 demo | Owner and work RUCs on the VPS only (never in git) |
| 5 | `flow-a` | E6, E7, E8 (MVP part), E9, E15 | M2 demo | S0.4, S0.5, S0.10 (GitHub App + private ops repo), Google OAuth app in production status |
| 6 | `portals` | E10 | M3 demo | S0.6: top 10–20 issuers and their portals |
| 7 | `flow-b` | E11 (`on_command` only) | M4 demo | S0.8 (Entra app; work tenant consent), Q2–Q4 answers, Excel template |
| 8 | `ops` | E12, then S12.7 (repo private, on the owner's word) | M5 demo | VPS chosen (D42) |
| – | v1.1 `contract` | E13, E14 | M6 | — |
| – | optional `llm-fallback` | E3b | — | DeepSeek key; flows opted in (never flow B, D43) |

Rules for every phase plan: steps of one concern in at most about 3 files, each with its own test command; tests cite requirement and rule IDs; no real RUCs, phone numbers or photos in git (D27).
