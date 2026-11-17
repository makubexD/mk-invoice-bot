"""Job step names, kept apart so handlers can name each other's steps.

The fetch handler enqueues the deliver step, but the deliver handler reaches the fetch
module through the button actions; naming both steps here keeps the import graph acyclic.
"""

FETCH_STEP = "fetch"
DELIVER_STEP = "deliver"
#: Job step that asks SUNAT again after a BR-V01/BR-V04 answer (BR-V01, D125).
SUNAT_RECHECK_STEP = "sunat_recheck"
#: Job step that attaches a sender's uploaded document to a stuck invoice (R-1.5, S7.5).
ATTACH_STEP = "attach_upload"
#: Job step that converges an invoice's folder name to its sent and paid status (R-7.11).
FOLDER_STATUS_STEP = "folder_status"
#: Job step that hands a [Por la laptop] mail to the laptop agent (R-7.14).
RESEND_LAPTOP_STEP = "resend_laptop"
#: Job step that rewrites and answers a [Lo envío yo] mail (R-7.8).
RESEND_EML_STEP = "resend_eml"
