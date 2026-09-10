#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import quote

from merchant_margin_labels_audit_v2 import request_json

ACCOUNT_ID = os.getenv("CZ_MERCHANT_ACCOUNT_ID", "5757276720").strip()
DATA_SOURCE = os.getenv("CZ_RECOMMENDED_DATA_SOURCE", "accounts/5757276720/dataSources/10697609710").strip()
OFFER_ID = os.getenv("TEST_OFFER_ID", "shopify_ZZ_15493147984203_56386003730763").strip()
LANGUAGE = "cs"
FEED_LABEL = "CZK_105791684939"
OUT = Path("out/sale-effective-date-test")

PRODUCT_INPUTS = "https://merchantapi.googleapis.com/products/v1"
PRODUCTS = "https://merchantapi.googleapis.com/products/v1"

DESIRED = {
    "startTime": "2026-09-09T22:00:00Z",  # 2026-09-10 00:00 Europe/Prague
    "endTime": "2026-09-13T21:59:00Z",    # 2026-09-13 23:59 Europe/Prague
}


def resolved_product_name() -> str:
    product_id = f"{LANGUAGE}~{FEED_LABEL}~{OFFER_ID}"
    return f"accounts/{ACCOUNT_ID}/products/{product_id}"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    before = request_json("GET", f"{PRODUCTS}/{resolved_product_name()}")
    before_attrs = before.get("productAttributes") or {}

    body = {
        "offerId": OFFER_ID,
        "contentLanguage": LANGUAGE,
        "feedLabel": FEED_LABEL,
        "productAttributes": {
            "price": {
                "amountMicros": "2488000000",
                "currencyCode": "CZK",
            },
            "salePrice": {
                "amountMicros": "1806000000",
                "currencyCode": "CZK",
            },
            "salePriceEffectiveDate": DESIRED,
        },
    }

    insert_url = (
        f"{PRODUCT_INPUTS}/accounts/{quote(ACCOUNT_ID, safe='')}/productInputs:insert"
        f"?dataSource={quote(DATA_SOURCE, safe='')}"
    )
    inserted = request_json("POST", insert_url, body)

    polls = []
    matched = False
    final = None
    for attempt in range(1, 13):
        time.sleep(15)
        final = request_json("GET", f"{PRODUCTS}/{resolved_product_name()}")
        attrs = final.get("productAttributes") or {}
        interval = attrs.get("salePriceEffectiveDate")
        snapshot = {
            "attempt": attempt,
            "salePriceEffectiveDate": interval,
            "price": attrs.get("price"),
            "salePrice": attrs.get("salePrice"),
            "lastUpdateDate": (final.get("productStatus") or {}).get("lastUpdateDate"),
        }
        polls.append(snapshot)
        print(json.dumps(snapshot, ensure_ascii=False), flush=True)
        if interval == DESIRED:
            matched = True
            break

    result = {
        "accountId": ACCOUNT_ID,
        "dataSource": DATA_SOURCE,
        "offerId": OFFER_ID,
        "desiredSalePriceEffectiveDate": DESIRED,
        "before": {
            "price": before_attrs.get("price"),
            "salePrice": before_attrs.get("salePrice"),
            "salePriceEffectiveDate": before_attrs.get("salePriceEffectiveDate"),
            "lastUpdateDate": (before.get("productStatus") or {}).get("lastUpdateDate"),
        },
        "insertResponse": inserted,
        "polls": polls,
        "matched": matched,
        "final": final,
    }
    (OUT / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=== TEST RESULT ===")
    print(json.dumps({
        "matched": matched,
        "before": result["before"],
        "lastPoll": polls[-1] if polls else None,
    }, ensure_ascii=False, indent=2))

    if not matched:
        raise RuntimeError("Structured salePriceEffectiveDate was not reflected in resolved product within 3 minutes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
