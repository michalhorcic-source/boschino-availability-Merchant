#!/usr/bin/env python3
from __future__ import annotations

import argparse
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

OUT = Path("out/margin-labels")
ACCOUNTS = "https://merchantapi.googleapis.com/accounts/v1"
PRODUCTS = "https://merchantapi.googleapis.com/products/v1"
DATASOURCES = "https://merchantapi.googleapis.com/datasources/v1"
SHOP = os.getenv("SHOPIFY_SHOP", "vvircm-fz.myshopify.com")
API = os.getenv("SHOPIFY_API_VERSION", "2026-04")
SHOP_URL = f"https://{SHOP}/admin/api/{API}/graphql.json"
SHOP_TOKEN = os.getenv("SHOPIFY_ADMIN_TOKEN", "").strip()
VAT = {
    "CZ": Decimal(os.getenv("VAT_CZ", "0.21")),
    "SK": Decimal(os.getenv("VAT_SK", "0.23")),
}
SOURCE = os.getenv("MARGIN_SOURCE_DISPLAY_NAME", "BOSCHINO_MARGIN_LABELS_API")


def D(value: Any) -> Optional[Decimal]:
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def google_headers() -> Dict[str, str]:
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/content"]
    )
    credentials.refresh(google.auth.transport.requests.Request())
    return {
        "Authorization": f"Bearer {credentials.token}",
        "Content-Type": "application/json",
    }


def request_json(
    method: str,
    url: str,
    body: Optional[Dict[str, Any]] = None,
    retries: int = 7,
) -> Dict[str, Any]:
    last = ""
    for attempt in range(1, retries + 1):
        try:
            response = requests.request(
                method,
                url,
                headers=google_headers(),
                json=body,
                timeout=(20, 90),
            )
            try:
                payload = response.json()
            except Exception:
                payload = {"raw_text": response.text[:4000]}

            if response.status_code < 400:
                return payload

            last = (
                f"HTTP {response.status_code}: "
                f"{json.dumps(payload, ensure_ascii=False)[:3000]}"
            )
            if (
                response.status_code
                in (401, 408, 409, 429, 500, 502, 503, 504)
                and attempt < retries
            ):
                time.sleep(min(60, 2**attempt))
                continue
            raise RuntimeError(last)
        except requests.RequestException as exc:
            last = f"{exc.__class__.__name__}: {exc}"
            if attempt < retries:
                time.sleep(min(60, 2**attempt))
                continue
            raise RuntimeError(last) from exc
    raise RuntimeError(last or "Merchant request failed")


