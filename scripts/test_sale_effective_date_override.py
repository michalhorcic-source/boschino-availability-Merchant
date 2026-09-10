#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import quote

from merchant_margin_labels_audit_v2 import paged_get, request_json

ACCOUNT_ID = os.getenv("CZ_MERCHANT_ACCOUNT_ID", "5757276720").strip()
PRIMARY_DISPLAY_NAME = os.getenv("CZ_PRIMARY_DISPLAY_NAME", "Shopify App API").strip()
TEST_SOURCE_DISPLAY_NAME = os.getenv("CZ_TEST_SOURCE_DISPLAY_NAME", "BOSCHINO_CZ_PRICE_API_TEST").strip()
OFFER_ID = os.getenv("TEST_OFFER_ID", "shopify_ZZ_15493147984203_56386003730763").strip()
LANGUAGE = "cs"
FEED_LABEL = "CZK_105791684939"
OUT = Path("out/sale-effective-date-test")

DATASOURCES = "https://merchantapi.googleapis.com/datasources/v1"
PRODUCT_INPUTS = "https://merchantapi.googleapis.com/products/v1"
PRODUCTS = "https://merchantapi.googleapis.com/products/v1"

DESIRED = {
    "startTime": "2026-09-09T22:00:00Z",
    "endTime": "2026-09-13T21:59:00Z",
}


def resolved_product_name() -> str:
    product_id = f"{LANGUAGE}~{FEED_LABEL}~{OFFER_ID}"
    return f"accounts/{ACCOUNT_ID}/products/{product_id}"


def list_sources():
    return paged_get(
        f"{DATASOURCES}/accounts/{quote(ACCOUNT_ID, safe='')}/dataSources",
        "dataSources",
        100,
    )


def ensure_api_source():
    sources = list_sources()
    matches = [
        s for s in sources
        if s.get("displayName") == TEST_SOURCE_DISPLAY_NAME
        and s.get("supplementalProductDataSource") is not None
    ]
    if len(matches) > 1:
        raise RuntimeError(f"Multiple API test sources named {TEST_SOURCE_DISPLAY_NAME}")
    if matches:
        source = matches[0]
        if source.get("input") != "API":
            raise RuntimeError(f"Existing {TEST_SOURCE_DISPLAY_NAME} is not API input")
        return source
    return request_json(
        "POST",
        f"{DATASOURCES}/accounts/{quote(ACCOUNT_ID, safe='')}/dataSources",
        {"displayName": TEST_SOURCE_DISPLAY_NAME, "supplementalProductDataSource": {}},
    )


def get_primary():
    matches = [
        s for s in list_sources()
        if s.get("displayName") == PRIMARY_DISPLAY_NAME
        and s.get("primaryProductDataSource") is not None
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one primary {PRIMARY_DISPLAY_NAME}, found {len(matches)}")
    return request_json("GET", f"{DATASOURCES}/{matches[0]['name']}")


def link_test_source_first(primary, test_source_name):
    primary_name = primary["name"]
    current = (
        (primary.get("primaryProductDataSource") or {})
        .get("defaultRule", {})
        .get("takeFromDataSources", [])
        or []
    )
    kept = [
        dict(ref) for ref in current
        if ref.get("supplementalDataSourceName") != test_source_name
    ]
    wanted = [{"supplementalDataSourceName": test_source_name}, *kept]
    if wanted != current:
        request_json(
            "PATCH",
            f"{DATASOURCES}/{primary_name}?updateMask=primaryProductDataSource.defaultRule",
            {
                "name": primary_name,
                "primaryProductDataSource": {
                    "defaultRule": {"takeFromDataSources": wanted}
                },
            },
        )
    after = request_json("GET", f"{DATASOURCES}/{primary_name}")
    refs = (
        (after.get("primaryProductDataSource") or {})
        .get("defaultRule", {})
        .get("takeFromDataSources", [])
        or []
    )
    if not refs or refs[0].get("supplementalDataSourceName") != test_source_name:
        raise RuntimeError("API test source is not first in primary defaultRule")
    return refs


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    before = request_json("GET", f"{PRODUCTS}/{resolved_product_name()}")
    before_attrs = before.get("productAttributes") or {}

    test_source = ensure_api_source()
    test_source_name = str(test_source["name"])

    body = {
        "offerId": OFFER_ID,
        "contentLanguage": LANGUAGE,
        "feedLabel": FEED_LABEL,
        "productAttributes": {
            "price": {"amountMicros": "2488000000", "currencyCode": "CZK"},
            "salePrice": {"amountMicros": "1806000000", "currencyCode": "CZK"},
            "salePriceEffectiveDate": DESIRED,
        },
    }
    insert_url = (
        f"{PRODUCT_INPUTS}/accounts/{quote(ACCOUNT_ID, safe='')}/productInputs:insert"
        f"?dataSource={quote(test_source_name, safe='')}"
    )
    inserted = request_json("POST", insert_url, body)

    primary = get_primary()
    rule_after = link_test_source_first(primary, test_source_name)

    polls = []
    matched = False
    final = None
    for attempt in range(1, 17):
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
        "dataSource": test_source_name,
        "dataSourceDisplayName": TEST_SOURCE_DISPLAY_NAME,
        "offerId": OFFER_ID,
        "desiredSalePriceEffectiveDate": DESIRED,
        "before": {
            "price": before_attrs.get("price"),
            "salePrice": before_attrs.get("salePrice"),
            "salePriceEffectiveDate": before_attrs.get("salePriceEffectiveDate"),
            "lastUpdateDate": (before.get("productStatus") or {}).get("lastUpdateDate"),
        },
        "insertResponse": inserted,
        "primaryRuleAfter": rule_after,
        "polls": polls,
        "matched": matched,
        "final": final,
    }
    (OUT / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=== TEST RESULT ===")
    print(json.dumps({
        "matched": matched,
        "dataSource": test_source_name,
        "before": result["before"],
        "lastPoll": polls[-1] if polls else None,
    }, ensure_ascii=False, indent=2))

    if not matched:
        raise RuntimeError("API supplemental source did not override resolved salePriceEffectiveDate within 4 minutes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
