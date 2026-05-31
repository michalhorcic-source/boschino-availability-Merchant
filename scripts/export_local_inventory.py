#!/usr/bin/env python3
"""Export Boschino Shopify local availability to Google Merchant Center.

The script produces a Google local product inventory file and can upload the
same rows through Merchant API localInventories:insert. Before uploading, it can
resolve the real Merchant product resource by trying the common product key
formats, so local inventory is attached to an existing processed product instead
of guessing the parent resource name.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import google.auth
import google.auth.transport.requests
from google.oauth2 import service_account
import requests


SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-10")
SHOPIFY_SHOP_DOMAIN = os.getenv("SHOPIFY_SHOP_DOMAIN", "vvircm-fz.myshopify.com")
SHOPIFY_GRAPHQL_URL = f"https://{SHOPIFY_SHOP_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
SHOPIFY_PAGE_SIZE = int(os.getenv("SHOPIFY_PAGE_SIZE", "50"))
SHOPIFY_PAGE_SLEEP_SECONDS = float(os.getenv("SHOPIFY_PAGE_SLEEP_SECONDS", "0.35"))

MERCHANT_ID = os.getenv("GOOGLE_MERCHANT_ID", "")
GOOGLE_LANGUAGE = os.getenv("GOOGLE_LANGUAGE", "cs")
GOOGLE_FEED_LABEL = os.getenv("GOOGLE_FEED_LABEL", "CZ")
MERCHANT_PRODUCT_KEY_MODE = os.getenv("MERCHANT_PRODUCT_KEY_MODE", "auto").strip().lower()

LOCATION_TO_STORE_CODE = {
    "gid://shopify/Location/115128074571": "06275645225922442974",  # Praha 8 / Horovo namesti
    "gid://shopify/Location/115128107339": "06824451997053158379",  # Praha 10 / Francouzska
    "gid://shopify/Location/115128140107": "14326918149907693002",  # Benatky nad Jizerou
}

TEST_SKU = "8996470703070"
TEST_MERCHANT_OFFER_ID = "shopify_ZZ_15493147984203_56386003730763"
OUT_DIR = Path("out")


@dataclass
class ShopifyVariant:
    gid: str
    product_gid: str
    sku: str
    title: str
    price: str
    compare_at_price: str
    inventory_quantity: int
    product_title: str
    product_status: str
    product_total_inventory: int
    product_availability: str
    variant_availability: str
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


def to_decimal(value: Any) -> Decimal:
    if value is None or str(value).strip() == "":
        return Decimal("0")
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def normalize_availability(value: Any) -> str:
    return normalize_text(value).replace(" ", "_")


def load_shopify_token() -> str:
    token = os.getenv("SHOPIFY_ADMIN_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Missing SHOPIFY_ADMIN_TOKEN secret/env var")
    return token


def shopify_graphql(query: str, variables: Optional[Dict[str, Any]] = None, max_retries: int = 12) -> Dict[str, Any]:
    token = load_shopify_token()
    body = {"query": query, "variables": variables or {}}

    for attempt in range(1, max_retries + 1):
        response = requests.post(
            SHOPIFY_GRAPHQL_URL,
            headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
            json=body,
            timeout=90,
        )

        if response.status_code == 429 or response.status_code >= 500:
            wait_seconds = min(90, (2 ** (attempt - 1)) * 2)
            print(f"Shopify HTTP transient error {response.status_code}. Attempt {attempt}/{max_retries}. Waiting {wait_seconds}s.", flush=True)
            time.sleep(wait_seconds)
            continue

        response.raise_for_status()
        payload = response.json()
        errors = payload.get("errors") or []

        if not errors:
            throttle = ((payload.get("extensions") or {}).get("cost") or {}).get("throttleStatus") or {}
            currently_available = float(throttle.get("currentlyAvailable") or 1000)
            restore_rate = float(throttle.get("restoreRate") or 50)
            if currently_available < 250:
                wait_seconds = max(1.0, (350 - currently_available) / max(restore_rate, 1))
                print(f"Shopify throttle low ({currently_available}). Waiting {wait_seconds:.1f}s.", flush=True)
                time.sleep(wait_seconds)
            return payload["data"]

        is_throttled = any((err.get("extensions") or {}).get("code") == "THROTTLED" for err in errors)
        if is_throttled and attempt < max_retries:
            throttle = ((payload.get("extensions") or {}).get("cost") or {}).get("throttleStatus") or {}
            requested = float(((payload.get("extensions") or {}).get("cost") or {}).get("requestedQueryCost") or 500)
            available = float(throttle.get("currentlyAvailable") or 0)
            restore_rate = float(throttle.get("restoreRate") or 50)
            missing = max(0, requested - available)
            wait_seconds = min(120, max(5, (missing / max(restore_rate, 1)) + 5))
            print(f"Shopify GraphQL throttled. Attempt {attempt}/{max_retries}. Waiting {wait_seconds:.1f}s.", flush=True)
            time.sleep(wait_seconds)
            continue

        raise RuntimeError(json.dumps(errors, ensure_ascii=False, indent=2))

    raise RuntimeError("Shopify GraphQL failed after retries")


def first_metafield_value(*values: Any) -> str:
    for value in values:
        if isinstance(value, dict) and value.get("value"):
            return str(value.get("value"))
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def fetch_shopify_variants() -> List[ShopifyVariant]:
    query = """
    query ProductVariantsForLocalInventory($cursor: String, $pageSize: Int!) {
      productVariants(first: $pageSize, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          sku
          title
          price
          compareAtPrice
          inventoryQuantity
          variantAvailability: metafield(namespace: "custom", key: "availability") { value }
          variantDostupnost: metafield(namespace: "custom", key: "dostupnost") { value }
          product {
            id
            title
            status
            totalInventory
            productAvailability: metafield(namespace: "custom", key: "availability") { value }
            productDostupnostCustom: metafield(namespace: "custom", key: "dostupnost") { value }
          }
          inventoryItem {
            id
            sku
            tracked
            inventoryLevels(first: 20) {
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

    while True:
        data = shopify_graphql(query, {"cursor": cursor, "pageSize": SHOPIFY_PAGE_SIZE})
        connection = data["productVariants"]
        for node in connection["nodes"]:
            product = node.get("product") or {}
            inventory_item = node.get("inventoryItem") or {}
            inventory_levels = (inventory_item.get("inventoryLevels") or {}).get("nodes") or []

            variants.append(
                ShopifyVariant(
                    gid=node.get("id", ""),
                    product_gid=product.get("id", ""),
                    sku=node.get("sku") or "",
                    title=node.get("title") or "",
                    price=str(node.get("price") or "0"),
                    compare_at_price=str(node.get("compareAtPrice") or ""),
                    inventory_quantity=int(node.get("inventoryQuantity") or 0),
                    product_title=product.get("title", ""),
                    product_status=product.get("status", ""),
                    product_total_inventory=int(product.get("totalInventory") or 0),
                    product_availability=first_metafield_value(product.get("productAvailability"), product.get("productDostupnostCustom")),
                    variant_availability=first_metafield_value(node.get("variantAvailability"), node.get("variantDostupnost")),
                    inventory_levels=inventory_levels,
                )
            )

        print(f"Fetched Shopify variants: {len(variants)}", flush=True)
        if not connection["pageInfo"].get("hasNextPage"):
            break
        cursor = connection["pageInfo"].get("endCursor")
        time.sleep(SHOPIFY_PAGE_SLEEP_SECONDS)

    return variants


def google_credentials():
    scopes = ["https://www.googleapis.com/auth/content"]
    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw_json:
        creds = service_account.Credentials.from_service_account_info(json.loads(raw_json), scopes=scopes)
    else:
        creds, _project = google.auth.default(scopes=scopes)
    request = google.auth.transport.requests.Request()
    creds.refresh(request)
    return creds


def google_headers() -> Dict[str, str]:
    creds = google_credentials()
    return {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}


def request_json_with_retry(method: str, url: str, headers: Dict[str, str], *, json_body: Optional[Dict[str, Any]] = None, timeout: int = 60, max_retries: int = 8) -> Tuple[int, Dict[str, Any], str]:
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.request(method, url, headers=headers, json=json_body, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            if attempt == max_retries:
                return 598, {}, str(exc)[:4000]
            wait_seconds = min(90, (2 ** (attempt - 1)) * 5)
            print(f"Merchant API request exception. Attempt {attempt}/{max_retries}. Waiting {wait_seconds}s. {exc}", flush=True)
            time.sleep(wait_seconds)
            continue

        if response.status_code < 400:
            payload = response.json() if response.text.strip() else {}
            return response.status_code, payload, response.text

        retryable = response.status_code == 429 or response.status_code >= 500
        if not retryable or attempt == max_retries:
            return response.status_code, {}, response.text[:4000]

        wait_seconds = min(90, (2 ** (attempt - 1)) * 5)
        print(f"Merchant API transient error {response.status_code}. Attempt {attempt}/{max_retries}. Waiting {wait_seconds}s. URL: {response.url}", flush=True)
        if response.text:
            print(response.text[:1000], flush=True)
        time.sleep(wait_seconds)

    return 599, {}, "Merchant API failed after retries"


def qty_from_level(level: Dict[str, Any]) -> int:
    for quantity in level.get("quantities") or []:
        if quantity.get("name") == "available":
            return int(quantity.get("quantity") or 0)
    return 0


def mapped_quantities(variant: ShopifyVariant) -> Dict[str, int]:
    qty_by_location = {location_id: 0 for location_id in LOCATION_TO_STORE_CODE}
    for level in variant.inventory_levels:
        location_id = ((level.get("location") or {}).get("id") or "")
        if location_id in qty_by_location:
            qty_by_location[location_id] = qty_from_level(level)
    return qty_by_location


def is_central_available(variant: ShopifyVariant, mapped_total_qty: int) -> bool:
    text = normalize_text(f"{variant.variant_availability} {variant.product_availability}")
    if "centr" in text:
        return True
    return variant.inventory_quantity > mapped_total_qty


def merchant_price_for_variant(variant: ShopifyVariant) -> Tuple[str, str]:
    current_price = to_decimal(variant.price)
    compare_at = to_decimal(variant.compare_at_price)
    regular_price = compare_at if compare_at > current_price else current_price
    sale_price = current_price if current_price < regular_price else Decimal("0")
    return f"{regular_price} CZK", (f"{sale_price} CZK" if sale_price > 0 else "")


def calculate_local_rows(variant: ShopifyVariant) -> List[Dict[str, Any]]:
    qty_by_location = mapped_quantities(variant)
    mapped_total_qty = sum(qty_by_location.values())
    central_available = is_central_available(variant, mapped_total_qty)
    price, sale_price = merchant_price_for_variant(variant)

    rows: List[Dict[str, Any]] = []
    for location_id, store_code in LOCATION_TO_STORE_CODE.items():
        local_qty = qty_by_location.get(location_id, 0)

        if local_qty > 0:
            availability = "limited_availability" if local_qty <= 2 else "in_stock"
            pickup_method = "buy"
            pickup_sla = "same day"
            delivery_source = "local_store"
        elif mapped_total_qty > 0:
            availability = "out_of_stock"
            pickup_method = "ship to store"
            pickup_sla = "next day"
            delivery_source = "other_store"
        elif central_available:
            availability = "out_of_stock"
            pickup_method = "ship to store"
            pickup_sla = "multi-week"
            delivery_source = "central_stock"
        else:
            availability = "out_of_stock"
            pickup_method = "not supported"
            pickup_sla = ""
            delivery_source = "unavailable"

        rows.append(
            {
                "id": variant.merchant_offer_id,
                "sku": variant.sku,
                "product_title": variant.product_title,
                "product_status": variant.product_status,
                "store_code": store_code,
                "availability": availability,
                "quantity": local_qty,
                "price": price,
                "sale_price": sale_price,
                "sale_price_effective_date": "",
                "pickup_method": pickup_method,
                "pickup_sla": pickup_sla,
                "instore_product_location": "",
                "local_shipping_label": "",
                "shopify_inventory_quantity": variant.inventory_quantity,
                "mapped_locations_quantity": mapped_total_qty,
                "central_available": central_available,
                "delivery_source": delivery_source,
                "shopify_availability_text": first_metafield_value(variant.variant_availability, variant.product_availability),
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
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def write_tsv(path: Path, rows: List[Dict[str, Any]]) -> None:
    # File-feed header follows the local product inventory feed naming style.
    field_map = [
        ("id", "id"),
        ("store code", "store_code"),
        ("availability", "availability"),
        ("price", "price"),
        ("sale price", "sale_price"),
        ("quantity", "quantity"),
        ("pickup method", "pickup_method"),
        ("pickup SLA", "pickup_sla"),
        ("instore product location", "instore_product_location"),
        ("local shipping label", "local_shipping_label"),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[name for name, _key in field_map], delimiter="\t")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(key, "") for name, key in field_map})



def calculate_supplemental_rows(variants: List[ShopifyVariant]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for variant in variants:
        if not variant.sku.strip():
            continue
        if variant.product_status != "ACTIVE":
            continue

        qty_by_location = mapped_quantities(variant)
        mapped_total_qty = sum(qty_by_location.values())
        total_qty = max(int(variant.inventory_quantity or 0), mapped_total_qty)

        price, sale_price = merchant_price_for_variant(variant)

        rows.append(
            {
                "id": variant.merchant_offer_id,
                "availability": "in_stock" if total_qty > 0 else "out_of_stock",
                "price": price,
                "sale_price": sale_price,
                "sell_on_google_quantity": total_qty,
                "sku": variant.sku,
                "product_title": variant.product_title,
                "product_status": variant.product_status,
            }
        )
    return rows


def write_supplemental_tsv(path: Path, rows: List[Dict[str, Any]]) -> None:
    field_map = [
        ("id", "id"),
        ("availability", "availability"),
        ("price", "price"),
        ("sale price", "sale_price"),
        ("sell on google quantity", "sell_on_google_quantity"),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[name for name, _key in field_map], delimiter="\t")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(key, "") for name, key in field_map})

def merchant_price_from_czk(price: str) -> Dict[str, str]:
    parts = str(price).split()
    amount = Decimal(parts[0]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    currency = parts[1] if len(parts) > 1 else "CZK"
    return {"amountMicros": str(int(amount * Decimal("1000000"))), "currencyCode": currency}


def merchant_availability(value: str) -> str:
    mapping = {
        "in_stock": "IN_STOCK",
        "limited_availability": "LIMITED_AVAILABILITY",
        "out_of_stock": "OUT_OF_STOCK",
    }
    return mapping.get(normalize_availability(value), "OUT_OF_STOCK")


def merchant_pickup_method(value: str) -> str:
    mapping = {
        "buy": "BUY",
        "reserve": "RESERVE",
        "ship_to_store": "SHIP_TO_STORE",
        "ship-to-store": "SHIP_TO_STORE",
        "ship to store": "SHIP_TO_STORE",
        "not_supported": "NOT_SUPPORTED",
        "not-supported": "NOT_SUPPORTED",
        "not supported": "NOT_SUPPORTED",
    }
    return mapping.get(normalize_availability(value), "NOT_SUPPORTED")


def merchant_pickup_sla(value: str) -> str:
    mapping = {
        "same_day": "SAME_DAY", "same-day": "SAME_DAY",
        "next_day": "NEXT_DAY", "next-day": "NEXT_DAY",
        "2-day": "TWO_DAY", "two_day": "TWO_DAY",
        "3-day": "THREE_DAY", "three_day": "THREE_DAY",
        "4-day": "FOUR_DAY", "four_day": "FOUR_DAY",
        "5-day": "FIVE_DAY", "five_day": "FIVE_DAY",
        "6-day": "SIX_DAY", "six_day": "SIX_DAY",
        "7-day": "SEVEN_DAY", "seven_day": "SEVEN_DAY",
        "multi_week": "MULTI_WEEK", "multi-week": "MULTI_WEEK",
    }
    return mapping.get(normalize_availability(value), "")


def merchant_local_inventory_body(row: Dict[str, Any]) -> Dict[str, Any]:
    attributes: Dict[str, Any] = {
        "availability": merchant_availability(row["availability"]),
        "quantity": str(int(row["quantity"])),
        "price": merchant_price_from_czk(row["price"]),
        "pickupMethod": merchant_pickup_method(row["pickup_method"]),
    }
    pickup_sla = merchant_pickup_sla(row.get("pickup_sla", ""))
    if pickup_sla:
        attributes["pickupSla"] = pickup_sla
    if row.get("sale_price"):
        attributes["salePrice"] = merchant_price_from_czk(row["sale_price"])
    if row.get("sale_price_effective_date"):
        attributes["salePriceEffectiveDate"] = row["sale_price_effective_date"]
    if row.get("instore_product_location"):
        attributes["instoreProductLocation"] = row["instore_product_location"]

    return {"storeCode": row["store_code"], "localInventoryAttributes": attributes}


def product_key_candidates(offer_id: str) -> List[str]:
    direct = f"{GOOGLE_LANGUAGE}~{GOOGLE_FEED_LABEL}~{offer_id}"
    online = f"online~{GOOGLE_LANGUAGE}~{GOOGLE_FEED_LABEL}~{offer_id}"
    local = f"local~{GOOGLE_LANGUAGE}~{GOOGLE_FEED_LABEL}~{offer_id}"
    candidates = [direct, online, local]
    if MERCHANT_PRODUCT_KEY_MODE == "direct":
        candidates = [direct]
    elif MERCHANT_PRODUCT_KEY_MODE == "online":
        candidates = [online, direct]
    elif MERCHANT_PRODUCT_KEY_MODE == "local":
        candidates = [local, direct]
    return list(dict.fromkeys(candidates))


def merchant_product_parent_from_key(product_key: str) -> str:
    return f"accounts/{MERCHANT_ID}/products/{product_key}"


def resolve_product_parent(offer_id: str, headers: Dict[str, str], cache: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    if offer_id in cache:
        return cache[offer_id]

    checked: List[Dict[str, Any]] = []
    for product_key in product_key_candidates(offer_id):
        parent = merchant_product_parent_from_key(product_key)
        url = f"https://merchantapi.googleapis.com/products/v1/{quote(parent, safe='/~:_-')}"
        status_code, payload, text = request_json_with_retry("GET", url, headers, timeout=45, max_retries=3)
        checked.append({"product_key": product_key, "status_code": status_code, "response": text[:800]})
        if status_code < 400:
            resolved = {"found": True, "parent": payload.get("base64EncodedName") or payload.get("name") or parent, "plain_parent": parent, "product_key": product_key, "checked": checked, "product": payload}
            cache[offer_id] = resolved
            return resolved

    resolved = {"found": False, "parent": merchant_product_parent_from_key(product_key_candidates(offer_id)[0]), "product_key": product_key_candidates(offer_id)[0], "checked": checked, "product": {}}
    cache[offer_id] = resolved
    return resolved


def upload_local_inventory(rows: List[Dict[str, Any]], limit: Optional[int] = None) -> Dict[str, Any]:
    if not MERCHANT_ID:
        raise RuntimeError("Missing GOOGLE_MERCHANT_ID secret/env var")

    headers = google_headers()
    upload_rows = rows[:limit] if limit else rows
    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    product_cache: Dict[str, Dict[str, Any]] = {}
    payload_audit: List[Dict[str, Any]] = []

    for index, row in enumerate(upload_rows, start=1):
        resolved = resolve_product_parent(row["id"], headers, product_cache)
        parent = resolved["parent"]
        product_segment = resolved["product_key"]
        inventory_parent_url = f"https://merchantapi.googleapis.com/inventories/v1/accounts/{MERCHANT_ID}/products/{quote(product_segment, safe='')}"
        list_url = f"{inventory_parent_url}/localInventories"
        list_status_code, list_payload, list_text = request_json_with_retry("GET", list_url, headers, timeout=45, max_retries=3)
        url = f"{inventory_parent_url}/localInventories:insert"
        body = merchant_local_inventory_body(row)
        payload_audit.append({"row_index": index, "id": row.get("id", ""), "sku": row.get("sku", ""), "store_code": row.get("store_code", ""), "resolved_parent": parent, "inventory_list_url": list_url, "inventory_list_status_code": list_status_code, "inventory_list_response": list_text[:1200], "insert_url": url, "product_lookup_found": resolved.get("found"), "body": body})

        if not resolved.get("found"):
            errors.append({"row_index": index, "id": row.get("id", ""), "store_code": row.get("store_code", ""), "status_code": 404, "resolved_parent": parent, "response": json.dumps({"message": "Product parent was not found before local inventory upload", "checked": resolved.get("checked", [])}, ensure_ascii=False)[:4000]})
            print(f"Product parent not found: {row.get('id')} store={row.get('store_code')}", flush=True)
            continue

        status_code, payload, text = request_json_with_retry("POST", url, headers, json_body=body, timeout=60, max_retries=5)

        if status_code >= 400:
            errors.append({"row_index": index, "id": row.get("id", ""), "store_code": row.get("store_code", ""), "status_code": status_code, "resolved_parent": parent, "insert_url": url, "inventory_list_status_code": list_status_code, "inventory_list_response": list_text[:1200], "response": text[:4000]})
            print(f"Local inventory upload error {status_code}: {row.get('id')} store={row.get('store_code')}", flush=True)
            continue

        results.append({"row_index": index, "id": row.get("id", ""), "store_code": row.get("store_code", ""), "resolved_parent": parent, "response": payload})
        if index % 50 == 0 or index == len(upload_rows):
            print(f"Uploaded local inventory entries={index} errors={len(errors)}", flush=True)
        time.sleep(0.08)

    write_csv(OUT_DIR / "upload_payload_audit.csv", payload_audit)
    if errors:
        write_csv(OUT_DIR / "upload_errors.csv", errors)

    return {"uploaded_rows": len(upload_rows), "errors": len(errors), "sample_results": results[:5], "product_parent_cache": {key: {"found": value.get("found"), "parent": value.get("parent"), "product_key": value.get("product_key")} for key, value in product_cache.items()}}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upload", action="store_true", help="Upload to Google Merchant API local inventory")
    parser.add_argument("--upload-limit", type=int, default=0, help="Optional row limit for test uploads")
    parser.add_argument("--upload-sku", default="", help="Optional SKU filter for targeted test uploads")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    variants = fetch_shopify_variants()
    rows: List[Dict[str, Any]] = []
    skipped_missing_sku: List[Dict[str, Any]] = []
    skipped_inactive: List[Dict[str, Any]] = []

    for variant in variants:
        if not variant.sku.strip():
            skipped_missing_sku.append({"variant_gid": variant.gid, "product_gid": variant.product_gid, "title": variant.title})
            continue
        if variant.product_status != "ACTIVE":
            skipped_inactive.append({"id": variant.merchant_offer_id, "sku": variant.sku, "status": variant.product_status})
            continue
        rows.extend(calculate_local_rows(variant))


    supplemental_rows = calculate_supplemental_rows(variants)

    write_supplemental_tsv(OUT_DIR / "SUPPLEMENTAL_SOURCE_3.tsv", supplemental_rows)
    write_csv(OUT_DIR / "supplemental_source_3_preview.csv", supplemental_rows)

    write_tsv(OUT_DIR / "LOCAL_INVENTORY.tsv", rows)
    write_csv(OUT_DIR / "local_inventory_shopify_preview.csv", rows)
    write_csv(OUT_DIR / "skipped_missing_sku.csv", skipped_missing_sku)
    write_csv(OUT_DIR / "skipped_inactive_product.csv", skipped_inactive)

    test_rows = [row for row in rows if row.get("sku") == TEST_SKU or row.get("id") == TEST_MERCHANT_OFFER_ID]
    write_csv(OUT_DIR / "control_sku_8996470703070.csv", test_rows)

    upload_rows_source = rows
    if args.upload_sku:
        upload_rows_source = [row for row in rows if row.get("sku") == args.upload_sku]
        if not upload_rows_source:
            raise RuntimeError(f"No rows found for upload SKU {args.upload_sku}")

    summary = {
        "shopify_variants_total": len(variants),`r`n        "supplemental_rows": len(supplemental_rows),
        "local_inventory_rows": len(rows),
        "unique_offer_ids_in_upload": len({row["id"] for row in rows}),
        "skipped_missing_sku": len(skipped_missing_sku),
        "skipped_inactive_product": len(skipped_inactive),
        "store_code_counts": {code: sum(1 for row in rows if row["store_code"] == code) for code in LOCATION_TO_STORE_CODE.values()},
        "control_sku_rows": test_rows,
        "upload_requested": args.upload,
        "upload_limit": args.upload_limit,
        "upload_sku": args.upload_sku,
        "upload_rows_source_count": len(upload_rows_source),
        "shopify_page_size": SHOPIFY_PAGE_SIZE,
        "google_language": GOOGLE_LANGUAGE,
        "google_feed_label": GOOGLE_FEED_LABEL,
        "merchant_product_key_mode": MERCHANT_PRODUCT_KEY_MODE,`r`n        "supplemental_template": ["id", "availability", "price", "sale price", "sell on google quantity"],
        "local_inventory_template": ["id", "store code", "availability", "price", "sale price", "quantity", "pickup method", "pickup SLA", "instore product location", "local shipping label"],
    }

    if len(test_rows) != 3:
        summary["warnings"] = summary.get("warnings", []) + [f"Expected 3 control rows for {TEST_SKU}, got {len(test_rows)}"]

    if args.upload:
        summary["upload_result"] = upload_local_inventory(upload_rows_source, limit=args.upload_limit or None)

    with (OUT_DIR / "summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise






