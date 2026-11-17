#!/usr/bin/env python3
"""Draw synthetic Peruvian receipts with their D127 expected JSON (S3.9, D54, D127).

Usage: ``make_golden.py --seed N --count K --out DIR``. Each receipt is a canvas of
``cv2.putText`` labels in the layout ``config/parsing_rules.yaml`` reads, an optional SUNAT
QR drawn by zxing-cpp, and a mild photo distortion. The same seed writes byte-identical
files. RUCs come only from ``tests/synthetic_rucs.txt`` (R-9.17), so a generated set passes
``scripts/check_fixtures.py`` once that allowlist sits beside it.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import cv2
import numpy as np
import zxingcpp

from invoicebot.extract.validate import is_valid_ruc

REPO_ROOT = Path(__file__).resolve().parents[1]
RUCS_PATH = REPO_ROOT / "tests" / "synthetic_rucs.txt"
ISSUER_PREFIX = "20"
MIN_RUCS = 2

WHITE = 255
BLACK = 0
CANVAS_WIDTH = 900
CANVAS_HEIGHT = 1000
MARGIN_X = 60
MARGIN_BOTTOM = 50
FIRST_LINE_BASELINE = 90
LINE_HEIGHT = 34
FONT = cv2.FONT_HERSHEY_SIMPLEX
TITLE_SCALE = 1.2
TEXT_SCALE = 0.8
THICKNESS = 2
QR_SCALE = 7
QR_LEFT = MARGIN_X

CENT = Decimal("0.01")
IGV_RATES = (
    (Decimal("0.18"), "18"),
    (Decimal("0.10"), "10"),
    (Decimal("0.105"), "10.5"),
)
SERVICE_RATE = Decimal("0.10")
ITEM_DESCRIPTIONS = (
    "SERVICIO DE SOPORTE",
    "LICENCIA MENSUAL",
    "MANTENIMIENTO",
    "CONSULTORIA",
    "HOSTING",
)
MIN_ITEMS = 1
MAX_ITEMS = 4
MAX_QUANTITY = 5
MIN_UNIT_CENTS = 500
MAX_UNIT_CENTS = 500_000
FIRST_DAY = date(2024, 1, 1)
DAY_RANGE = 900
TIPO_SERIES = (("01", "F"), ("03", "B"))
BUYER_DOC_TYPE = "6"

SERVICE_CHARGE_PROBABILITY = 0.5
QR_PROBABILITY = 0.5
JPEG_QUALITY = 88
MAX_ANGLE = 1.2
BLUR_KERNEL = (3, 3)
BLUR_SIGMA = 0.6
NOISE_STD = 3.0


@dataclass(frozen=True)
class _Item:
    """One receipt line: its description, quantity and unit price."""

    description: str
    quantity: int
    unit_price: Decimal

    @property
    def amount(self) -> Decimal:
        """Return ``quantity * unit_price``, exact to two decimals."""
        return self.unit_price * self.quantity


@dataclass(frozen=True)
class _Receipt:
    """Every value a generated receipt prints and its expected JSON carries (D127)."""

    ruc: str
    buyer_ruc: str
    tipo: str
    serie: str
    numero: str
    fecha: date
    items: tuple[_Item, ...]
    igv_label: str
    base: Decimal
    igv: Decimal
    service_charge: Decimal
    has_service_charge: bool
    total: Decimal
    has_qr: bool


def _load_synthetic_rucs(path: Path) -> tuple[str, ...]:
    """Return the valid ``20``-prefixed RUCs listed in ``path`` (R-9.17)."""
    values: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.split("#", 1)[0].strip()
        if value.startswith(ISSUER_PREFIX) and is_valid_ruc(value):
            values.append(value)
    if len(values) < MIN_RUCS:
        raise ValueError(f"{path} needs at least {MIN_RUCS} valid synthetic RUCs")
    return tuple(values)


def _money(amount: Decimal) -> Decimal:
    """Return ``amount`` rounded to cents, half up."""
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def _format(amount: Decimal) -> str:
    """Return ``amount`` as the two-decimal string every amount uses."""
    return f"{amount:.2f}"


def _pick_rucs(rng: random.Random, rucs: tuple[str, ...]) -> tuple[str, str]:
    """Return a distinct issuer and buyer RUC drawn from ``rucs``."""
    issuer = rng.choice(rucs)
    others = tuple(value for value in rucs if value != issuer)
    return issuer, rng.choice(others)


def _make_item(rng: random.Random) -> _Item:
    """Return one random receipt line."""
    cents = rng.randrange(MIN_UNIT_CENTS, MAX_UNIT_CENTS)
    return _Item(
        description=rng.choice(ITEM_DESCRIPTIONS),
        quantity=rng.randint(1, MAX_QUANTITY),
        unit_price=Decimal(cents) * CENT,
    )


def _make_items(rng: random.Random) -> tuple[_Item, ...]:
    """Return between :data:`MIN_ITEMS` and :data:`MAX_ITEMS` random lines."""
    count = rng.randint(MIN_ITEMS, MAX_ITEMS)
    return tuple(_make_item(rng) for _ in range(count))


def _amounts(
    items: tuple[_Item, ...], igv_rate: Decimal, has_service_charge: bool
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Return the base, IGV, service charge and total, each rounded to cents."""
    base = _money(sum((item.amount for item in items), Decimal("0")))
    igv = _money(base * igv_rate)
    service_charge = _money(base * SERVICE_RATE) if has_service_charge else Decimal("0")
    return base, igv, service_charge, base + igv + service_charge


