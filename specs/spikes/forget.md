# Live run: forget an invoice (9z3, D196)

Date: 2026-11-26. Live image `c2edf78512f4`, invoice #34 (`DONE`, flow `work_microsoft`, its flow B folder renamed
`_Sent` after the 9z send). Its natural key had already been released by hand earlier that day.

| Check                                         | Result                                                                                                                                           |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `invoice forget 34` (dry run)                 | names #34, `drive: kept`, `files: kept`, `database: would release key`, `dry run: nothing changed`                                               |
| `invoice forget 34 --drive --purge` (dry run) | `drive: would delete /Reembolsos/2026-11-Support_2_…_Sent`, `files: would remove 1`, row unchanged                                               |
| `invoice forget 34 --drive --purge --yes`     | exit 0; drive deleted, `files: removed 1`, `database: purged (files 4, fetch_attempts 6, messages 1, outbox 13, invoices 1, batches 1)`          |
| `rclone lsf onedrive:Reembolsos/` (read-only) | the `2026-11-Support_2_…_Sent` folder is gone; `2026-11-Support_1_…` is untouched                                                                |
| Database                                      | no row for #34 in `invoices`, `files`, `outbox`, `events`, `jobs`, `fetch_attempts`, `messages`                                                  |
| Host files                                    | `/data/tmp/34`, `/data/mail/34` and `/data/mail/34.eml` are absent                                                                               |
| The same photo sent again                     | filed as #35 with no duplicate answer; BR-D19 review, [Enviar], then BR-D10 "archivada en 2026-11-Support_2_…": the sequence number 2 was reused |

Findings:

- **Forget works end to end.** The resent photo is a new invoice (#35), its folder takes `Support_2` again (D88), and
  the 9z2 texts show: the pressed keyboard settles and BR-D10 says Outlook accepted the mail, for which address.
- **The mail bounced again with 5.7.501** (the recipient tenant refusing an Outlook.com outbound IP range), as in
  `open-links.md`. Not a forget issue: 9x `inbox-match` is where the bot learns of a bounce and offers [Reintentar].
