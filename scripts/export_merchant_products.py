#!/usr/bin/env python3
"""Export the final processed Boschino.cz Merchant catalog to a Merchant-compatible TSV.

Read-only against Google Merchant API. The script lists processed Product resources,
filters them to the requested language/feed label, flattens ProductAttributes to
standard Merchant text-feed attribute names, validates the snapshot, and writes a
summary for auditing.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

import google.auth
import google.auth.transport.requests
import requests

PRODUCTS_BASE = "https://merchantapi.googleapis.com/products/v1"
OUT_DIR = Path(os.getenv("MERCHANT_EXPORT_OUT", "out/merchant-full-feed"))
OUTPUT_NAME = os.getenv("MERCHANT_EXPORT_NAME", "BOSCHINO_CZ_PRODUCTS.tsv")
SUMMARY_NAME = os.getenv("MERCHANT_SUMMARY_NAME", "BOSCHINO_CZ_PRODUCTS.summary.json")
PAGE_SIZE = int(os.getenv("MERCHANT_PAGE_SIZE", "1000"))
MIN_EXPECTED_PRODUCTS = int(os.getenv("MIN_EXPECTED_PRODUCTS", "10000"))
MAX_DROP_RATIO = Decimal(os.getenv("MAX_DROP_RATIO", "0.30"))

FIELDS = [
    "id",
    "title",
    "description",
    "link",
    "mobile_link",
    "canonical_link",
    "image_link",
    "additional_image_link",
    "availability",
    "availability_date",
    "price",
    "sale_price",
    "sale_price_effective_date",
    "brand",
    "gtin",
    "mpn",
    "condition",
    "google_product_category",
    "product_type",
    "item_group_id",
    "identifier_exists",
    "color",
    "adult",
    "age_group",
    "gender",
    "material",
    "pattern",
    "size",
    "size_system",
    "size_type",
    "energy_efficiency_class",
    "min_energy_efficiency_class",
    "max_energy_efficiency_class",
    "multipack",
    "is_bundle",
    "unit_pricing_measure",
    "unit_pricing_base_measure",
    "auto_pricing_min_price",
    "cost_of_goods_sold",
    "sell_on_google_quantity",
    "shipping(country:region:service:price)",
    "free_shipping_threshold(country:price_threshold)",
    "shipping_weight",
    "shipping_label",
    "return_policy_label",
    "transit_time_label",
    "product_detail(section_name:attribute_name:attribute_value)",
    "product_highlight",
    "custom_label_0",
    "custom_label_1",
    "custom_label_2",
    "custom_label_3",
    "custom_label_4",
]


def google_credentials():
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/content"]
    )
    credentials.refresh(google.auth.transport.requests.Request())
    return credentials


def request_json(
    credentials,
    method: str,
    url: str,
    *,
    retries: int = 7,
) -> Dict[str, Any]:
    last_error = ""
    for attempt in range(1, retries + 1):
        if not credentials.valid or credentials.expired:
            credentials.refresh(google.auth.transport.requests.Request())
        try:
            response = requests.request(
                method,
                url,
                headers={
                    "Authorization": f"Bearer {credentials.token}",
                    "Accept": "application/json",
                },
                timeout=(20, 120),
            )
            try:
                payload = response.json()
            except Exception:
                payload = {"raw_text": response.text[:4000]}

            if response.status_code < 400:
                return payload

            last_error = (
                f"HTTP {response.status_code}: "
                f"{json.dumps(payload, ensure_ascii=False)[:3000]}"
            )
            if response.status_code in (401, 408, 409, 429, 500, 502, 503, 504) and attempt < retries:
                if response.status_code == 401:
                    credentials.refresh(google.auth.transport.requests.Request())
                time.sleep(min(60, 2**attempt))
                continue
            raise RuntimeError(last_error)
        except requests.RequestException as exc:
            last_error = f"{exc.__class__.__name__}: {exc}"
            if attempt < retries:
                time.sleep(min(60, 2**attempt))
                continue
            raise RuntimeError(last_error) from exc
    raise RuntimeError(last_error or "Merchant API request failed")


def list_products(credentials, account_id: str) -> List[Dict[str, Any]]:
    products: List[Dict[str, Any]] = []
    token = ""
    page = 0
    while True:
        page += 1
        url = f"{PRODUCTS_BASE}/accounts/{quote(account_id, safe='')}/products?pageSize={PAGE_SIZE}"
        if token:
            url += "&pageToken=" + quote(token, safe="")
        payload = request_json(credentials, "GET", url)
        batch = payload.get("products") or []
        products.extend(batch)
        print(f"Merchant page {page}: +{len(batch)} products, total={len(products)}", flush=True)
        token = str(payload.get("nextPageToken") or "")
        if not token:
            return products


def clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\x00", "")
    return " ".join(text.replace("\t", " ").replace("\r", " ").replace("\n", " ").split())


def enum_value(value: Any) -> str:
    text = clean(value)
    return text.lower() if text else ""


def bool_value(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    text = clean(value).lower()
    if text in {"true", "false"}:
        return text
    return ""


def format_price(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    micros = value.get("amountMicros")
    currency = clean(value.get("currencyCode"))
    if micros in (None, "") or not currency:
        return ""
    try:
        amount = Decimal(str(micros)) / Decimal("1000000")
    except Exception:
        return ""
    text = format(amount.normalize(), "f")
    if "." not in text:
        text += ".00"
    else:
        decimals = len(text.rsplit(".", 1)[1])
        if decimals < 2:
            text += "0" * (2 - decimals)
    return f"{text} {currency}"


def format_interval(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    start = clean(value.get("startTime") or value.get("startDate"))
    end = clean(value.get("endTime") or value.get("endDate"))
    if start and end:
        return f"{start}/{end}"
    return start or end


def format_measure(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    amount = clean(value.get("value"))
    unit = clean(value.get("unit"))
    return f"{amount} {unit}".strip() if amount else ""


def quote_component(value: Any) -> str:
    text = clean(value)
    if any(ch in text for ch in [":", ",", '"', "\\"]):
        text = text.replace("\\", "\\\\").replace('"', '""')
        return f'"{text}"'
    return text


def format_repeated(values: Iterable[Any]) -> str:
    parts = []
    for value in values or []:
        text = clean(value)
        if text:
            parts.append(quote_component(text))
    return ",".join(parts)


def format_shipping(values: Iterable[Any]) -> str:
    rows: List[str] = []
    for item in values or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            ":".join(
                [
                    quote_component(item.get("country")),
                    quote_component(item.get("region")),
                    quote_component(item.get("service")),
                    quote_component(format_price(item.get("price"))),
                ]
            )
        )
    return ",".join(row for row in rows if row.strip(":"))


def format_free_shipping(values: Iterable[Any]) -> str:
    rows: List[str] = []
    for item in values or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            ":".join(
                [
                    quote_component(item.get("country")),
                    quote_component(format_price(item.get("priceThreshold"))),
                ]
            )
        )
    return ",".join(row for row in rows if row.strip(":"))


def format_product_details(values: Iterable[Any]) -> str:
    rows: List[str] = []
    for item in values or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            ":".join(
                [
                    quote_component(item.get("sectionName")),
                    quote_component(item.get("attributeName")),
                    quote_component(item.get("attributeValue")),
                ]
            )
        )
    return ",".join(row for row in rows if row.count(":") == 2)


def flatten_product(product: Dict[str, Any]) -> Dict[str, str]:
    a = product.get("productAttributes") or {}
    row = {
        "id": clean(product.get("offerId")),
        "title": clean(a.get("title")),
        "description": clean(a.get("description")),
        "link": clean(a.get("link")),
        "mobile_link": clean(a.get("mobileLink")),
        "canonical_link": clean(a.get("canonicalLink")),
        "image_link": clean(a.get("imageLink")),
        "additional_image_link": format_repeated(a.get("additionalImageLinks") or []),
        "availability": enum_value(a.get("availability")),
        "availability_date": clean(a.get("availabilityDate")),
        "price": format_price(a.get("price")),
        "sale_price": format_price(a.get("salePrice")),
        "sale_price_effective_date": format_interval(a.get("salePriceEffectiveDate")),
        "brand": clean(a.get("brand")),
        "gtin": format_repeated(a.get("gtins") or []),
        "mpn": clean(a.get("mpn")),
        "condition": enum_value(a.get("condition")),
        "google_product_category": clean(a.get("googleProductCategory")),
        "product_type": format_repeated(a.get("productTypes") or []),
        "item_group_id": clean(a.get("itemGroupId")),
        "identifier_exists": bool_value(a.get("identifierExists")),
        "color": clean(a.get("color")),
        "adult": bool_value(a.get("adult")),
        "age_group": enum_value(a.get("ageGroup")),
        "gender": enum_value(a.get("gender")),
        "material": clean(a.get("material")),
        "pattern": clean(a.get("pattern")),
        "size": clean(a.get("size")),
        "size_system": enum_value(a.get("sizeSystem")),
        "size_type": format_repeated(enum_value(v) for v in (a.get("sizeTypes") or [])),
        "energy_efficiency_class": enum_value(a.get("energyEfficiencyClass")),
        "min_energy_efficiency_class": enum_value(a.get("minEnergyEfficiencyClass")),
        "max_energy_efficiency_class": enum_value(a.get("maxEnergyEfficiencyClass")),
        "multipack": clean(a.get("multipack")),
        "is_bundle": bool_value(a.get("isBundle")),
        "unit_pricing_measure": format_measure(a.get("unitPricingMeasure")),
        "unit_pricing_base_measure": format_measure(a.get("unitPricingBaseMeasure")),
        "auto_pricing_min_price": format_price(a.get("autoPricingMinPrice")),
        "cost_of_goods_sold": format_price(a.get("costOfGoodsSold")),
        "sell_on_google_quantity": clean(a.get("sellOnGoogleQuantity")),
        "shipping(country:region:service:price)": format_shipping(a.get("shipping") or []),
        "free_shipping_threshold(country:price_threshold)": format_free_shipping(a.get("freeShippingThreshold") or []),
        "shipping_weight": format_measure(a.get("shippingWeight")),
        "shipping_label": clean(a.get("shippingLabel")),
        "return_policy_label": clean(a.get("returnPolicyLabel")),
        "transit_time_label": clean(a.get("transitTimeLabel")),
        "product_detail(section_name:attribute_name:attribute_value)": format_product_details(a.get("productDetails") or []),
        "product_highlight": format_repeated(a.get("productHighlights") or []),
        "custom_label_0": clean(a.get("customLabel0")),
        "custom_label_1": clean(a.get("customLabel1")),
        "custom_label_2": clean(a.get("customLabel2")),
        "custom_label_3": clean(a.get("customLabel3")),
        "custom_label_4": clean(a.get("customLabel4")),
    }
    return {field: row.get(field, "") for field in FIELDS}


def previous_row_count(path: Path) -> Optional[int]:
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return max(0, sum(1 for _ in handle) - 1)
    except OSError:
        return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    account_id = os.getenv("GOOGLE_MERCHANT_ID", "").strip()
    language = os.getenv("GOOGLE_LANGUAGE", "cs").strip()
    feed_label = os.getenv("GOOGLE_FEED_LABEL", "CZK_105791684939").strip()
    previous_path = Path(os.getenv("PREVIOUS_FEED_PATH", f"merchant/{OUTPUT_NAME}"))

    if not account_id:
        raise RuntimeError("Missing GOOGLE_MERCHANT_ID")
    if not language or not feed_label:
        raise RuntimeError("GOOGLE_LANGUAGE and GOOGLE_FEED_LABEL must be set")

    credentials = google_credentials()
    all_products = list_products(credentials, account_id)
    selected = [
        p for p in all_products
        if clean(p.get("contentLanguage")) == language
        and clean(p.get("feedLabel")) == feed_label
    ]

    print(
        f"Selected {len(selected)} products for language={language!r}, feed_label={feed_label!r} "
        f"from {len(all_products)} processed products.",
        flush=True,
    )

    if len(selected) < MIN_EXPECTED_PRODUCTS:
        raise RuntimeError(
            f"Refusing suspiciously small Merchant snapshot: {len(selected)} < {MIN_EXPECTED_PRODUCTS}"
        )

    rows = [flatten_product(product) for product in selected]
    seen = set()
    missing_required: Counter[str] = Counter()
    duplicates: List[str] = []
    required = ["id", "title", "link", "image_link", "availability", "price"]

    for row in rows:
        item_id = row["id"]
        if item_id in seen:
            duplicates.append(item_id)
        seen.add(item_id)
        for field in required:
            if not row[field]:
                missing_required[field] += 1

    if duplicates:
        raise RuntimeError(f"Duplicate Merchant ids detected: {duplicates[:10]}")
    if missing_required:
        raise RuntimeError(
            "Required Merchant attributes missing: "
            + json.dumps(dict(missing_required), ensure_ascii=False)
        )

    previous_count = previous_row_count(previous_path)
    if previous_count:
        minimum_allowed = int(Decimal(previous_count) * (Decimal("1") - MAX_DROP_RATIO))
        if len(rows) < minimum_allowed:
            raise RuntimeError(
                f"Refusing unexpected product-count drop: previous={previous_count}, "
                f"new={len(rows)}, minimum_allowed={minimum_allowed}"
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
        writer.writerows(rows)

    availability = Counter(row["availability"] for row in rows)
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "content_language": language,
        "feed_label": feed_label,
        "processed_products_seen": len(all_products),
        "exported_products": len(rows),
        "previous_products": previous_count,
        "availability": dict(sorted(availability.items())),
        "file": output_path.name,
        "file_size_bytes": output_path.stat().st_size,
        "sha256": sha256_file(output_path),
        "columns": FIELDS,
    }
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
