# Spike: xml-confirm live run (9z8, D202, D203)

Status: waiting for the deploy of 9z8 and a live receipt.

## Setup

- `main` with 9z8 deployed with `deploy/deploy-live.sh <tag>`; the webhook watch's journal shows `webhook ok` (9z7).
- The live config's flow B mailer set to `send: if_doubtful`, then reloaded (runbook §12).

## What to record

| Check                                                                              | Result |
| ---------------------------------------------------------------------------------- | ------ |
| BR-F16 `📄 Encontré el PDF y el XML. Lo archivo…` arrives after the summary        |        |
| A summary with `⚠️ Revisa: serie, número` gets `✅ El XML confirma: serie, número` |        |
| The mail goes out without [Enviar] (also 9z4 step 3's live HTML send)              |        |
| A receipt with a field the XML does not confirm is held with BR-D19 naming it      |        |
