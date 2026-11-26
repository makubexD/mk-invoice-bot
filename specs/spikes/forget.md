# Live run: forget an invoice (9z3, D196)

Date: 2026-11-26. Live image `c2edf78512f4`, invoice #34 (`DONE`, flow `work_microsoft`, its flow B folder renamed
`_Sent` after the 9z send). Its natural key had already been released by hand earlier that day.

| Check                                         | Result                                                                                                                                  |
| --------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `invoice forget 34` (dry run)                 | names #34, `drive: kept`, `files: kept`, `database: would release key`, `dry run: nothing changed`                                      |
| `invoice forget 34 --drive --purge` (dry run) | `drive: would delete /Reembolsos/2026-11-Support_2_…_Sent`, `files: would remove 1`, row unchanged                                      |
| `invoice forget 34 --drive --purge --yes`     | exit 0; drive deleted, `files: removed 1`, `database: purged (files 4, fetch_attempts 6, messages 1, outbox 13, invoices 1, batches 1)` |
| `rclone lsf onedrive:Reembolsos/` (read-only) | the `2026-11-Support_2_…_Sent` folder is gone; `2026-11-Support_1_…` is untouched                                                       |
| Database                                      | no row for #34 in `invoices`, `files`, `outbox`, `events`, `jobs`, `fetch_attempts`, `messages`                                         |
| Host files                                    | `/data/tmp/34`, `/data/mail/34` and `/data/mail/34.eml` are absent                                                                      |
| The same photo sent again                     | pending: the owner resends it; expected to file as `…_Support_2_…` with no duplicate answer                                             |
