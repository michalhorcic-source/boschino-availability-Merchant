#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import google.auth
import google.auth.transport.requests
import requests

ACCOUNT_ID = os.getenv("CZ_MERCHANT_ACCOUNT_ID", "5757276720").strip()
SHOP = os.getenv("SHOPIFY_SHOP", "vvircm-fz.myshopify.com").strip()
API = os.getenv("SHOPIFY_API_VERSION", "2026-04").strip()
SHOP_TOKEN = os.getenv("SHOPIFY_ADMIN_TOKEN", "").strip()
SHOP_URL = f"https://{SHOP}/admin/api/{API}/graphql.json"
REPORTS_URL = f"https://merchantapi.googleapis.com/reports/v1/accounts/{ACCOUNT_ID}/reports:search"
OUT = Path("out/cz-appliance-price-audit")
VAT_FACTOR = Decimal("1.21")
STALE_MARGIN_FACTOR = Decimal("1.05")
FRESH_FIXED_GROSS = Decimal("300")
CONTROL_TITLE_TOKEN = "BCHF216S"

_google_creds = None


def D(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        try:
            parsed = json.loads(text)
        except Exception:
            return None
        if isinstance(parsed, dict):
            for key in ("amount", "value"):
                if key in parsed:
                    return D(parsed[key])
        return None


def parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def gid_num(gid: str) -> str:
    return str(gid or "").rstrip("/").rsplit("/", 1)[-1]


def half_up_czk(value: Decimal) -> Decimal:
    return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def google_headers() -> Dict[str, str]:
    global _google_creds
    if _google_creds is None:
        _google_creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/content"])
    if not _google_creds.valid or _google_creds.expired or not _google_creds.token:
        _google_creds.refresh(google.auth.transport.requests.Request())
    return {
        "Authorization": f"Bearer {_google_creds.token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def load_price_insights() -> Dict[str, Dict[str, Any]]:
    query = """
SELECT id, offer_id, title, price, suggested_price
FROM price_insights_product_view
""".strip()
    token = ""
    result: Dict[str, Dict[str, Any]] = {}
    page = 0
    while True:
        page += 1
        body: Dict[str, Any] = {"query": query, "pageSize": 1000}
        if token:
            body["pageToken"] = token
        last_error = ""
        for attempt in range(1, 8):
            r = requests.post(REPORTS_URL, headers=google_headers(), json=body, timeout=(20, 120))
            try:
                payload = r.json()
            except Exception:
                payload = {"rawText": r.text[:4000]}
            if r.status_code < 400:
                break
            last_error = f"HTTP {r.status_code}: {json.dumps(payload, ensure_ascii=False)[:3000]}"
            if r.status_code in (401, 408, 429, 500, 502, 503, 504) and attempt < 7:
                time.sleep(min(60, 2 ** attempt))
                continue
            raise RuntimeError(f"Merchant Reports search failed: {last_error}")
        else:
            raise RuntimeError(f"Merchant Reports search failed: {last_error}")

        for item in payload.get("results") or []:
            row = item.get("priceInsightsProductView") or {}
            offer = str(row.get("offerId") or "").strip()
            suggested = row.get("suggestedPrice") or {}
            amount_micros = D(suggested.get("amountMicros"))
            currency = str(suggested.get("currencyCode") or "").upper()
            if not offer or amount_micros is None or amount_micros <= 0:
                continue
            normalized = {
                "id": row.get("id"),
                "offerId": offer,
                "title": row.get("title"),
                "suggested": amount_micros / Decimal("1000000"),
                "currency": currency,
                "currentPrice": (D((row.get("price") or {}).get("amountMicros")) or Decimal("0")) / Decimal("1000000"),
            }
            existing = result.get(offer)
            if existing and (existing["suggested"] != normalized["suggested"] or existing["currency"] != normalized["currency"]):
                raise RuntimeError(f"Conflicting price insight rows for {offer}: {existing} vs {normalized}")
            result[offer] = normalized
        print(f"Merchant price insights page={page}, suggestions={len(result)}", flush=True)
        token = str(payload.get("nextPageToken") or "")
        if not token:
            return result


def shopify_query(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    if not SHOP_TOKEN:
        raise RuntimeError("Missing SHOPIFY_ADMIN_TOKEN")
    headers = {"X-Shopify-Access-Token": SHOP_TOKEN, "Content-Type": "application/json"}
    last = ""
    for attempt in range(1, 10):
        try:
            r = requests.post(SHOP_URL, headers=headers, json={"query": query, "variables": variables}, timeout=(20, 120))
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}: {r.text[:1000]}"
                time.sleep(min(60, 2 ** attempt))
                continue
            r.raise_for_status()
            payload = r.json()
            if payload.get("errors"):
                text = json.dumps(payload["errors"], ensure_ascii=False)
                last = text
                if "THROTTLED" in text.upper() and attempt < 9:
                    time.sleep(min(60, 2 ** attempt))
                    continue
                raise RuntimeError(text)
            return payload["data"]
        except requests.RequestException as exc:
            last = str(exc)
            if attempt < 9:
                time.sleep(min(60, 2 ** attempt))
                continue
            raise
    raise RuntimeError(f"Shopify retries exhausted: {last}")


APPLIANCE_QUERY = r"""
query ApplianceProducts($cursor: String) {
  products(first: 100, after: $cursor, query: "metafields.custom.rozrazeni_produktu:Spotřebič") {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      title
      status
      productType
      tags
      classification: metafield(namespace: "custom", key: "rozrazeni_produktu") { value }
      variants(first: 100) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          sku
          price
          compareAtPrice
          availableForSale
          inventoryQuantity
          inventoryPolicy
          updatedAt
          inventoryItem { id unitCost { amount currencyCode } }
          priceSetting: metafield(namespace: "mm-google-shopping", key: "price_setting") { value }
          recommended: metafield(namespace: "custom", key: "google_recommended_prices") {
            id
            type
            value
            updatedAt
            reference {
              ... on Metaobject {
                id
                type
                updatedAt
                fields { key type value }
              }
            }
          }
        }
      }
    }
  }
}
"""


def load_appliances() -> List[Dict[str, Any]]:
    cursor: Optional[str] = None
    products: List[Dict[str, Any]] = []
    while True:
        conn = shopify_query(APPLIANCE_QUERY, {"cursor": cursor})["products"]
        for product in conn.get("nodes") or []:
            variants = product.get("variants") or {}
            if (variants.get("pageInfo") or {}).get("hasNextPage"):
                raise RuntimeError(f"Appliance product has >100 variants; refusing partial audit: {product.get('id')} {product.get('title')}")
            products.append(product)
        print(f"Shopify appliance products loaded: {len(products)}", flush=True)
        page_info = conn.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            return products
        cursor = page_info.get("endCursor")


def metaobject_fields(recommended: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    ref = recommended.get("reference") if isinstance(recommended, dict) else None
    if not isinstance(ref, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for f in ref.get("fields") or []:
        key = str(f.get("key") or "").strip()
        if key:
            out[key] = f
    return out


def normalized_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


def pick_matrix_values(recommended: Dict[str, Any]) -> Tuple[Optional[Decimal], Optional[datetime], str, Dict[str, Any]]:
    fields = metaobject_fields(recommended)
    if not fields:
        return None, None, "REFERENCE_UNAVAILABLE", {}

    price_hits: List[Tuple[str, Decimal]] = []
    time_hits: List[Tuple[str, datetime]] = []
    compact: Dict[str, Any] = {}
    for key, field in fields.items():
        nk = normalized_key(key)
        raw = field.get("value")
        compact[key] = raw
        if "cz" in nk and "recommended" in nk and "price" in nk:
            value = D(raw)
            if value is not None and value > 0:
                price_hits.append((key, value))
        if "cz" in nk and ("last" in nk or "written" in nk) and ("at" in nk or "time" in nk or "date" in nk or "written" in nk):
            dt = parse_dt(raw)
            if dt:
                time_hits.append((key, dt))

    # Some app-owned definitions use generic names inside a CZ-specific object.
    if not price_hits:
        for key, field in fields.items():
            nk = normalized_key(key)
            if nk in {"recommended_price", "price_recommended"}:
                value = D(field.get("value"))
                if value is not None and value > 0:
                    price_hits.append((key, value))
    if not time_hits:
        for key, field in fields.items():
            nk = normalized_key(key)
            if nk in {"last_written_at", "written_at", "recommended_last_written_at"}:
                dt = parse_dt(field.get("value"))
                if dt:
                    time_hits.append((key, dt))

    if len(price_hits) != 1 or len(time_hits) != 1:
        return None, None, f"AMBIGUOUS_MATRIX_FIELDS price={price_hits} time={[x[0] for x in time_hits]}", compact
    return price_hits[0][1], time_hits[0][1], "MATRIX", compact


def write_csv(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    rows = list(rows)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: List[str] = []
    seen = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    insights = load_price_insights()
    products = load_appliances()

    rows: List[Dict[str, Any]] = []
    control_rows: List[Dict[str, Any]] = []
    counts = Counter()
    matrix_field_examples: List[Dict[str, Any]] = []

    for product in products:
        classification = str(((product.get("classification") or {}).get("value") or "")).strip()
        if classification.casefold() != "spotřebič".casefold():
            counts["classification_filter_mismatch"] += 1
            continue
        product_id = gid_num(product.get("id"))
        for variant in (product.get("variants") or {}).get("nodes") or []:
            variant_id = gid_num(variant.get("id"))
            offer_id = f"shopify_ZZ_{product_id}_{variant_id}"
            cost_obj = (variant.get("inventoryItem") or {}).get("unitCost") or {}
            cost = D(cost_obj.get("amount"))
            cost_currency = str(cost_obj.get("currencyCode") or "").upper()
            current = D(variant.get("price"))
            compare_at = D(variant.get("compareAtPrice"))
            setting_raw = (variant.get("priceSetting") or {}).get("value")
            setting = str(setting_raw or "").strip()
            setting_mode = "AUTOMATIC_DEFAULT" if not setting else setting.upper()
            explicitly_manual = bool(setting) and setting.casefold() != "automatic"
            buyable = bool(variant.get("availableForSale")) and (
                int(variant.get("inventoryQuantity") or 0) > 0 or str(variant.get("inventoryPolicy") or "").upper() == "CONTINUE"
            )

            matrix_r, matrix_time, matrix_status, matrix_fields = pick_matrix_values(variant.get("recommended") or {})
            insight = insights.get(offer_id)
            insight_r = insight.get("suggested") if insight else None
            insight_currency = str((insight or {}).get("currency") or "").upper()

            R: Optional[Decimal] = None
            age_days: Optional[int] = None
            recommendation_source = "NONE"
            if matrix_r is not None and matrix_time is not None:
                R = matrix_r
                age_days = max(0, int((now - matrix_time).total_seconds() // 86400))
                recommendation_source = "MATRIX"
            elif insight_r is not None and insight_currency == "CZK":
                # A suggestion returned by today's Price Insights report is a fresh recommendation.
                R = insight_r
                age_days = 0
                recommendation_source = "PRICE_INSIGHTS_CURRENT"

            target: Optional[Decimal] = None
            rule = ""
            reason = ""
            if str(product.get("status") or "").upper() != "ACTIVE":
                reason = "PRODUCT_NOT_ACTIVE"
            elif not buyable:
                reason = "NOT_BUYABLE"
            elif explicitly_manual:
                reason = "EXPLICIT_NON_AUTOMATIC_PRICE_SETTING"
            elif cost is None or cost <= 0:
                reason = "MISSING_OR_INVALID_COST"
            elif cost_currency != "CZK":
                reason = "COST_NOT_CZK"
            elif R is None or age_days is None:
                reason = "NO_VERIFIABLE_RECOMMENDATION"
            elif R <= 0:
                reason = "INVALID_RECOMMENDATION"
            elif age_days <= 6:
                target = half_up_czk(max(R, cost * VAT_FACTOR + FRESH_FIXED_GROSS))
                rule = "B_FRESH_APPLIANCE"
                reason = "CALCULATED"
            else:
                target = half_up_czk(cost * STALE_MARGIN_FACTOR * VAT_FACTOR)
                rule = "F_STALE_APPLIANCE"
                reason = "CALCULATED"

            current_ex_vat = (current / VAT_FACTOR) if current is not None else None
            current_margin = ((current_ex_vat - cost) / current_ex_vat) if current_ex_vat and cost is not None and current_ex_vat > 0 else None
            target_ex_vat = (target / VAT_FACTOR) if target is not None else None
            target_margin = ((target_ex_vat - cost) / target_ex_vat) if target_ex_vat and cost is not None and target_ex_vat > 0 else None
            current_loss = bool(current is not None and cost is not None and current < cost * VAT_FACTOR)
            needs_change = bool(target is not None and current is not None and target != current)

            row = {
                "product_id": product_id,
                "variant_id": variant_id,
                "offer_id": offer_id,
                "title": product.get("title") or "",
                "sku": variant.get("sku") or "",
                "status": product.get("status") or "",
                "classification": classification,
                "buyable": buyable,
                "inventory_quantity": variant.get("inventoryQuantity"),
                "inventory_policy": variant.get("inventoryPolicy"),
                "price_setting": setting or "(missing=>Automatic)",
                "current_price_czk": str(current) if current is not None else "",
                "compare_at_price_czk": str(compare_at) if compare_at is not None else "",
                "cost_ex_vat_czk": str(cost) if cost is not None else "",
                "cost_currency": cost_currency,
                "recommended_czk": str(R) if R is not None else "",
                "recommendation_age_days": age_days if age_days is not None else "",
                "recommendation_source": recommendation_source,
                "matrix_status": matrix_status,
                "insight_suggested_czk": str(insight_r) if insight_r is not None else "",
                "target_price_czk": str(target) if target is not None else "",
                "rule": rule,
                "reason": reason,
                "current_margin_pct": str((current_margin * 100).quantize(Decimal("0.01"))) if current_margin is not None else "",
                "target_margin_pct": str((target_margin * 100).quantize(Decimal("0.01"))) if target_margin is not None else "",
                "current_below_cost_plus_vat": current_loss,
                "needs_change": needs_change,
                "delta_czk": str(target - current) if needs_change and target is not None and current is not None else "",
                "recommended_metafield_updated_at": (variant.get("recommended") or {}).get("updatedAt") or "",
            }
            rows.append(row)
            counts[reason] += 1
            if current_loss:
                counts["CURRENT_BELOW_COST_PLUS_VAT"] += 1
            if needs_change:
                counts["NEEDS_CHANGE"] += 1
            if CONTROL_TITLE_TOKEN.casefold() in str(product.get("title") or "").casefold():
                control_rows.append(row)
            if matrix_fields and len(matrix_field_examples) < 10:
                matrix_field_examples.append({
                    "title": product.get("title"),
                    "variant_id": variant_id,
                    "matrix_status": matrix_status,
                    "fields": matrix_fields,
                })

    if not control_rows:
        raise RuntimeError(f"Control appliance {CONTROL_TITLE_TOKEN} not found in authoritative Spotřebič selection")

    write_csv(OUT / "appliance_price_audit.csv", rows)
    (OUT / "appliance_price_audit.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "generated_at": now.isoformat(),
        "account_id": ACCOUNT_ID,
        "appliance_products": len(products),
        "appliance_variants": len(rows),
        "merchant_current_suggestions": len(insights),
        "counts": dict(counts),
        "control_BCHF216S": control_rows,
        "matrix_field_examples": matrix_field_examples,
        "policy": {
            "classification": "custom.rozrazeni_produktu == Spotřebič",
            "missing_price_setting": "treated as Automatic default; explicit non-Automatic is protected",
            "missing_cost": "never mutate",
            "fresh_B": "max(R, N*1.21+300)",
            "stale_F": "N*1.05*1.21",
            "rounding": "whole CZK half-up",
        },
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=== CZ APPLIANCE PRICE AUDIT SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("=== CONTROL BCHF216S ===")
    print(json.dumps(control_rows, ensure_ascii=False, indent=2))

    # Audit only. Apply is deliberately separate so the resulting dataset can be inspected first.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