def _make_receipt(rng: random.Random, rucs: tuple[str, ...]) -> _Receipt:
    """Return one random receipt whose values satisfy D127's arithmetic."""
    ruc, buyer_ruc = _pick_rucs(rng, rucs)
    tipo, serie_prefix = rng.choice(TIPO_SERIES)
    items = _make_items(rng)
    igv_rate, igv_label = rng.choice(IGV_RATES)
    has_service_charge = rng.random() < SERVICE_CHARGE_PROBABILITY
    base, igv, service_charge, total = _amounts(items, igv_rate, has_service_charge)
    return _Receipt(
        ruc=ruc,
        buyer_ruc=buyer_ruc,
        tipo=tipo,
        serie=f"{serie_prefix}{rng.randrange(1, 1000):03d}",
        numero=str(rng.randrange(1, 100_000)),
        fecha=FIRST_DAY + timedelta(days=rng.randrange(DAY_RANGE)),
        items=items,
        igv_label=igv_label,
        base=base,
        igv=igv,
        service_charge=service_charge,
        has_service_charge=has_service_charge,
        total=total,
        has_qr=rng.random() < QR_PROBABILITY,
    )


def _item_lines(items: tuple[_Item, ...]) -> list[str]:
    """Return the printed line for each receipt item."""
    return [
        f"{index}. {item.description} x{item.quantity} S/ {_format(item.unit_price)}"
        f" = S/ {_format(item.amount)}"
        for index, item in enumerate(items, start=1)
    ]


def _total_lines(receipt: _Receipt) -> list[str]:
    """Return the printed base, IGV, optional service charge and total lines."""
    lines = [
        f"OP. GRAVADAS S/ {_format(receipt.base)}",
        f"IGV ({receipt.igv_label}%) S/ {_format(receipt.igv)}",
    ]
    if receipt.has_service_charge:
        lines.append(f"RECARGO S/ {_format(receipt.service_charge)}")
    lines.append(f"IMPORTE TOTAL S/ {_format(receipt.total)}")
    return lines


def _receipt_lines(receipt: _Receipt) -> list[tuple[str, float]]:
    """Return every text line in drawing order with its font scale.

    The title and the serie-numero print large, as real receipts print them; at body size
    the blur and noise make OCR drop or misread the serie too often for a stable set.
    """
    title = "FACTURA ELECTRONICA" if receipt.tipo == "01" else "BOLETA DE VENTA ELECTRONICA"
    document_id = f"{receipt.serie}-{receipt.numero}"
    head = [
        title,
        f"RUC {receipt.ruc}",
        f"CLIENTE: RUC {receipt.buyer_ruc}",
        f"FECHA {receipt.fecha.isoformat()}",
        document_id,
        "",
    ]
    body = [*_item_lines(receipt.items), "", *_total_lines(receipt)]
    headlines = {title, document_id}
    return [(text, TITLE_SCALE if text in headlines else TEXT_SCALE) for text in [*head, *body]]


def _draw_text(lines: list[tuple[str, float]]) -> np.ndarray:
    """Return a white canvas with ``lines`` drawn top to bottom."""
    canvas = np.full((CANVAS_HEIGHT, CANVAS_WIDTH), WHITE, dtype=np.uint8)
    baseline = FIRST_LINE_BASELINE
    for text, scale in lines:
        if text:
            cv2.putText(canvas, text, (MARGIN_X, baseline), FONT, scale, BLACK, THICKNESS, cv2.LINE_AA)
        baseline += LINE_HEIGHT
    return canvas


