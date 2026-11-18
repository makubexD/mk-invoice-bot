# Accuracy report

Synthetic set (`tests/golden`, from `scripts/accuracy_report.py --set tests/golden --write`); the private set's
numbers come with the live check (`docs/plan-mvp-read-more.md` § "Deferred: live check").

- Date: 2026-11-16
- Set size: 12 pair(s)
- QR rate: 100.0%
- OCR-only required-field rate: 100.0%
- Pass: yes

| field | right | total | rate |
|---|---:|---:|---:|
| base | 10 | 12 | 83.3% |
| fecha_emision | 12 | 12 | 100.0% |
| igv | 12 | 12 | 100.0% |
| num_doc_adq | 12 | 12 | 100.0% |
| numero | 12 | 12 | 100.0% |
| ruc_emisor | 12 | 12 | 100.0% |
| serie | 12 | 12 | 100.0% |
| service_charge | 4 | 4 | 100.0% |
| tipo | 12 | 12 | 100.0% |
| total | 12 | 12 | 100.0% |

## Padrón import (S3.4, live host)

`scripts/import_padron.py` on the real `padron_reducido_ruc.zip`, run in the live app container on 2026-11-18
(9d step 9). The container has no `/usr/bin/time`, so peak RSS is the child's `ru_maxrss` from `getrusage`.

- ZIP: 377 MB (one 1.58 GB `.txt` member).
- Rows: 18,535,352 imported, 1 skipped. Wall time 480 s (142 s CPU in Python).
- Peak RSS: **598 MB. Misses the < 50 MB target.** The ZIP is read line by line, so the likely cause is the async
  `COPY`: psycopg 3.3's `AsyncLibpqWriter` hands rows to libpq without waiting for a flush, and libpq's output
  buffer grows while Postgres falls behind (unverified). The S3.4 test only covers a mini-ZIP, so it does not
  catch this.
- Peak disk: +3.0 GB on the host filesystem (the ZIP copied into the data volume, the 2.06 GB `padron` table with
  its index, and WAL), +2.9 GB once the import ends, before the ZIP is deleted.
