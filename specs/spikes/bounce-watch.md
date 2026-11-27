# Spike: what a bounce looks like and a draft from Sent

Date: 2026-11-27. Account: the owner's personal Outlook.com mailbox (config account `personal`). Plan:
`docs/archive/plan-mvp-bounce-watch.md`, step 1.

## Probe

A one-off script inside the live app container signed the personal account in with a device code for
`Mail.ReadWrite`, into a separate cache file. The live cache was used only for its silent `SMTP.Send` token. The
script sent one SMTP mail (D205 shape: plain text, the HTML alternative, one small text attachment) with a
`Message-ID` it chose itself, to a made-up address at Outlook.com, so that a real delivery report came back. It then
read the report, made a draft from the Sent copy, and deleted the draft. The probe cache and the script were removed
afterwards. The report and the Sent copy stay in the mailbox.

## The report

| Check                        | Result                                                                              |
| ---------------------------- | ----------------------------------------------------------------------------------- |
| Arrival                      | 13 s after the send, in the inbox (nothing in Junk)                                 |
| Sender                       | display name `Microsoft Outlook`, a `MicrosoftExchange<hex>` local part             |
| Subject                      | `No se puede entregar: <original subject>` (the mailbox language decides the words) |
| `Status:` / `Action:`        | `5.5.0` / `failed` (unknown user); the #45-era Graph bounces: `5.7.501` / `failed`  |
| Original `Message-ID` header | **rewritten**: Outlook.com gives the mail its own `Message-ID` at submission        |
| Where the bot's own id stays | `X-Microsoft-Original-Message-ID` in the `message/rfc822` part's headers            |
| Report `In-Reply-To`         | Outlook's id, the same as the `message/rfc822` part's `Message-ID`                  |
| The bot's id elsewhere       | also quoted in the human-readable `text/plain` and `text/html` parts                |

The part tree is the same for the SMTP probe and for the two Graph bounces of 2026-11-26:

```text
multipart/report
  multipart/alternative
    text/plain            (the readable notice)
    text/html
  message/delivery-status
    per-message fields: Reporting-MTA, Arrival-Date (Graph ones also Original-Envelope-Id)
    per-recipient fields: Final-Recipient, Action: failed, Status: <x.y.z>, Diagnostic-Code, Remote-MTA
  message/rfc822          (the original: its headers, then its body)
    headers include Message-ID (Outlook's) and X-Microsoft-Original-Message-ID (the bot's)
```

## The draft

| Check                                                      | Result                                                                                                                 |
| ---------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| Sent copy found by the bot's id (`internetMessageId eq …`) | **no match**: the Sent copy has Outlook's id                                                                           |
| Sent copy found by the report's `In-Reply-To`              | one match; its `X-Microsoft-Original-Message-ID` is the bot's id                                                       |
| `POST /me/messages` with the Sent copy's base64 MIME       | `201`, `isDraft: true`, in Drafts, same recipient, subject and attachment                                              |
| The draft's `webLink`                                      | `https://outlook.live.com/owa/?ItemID=…&viewmodel=ReadMessageItem`                                                     |
| The owner opened the link on the phone                     | opens the draft (`Borrador · Este mensaje no se ha enviado`) with its attachment; the ✏️ opens it for sending, one tap |
| `DELETE` of the draft                                      | `204`                                                                                                                  |

## What changes in the plan

- A report matches a sent mail when its `message/rfc822` part's `X-Microsoft-Original-Message-ID` equals the id the bot
  recorded. The status is the first `Status:` in `message/delivery-status`.
- The Sent copy is looked up by the report's `In-Reply-To` (Outlook's id), never by the bot's id.
- BR-D26 tells the owner to tap ✏️ in the draft and send it.
- Gate: both conditions hold (the report carries the bot's id, and the draft opens ready to send in one tap), so the
  plan goes on.

## Live run

2026-11-27, image `aa49e52d6441`, after the owner signed in again with `Mail.ReadWrite`
(`invoicebot auth-microsoft --account personal`).

| Check                                     | Result                                                                 |
| ----------------------------------------- | ---------------------------------------------------------------------- |
| #49 (flow B, La Leña) sent by SMTP        | 18:12:43 UTC; the owner received it in the work inbox                  |
| `bounce_check` runs                       | 7, from 18:14:45 to 18:45:42, about every 5 min                        |
| Result                                    | `bounce watch clear` for invoice 49 at 18:45:42, 33 min after the send |
| `bounce watch blocked` or a BR-D26 notice | none                                                                   |

The bounce path itself was proven by the probe above and is covered by the tests against its report; a forced bounce
through the pipeline was not run (step 12).