def paged_get(url: str, key: str, page_size: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    token = ""
    while True:
        separator = "&" if "?" in url else "?"
        page_url = f"{url}{separator}pageSize={page_size}"
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

    headers = {
        "X-Shopify-Access-Token": SHOP_TOKEN,
        "Content-Type": "application/json",
    }
    for attempt in range(1, 10):
        response = requests.post(
            SHOP_URL,
            headers=headers,
            json={"query": query, "variables": variables},
            timeout=90,
        )
        if response.status_code == 429 or response.status_code >= 500:
            time.sleep(min(60, 2**attempt))
            continue
        response.raise_for_status()
        payload = response.json()
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
        connection = shopify_query(query, {"cursor": cursor})[
            "productVariants"
        ]
        for node in connection["nodes"]:
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
        if not connection["pageInfo"].get("hasNextPage"):
            return result
        cursor = connection["pageInfo"].get("endCursor")


def choose_accounts(accounts: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    selected: Dict[str, Dict[str, Any]] = {}
    for account in accounts:
        name = str(account.get("accountName") or "").lower()
        if "boschino.cz" in name:
            selected["CZ"] = account
        elif "boschino.sk" in name:
            selected["SK"] = account

    if set(selected) != {"CZ", "SK"}:
        listing = [
            {
                "id": account.get("accountId"),
                "name": account.get("accountName"),
            }
            for account in accounts
        ]
        raise RuntimeError(
            "Could not select CZ+SK accounts: "
            + json.dumps(listing, ensure_ascii=False)
        )
    return selected


def cnb_eur_czk() -> Decimal:
    url = (
        "https://www.cnb.cz/en/financial_markets/foreign_exchange_market/"
        "exchange_rate_fixing/daily.txt"
    )
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    for line in response.text.splitlines():
        parts = line.split("|")
        if len(parts) >= 5 and parts[3] == "EUR":
            amount = D(parts[2])
            rate = D(parts[4].replace(",", "."))
            if amount and rate:
                return rate / amount
    raise RuntimeError("EUR/CZK unavailable")


def convert_money(
    amount: Decimal,
    from_currency: str,
    to_currency: str,
    eur_czk: Decimal,
) -> Decimal:
    if from_currency == to_currency:
        return amount
    if from_currency == "CZK" and to_currency == "EUR":
        return amount / eur_czk
    if from_currency == "EUR" and to_currency == "CZK":
        return amount * eur_czk
    raise RuntimeError(
        f"Unsupported currency conversion {from_currency}->{to_currency}"
    )


def merchant_money(value: Any) -> Tuple[Optional[Decimal], str]:
    if not isinstance(value, dict):
        return None, ""
    micros = D(value.get("amountMicros"))
    if micros is None:
        return None, value.get("currencyCode") or ""
    return micros / Decimal("1000000"), value.get("currencyCode") or ""


def effective_price(
    attributes: Dict[str, Any],
) -> Tuple[Optional[Decimal], str, str]:
    sale, sale_currency = merchant_money(attributes.get("salePrice"))
    regular, regular_currency = merchant_money(attributes.get("price"))
    if sale is not None and sale > 0:
        return sale, sale_currency, "salePrice"
    return regular, regular_currency, "price"


def variant_id_from_offer(offer_id: str) -> Optional[str]:
    match = re.match(
        r"^shopify_[^_]+_(\d+)_(\d+)$",
        offer_id or "",
        re.I,
    )
    return match.group(2) if match else None


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

    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def build_rows(
    market: str,
    account: Dict[str, Any],
    products: List[Dict[str, Any]],
    shopify: Dict[str, Dict[str, Any]],
    eur_czk: Decimal,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    rows: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for product in products:
        offer_id = str(product.get("offerId") or "")
        variant_id = variant_id_from_offer(offer_id)
        if not variant_id:
            skipped.append(
                {
                    "market": market,
                    "offer_id": offer_id,
                    "reason": "UNSUPPORTED_OFFER_ID",
                }
            )
            continue

        variant = shopify.get(variant_id)
        if not variant:
            skipped.append(
                {
                    "market": market,
                    "offer_id": offer_id,
                    "variant_id": variant_id,
                    "reason": "SHOPIFY_VARIANT_NOT_FOUND",
                }
            )
            continue

        if str(variant.get("status") or "").upper() != "ACTIVE":
            skipped.append(
                {
                    "market": market,
                    "offer_id": offer_id,
                    "variant_id": variant_id,
                    "reason": "SHOPIFY_PRODUCT_NOT_ACTIVE",
                }
            )
            continue

        attributes = product.get("productAttributes") or {}
        current3 = str(attributes.get("customLabel3") or "").strip()
        current4 = str(attributes.get("customLabel4") or "").strip()

        if current3 and not current3.startswith("MARGIN_"):
            conflicts.append(
                {
                    "market": market,
                    "account_id": account.get("accountId"),
                    "offer_id": offer_id,
                    "current_custom_label_3": current3,
                }
            )

        gross_price, currency, price_source = effective_price(attributes)
        cost = variant.get("cost")
        cost_currency = variant.get("cost_currency") or ""

        reason = ""
        net_price = None
        local_cost = None
        gross_profit = None
        margin = None
        max_ads_cost = None
        required_roas = None

        if gross_price is None or gross_price <= 0:
            reason = "MISSING_MERCHANT_PRICE"
        elif cost is None or cost <= 0 or not cost_currency:
            reason = "MISSING_OR_ZERO_COGS"
        else:
            net_price = gross_price / (Decimal("1") + VAT[market])
            local_cost = convert_money(
                cost,
                cost_currency,
                currency,
                eur_czk,
            )
            gross_profit = net_price - local_cost
            margin = gross_profit / net_price if net_price > 0 else None

            if gross_profit is not None and gross_profit > 0:
                max_ads_cost = gross_profit * Decimal("0.40")
                if max_ads_cost > 0:
                    required_roas = net_price / max_ads_cost * Decimal("100")

        rows.append(
            {
                "market": market,
                "merchant_account_id": account.get("accountId") or "",
                "merchant_account_name": account.get("accountName") or "",
                "offer_id": offer_id,
                "content_language": product.get("contentLanguage") or "",
                "feed_label": product.get("feedLabel") or "",
                "variant_id": variant_id,
                "sku": variant.get("sku") or "",
                "product_title": (
                    attributes.get("title") or variant.get("title") or ""
                ),
                "price_source": price_source,
                "price_gross": (
                    f"{gross_price:.4f}" if gross_price is not None else ""
                ),
                "currency": currency,
                "vat_pct": f"{VAT[market] * 100:.1f}",
                "price_net": (
                    f"{net_price:.4f}" if net_price is not None else ""
                ),
                "cogs": f"{cost:.4f}" if cost is not None else "",
                "cogs_currency": cost_currency,
                "cogs_in_sale_currency": (
                    f"{local_cost:.4f}" if local_cost is not None else ""
                ),
                "gross_profit": (
                    f"{gross_profit:.4f}" if gross_profit is not None else ""
                ),
                "gross_margin_pct": (
                    f"{margin * 100:.2f}" if margin is not None else ""
                ),
                "max_ads_cost_at_40pct_gp": (
                    f"{max_ads_cost:.4f}"
                    if max_ads_cost is not None
                    else ""
                ),
                "required_net_revenue_roas_pct": (
                    f"{required_roas:.1f}" if required_roas is not None else ""
                ),
                "current_custom_label_3": current3,
                "new_custom_label_3": margin_label(margin),
                "custom_label_4_elephant": current4,
                "reason": reason,
            }
        )

    return rows, conflicts, skipped


def source_for(account_id: str) -> Dict[str, Any]:
    sources = paged_get(
        f"{DATASOURCES}/accounts/{quote(account_id, safe='')}/dataSources",
        "dataSources",
        100,
    )
    for source in sources:
        if (
            source.get("displayName") == SOURCE
            and source.get("supplementalProductDataSource") is not None
        ):
            return source

    return request_json(
        "POST",
        f"{DATASOURCES}/accounts/{quote(account_id, safe='')}/dataSources",
        {"displayName": SOURCE, "supplementalProductDataSource": {}},
    )


def upload_rows(
    rows: List[Dict[str, Any]],
    sources: Dict[str, str],
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []

    for index, row in enumerate(rows, start=1):
        account_id = row["merchant_account_id"]
        url = (
            f"{PRODUCTS}/accounts/{quote(account_id, safe='')}/"
            f"productInputs:insert?dataSource="
            f"{quote(sources[row['market']], safe='')}"
        )
        body = {
            "offerId": row["offer_id"],
            "contentLanguage": row["content_language"],
            "feedLabel": row["feed_label"],
            "productAttributes": {
                "customLabel3": row["new_custom_label_3"]
            },
        }

        try:
            request_json("POST", url, body)
            results.append(
                {
                    "market": row["market"],
                    "offer_id": row["offer_id"],
                    "new_custom_label_3": row["new_custom_label_3"],
                    "status": "ACCEPTED",
                    "error": "",
                }
            )
        except Exception as exc:
            results.append(
                {
                    "market": row["market"],
                    "offer_id": row["offer_id"],
                    "new_custom_label_3": row["new_custom_label_3"],
                    "status": "ERROR",
                    "error": str(exc)[:2000],
                }
            )

        if index % 250 == 0 or index == len(rows):
            errors = sum(item["status"] == "ERROR" for item in results)
            print(
                f"Upload {index}/{len(rows)} errors={errors}",
                flush=True,
            )

    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)

    accounts = paged_get(f"{ACCOUNTS}/accounts", "accounts", 100)
    write_csv(
        OUT / "accessible_merchant_accounts.csv",
        [
            {
                "account_id": account.get("accountId"),
                "account_name": account.get("accountName"),
            }
            for account in accounts
        ],
    )

    selected = choose_accounts(accounts)
    print(
        "Selected:",
        {
            market: (
                selected[market].get("accountId"),
                selected[market].get("accountName"),
            )
            for market in ("CZ", "SK")
        },
    )

    shopify = load_shopify_variants()
    eur_czk = cnb_eur_czk()
    print("CNB EUR/CZK:", eur_czk)

    rows: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for market in ("CZ", "SK"):
        account = selected[market]
        products = paged_get(
            f"{PRODUCTS}/accounts/"
            f"{quote(str(account['accountId']), safe='')}/products",
            "products",
            250,
        )
        print(f"{market} Merchant products: {len(products)}")
        market_rows, market_conflicts, market_skipped = build_rows(
            market,
            account,
            products,
            shopify,
            eur_czk,
        )
        rows.extend(market_rows)
        conflicts.extend(market_conflicts)
        skipped.extend(market_skipped)

    write_csv(OUT / "margin_labels_preview.csv", rows)
    write_csv(OUT / "margin_label3_conflicts.csv", conflicts)
    write_csv(OUT / "margin_labels_skipped.csv", skipped)

    distribution = {
        market: dict(
            sorted(
                Counter(
                    row["new_custom_label_3"]
                    for row in rows
                    if row["market"] == market
                ).items()
            )
        )
        for market in ("CZ", "SK")
    }

    summary: Dict[str, Any] = {
        "mode": "APPLY" if args.apply else "DRY_RUN",
        "policy": "Ads spend uncapped; max 40% of gross profit",
        "custom_label_3": "MARGIN_*",
        "custom_label_4": "Elephant unchanged",
        "vat": {key: str(value) for key, value in VAT.items()},
        "eur_czk": str(eur_czk),
        "selected_accounts": {
            market: {
                "accountId": selected[market].get("accountId"),
                "accountName": selected[market].get("accountName"),
            }
            for market in ("CZ", "SK")
        },
        "preview_rows": len(rows),
        "conflicts": len(conflicts),
        "skipped": len(skipped),
        "elephant_rows_seen": sum(
            bool(row.get("custom_label_4_elephant")) for row in rows
        ),
        "distribution": distribution,
    }

    if conflicts:
        summary["status"] = "STOP_CONFLICT_CUSTOM_LABEL_3"
        (OUT / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 3

    if not args.apply:
        summary["status"] = "DRY_RUN_OK"
        (OUT / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    sources = {
        market: source_for(str(selected[market]["accountId"]))["name"]
        for market in ("CZ", "SK")
    }
    upload_results = upload_rows(rows, sources)
    write_csv(OUT / "upload_results.csv", upload_results)
    errors = sum(row["status"] == "ERROR" for row in upload_results)

    summary.update(
        {
            "sources": sources,
            "accepted": len(upload_results) - errors,
            "errors": errors,
            "status": "APPLY_OK" if not errors else "APPLY_PARTIAL",
        }
    )
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not errors else 4


if __name__ == "__main__":
    raise SystemExit(main())
