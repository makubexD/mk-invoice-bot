"""The browser helper package: headless Chromium for portal pages (D82).

This is the only package that imports Playwright, and only the helper process
imports this package; the rest of the app sees the helper's contract.
"""
