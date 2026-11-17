#!/usr/bin/env python3
"""Report a golden set's R-2.9 accuracy without exposing its invoices (R-2.9, R-9.16, D54).

Usage: ``accuracy_report.py --set DIR [--write [MD]]``. Each image or PDF in ``DIR`` is read
with the same helper argv the pipeline uses (D78, D122), scored against its ``.json`` expected
file with ``invoicebot.extract.accuracy``, and printed as the QR rate, the OCR-only rate, the
per-field rates and a pass/fail verdict. ``--write`` also writes those numbers, the date and
the set size to a markdown file (default ``specs/spikes/accuracy.md``). The report holds rates,
counts, field names and the verdict only: no field value, RUC or file name is ever printed
(R-9.16, D54).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from invoicebot.channels.base import AttachmentKind
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config
from invoicebot.extract.accuracy import Expected, Score, Summary, load_expected, score, summarize
from invoicebot.extract.fields import Reading, reading_from_json
from invoicebot.extract.merge import same_value
from invoicebot.pipeline.extract import helper_argv

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "config" / "example.yaml"
RULES_PATH = REPO_ROOT / "config" / "parsing_rules.yaml"
DEFAULT_REPORT_PATH = REPO_ROOT / "specs" / "spikes" / "accuracy.md"
#: The document suffixes a set holds and the helper kind each one is read as (D122).
MEDIA_KINDS: dict[str, AttachmentKind] = {".jpg": "photo", ".png": "photo", ".pdf": "pdf"}


@dataclass(frozen=True)
class _Pair:
    """One set member: its document, its expected file and how the helper reads it."""

    media: Path
    expected: Path
    kind: AttachmentKind


@dataclass(frozen=True)
class FieldRate:
    """One field's accuracy over the pairs whose expected file holds it (R-2.9)."""

    name: str
    right: int
    total: int

    @property
    def rate(self) -> float:
        """Return the share of pairs that read this field right."""
        return self.right / self.total


@dataclass(frozen=True)
class Report:
    """A whole set's accuracy: its size, its R-2.9 rates and its per-field rates."""

    pairs: int
    summary: Summary
    fields: tuple[FieldRate, ...]


def _pairs(set_dir: Path) -> list[_Pair]:
    """Return each image or PDF in ``set_dir`` with its expected file, in file order."""
    pairs: list[_Pair] = []
    for media in sorted(set_dir.iterdir()):
        kind = MEDIA_KINDS.get(media.suffix.lower())
        if kind is None:
            continue
        expected = media.with_suffix(".json")
        if not expected.is_file():
            raise ValueError("a set member has no matching .json expected file")
        pairs.append(_Pair(media=media, expected=expected, kind=kind))
    return pairs


def _read_media(pair: _Pair, cfg: Config) -> Reading | None:
    """Run the extract helper over ``pair`` and return its reading, if it has one (R-2.9)."""
    completed = subprocess.run(  # noqa: S603 - fixed argv built by the pipeline, never a shell
        helper_argv(str(pair.media), pair.kind, cfg.extraction, cfg.limits.media, RULES_PATH),
        capture_output=True,
        check=True,
        text=True,
        timeout=cfg.extraction.helper_timeout_s,
    )
    payload = json.loads(completed.stdout)
    return None if payload["reading"] is None else reading_from_json(payload["reading"])


def _field_hits(reading: Reading | None, expected: Expected) -> dict[str, tuple[int, int]]:
    """Return each expected field's right and total count for this pair (R-2.9)."""
    hits: dict[str, tuple[int, int]] = {}
    for name, expected_value in expected.fields.items():
        actual = None if reading is None else reading.value(name)
        right = int(actual is not None and same_value(name, actual, expected_value))
        hits[name] = (right, 1)
    return hits


def build_report(set_dir: Path, cfg: Config) -> Report:
    """Score every pair in ``set_dir`` and return the set's aggregate report (R-2.9)."""
    pairs = _pairs(set_dir)
    scores: list[Score] = []
    counts: dict[str, tuple[int, int]] = {}
    for pair in pairs:
        expected = load_expected(pair.expected)
        reading = _read_media(pair, cfg)
        scores.append(score(reading, expected))
        for name, (right, total) in _field_hits(reading, expected).items():
            seen_right, seen_total = counts.get(name, (0, 0))
            counts[name] = (seen_right + right, seen_total + total)
    fields = tuple(
        FieldRate(name=name, right=counts[name][0], total=counts[name][1]) for name in sorted(counts)
    )
    return Report(pairs=len(pairs), summary=summarize(scores), fields=fields)


def _percent(rate: float) -> str:
    """Return ``rate`` as a one-decimal percentage."""
    return f"{rate * 100:.1f}%"


def _verdict(report: Report) -> str:
    """Return ``yes`` or ``no`` for whether the set reaches the R-2.9 targets."""
    return "yes" if report.summary.passes() else "no"


def report_lines(report: Report, today: date) -> list[str]:
    """Return the report as the lines the command prints (R-9.16, D54)."""
    lines = [
        f"accuracy report: {report.pairs} pair(s), {today.isoformat()}",
        f"qr rate: {_percent(report.summary.qr_rate)}",
        f"ocr-only required-field rate: {_percent(report.summary.required_rate)}",
        "per-field rates:",
    ]
    lines.extend(
        f"  {field.name}: {_percent(field.rate)} ({field.right}/{field.total})" for field in report.fields
    )
    lines.append(f"pass: {_verdict(report)}")
    return lines


def render_markdown(report: Report, today: date) -> str:
    """Return the report as the markdown file ``--write`` stores (R-9.16, D54)."""
    lines = [
        "# Accuracy report",
        "",
        f"- Date: {today.isoformat()}",
        f"- Set size: {report.pairs} pair(s)",
        f"- QR rate: {_percent(report.summary.qr_rate)}",
        f"- OCR-only required-field rate: {_percent(report.summary.required_rate)}",
        f"- Pass: {_verdict(report)}",
        "",
        "| field | right | total | rate |",
        "|---|---:|---:|---:|",
    ]
    lines.extend(
        f"| {field.name} | {field.right} | {field.total} | {_percent(field.rate)} |"
        for field in report.fields
    )
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    """Return the command-line parser for the accuracy report."""
    parser = argparse.ArgumentParser(description="Report a golden set's R-2.9 accuracy.")
    parser.add_argument("--set", type=Path, required=True, help="directory of image/PDF and .json pairs")
    parser.add_argument(
        "--write",
        type=Path,
        nargs="?",
        const=DEFAULT_REPORT_PATH,
        default=None,
        help="also write the numbers as markdown (default: specs/spikes/accuracy.md)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Print the report and return 0 when the set passes, 1 otherwise (R-2.9)."""
    args = _parser().parse_args(argv)
    try:
        report = build_report(args.set, load_config(CONFIG_PATH))
    except subprocess.SubprocessError:
        print("error: the extract helper failed for one pair", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        # The type only: an OSError names the file and a pydantic error echoes the input (D54).
        print(f"error: {type(exc).__name__} while reading the set", file=sys.stderr)
        return 1
    today = datetime.now(UTC).astimezone().date()
    print("\n".join(report_lines(report, today)))
    if args.write is not None:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(render_markdown(report, today), encoding="utf-8")
    return 0 if report.summary.passes() else 1


if __name__ == "__main__":
    sys.exit(main())
