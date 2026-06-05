#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import os
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-10")
SHOPIFY_SHOP_DOMAIN = os.getenv("SHOPIFY_SHOP_DOMAIN", "vvircm-fz.myshopify.com")
SHOPIFY_GRAPHQL_URL = f"https://{SHOPIFY_SHOP_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
SHOPIFY_PAGE_SIZE = int(os.getenv("SHOPIFY_PAGE_SIZE", "50"))
SHOPIFY_PAGE_SLEEP_SECONDS = float(os.getenv("SHOPIFY_PAGE_SLEEP_SECONDS", "0.35"))
GOOGLE_LANGUAGE = os.getenv("GOOGLE_LANGUAGE", "cs")
GOOGLE_FEED_LABEL = os.getenv("GOOGLE_FEED_LABEL", "CZK_105791684939")
OUT_DIR = Path("out")

CENTRAL_TEST_ID = "shopify_ZZ_15464863891787_56264244887883"


@dataclass
class ShopifyVariant:
    gid: str
    product_gid: str
    sku: str
    title: str
    price: str
    compare_at_price: str
    inventory_quantity: int
    available_for_sale: bool
    product_title: str
    product_status: str
    product_total_inventory: int
    product_availability: str
    variant_availability: str

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
    return "" if value is None else str(value).strip().lower()


def first_metafield_value(*values: Any) -> str:
    for value in values:
        if isinstance(value, dict) and value.get("value"):
            return str(value.get("value"))
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def load_shopify_token() -> str:
    token = os.getenv("SHOPIFY_ADMIN_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Missing SHOPIFY_ADMIN_TOKEN")
    return token


def shopify_headers() -> Dict[str, str]:
    token_header = "X-" + "Shopify" + "-Access-Token"
    return {token_header: load_shopify_token(), "Content-Type": "application/json"}


def shopify_graphql(query: str, variables: Optional[Dict[str, Any]] = None, max_retries: int = 12) -> Dict[str, Any]:
    for attempt in range(1, max_retries + 1):
        response = requests.post(
            SHOPIFY_GRAPHQL_URL,
            headers=shopify_headers(),
            json={"query": query, "variables": variables or {}},
            timeout=90,
        )
        if response.status_code == 429 or response.status_code >= 500:
            wait = min(90, (2 ** (attempt - 1)) * 2)
            print(f"Shopify transient HTTP {response.status_code}, attempt {attempt}/{max_retries}, wait {wait}s", flush=True)
            time.sleep(wait)
            continue
        response.raise_for_status()
        payload = response.json()
        errors = payload.get("errors") or []
        if not errors:
            throttle = ((payload.get("extensions") or {}).get("cost") or {}).get("throttleStatus") or {}
            available = float(throttle.get("currentlyAvailable") or 1000)
            restore = float(throttle.get("restoreRate") or 50)
            if available < 250:
                time.sleep(max(1.0, (350 - available) / max(restore, 1)))
            return payload["data"]
        throttled = any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors)
        if throttled and attempt < max_retries:
            time.sleep(min(120, 5 * attempt))
            continue
        raise RuntimeError(json.dumps(errors, ensure_ascii=False, indent=2))
    raise RuntimeError("Shopify GraphQL failed after retries")


