"""Job step names, kept apart so handlers can name each other's steps.

The fetch handler enqueues the deliver step, but the deliver handler reaches the fetch
module through the button actions; naming both steps here keeps the import graph acyclic.
"""

FETCH_STEP = "fetch"
DELIVER_STEP = "deliver"
#: Job step that attaches a sender's uploaded document to a stuck invoice (R-1.5, S7.5).
ATTACH_STEP = "attach_upload"
