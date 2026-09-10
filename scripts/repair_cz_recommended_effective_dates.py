#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

from merchant_margin_labels_audit_v2 import paged_get, request_json

DATASOURCES = "https://merchantapi.googleapis.com/datasources/v1"
ACCOUNT_ID = os.getenv("CZ_RECOMMENDED_ACCOUNT_ID", "5757276720").strip()
SOURCE_DISPLAY_NAME = os.getenv("CZ_RECOMMENDED_SOURCE_DISPLAY_NAME", "Boschino CZ Recommended Price").strip()
UPSTREAM_URL = os.getenv(
    "CZ_RECOMMENDED_UPSTREAM_URL",
    "https://boschino-feed-worker.michal-horcic.workers.dev/feeds/cz-recommended-prices.tsv",
).strip()
TARGET_URL = os.getenv(
    "CZ_RECOMMENDED_TARGET_URL",
    "https://raw.githubusercontent.com/michalhorcic-source/boschino-availability-Merchant/main/merchant/BOSCHINO_CZ_RECOMMENDED_PRICE.tsv",
).strip()
OUTPUT_PATH = Path(os.getenv("CZ_RECOMMENDED_OUTPUT_PATH", "merchant/BOSCHINO_CZ_RECOMMENDED_PRICE.tsv"))
SUMMARY_PATH = Path(os.getenv("CZ_RECOMMENDED_SUMMARY_PATH", "merchant/BOSCHINO_CZ_RECOMMENDED_PRICE.summary.json"))
MIN_EXPECTED_ROWS = int(os.getenv("CZ_RECOMMENDED_MIN_EXPECTED_ROWS", "10000"))
CONTROL_ID = os.getenv(
    "CZ_RECOMMENDED_CONTROL_ID",
    "shopify_ZZ_15493147984203_56386003730763",
).strip()
PRAGUE = ZoneInfo("Europe/Prague")


def normalize_header(name: str) -> str:
    return re.sub(r"[ _]+", "_", str(name or "").strip().lower())


def canonical_columns(fieldnames: List[str]) -> Dict[str, str]:
    by_norm = {normalize_header(name): name for name in fieldnames}
    aliases = {
        "id": ["id"],
        "price": ["price"],
        "sale_price": ["sale_price", "saleprice"],
        "sale_price_effective_date": ["sale_price_effective_date", "salepriceeffectivedate"],
    }
    result: Dict[str, str] = {}
    for canonical, variants in aliases.items():
        for variant in variants:
            if variant in by_norm:
                result[canonical] = by_norm[variant]
                break
        if canonical not in result:
            raise RuntimeError(f"Missing required column {canonical!r}; header={fieldnames!r}")
    return result


def parse_money(value: str) -> Tuple[Decimal, str]:
    text = str(value or "").strip()
    match = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)\s+([A-Za-z]{3})", text)
    if not match:
        raise ValueError(f"Invalid Merchant money value: {text!r}")
    try:
        amount = Decimal(match.group(1))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid amount: {text!r}") from exc
    return amount, match.group(2).upper()


def format_money(amount: Decimal, currency: str) -> str:
    quantized = amount.quantize(Decimal("0.01"))
    return f"{quantized:.2f} {currency}"


def fresh_effective_range(now: datetime | None = None) -> str:
    local_now = (now or datetime.now(tz=PRAGUE)).astimezone(PRAGUE)
    start_day = local_now.date()
    end_day = start_day + timedelta(days=3)
    start = datetime(start_day.year, start_day.month, start_day.day, 0, 0, tzinfo=PRAGUE)
    end = datetime(end_day.year, end_day.month, end_day.day, 23, 59, tzinfo=PRAGUE)
    # Google sale_price_effective_date text-feed syntax is YYYY-MM-DDThh:mm[+/-hhmm].
    # Compute offsets independently to remain correct across Europe/Prague DST changes.
    return f"{start.strftime('%Y-%m-%dT%H:%M%z')}/{end.strftime('%Y-%m-%dT%H:%M%z')}"


def fetch_upstream() -> str:
    response = requests.get(UPSTREAM_URL, timeout=(20, 180))
    response.raise_for_status()
    text = response.content.decode("utf-8-sig")
    if not text.strip():
        raise RuntimeError("Upstream CZ Recommended feed is empty")
    return text