def fetch_shopify_variants() -> List[ShopifyVariant]:
    query = """
    query ProductVariantsForSupplementalFeed($cursor: String, $pageSize: Int!) {
      productVariants(first: $pageSize, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id sku title price compareAtPrice inventoryQuantity availableForSale
          variantAvailability: metafield(namespace: "custom", key: "availability") { value }
          variantDostupnost: metafield(namespace: "custom", key: "dostupnost") { value }
          product {
            id title status totalInventory
            productAvailability: metafield(namespace: "custom", key: "availability") { value }
            productDostupnostCustom: metafield(namespace: "custom", key: "dostupnost") { value }
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
            variants.append(ShopifyVariant(
                gid=node.get("id", ""),
                product_gid=product.get("id", ""),
                sku=node.get("sku") or "",
                title=node.get("title") or "",
                price=str(node.get("price") or "0"),
                compare_at_price=str(node.get("compareAtPrice") or ""),
                inventory_quantity=int(node.get("inventoryQuantity") or 0),
                available_for_sale=bool(node.get("availableForSale")),
                product_title=product.get("title", ""),
                product_status=product.get("status", ""),
                product_total_inventory=int(product.get("totalInventory") or 0),
                product_availability=first_metafield_value(product.get("productAvailability"), product.get("productDostupnostCustom")),
                variant_availability=first_metafield_value(node.get("variantAvailability"), node.get("variantDostupnost")),
            ))
        print(f"Fetched Shopify variants: {len(variants)}", flush=True)
        if not connection["pageInfo"].get("hasNextPage"):
            break
        cursor = connection["pageInfo"].get("endCursor")
        time.sleep(SHOPIFY_PAGE_SLEEP_SECONDS)
    return variants


def availability_text(variant: ShopifyVariant) -> str:
    return normalize_text(f"{variant.variant_availability} {variant.product_availability}")


def negative_text(text: str) -> bool:
    return any(marker in text for marker in ["neni skladem", "není skladem", "nedostup", "out of stock", "vyprod"])


def positive_text(variant: ShopifyVariant) -> bool:
    text = availability_text(variant)
    if not text or negative_text(text):
        return False
    return any(marker in text for marker in ["centr", "skladem", "in stock", "available"])


def merchant_price_for_variant(variant: ShopifyVariant) -> Tuple[str, str]:
    current = to_decimal(variant.price)
    compare = to_decimal(variant.compare_at_price)
    regular = compare if compare > current else current
    sale = current if current < regular else Decimal("0")
    return f"{regular} CZK", (f"{sale} CZK" if sale > 0 else "")


def calculate_supplemental_rows(variants: List[ShopifyVariant]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for variant in variants:
        if not variant.sku.strip() or variant.product_status != "ACTIVE":
            continue
        text_available = positive_text(variant)
        total_qty = max(int(variant.inventory_quantity or 0), int(variant.product_total_inventory or 0))
        overall_available = total_qty > 0 or text_available or variant.available_for_sale
        price, sale_price = merchant_price_for_variant(variant)
        rows.append({
            "id": variant.merchant_offer_id,
            "availability": "in_stock" if overall_available else "out_of_stock",
            "price": price,
            "sale_price": sale_price,
            "sell_on_google_quantity": total_qty,
            "sku": variant.sku,
            "product_title": variant.product_title,
            "variant_title": variant.title,
            "product_status": variant.product_status,
            "text_available": text_available,
            "shopify_available_for_sale": variant.available_for_sale,
            "shopify_inventory_quantity": variant.inventory_quantity,
            "shopify_product_total_inventory": variant.product_total_inventory,
            "shopify_availability_text": first_metafield_value(variant.variant_availability, variant.product_availability),
        })
    return rows


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: Optional[List[str]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = []
        for row in rows:
            for key in row:
                if key not in fieldnames:
                    fieldnames.append(key)
        if not fieldnames:
            fieldnames = ["empty"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def write_tsv(path: Path, rows: List[Dict[str, Any]], field_map: List[Tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[name for name, _ in field_map], delimiter="\t")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(key, "") for name, key in field_map})


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    variants = fetch_shopify_variants()

    skipped_missing_sku = [
        {"variant_gid": variant.gid, "product_gid": variant.product_gid, "title": variant.title}
        for variant in variants
        if not variant.sku.strip()
    ]
    skipped_inactive = [
        {"id": variant.merchant_offer_id, "sku": variant.sku, "status": variant.product_status}
        for variant in variants
        if variant.sku.strip() and variant.product_status != "ACTIVE"
    ]

    supplemental_rows = calculate_supplemental_rows(variants)
    supplemental_map = [
        ("id", "id"),
        ("availability", "availability"),
        ("price", "price"),
        ("sale price", "sale_price"),
        ("sell on google quantity", "sell_on_google_quantity"),
    ]

    write_tsv(OUT_DIR / "SUPPLEMENTAL_SOURCE_3.tsv", supplemental_rows, supplemental_map)
    write_csv(OUT_DIR / "supplemental_source_3_preview.csv", supplemental_rows)
    write_csv(OUT_DIR / "skipped_missing_sku.csv", skipped_missing_sku)
    write_csv(OUT_DIR / "skipped_inactive_product.csv", skipped_inactive)

    control_rows = [row for row in supplemental_rows if row.get("id") == CENTRAL_TEST_ID]
    write_csv(OUT_DIR / "control_variant_56264244887883_supplemental.csv", control_rows)

    summary = {
        "purpose": "SUPPLEMENTAL_SOURCE_3 product-level feed only. Local inventory is intentionally out of scope for this repository.",
        "shopify_variants_total": len(variants),
        "supplemental_rows": len(supplemental_rows),
        "in_stock_rows": sum(1 for row in supplemental_rows if row["availability"] == "in_stock"),
        "out_of_stock_rows": sum(1 for row in supplemental_rows if row["availability"] == "out_of_stock"),
        "skipped_missing_sku": len(skipped_missing_sku),
        "skipped_inactive_product": len(skipped_inactive),
        "control_variant_56264244887883_supplemental": control_rows,
        "shopify_page_size": SHOPIFY_PAGE_SIZE,
        "google_language": GOOGLE_LANGUAGE,
        "google_feed_label": GOOGLE_FEED_LABEL,
        "supplemental_template": [name for name, _ in supplemental_map],
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
