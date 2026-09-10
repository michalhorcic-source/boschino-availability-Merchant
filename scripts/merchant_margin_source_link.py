#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote

from merchant_margin_labels_audit_v2 import paged_get, request_json

DATASOURCES = "https://merchantapi.googleapis.com/datasources/v1"
OUT = Path("out/margin-source-link")
ACCOUNT_ID = os.getenv("MARGIN_CZ_ACCOUNT_ID", "5757276720").strip()
MARGIN_SOURCE_DISPLAY_NAME = os.getenv("MARGIN_SOURCE_DISPLAY_NAME", "BOSCHINO_MARGIN_LABELS_API").strip()
PRIMARY_SOURCE_DISPLAY_NAME = os.getenv("MARGIN_PRIMARY_SOURCE_DISPLAY_NAME_CZ", "Shopify App API").strip()


def list_data_sources(account_id: str) -> List[Dict[str, Any]]:
    return paged_get(
        f"{DATASOURCES}/accounts/{quote(account_id, safe='')}/dataSources",
        "dataSources",
        100,
    )


def require_one(sources: List[Dict[str, Any]], display_name: str, type_key: str) -> Dict[str, Any]:
    matches = [
        source
        for source in sources
        if source.get("displayName") == display_name and source.get(type_key) is not None
    ]
    if len(matches) != 1:
        compact = [
            {
                "name": source.get("name"),
                "displayName": source.get("displayName"),
                "input": source.get("input"),
                "type": next(
                    (
                        key
                        for key in (
                            "primaryProductDataSource",
                            "supplementalProductDataSource",
                            "localInventoryDataSource",
                            "regionalInventoryDataSource",
                        )
                        if source.get(key) is not None
                    ),
                    "other",
                ),
            }
            for source in sources
        ]
        raise RuntimeError(
            f"Expected exactly one {type_key} named {display_name!r}, found {len(matches)}. "
            + json.dumps(compact, ensure_ascii=False)
        )
    return matches[0]


def is_self_reference(ref: Dict[str, Any], primary_name: str) -> bool:
    return ref.get("self") is True or ref.get("primaryDataSourceName") == primary_name


def desired_rule(primary: Dict[str, Any], margin_source_name: str) -> List[Dict[str, Any]]:
    primary_name = str(primary.get("name") or "")
    current = (
        (primary.get("primaryProductDataSource") or {})
        .get("defaultRule", {})
        .get("takeFromDataSources", [])
        or []
    )

    # Keep every existing rule reference and its order, except the Margin source,
    # which is moved to the front so custom_label_3 is authoritative even if a
    # primary source happens to contain an older value (for example has_product_type).
    kept = [
        dict(ref)
        for ref in current
        if ref.get("supplementalDataSourceName") != margin_source_name
    ]

    self_refs = [ref for ref in kept if is_self_reference(ref, primary_name)]
    if len(self_refs) > 1:
        raise RuntimeError(
            "Primary defaultRule contains more than one self/primary reference; refusing destructive rewrite: "
            + json.dumps(current, ensure_ascii=False)
        )
    if not self_refs:
        kept.append({"self": True})

    return [{"supplementalDataSourceName": margin_source_name}, *kept]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    sources = list_data_sources(ACCOUNT_ID)
    margin = require_one(sources, MARGIN_SOURCE_DISPLAY_NAME, "supplementalProductDataSource")
    primary = require_one(sources, PRIMARY_SOURCE_DISPLAY_NAME, "primaryProductDataSource")

    primary_name = str(primary["name"])
    margin_name = str(margin["name"])
    before = (
        (primary.get("primaryProductDataSource") or {})
        .get("defaultRule", {})
        .get("takeFromDataSources", [])
        or []
    )
    wanted = desired_rule(primary, margin_name)
    changed = before != wanted

    if changed:
        url = f"{DATASOURCES}/{primary_name}?updateMask=primaryProductDataSource.defaultRule"
        body = {
            "name": primary_name,
            "primaryProductDataSource": {
                "defaultRule": {
                    "takeFromDataSources": wanted,
                }
            },
        }
        request_json("PATCH", url, body)

    # Read back from Merchant API and fail closed unless the exact primary rule is present.
    primary_after = request_json("GET", f"{DATASOURCES}/{primary_name}")
    after = (
        (primary_after.get("primaryProductDataSource") or {})
        .get("defaultRule", {})
        .get("takeFromDataSources", [])
        or []
    )

    margin_refs = [
        index
        for index, ref in enumerate(after)
        if ref.get("supplementalDataSourceName") == margin_name
    ]
    self_count = sum(1 for ref in after if is_self_reference(ref, primary_name))

    ok = margin_refs == [0] and self_count == 1
    summary = {
        "status": "LINKED" if ok else "INVALID_RULE",
        "account_id": ACCOUNT_ID,
        "primary_display_name": PRIMARY_SOURCE_DISPLAY_NAME,
        "primary_name": primary_name,
        "margin_source_display_name": MARGIN_SOURCE_DISPLAY_NAME,
        "margin_source_name": margin_name,
        "changed": changed,
        "before_take_from_data_sources": before,
        "after_take_from_data_sources": after,
        "margin_reference_indexes": margin_refs,
        "self_reference_count": self_count,
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if not ok:
        raise RuntimeError("CZ Margin supplemental source is not first in Shopify App API defaultRule after patch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
