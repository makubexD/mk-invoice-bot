"""Fetchers that get an invoice's official PDF and XML (spec 03 §6, R-5.1).

Playwright and Chromium load only in ``invoicebot.fetchers.browser``, which runs as a helper
process per fetch (D82).
"""
