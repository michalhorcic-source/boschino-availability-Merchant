#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import os
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import List

from export_merchant_products import (
    FIELDS,
    MAX_DROP_RATIO,
    MIN_EXPECTED_PRODUCTS,
    OUTPUT_NAME,
    OUT_DIR,
    SUMMARY_NAME,
    clean,
    flatten_product,
    google_credentials,
    list_products,
    previous_row_count,
    sha256_file,
)

PURCHASABLE_AVAILABILITY = {"in_stock", "preorder", "backorder"}


def main() -> int:
    account_id = os.getenv("GOOGLE_MERCHANT_ID", "").strip()
    language = os.getenv("GOOGLE_LANGUAGE", "cs").strip()
    feed_label = os.getenv("GOOGLE_FEED_LABEL", "CZK_105791684939").strip()
    previous_path = Path(os.getenv("PREVIOUS_FEED_PATH", f"merchant/{OUTPUT_NAME}"))

    if not account_id:
        raise RuntimeError("Missing GOOGLE_MERCHANT_ID")

    credentials = google_credentials()
    all_products = list_products(credentials, account_id)
    selected = [
        p for p in all_products
        if clean(p.get("contentLanguage")) == language
        and clean(p.get("feedLabel")) == feed_label
    ]

    rows = [flatten_product(product) for product in selected]
    availability_counts = Counter(row["availability"] for row in rows)
    purchasable = [row for row in rows if row["availability"] in PURCHASABLE_AVAILABILITY]

    invalid_required = Counter()
    valid_rows: List[dict[str, str]] = []
    seen = set()
    duplicates = []
    required = ["id", "title", "link", "image_link", "availability", "price"]

    for row in purchasable:
        missing = [field for field in required if not row[field]]
        if missing:
            invalid_required.update(missing)
            continue
        if row["id"] in seen:
            duplicates.append(row["id"])
            continue
        seen.add(row["id"])
        valid_rows.append(row)

    if duplicates:
        raise RuntimeError(f"Duplicate Merchant ids detected: {duplicates[:10]}")
    if len(valid_rows) < MIN_EXPECTED_PRODUCTS:
        raise RuntimeError(
            f"Refusing suspiciously small purchasable snapshot: {len(valid_rows)} < {MIN_EXPECTED_PRODUCTS}"
        )

    previous_count = previous_row_count(previous_path)
    if previous_count:
        minimum_allowed = int(Decimal(previous_count) * (Decimal("1") - MAX_DROP_RATIO))
        if len(valid_rows) < minimum_allowed:
            raise RuntimeError(
                f"Refusing unexpected product-count drop: previous={previous_count}, "
                f"new={len(valid_rows)}, minimum_allowed={minimum_allowed}"
            )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUT_DIR / OUTPUT_NAME
    summary_path = OUT_DIR / SUMMARY_NAME

    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=FIELDS,
            delimiter="\t",
            lineterminator="\n",
            quoting=csv.QUOTE_MINIMAL,
        )
        writer.writeheader()
        writer.writerows(valid_rows)

    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "Google Merchant API processed products",
        "account_id": account_id,
        "content_language": language,
        "feed_label": feed_label,
        "processed_products": len(all_products),
        "matching_market_products": len(selected),
        "availability_counts": dict(availability_counts),
        "purchasable_before_validation": len(purchasable),
        "skipped_invalid_required": sum(invalid_required.values()),
        "missing_required_fields": dict(invalid_required),
        "exported_products": len(valid_rows),
        "purchasable_availability": sorted(PURCHASABLE_AVAILABILITY),
        "sha256": sha256_file(output_path),
        "bytes": output_path.stat().st_size,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
