# Spike: portal-link live check (9t4a, D208)

Date: 2026-11-27. Plan: `docs/archive/plan-mvp-portal-link.md`. Flow A (Drive), the PEBSA ticket from live #50, resent after
`/cancelar` and `invoice forget --purge`.

Before any change: tag `live-2026-11-27-pre-pebsa` on the live commit (image `aa49e52d6441`); the live config was
copied to a backup folder. PEBSA's store and portal files (`captcha: image`, `status: todo`) were installed in the live
config before each deploy. They stay in the live config only: CI refuses a pull request that adds a portal that is not `active`, so they enter the repo with 9t4, once the portal is active.

| Row                                             | Result                                                                                                                                                                                                                            |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| #51 (image `6022f8581952`)                      | fail: BR-F13 read `• Portal sy.ecomprobantes.pe: captcha que no puedo resolver` with [🔗 Abrir el portal]; Telegram auto-linked the bare host to the site's root, which answers 403. The button itself opened the form (HTTP 200) |
| Fix                                             | `{portal}` names the comercio (`de PEBSA`), never the host (D208 (4)); BR-F03, BR-F04 and the `todo` bullet                                                                                                                       |
| #52 (image `65e1da948130`)                      | pass: BR-F13 reads `• Portal de PEBSA: captcha que no puedo resolver`, no `no lo conozco`, no link in the text; [🔗 Abrir el portal] opens the form "Autenticación Estándar" with its captcha                                     |
| [🔗 Enseñar el portal] hidden for a taught link | pass: BR-F13 offers only [📎 Te envío el PDF] and the link                                                                                                                                                                        |
| Flow B                                          | untouched: no flow B config changed                                                                                                                                                                                               |

Owner's ask at the check: the bot should fill the form and show the captcha in the chat. That is 9t4 `portal-guide`
(save and resume, D99 reopened); the site's form is an ASP.NET postback, so a link cannot carry the field values.
