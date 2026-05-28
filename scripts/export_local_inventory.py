#!/usr/bin/env python3
"""Export Boschino Shopify local availability to Google Merchant Center.

Modes:
- dry-run: fetch Shopify + Merchant products, validate matching, write artifacts only.
- upload: additionally sends local inventory records to Google Content API for Shopping.

The script intentionally keeps audit files in ./out so a GitHub Actions run can be
reviewed before enabling real uploads.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import google.auth.transport.requests
from google.oauth2 import service_account
import requests


SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-10")
SHOPIFY_SHOP_DOMAIN = os.getenv("SHOPIFY_SHOP_DOMAIN", "vvircm-fz.myshopify.com")
SHOPIFY_GRAPHQL_URL = f"https://{SHOPIFY_SHOP_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"

MERCHANT_ID = os.getenv("GOOGLE_MERCHANT_ID", "")
GOOGLE_LANGUAGE = os.getenv("GOOGLE_LANGUAGE", "cs")
GOOGLE_FEED_LABEL = os.getenv("GOOGLE_FEED_LABEL", "CZ")

# Verified Google Business Profile / Merchant Center store codes.
LOCATION_TO_STORE_CODE = {
    "gid://shopify/Location/115128074571": "06275645225922442974",  # Praha 8 / Horovo namesti
    "gid://shopify/Location/115128107339": "06824451997053158379",  # Praha 10 / Francouzska
    "gid://shopify/Location/115128140107": "14326918149907693002",  # Benatky nad Jizerou
}

TEST_SKU = "8996470703070"
TEST_MERCHANT_OFFER_ID = "shopify_ZZ_15493147984203_56386003730763"

OUT_DIR = Path("out")


@dataclass
class MerchantProduct:
    content_product_id: str
    offer_id: str
    availability: str
    raw: Dict[str, Any]


@dataclass
class ShopifyVariant:
    gid: str
    product_gid: str
    sku: str
    title: str
    price: str
    product_title: str
    product_status: str
    inventory_levels: List[Dict[str, Any]]

    @property
    def product_number(self) -> str:
        return gid_number(self.product_gid)

    @property
    def variant_number(self) -> str:
        return gid_number(self.gid)

    @property
    def merchant_offer_id(self) -> str:
        return f"shopify_ZZ_{self.product_number}_{self.variant_number}"


def gid_number(gid: str) -> str:
    return gid.rsplit("/", 1)[-1] if gid else ""


def format_czk(value: Any) -> str:
    decimal_value = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{decimal_value} CZK"


def availability_for_positive_quantity(quantity: int) -> str:
    if quantity <= 0:
        return "out_of_stock"
    if quantity <= 2:
        return "limited_availability"
    return "in_stock"


def normalize_availability(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower().replace(" ", "_")


def load_shopify_token() -> str:
    token = os.getenv("SHOPIFY_ADMIN_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Missing SHOPIFY_ADMIN_TOKEN secret/env var")
    return token


def shopify_graphql(query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    token = load_shopify_token()
    response = requests.post(
        SHOPIFY_GRAPHQL_URL,
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
        json={"query": query, "variables": variables or {}},
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"], ensure_ascii=False, indent=2))
    return payload["data"]


def fetch_shopify_variants() -> List[ShopifyVariant]:
    query = """
    query ProductVariantsForLocalInventory($cursor: String) {
      productVariants(first: 250, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          sku
          title
          price
          product { id title status }
          inventoryItem {
            id
            sku
            tracked
            inventoryLevels(first: 50) {
              nodes {
                location { id }
                quantities(names: ["available"]) { name quantity }
              }
            }
          }
        }
      }
    }
    """
    variants: List[ShopifyVariant] = []
    cursor: Optional[str] = None
    page = 0
    while True:
        page += 1
        data = shopify_graphql(query, {"cursor": cursor})
        connection = data["productVariants"]
        for node in connection["nodes"]:
            variants.append(
                ShopifyVariant(
                    gid=node.get("id", ""),
                    product_gid=(node.get("product") or {}).get("id", ""),
                    sku=node.get("sku") or "",
                    title=node.get("title") or "",
                    price=str(node.get("price") or "0"),
                    product_title=(node.get("product") or {}).get("title", ""),
                    product_status=(node.get("product") or {}).get("status", ""),
                    inventory_levels=((node.get("inventoryItem") or {}).get("inventoryLevels") or {}).get("nodes") or [],
                )
            )
        print(f"Fetched Shopify variants: {len(variants)}", flush=True)
        if not connection["pageInfo"].get("hasNextPage"):
            break
        cursor = connection["pageInfo"].get("endCursor")
    return variants


def google_credentials() -> service_account.Credentials:
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON secret/env var")
    info = json.loads(raw)
    scopes = ["https://www.googleapis.com/auth/content"]
    creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    request = google.auth.transport.requests.Request()
    creds.refresh(request)
    return creds


def google_headers() -> Dict[str, str]:
    creds = google_credentials()
    return {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}


def fetch_merchant_products() -> Dict[str, MerchantProduct]:
    if not MERCHANT_ID:
        raise RuntimeError("Missing GOOGLE_MERCHANT_ID secret/env var")

    headers = google_headers()
    url = f"https://shoppingcontent.googleapis.com/content/v2.1/{MERCHANT_ID}/products"
    params: Dict[str, Any] = {"maxResults": 250}
    by_offer_id: Dict[str, MerchantProduct] = {}
    count = 0

    while True:
        response = requests.get(url, headers=headers, params=params, timeout=60)
        response.raise_for_status()
        payload = response.json()
        for product in payload.get("resources", []):
            content_product_id = product.get("id", "")
            offer_id = product.get("offerId") or product.get("id", "").split(":")[-1]
            availability = normalize_availability(product.get("availability"))
            if offer_id:
                by_offer_id[offer_id] = MerchantProduct(content_product_id, offer_id, availability, product)
                count += 1
        token = payload.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token
        print(f"Fetched Merchant products: {count}", flush=True)

    print(f"Fetched Merchant products total: {count}", flush=True)
    return by_offer_id


def qty_from_level(level: Dict[str, Any]) -> int:
    quantities = level.get("quantities") or []
    for quantity in quantities:
        if quantity.get("name") == "available":
            return int(quantity.get("quantity") or 0)
    return 0


def calculate_local_rows(
    variant: ShopifyVariant,
    merchant_product: MerchantProduct,
) -> List[Dict[str, Any]]:
    qty_by_location: Dict[str, int] = {location_id: 0 for location_id in LOCATION_TO_STORE_CODE}
    for level in variant.inventory_levels:
        location_id = ((level.get("location") or {}).get("id") or "")
        if location_id in qty_by_location:
            qty_by_location[location_id] = qty_from_level(level)

    total_qty = sum(qty_by_location.values())
    global_availability = normalize_availability(merchant_product.availability)
    rows: List[Dict[str, Any]] = []

    for location_id, store_code in LOCATION_TO_STORE_CODE.items():
        local_qty = qty_by_location.get(location_id, 0)

        if local_qty > 0:
            availability = availability_for_positive_quantity(local_qty)
            pickup_sla = "same day"
        elif total_qty > 0:
            # Transfer from another Boschino location.
            availability = "in_stock"
            pickup_sla = "next day"
        elif global_availability in {"in_stock", "in stock"}:
            # Globally available, but not physically on the three mapped locations.
            availability = "in_stock"
            pickup_sla = "6-day"
        else:
            availability = "out_of_stock"
            pickup_sla = ""

        rows.append(
            {
                "id": variant.merchant_offer_id,
                "content_product_id": merchant_product.content_product_id,
                "sku": variant.sku,
                "store_code": store_code,
                "availability": availability,
                "quantity": local_qty,
                "price": format_czk(variant.price),
                "sale_price": "",
                "sale_price_effective_date": "",
                "pickup_method": "buy",
                "pickup_sla": pickup_sla,
                "pickup_cost": "0.00 CZK",
                "instore_product_location": "",
                "local_shipping_label": "",
                "product_title": variant.product_title,
                "global_availability": global_availability,
                "total_qty_across_locations": total_qty,
            }
        )
    return rows


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: Optional[List[str]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        keys: List[str] = []
        for row in rows:
            for key in row.keys():
                if key not in keys:
                    keys.append(key)
        fieldnames = keys or ["empty"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def write_tsv(path: Path, rows: List[Dict[str, Any]]) -> None:
    fieldnames = [
        "id",
        "store_code",
        "availability",
        "quantity",
        "price",
        "sale_price",
        "sale_price_effective_date",
        "pickup_method",
        "pickup_sla",
        "pickup_cost",
        "instore_product_location",
        "local_shipping_label",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def content_api_localinventory_entry(batch_id: int, row: Dict[str, Any]) -> Dict[str, Any]:
    # Content API for Shopping localinventory.batch expects a Content API product id,
    # e.g. online:cs:CZ:shopify_ZZ_...
    return {
        "batchId": batch_id,
        "merchantId": MERCHANT_ID,
        "method": "insert",
        "productId": row["content_product_id"],
        "storeCode": row["store_code"],
        "localInventory": {
            "availability": row["availability"],
            "quantity": int(row["quantity"]),
            "price": money_from_price(row["price"]),
            "pickupMethod": row["pickup_method"],
            "pickupSla": row["pickup_sla"],
            "pickupCost": money_from_price(row["pickup_cost"]),
        },
    }


def money_from_price(price: str) -> Dict[str, str]:
    parts = price.split()
    return {"value": parts[0], "currency": parts[1] if len(parts) > 1 else "CZK"}


def chunks(items: List[Any], size: int) -> Iterable[List[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def upload_local_inventory(rows: List[Dict[str, Any]], limit: Optional[int] = None) -> Dict[str, Any]:
    headers = google_headers()
    url = "https://shoppingcontent.googleapis.com/content/v2.1/localinventory/batch"
    upload_rows = rows[:limit] if limit else rows
    results: List[Dict[str, Any]] = []
    batch_id = 1

    for row_chunk in chunks(upload_rows, 1000):
        entries = []
        for row in row_chunk:
            entries.append(content_api_localinventory_entry(batch_id, row))
            batch_id += 1
        response = requests.post(url, headers=headers, json={"entries": entries}, timeout=120)
        if response.status_code >= 400:
            raise RuntimeError(f"Upload failed {response.status_code}: {response.text[:4000]}")
        payload = response.json()
        results.append(payload)
        errors = [entry for entry in payload.get("entries", []) if "errors" in entry]
        print(f"Uploaded batch entries={len(entries)} errors={len(errors)}", flush=True)
        time.sleep(0.2)

    return {"batches": results, "uploaded_rows": len(upload_rows)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upload", action="store_true", help="Upload to Google Content API localinventory.batch")
    parser.add_argument("--upload-limit", type=int, default=0, help="Optional row limit for test uploads")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    variants = fetch_shopify_variants()
    merchant_products = fetch_merchant_products()

    rows: List[Dict[str, Any]] = []
    skipped_missing_sku: List[Dict[str, Any]] = []
    skipped_inactive: List[Dict[str, Any]] = []
    skipped_not_in_merchant: List[Dict[str, Any]] = []

    for variant in variants:
        if not variant.sku.strip():
            skipped_missing_sku.append({"variant_gid": variant.gid, "product_gid": variant.product_gid, "title": variant.title})
            continue
        if variant.product_status != "ACTIVE":
            skipped_inactive.append({"id": variant.merchant_offer_id, "sku": variant.sku, "status": variant.product_status})
            continue
        merchant_product = merchant_products.get(variant.merchant_offer_id)
        if not merchant_product:
            skipped_not_in_merchant.append({"id": variant.merchant_offer_id, "sku": variant.sku, "product_title": variant.product_title})
            continue
        rows.extend(calculate_local_rows(variant, merchant_product))

    write_tsv(OUT_DIR / "local_inventory_shopify.tsv", rows)
    write_csv(OUT_DIR / "local_inventory_shopify_preview.csv", rows)
    write_csv(OUT_DIR / "skipped_missing_sku.csv", skipped_missing_sku)
    write_csv(OUT_DIR / "skipped_inactive_product.csv", skipped_inactive)
    write_csv(OUT_DIR / "skipped_not_in_merchant.csv", skipped_not_in_merchant)

    test_rows = [row for row in rows if row.get("sku") == TEST_SKU or row.get("id") == TEST_MERCHANT_OFFER_ID]
    write_csv(OUT_DIR / "control_sku_8996470703070.csv", test_rows)

    summary = {
        "shopify_variants_total": len(variants),
        "merchant_products_total": len(merchant_products),
        "local_inventory_rows": len(rows),
        "unique_offer_ids_in_upload": len({row["id"] for row in rows}),
        "skipped_missing_sku": len(skipped_missing_sku),
        "skipped_inactive_product": len(skipped_inactive),
        "skipped_not_in_merchant": len(skipped_not_in_merchant),
        "store_code_counts": {code: sum(1 for row in rows if row["store_code"] == code) for code in LOCATION_TO_STORE_CODE.values()},
        "control_sku_rows": test_rows,
        "upload_requested": args.upload,
        "upload_limit": args.upload_limit,
    }

    if len(test_rows) != 3:
        summary["warnings"] = summary.get("warnings", []) + [f"Expected 3 control rows for {TEST_SKU}, got {len(test_rows)}"]

    if args.upload:
        upload_result = upload_local_inventory(rows, limit=args.upload_limit or None)
        summary["upload_result"] = upload_result

    with (OUT_DIR / "summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if len(test_rows) != 3:
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
