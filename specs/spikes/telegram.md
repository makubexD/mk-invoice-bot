# Spike: Telegram live run (T2)

Date 2026-11-23, image `localhost/invoicebot-app:0f99a5b17b37`, from the owner's Telegram (step 13 of
`docs/archive/plan-telegram-live.md`). The owner had one new invoice to hand, so only part of the checklist ran.

## What ran

| Item                                               | Result                                                                                                                                                                                                                                            |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0. `invoicebot telegram commands` and the `/` menu | `menu set: default`, `menu set: <admin profile>`; the menu and autocomplete show on the phone, admin entries included (D187)                                                                                                                      |
| 1. Photo of a factura (sent as a photo)            | Read in about 4 s, every field from the QR (confidence 1.0); `razon_social` from the padrón. It was a copy of an invoice already filed, so BR-E04 answered and cancelled it, as specified. `moneda` and `base` stay empty: the QR carries neither |
| 5. Commands                                        | `/estado <id>` answered; `/estado` with no id answered "Falta el número…"; `/pendientes` and `/gaps` answered                                                                                                                                     |
| 7. Admin alert                                     | A test alert arrived on Telegram                                                                                                                                                                                                                  |

## Not run (no invoice to hand)

Flow B photo sent as a file, a `personal_dni` boleta, a button press (D177), `/reprocesar` and `/cancelar`, the
`/cmd@<bot>` form, the XML mime question, a gap notice. They move to the T3 row of `docs/plan-telegram.md`.

## Surprises

- `/pendientes` lists the console rehearsal invoices as "emisor sin leer", and answering it re-sent their BR-E01 ask
  on the console channel, not the channel the command came from.
- BR-E01 does not say which fields were read and which are missing; the owner wants that, with a re-send or
  type-it choice. Planned as `smart-replies`.