def _qr_payload(receipt: _Receipt) -> str:
    """Return the SUNAT QR payload carrying the receipt's match fields (R-2.2)."""
    return "|".join(
        (
            receipt.ruc,
            receipt.tipo,
            receipt.serie,
            receipt.numero,
            _format(receipt.igv),
            _format(receipt.total),
            receipt.fecha.isoformat(),
            BUYER_DOC_TYPE,
            receipt.buyer_ruc,
        )
    )


def _add_qr(canvas: np.ndarray, payload: str) -> np.ndarray:
    """Paste the zxing-cpp QR for ``payload`` at the bottom of ``canvas``."""
    code = np.array(zxingcpp.create_barcode(payload, zxingcpp.BarcodeFormat.QRCode).to_image())
    code = cv2.resize(code, None, fx=QR_SCALE, fy=QR_SCALE, interpolation=cv2.INTER_NEAREST)
    top = CANVAS_HEIGHT - code.shape[0] - MARGIN_BOTTOM
    canvas[top : top + code.shape[0], QR_LEFT : QR_LEFT + code.shape[1]] = code
    return canvas


def _rotate(image: np.ndarray, angle: float) -> np.ndarray:
    """Return ``image`` turned by ``angle`` degrees on a white background."""
    height, width = image.shape
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
    return cv2.warpAffine(image, matrix, (width, height), flags=cv2.INTER_LINEAR, borderValue=WHITE)


def _distort(image: np.ndarray, angle: float, noise_seed: int) -> np.ndarray:
    """Return ``image`` mildly rotated, blurred and noised, as a phone photo would be."""
    rotated = _rotate(image, angle)
    blurred = cv2.GaussianBlur(rotated, BLUR_KERNEL, BLUR_SIGMA)
    noise = np.random.default_rng(noise_seed).normal(0.0, NOISE_STD, blurred.shape)
    return np.clip(blurred.astype(np.float64) + noise, 0, WHITE).astype(np.uint8)


def _photo(receipt: _Receipt, rng: random.Random) -> np.ndarray:
    """Return the finished, distorted JPEG-ready image for ``receipt``."""
    canvas = _draw_text(_receipt_lines(receipt))
    if receipt.has_qr:
        canvas = _add_qr(canvas, _qr_payload(receipt))
    return _distort(canvas, rng.uniform(-MAX_ANGLE, MAX_ANGLE), rng.getrandbits(32))


def _expected(receipt: _Receipt) -> dict[str, object]:
    """Return the D127 expected object for ``receipt``."""
    fields = {
        "ruc_emisor": receipt.ruc,
        "tipo": receipt.tipo,
        "serie": receipt.serie,
        "numero": receipt.numero,
        "fecha_emision": receipt.fecha.isoformat(),
        "base": _format(receipt.base),
        "igv": _format(receipt.igv),
        "total": _format(receipt.total),
        "num_doc_adq": receipt.buyer_ruc,
    }
    if receipt.has_service_charge:
        fields["service_charge"] = _format(receipt.service_charge)
    return {"has_qr": receipt.has_qr, "fields": fields}


def _write_pair(out: Path, name: str, receipt: _Receipt, rng: random.Random) -> None:
    """Write ``<name>.jpg`` and its D127 ``<name>.json`` into ``out``."""
    image_path = out / f"{name}.jpg"
    written = cv2.imwrite(str(image_path), _photo(receipt, rng), [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not written:
        raise OSError(f"could not write {image_path}")
    expected_path = out / f"{name}.json"
    expected_path.write_text(
        json.dumps(_expected(receipt), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def generate(seed: int, count: int, out: Path) -> list[Path]:
    """Write ``count`` receipt/JSON pairs seeded with ``seed`` into ``out``."""
    if count < 1:
        raise ValueError("count must be at least 1")
    rucs = _load_synthetic_rucs(RUCS_PATH)
    rng = random.Random(seed)  # noqa: S311 - reproducible synthetic data, never a secret
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for index in range(count):
        name = f"receipt-{index:03d}"
        _write_pair(out, name, _make_receipt(rng, rucs), rng)
        written.append(out / f"{name}.jpg")
    return written


def _parser() -> argparse.ArgumentParser:
    """Return the command-line parser for the generator."""
    parser = argparse.ArgumentParser(description="Draw synthetic receipts and their expected JSON.")
    parser.add_argument("--seed", type=int, required=True, help="seed that makes the output reproducible")
    parser.add_argument("--count", type=int, required=True, help="number of receipt pairs to write")
    parser.add_argument("--out", type=Path, required=True, help="directory for the image/JSON pairs")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Write the requested set and return 0, or 1 when it cannot be drawn."""
    args = _parser().parse_args(argv)
    try:
        written = generate(args.seed, args.count, args.out)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {len(written)} receipt(s) to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
