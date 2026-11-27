# Spike: SMTP submission avoids the Graph 5.7.501 bounce

Date: 2026-11-27. Account: the owner's personal Outlook.com mailbox (config account `personal`). Recipient: the owner's
work address (a Microsoft 365 tenant). One send only, never a burst (D197).

## Why

Every flow B mail the bot sent through Graph since 2026-11-26 bounced at the work tenant with
`550 5.7.501 Service unavailable. Spam abuse detected from IP range. AS(130)`, the HTML body of 9z4 (D197) included:
the 9z8 live run (#45, 2026-11-27 05:33 UTC) was the HTML shape of the A/B "mail B" and still bounced. A public Microsoft
Q&A thread reports the same NDR for every `/me/sendMail` from personal Outlook.com and Hotmail accounts since
2026-11-16, to any recipient, while the outlook.com web UI keeps sending; Microsoft has not answered it. The bot's own
records agree: a Graph send was delivered on 2026-11-25 (`portal-probe.md`), every one after 2026-11-26 bounced, and
every mail the owner sent by hand arrived. Mail B was a lucky draw, not a fix.

## Probe

A one-off script inside the live app container signed the personal account in with the device-code flow for the scope
`https://outlook.office.com/SMTP.Send` (a separate cache file; the live cache untouched), then sent one mail through
`smtp-mail.outlook.com:587` (STARTTLS, `AUTH XOAUTH2`): the same subject, body (plain text plus the D197 HTML
alternative) and three attachments as #45's bounced mail.

| Check                                           | Result                                                     |
| ----------------------------------------------- | ---------------------------------------------------------- |
| Device-code sign-in for `SMTP.Send`             | pass: the owner consented; Microsoft mailed a new-app note |
| `AUTH XOAUTH2`                                  | `235 2.7.0 Authentication successful`                      |
| `send_message`                                  | no recipient refused; a copy in the Sent folder            |
| Bounce in the personal mailbox, next 10 minutes | none (Graph bounces came within seconds)                   |
| The work inbox                                  | delivered with its three attachments (owner, 00:50 -05)    |

## Token findings

- MSAL cannot mix resources in one request: Graph scopes (`Mail.Send`, `Files.ReadWrite`, `Mail.Read`) and
  `SMTP.Send` (resource `outlook.office.com`) need separate token requests.
- A cache signed in for `SMTP.Send` returned a Graph `Mail.Send` token silently (the Graph consent already existed).
  The live cache, signed in for Graph only, could not get an `SMTP.Send` token silently (`AuthRequired`): the first
  sign-in for an SMTP account has to ask for that scope too.

## Decision

D205: flow B sends through SMTP with OAuth (`smtp` mail mode); Graph keeps OneDrive and the inbox. Bounce reading and
a draft fallback come in 9z11 `bounce-watch`; the chat changes in 9z12 `chat-tidy`.

## Live run

2026-11-27, image `3cfe2543c462` (9z10 merged), live flow B on `modes: [smtp]`.

| Check                                          | Result                                                                          |
| ---------------------------------------------- | ------------------------------------------------------------------------------- |
| `auth-microsoft --account personal`            | pass: two device codes, the second for `SMTP.Send`; `resumed 0 invoice(s)`      |
| Silent `SMTP.Send` token from the live cache   | pass                                                                            |
| #47, the #45 receipt resent after `forget`     | filed, XML confirmed, mail sent through SMTP (deliver 14:19 UTC), BR-D10 posted |
| Bounce in the personal mailbox, next 8 minutes | none; the only new item is the mail's copy in Sent                              |
| The work inbox                                 | delivered with its three attachments (owner)                                    |

Seen on the way, not caused by 9z10: the host lost its connection 13:44–13:53 UTC (Telegram `ConnectError`, two
outbox rows waited and then went out), and at 14:15 the Cloudflare tunnel stalled until the webhook watch restarted it
(D201), which held one photo two minutes. The resent photo first answered BR-E04 (a duplicate of #45), as it should.
