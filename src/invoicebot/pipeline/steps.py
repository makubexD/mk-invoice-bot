"""Job step names, kept apart so handlers can name each other's steps.

The fetch handler enqueues the deliver step, but the deliver handler reaches the fetch
module through the button actions; naming both steps here keeps the import graph acyclic.
"""

#: Job step that downloads a photo before extraction (R-1.4).
DOWNLOAD_STEP = "download_media"
#: Job step that reads the downloaded photo or PDF (S3.11).
EXTRACT_STEP = "extract"
FETCH_STEP = "fetch"
#: Job step that ends a captcha ask nobody answered in time (D209, BR-F05).
CAPTCHA_TIMEOUT_STEP = "captcha_timeout"
#: Job step that tells the sender the fetch is still running, after a short wait (D206).
FETCH_PROGRESS_STEP = "fetch_progress"
DELIVER_STEP = "deliver"
#: Job step that sends an invoice's kept documents after [📎 Ver documentos] (D206, BR-D24).
SEND_DOCUMENTS_STEP = "send_documents"
#: Job step that asks SUNAT again after a BR-V01/BR-V04 answer (BR-V01, D125).
SUNAT_RECHECK_STEP = "sunat_recheck"
#: Job step that tries a ticket's printed URL or strip and offers the portal found (BR-F14, D192).
PORTAL_CHECK_STEP = "portal_check"
#: Job step that opens a taught portal link once, fills its form and files what it downloads (D193).
PORTAL_PROBE_STEP = "portal_probe"
#: Job step that attaches a sender's uploaded document to a stuck invoice (R-1.5, S7.5).
ATTACH_STEP = "attach_upload"
#: Job step that converges an invoice's folder name to its sent and paid status (R-7.11).
FOLDER_STATUS_STEP = "folder_status"
#: Job step that hands a [Por la laptop] mail to the laptop agent (R-7.14).
RESEND_LAPTOP_STEP = "resend_laptop"
#: Job step that rewrites and answers a [Lo envío yo] mail (R-7.8).
RESEND_EML_STEP = "resend_eml"
#: Job step that looks for a bounce of an smtp send in the inbox (D207).
BOUNCE_CHECK_STEP = "bounce_check"