def prepare() -> int:
    text = fetch_upstream()
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    if not reader.fieldnames:
        raise RuntimeError("Upstream CZ Recommended feed has no header")
    columns = canonical_columns(reader.fieldnames)

    output_rows: List[Dict[str, str]] = []
    sale_rows = 0
    non_sale_rows = 0
    normalized_sale_rows = 0
    converted_non_sale_rows = 0
    invalid_rows: List[Dict[str, Any]] = []
    ids = set()
    control_row: Dict[str, str] | None = None
    effective_range = fresh_effective_range()

    for index, original in enumerate(reader, start=2):
        row = dict(original)
        offer_id = str(row.get(columns["id"], "") or "").strip()
        price_raw = str(row.get(columns["price"], "") or "").strip()
        sale_raw = str(row.get(columns["sale_price"], "") or "").strip()

        if not offer_id:
            invalid_rows.append({"line": index, "reason": "MISSING_ID"})
            continue
        if offer_id in ids:
            invalid_rows.append({"line": index, "id": offer_id, "reason": "DUPLICATE_ID"})
            continue
        ids.add(offer_id)

        try:
            price_amount, price_currency = parse_money(price_raw)
        except Exception as exc:
            invalid_rows.append({"line": index, "id": offer_id, "reason": "INVALID_PRICE", "detail": str(exc)})
            continue
        if price_amount <= 0:
            invalid_rows.append({"line": index, "id": offer_id, "reason": "NONPOSITIVE_PRICE", "value": price_raw})
            continue

        row[columns["price"]] = format_money(price_amount, price_currency)

        if sale_raw:
            try:
                sale_amount, sale_currency = parse_money(sale_raw)
            except Exception as exc:
                invalid_rows.append({"line": index, "id": offer_id, "reason": "INVALID_SALE_PRICE", "detail": str(exc)})
                continue
            if sale_amount <= 0 or sale_currency != price_currency:
                invalid_rows.append({"line": index, "id": offer_id, "reason": "INVALID_SALE_PRICE_CURRENCY_OR_AMOUNT", "value": sale_raw})
                continue

            if sale_amount < price_amount:
                row[columns["sale_price"]] = format_money(sale_amount, sale_currency)
                row[columns["sale_price_effective_date"]] = effective_range
                sale_rows += 1
                normalized_sale_rows += 1
            else:
                # Not a genuine sale. The current selling price becomes regular price;
                # sale_price and its date must both be blank.
                row[columns["price"]] = format_money(sale_amount, sale_currency)
                row[columns["sale_price"]] = ""
                row[columns["sale_price_effective_date"]] = ""
                non_sale_rows += 1
                converted_non_sale_rows += 1
        else:
            row[columns["sale_price"]] = ""
            row[columns["sale_price_effective_date"]] = ""
            non_sale_rows += 1

        output_rows.append(row)
        if offer_id == CONTROL_ID:
            control_row = {
                "id": offer_id,
                "price": row[columns["price"]],
                "sale_price": row[columns["sale_price"]],
                "sale_price_effective_date": row[columns["sale_price_effective_date"]],
            }

    if invalid_rows:
        Path("out/cz-recommended-date-repair").mkdir(parents=True, exist_ok=True)
        Path("out/cz-recommended-date-repair/invalid_rows.json").write_text(
            json.dumps(invalid_rows[:500], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        raise RuntimeError(f"Refusing invalid CZ Recommended input: {len(invalid_rows)} invalid rows")

    if len(output_rows) < MIN_EXPECTED_ROWS:
        raise RuntimeError(
            f"Refusing suspiciously small CZ Recommended feed: {len(output_rows)} < {MIN_EXPECTED_ROWS}"
        )

    if control_row is None:
        raise RuntimeError(f"Control offer {CONTROL_ID} is missing from CZ Recommended feed")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=reader.fieldnames, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(output_rows)

    summary = {
        "status": "PREPARED",
        "upstream_url": UPSTREAM_URL,
        "target_url": TARGET_URL,
        "rows": len(output_rows),
        "sale_rows": sale_rows,
        "non_sale_rows": non_sale_rows,
        "normalized_sale_rows": normalized_sale_rows,
        "converted_non_sale_rows": converted_non_sale_rows,
        "effective_range": effective_range,
        "control": control_row,
        "schema": reader.fieldnames,
    }
    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    Path("out/cz-recommended-date-repair").mkdir(parents=True, exist_ok=True)
    Path("out/cz-recommended-date-repair/prepare.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def list_sources() -> List[Dict[str, Any]]:
    return paged_get(
        f"{DATASOURCES}/accounts/{quote(ACCOUNT_ID, safe='')}/dataSources",
        "dataSources",
        100,
    )


def find_source() -> Dict[str, Any]:
    matches = [
        source
        for source in list_sources()
        if source.get("displayName") == SOURCE_DISPLAY_NAME
        and source.get("supplementalProductDataSource") is not None
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one supplemental source {SOURCE_DISPLAY_NAME!r}; found {len(matches)}")
    source = matches[0]
    if source.get("input") != "FILE":
        raise RuntimeError(f"{SOURCE_DISPLAY_NAME} is not a FILE source: {source.get('input')!r}")
    file_input = source.get("fileInput") or {}
    if file_input.get("fileInputType") != "FETCH":
        raise RuntimeError(
            f"{SOURCE_DISPLAY_NAME} is not a FETCH file source: {file_input.get('fileInputType')!r}"
        )
    return source


def latest_upload(source_name: str) -> Dict[str, Any]:
    return request_json("GET", f"{DATASOURCES}/{source_name}/fileUploads/latest")


def apply(wait_seconds: int) -> int:
    source = find_source()
    source_name = str(source["name"])
    before_uri = (((source.get("fileInput") or {}).get("fetchSettings") or {}).get("fetchUri") or "")
    before_upload = latest_upload(source_name)
    before_time = str(before_upload.get("uploadTime") or "")

    if before_uri != TARGET_URL:
        request_json(
            "PATCH",
            f"{DATASOURCES}/{source_name}?updateMask=fileInput.fetchSettings.fetchUri",
            {
                "name": source_name,
                "fileInput": {"fetchSettings": {"fetchUri": TARGET_URL}},
            },
        )

    readback = request_json("GET", f"{DATASOURCES}/{source_name}")
    readback_uri = (((readback.get("fileInput") or {}).get("fetchSettings") or {}).get("fetchUri") or "")
    if readback_uri != TARGET_URL:
        raise RuntimeError(f"Merchant datasource URI readback mismatch: {readback_uri!r}")

    request_json("POST", f"{DATASOURCES}/{source_name}:fetch", {})

    deadline = time.time() + max(0, wait_seconds)
    latest: Dict[str, Any] = {}
    saw_new_upload = False
    while True:
        latest = latest_upload(source_name)
        upload_time = str(latest.get("uploadTime") or "")
        state = str(latest.get("processingState") or "")
        if upload_time and upload_time != before_time:
            saw_new_upload = True
        print(
            json.dumps(
                {
                    "uploadTime": upload_time,
                    "processingState": state,
                    "itemsTotal": latest.get("itemsTotal"),
                    "itemsCreated": latest.get("itemsCreated"),
                    "itemsUpdated": latest.get("itemsUpdated"),
                    "sawNewUpload": saw_new_upload,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if saw_new_upload and state in {"SUCCEEDED", "FAILED"}:
            break
        if time.time() >= deadline:
            break
        time.sleep(30)

    issues = latest.get("issues") or []
    error_issues = [issue for issue in issues if str(issue.get("severity") or "").upper() == "ERROR"]
    state = str(latest.get("processingState") or "")
    status = "FETCH_PENDING"
    if saw_new_upload and state == "SUCCEEDED" and not error_issues:
        status = "SUCCEEDED"
    elif saw_new_upload and (state == "FAILED" or error_issues):
        status = "FAILED"

    summary = {
        "status": status,
        "account_id": ACCOUNT_ID,
        "source_display_name": SOURCE_DISPLAY_NAME,
        "source_name": source_name,
        "old_fetch_uri": before_uri,
        "new_fetch_uri": readback_uri,
        "fetch_requested": True,
        "saw_new_upload": saw_new_upload,
        "latest_upload": latest,
        "error_issue_count": len(error_issues),
    }
    Path("out/cz-recommended-date-repair").mkdir(parents=True, exist_ok=True)
    Path("out/cz-recommended-date-repair/apply.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if status == "FAILED":
        return 7
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["prepare", "apply"])
    parser.add_argument("--wait-seconds", type=int, default=1200)
    args = parser.parse_args()
    if args.mode == "prepare":
        return prepare()
    return apply(args.wait_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
