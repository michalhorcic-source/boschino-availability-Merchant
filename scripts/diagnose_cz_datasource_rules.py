#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote

from merchant_margin_labels_audit_v2 import paged_get, request_json

ACCOUNT_ID = os.getenv("CZ_MERCHANT_ACCOUNT_ID", "5757276720").strip()
DATASOURCES = "https://merchantapi.googleapis.com/datasources/v1"
OUT = Path("out/cz-datasource-rules")
WATCH_IDS = {
    "10697609710",  # Boschino CZ Recommended Price
    "10714978275",  # dataSource reported by resolved product 12014980
}
WATCH_NAMES = {
    "Shopify App API",
    "Boschino CZ Recommended Price",
    "BOSCHINO_MARGIN_LABELS_API",
    "Boschino Ads Product Ownership",
}


def source_id(source: Dict[str, Any]) -> str:
    return str(source.get("name") or "").rstrip("/").split("/")[-1]


def source_type(source: Dict[str, Any]) -> str:
    for key in (
        "primaryProductDataSource",
        "supplementalProductDataSource",
        "localInventoryDataSource",
        "regionalInventoryDataSource",
    ):
        if source.get(key) is not None:
            return key
    return "other"


def summarize(source: Dict[str, Any]) -> Dict[str, Any]:
    primary = source.get("primaryProductDataSource") or {}
    file_input = source.get("fileInput") or {}
    fetch_settings = file_input.get("fetchSettings") or {}
    return {
        "id": source_id(source),
        "name": source.get("name"),
        "displayName": source.get("displayName"),
        "input": source.get("input"),
        "type": source_type(source),
        "fetchUri": fetch_settings.get("fetchUri"),
        "defaultRule": primary.get("defaultRule"),
        "primaryProductDataSource": primary or None,
        "topLevelKeys": sorted(source.keys()),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    listed = paged_get(
        f"{DATASOURCES}/accounts/{quote(ACCOUNT_ID, safe='')}/dataSources",
        "dataSources",
        100,
    )

    full_sources: List[Dict[str, Any]] = []
    for item in listed:
        name = str(item.get("name") or "")
        if not name:
            continue
        full_sources.append(request_json("GET", f"{DATASOURCES}/{name}"))

    compact = [summarize(source) for source in full_sources]
    watched = [
        row
        for row in compact
        if row["id"] in WATCH_IDS or row.get("displayName") in WATCH_NAMES
    ]

    # Recursively find any keys whose names suggest rule/attribute/sale/effective-date
    # configuration. This is intentionally schema-agnostic so new Merchant API fields
    # are not silently missed.
    interesting: List[Dict[str, Any]] = []

    def walk(value: Any, path: str, source: Dict[str, Any]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else key
                low = key.lower()
                if any(token in low for token in ("rule", "attribute", "sale", "effective", "price")):
                    interesting.append(
                        {
                            "sourceId": source_id(source),
                            "displayName": source.get("displayName"),
                            "path": child_path,
                            "value": child,
                        }
                    )
                walk(child, child_path, source)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]", source)

    for source in full_sources:
        walk(source, "", source)

    payload = {
        "accountId": ACCOUNT_ID,
        "sourceCount": len(full_sources),
        "watched": watched,
        "sources": compact,
        "interestingRuleLikeFields": interesting,
        "rawSources": full_sources,
    }
    (OUT / "datasources.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("=== WATCHED DATASOURCES ===")
    print(json.dumps(watched, ensure_ascii=False, indent=2))
    print("=== RULE / ATTRIBUTE / SALE / PRICE FIELDS ===")
    print(json.dumps(interesting, ensure_ascii=False, indent=2))

    resolved = [row for row in compact if row["id"] == "10714978275"]
    if len(resolved) != 1:
        raise RuntimeError(
            f"Expected exactly one datasource 10714978275 reported by product 12014980; found {len(resolved)}"
        )

    print("=== RESOLVED PRODUCT DATASOURCE 10714978275 ===")
    print(json.dumps(resolved[0], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
