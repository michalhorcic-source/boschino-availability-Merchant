#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import os
import re
import time
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import google.auth
import google.auth.transport.requests
import requests

OUT = Path("out/margin-labels-v2")
ACCOUNTS = "https://merchantapi.googleapis.com/accounts/v1"
PRODUCTS = "https://merchantapi.googleapis.com/products/v1"
SHOP = os.getenv("SHOPIFY_SHOP", "vvircm-fz.myshopify.com")
API = os.getenv("SHOPIFY_API_VERSION", "2026-04")
SHOP_URL = f"https://{SHOP}/admin/api/{API}/graphql.json"
SHOP_TOKEN = os.getenv("SHOPIFY_ADMIN_TOKEN", "").strip()
VAT = {"CZ": Decimal(os.getenv("VAT_CZ", "0.21")), "SK": Decimal(os.getenv("VAT_SK", "0.23"))}


def D(value: Any) -> Optional[Decimal]:
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def google_headers() -> Dict[str, str]:
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/content"])
    creds.refresh(google.auth.transport.requests.Request())
    return {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}


def request_json(method: str, url: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    last = ""
    for attempt in range(1, 8):
        try:
            r = requests.request(method, url, headers=google_headers(), json=body, timeout=(20, 90))
            try:
                payload = r.json()
            except Exception:
                payload = {"raw_text": r.text[:4000]}
            if r.status_code < 400:
                return payload
            last = f"HTTP {r.status_code}: {json.dumps(payload, ensure_ascii=False)[:3000]}"
            if r.status_code in (401, 408, 409, 429, 500, 502, 503, 504) and attempt < 7:
                time.sleep(min(60, 2**attempt))
                continue
            raise RuntimeError(last)
        except requests.RequestException as exc:
            last = f"{exc.__class__.__name__}: {exc}"
            if attempt < 7:
                time.sleep(min(60, 2**attempt))
                continue
            raise RuntimeError(last) from exc
    raise RuntimeError(last or "Merchant request failed")


def paged_get(url: str, key: str, page_size: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    token = ""
    while True:
        sep = "&" if "?" in url else "?"
        page_url = f"{url}{sep}pageSize={page_size}"
        if token:
            page_url += "&pageToken=" + quote(token, safe="")
        payload = request_json("GET", page_url)
        rows.extend(payload.get(key) or [])
        token = payload.get("nextPageToken") or ""
        if not token:
            return rows


def shopify_query(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    if not SHOP_TOKEN:
        raise RuntimeError("Missing SHOPIFY_ADMIN_TOKEN")
    headers = {"X-Shopify-Access-Token": SHOP_TOKEN, "Content-Type": "application/json"}
    for attempt in range(1, 10):
        r = requests.post(SHOP_URL, headers=headers, json={"query": query, "variables": variables}, timeout=90)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(min(60, 2**attempt))
            continue
        r.raise_for_status()
        payload = r.json()
        if payload.get("errors"):
            text = json.dumps(payload["errors"], ensure_ascii=False)
            if "THROTTLED" in text.upper() and attempt < 9:
                time.sleep(min(60, 2**attempt))
                continue
            raise RuntimeError(text)
        return payload["data"]
    raise RuntimeError("Shopify retries exhausted")


def load_shopify_variants() -> Dict[str, Dict[str, Any]]:
    query = """
    query MarginVariants($cursor: String) {
      productVariants(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          sku
          product { id title status }
          inventoryItem { unitCost { amount currencyCode } }
        }
      }
    }
    """
    cursor: Optional[str] = None
    result: Dict[str, Dict[str, Any]] = {}
    while True:
        c = shopify_query(query, {"cursor": cursor})["productVariants"]
        for node in c["nodes"]:
            variant_id = str(node.get("id") or "").rsplit("/", 1)[-1]
            product = node.get("product") or {}
            unit = (node.get("inventoryItem") or {}).get("unitCost") or {}
            result[variant_id] = {
                "sku": node.get("sku") or "",
                "title": product.get("title") or "",
                "status": product.get("status") or "",
                "cost": D(unit.get("amount")),
                "cost_currency": unit.get("currencyCode") or "",
            }
        print(f"Shopify variants loaded: {len(result)}", flush=True)
        if not c["pageInfo"].get("hasNextPage"):
            return result
        cursor = c["pageInfo"].get("endCursor")


def choose_accounts(accounts: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    selected: Dict[str, Dict[str, Any]] = {}
    for a in accounts:
        name = str(a.get("accountName") or "").lower()
        if "boschino.cz" in name:
            selected["CZ"] = a
        elif "boschino.sk" in name:
            selected["SK"] = a
    if set(selected) != {"CZ", "SK"}:
        raise RuntimeError("Could not select CZ+SK accounts: " + json.dumps([
            {"id": a.get("accountId"), "name": a.get("accountName")} for a in accounts
        ], ensure_ascii=False))
    return selected


def cnb_eur_czk() -> Decimal:
    r = requests.get("https://www.cnb.cz/en/financial_markets/foreign_exchange_market/exchange_rate_fixing/daily.txt", timeout=30)
    r.raise_for_status()
    for line in r.text.splitlines():
        p = line.split("|")
        if len(p) >= 5 and p[3] == "EUR":
            amount, rate = D(p[2]), D(p[4].replace(",", "."))
            if amount and rate:
                return rate / amount
    raise RuntimeError("EUR/CZK unavailable")


def convert_money(amount: Decimal, from_currency: str, to_currency: str, eur_czk: Decimal) -> Decimal:
    if from_currency == to_currency:
        return amount
    if from_currency == "CZK" and to_currency == "EUR":
        return amount / eur_czk
    if from_currency == "EUR" and to_currency == "CZK":
        return amount * eur_czk
    raise RuntimeError(f"Unsupported currency conversion {from_currency}->{to_currency}")


def merchant_money(value: Any) -> Tuple[Optional[Decimal], str]:
    if not isinstance(value, dict):
        return None, ""
    micros = D(value.get("amountMicros"))
    if micros is None:
        return None, value.get("currencyCode") or ""
    return micros / Decimal("1000000"), value.get("currencyCode") or ""


def effective_price(attrs: Dict[str, Any]) -> Tuple[Optional[Decimal], str, str]:
    sale, sale_currency = merchant_money(attrs.get("salePrice"))
    regular, regular_currency = merchant_money(attrs.get("price"))
    if sale is not None and sale > 0:
        return sale, sale_currency, "salePrice"
    return regular, regular_currency, "price"


def merchant_item_variant_id(product: Dict[str, Any]) -> Optional[str]:
    for attr in product.get("customAttributes") or []:
        if str(attr.get("name") or "").strip().lower() != "merchant item id":
            continue
        value = str(attr.get("value") or "").strip()
        m = re.search(r"ProductVariant/(\d+)$", value)
        if m:
            return m.group(1)
        if value.isdigit():
            return value
    return None


def resolve_variant_id(product: Dict[str, Any], shopify: Dict[str, Dict[str, Any]]) -> Tuple[Optional[str], str]:
    offer_id = str(product.get("offerId") or "")
    full = re.match(r"^shopify_[^_]+_(\d+)_(\d+)$", offer_id, re.I)
    if full and full.group(2) in shopify:
        return full.group(2), "FULL_OFFER_ID"

    merchant_item = merchant_item_variant_id(product)
    if merchant_item and merchant_item in shopify:
        return merchant_item, "MERCHANT_ITEM_ID"

    short = re.match(r"^shopify_[^_]+_(\d+)$", offer_id, re.I)
    if short and short.group(1) in shopify:
        return short.group(1), "SHORT_OFFER_VERIFIED_AS_VARIANT"

    return None, "UNRESOLVED"


def margin_label(margin: Optional[Decimal]) -> str:
    if margin is None:
        return "MARGIN_UNKNOWN"
    if margin <= 0:
        return "MARGIN_LOSS"
    if margin < Decimal("0.10"):
        return "MARGIN_0_10"
    if margin < Decimal("0.20"):
        return "MARGIN_10_20"
    if margin < Decimal("0.30"):
        return "MARGIN_20_30"
    if margin < Decimal("0.40"):
        return "MARGIN_30_40"
    return "MARGIN_40_PLUS"


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: List[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def audit_label_slots(market: str, products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    counts: Dict[Tuple[int, str], int] = Counter()
    for p in products:
        attrs = p.get("productAttributes") or {}
        for slot in range(5):
            value = str(attrs.get(f"customLabel{slot}") or "").strip()
            if value:
                counts[(slot, value)] += 1
    return [
        {"market": market, "slot": slot, "value": value, "rows": count}
        for (slot, value), count in sorted(counts.items())
    ]


def build_rows(market: str, account: Dict[str, Any], products: List[Dict[str, Any]], shopify: Dict[str, Dict[str, Any]], eur_czk: Decimal):
    rows, skipped = [], []
    mapping = Counter()
    for product in products:
        offer_id = str(product.get("offerId") or "")
        variant_id, method = resolve_variant_id(product, shopify)
        mapping[method] += 1
        if not variant_id:
            skipped.append({"market": market, "offer_id": offer_id, "reason": "UNRESOLVED_VARIANT_MAPPING"})
            continue
        variant = shopify[variant_id]
        if str(variant.get("status") or "").upper() != "ACTIVE":
            skipped.append({"market": market, "offer_id": offer_id, "variant_id": variant_id, "reason": "SHOPIFY_PRODUCT_NOT_ACTIVE"})
            continue

        attrs = product.get("productAttributes") or {}
        gross_price, currency, price_source = effective_price(attrs)
        cost = variant.get("cost")
        cost_currency = variant.get("cost_currency") or ""
        reason = ""
        net_price = local_cost = gross_profit = margin = None
        if gross_price is None or gross_price <= 0:
            reason = "MISSING_MERCHANT_PRICE"
        elif cost is None or cost <= 0 or not cost_currency:
            reason = "MISSING_OR_ZERO_COGS"
        else:
            net_price = gross_price / (Decimal("1") + VAT[market])
            local_cost = convert_money(cost, cost_currency, currency, eur_czk)
            gross_profit = net_price - local_cost
            margin = gross_profit / net_price if net_price > 0 else None

        row = {
            "market": market,
            "merchant_account_id": account.get("accountId") or "",
            "offer_id": offer_id,
            "mapping_method": method,
            "variant_id": variant_id,
            "sku": variant.get("sku") or "",
            "product_title": attrs.get("title") or variant.get("title") or "",
            "price_source": price_source,
            "price_gross": f"{gross_price:.4f}" if gross_price is not None else "",
            "currency": currency,
            "vat_pct": f"{VAT[market] * 100:.1f}",
            "price_net": f"{net_price:.4f}" if net_price is not None else "",
            "cogs": f"{cost:.4f}" if cost is not None else "",
            "cogs_currency": cost_currency,
            "cogs_in_sale_currency": f"{local_cost:.4f}" if local_cost is not None else "",
            "gross_profit": f"{gross_profit:.4f}" if gross_profit is not None else "",
            "gross_margin_pct": f"{margin * 100:.2f}" if margin is not None else "",
            "candidate_margin_label": margin_label(margin),
            "reason": reason,
        }
        for slot in range(5):
            row[f"custom_label_{slot}"] = str(attrs.get(f"customLabel{slot}") or "").strip()
        rows.append(row)
    return rows, skipped, mapping


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    accounts = paged_get(f"{ACCOUNTS}/accounts", "accounts", 100)
    selected = choose_accounts(accounts)
    shopify = load_shopify_variants()
    eur_czk = cnb_eur_czk()

    all_rows: List[Dict[str, Any]] = []
    all_skipped: List[Dict[str, Any]] = []
    label_usage: List[Dict[str, Any]] = []
    mapping_summary: Dict[str, Dict[str, int]] = {}
    merchant_counts: Dict[str, int] = {}

    for market in ("CZ", "SK"):
        account = selected[market]
        products = paged_get(f"{PRODUCTS}/accounts/{quote(str(account['accountId']), safe='')}/products", "products", 250)
        merchant_counts[market] = len(products)
        label_usage.extend(audit_label_slots(market, products))
        rows, skipped, mapping = build_rows(market, account, products, shopify, eur_czk)
        all_rows.extend(rows)
        all_skipped.extend(skipped)
        mapping_summary[market] = dict(sorted(mapping.items()))
        print(f"{market}: merchant={len(products)} preview={len(rows)} skipped={len(skipped)} mapping={dict(mapping)}")

    write_csv(OUT / "margin_candidates.csv", all_rows)
    write_csv(OUT / "mapping_skipped.csv", all_skipped)
    write_csv(OUT / "custom_label_slot_usage.csv", label_usage)

    slot_summary: Dict[str, Dict[str, Any]] = {}
    for market in ("CZ", "SK"):
        slot_summary[market] = {}
        for slot in range(5):
            subset = [r for r in label_usage if r["market"] == market and r["slot"] == slot]
            slot_summary[market][str(slot)] = {
                "occupied_rows": sum(int(r["rows"]) for r in subset),
                "unique_values": len(subset),
                "values": sorted(subset, key=lambda r: -int(r["rows"]))[:20],
            }

    distribution = {
        market: dict(sorted(Counter(r["candidate_margin_label"] for r in all_rows if r["market"] == market).items()))
        for market in ("CZ", "SK")
    }
    skipped_reasons = {
        market: dict(sorted(Counter(r["reason"] for r in all_skipped if r["market"] == market).items()))
        for market in ("CZ", "SK")
    }

    summary = {
        "mode": "READ_ONLY_AUDIT_V2",
        "merchant_write": False,
        "eur_czk": str(eur_czk),
        "selected_accounts": {m: {"accountId": selected[m].get("accountId"), "accountName": selected[m].get("accountName")} for m in ("CZ", "SK")},
        "merchant_product_rows": merchant_counts,
        "candidate_rows": {m: sum(r["market"] == m for r in all_rows) for m in ("CZ", "SK")},
        "skipped_rows": {m: sum(r["market"] == m for r in all_skipped) for m in ("CZ", "SK")},
        "mapping_methods": mapping_summary,
        "skipped_reasons": skipped_reasons,
        "custom_label_slots": slot_summary,
        "candidate_margin_distribution": distribution,
        "status": "AUDIT_V2_OK",
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
