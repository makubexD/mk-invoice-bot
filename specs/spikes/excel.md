# Spike: filling the reimbursement Excel (2026-11-12)

A copy of one of the owner's 2026 files was loaded and saved with openpyxl after writing five cells, then compared with the original package.

| Check | Result |
|---|---|
| Drawing part | 6.4 KB → 2.3 KB: the 3 drawn shapes are gone; the 2 images are still listed |
| Total cell (`=MIN(I16, 50)`) | Saved with an empty cached value; Excel recalculates on open (`fullCalcOnLoad`), a preview that doesn't recalculate shows it blank |
| Other parts | `customXml/*` (document metadata), `printerSettings` and `calcChain` are dropped |

**Decision (D71):** patch only the mapped cells inside the package and copy every other part unchanged, writing the cached values of dependent formulas too. That method is specified (R-7.4, S11.3) but not built or tested yet.
