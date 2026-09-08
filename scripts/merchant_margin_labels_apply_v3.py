#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import google.auth
import google.auth.transport.requests
import requests

from merchant_margin_labels_audit_v2 import (
    ACCOUNTS,
    PRODUCTS,
    VAT,
    D,
    cnb_eur_czk,
    choose_accounts,
    effective_price,
    load_shopify_variants,
    paged_get,
    resolve_variant_id,
    convert_money,
)

OUT = Path("out/margin-labels-apply")
DATASOURCES = "https://merchantapi.googleapis.com/datasources/v1"
SOURCE_DISPLAY_NAME = os.getenv("MARGIN_SOURCE_DISPLAY_NAME", "BOSCHINO_MARGIN_LABELS_API")


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
    raise RuntimeError(last or "request failed")


def margin_label(margin):
    if margin is None:
        return "MARGIN_UNKNOWN"
    if margin <= 0:
        return "MARGIN_LOSS"
    if margin < D("0.10"):
        return "MARGIN_0_10"
    if margin < D("0.20"):
        return "MARGIN_10_20"
    if margin < D("0.30"):
        return "MARGIN_20_30"
    if margin < D("0.40"):
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


def ensure_source(account_id: str) -> Dict[str, Any]:
    sources = paged_get(
        f"{DATASOURCES}/accounts/{quote(account_id, safe='')}/dataSources",
        "dataSources",
        100,
    )
    for source in sources:
        if source.get("displayName") == SOURCE_DISPLAY_NAME and source.get("supplementalProductDataSource") is not None:
            return source
    return request_json(
        "POST",
        f"{DATASOURCES}/accounts/{quote(account_id, safe='')}/dataSources",
        {"displayName": SOURCE_DISPLAY_NAME, "supplementalProductDataSource": {}},
    )


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    accounts = paged_get(f"{ACCOUNTS}/accounts", "accounts", 100)
    selected = choose_accounts(accounts)
    shopify = load_shopify_variants()
    eur_czk = cnb_eur_czk()

    planned: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    backups: List[Dict[str, Any]] = []

    for market in ("CZ", "SK"):
        account = selected[market]
        products = paged_get(
            f"{PRODUCTS}/accounts/{quote(str(account['accountId']), safe='')}/products",
            "products",
            250,
        )
        for product in products:
            offer_id = str(product.get("offerId") or "")
            variant_id, mapping_method = resolve_variant_id(product, shopify)
            if not variant_id:
                skipped.append({"market": market, "offer_id": offer_id, "reason": "UNRESOLVED_VARIANT_MAPPING"})
                continue
            variant = shopify.get(variant_id)
            if not variant or str(variant.get("status") or "").upper() != "ACTIVE":
                skipped.append({"market": market, "offer_id": offer_id, "variant_id": variant_id, "reason": "SHOPIFY_NOT_ACTIVE_OR_MISSING"})
                continue

            attrs = product.get("productAttributes") or {}
            current3 = str(attrs.get("customLabel3") or "").strip()
            backups.append({
                "market": market,
                "account_id": account.get("accountId"),
                "offer_id": offer_id,
                "content_language": product.get("contentLanguage") or "",
                "feed_label": product.get("feedLabel") or "",
                "old_custom_label_3": current3,
                "custom_label_4_untouched": str(attrs.get("customLabel4") or "").strip(),
            })

            gross_price, currency, _price_source = effective_price(attrs)
            cost = variant.get("cost")
            cost_currency = variant.get("cost_currency") or ""
            margin = None
            reason = ""
            if gross_price is None or gross_price <= 0:
                reason = "MISSING_MERCHANT_PRICE"
            elif cost is None or cost <= 0 or not cost_currency:
                reason = "MISSING_OR_ZERO_COGS"
            else:
                net_price = gross_price / (D("1") + VAT[market])
                local_cost = convert_money(cost, cost_currency, currency, eur_czk)
                margin = (net_price - local_cost) / net_price if net_price > 0 else None

            label = margin_label(margin)
            planned.append({
                "market": market,
                "merchant_account_id": account.get("accountId"),
                "offer_id": offer_id,
                "content_language": product.get("contentLanguage") or "",
                "feed_label": product.get("feedLabel") or "",
                "variant_id": variant_id,
                "mapping_method": mapping_method,
                "old_custom_label_3": current3,
                "new_custom_label_3": label,
                "reason": reason,
            })

    write_csv(OUT / "backup_custom_label_3_before.csv", backups)
    write_csv(OUT / "planned_margin_label_3.csv", planned)
    write_csv(OUT / "skipped.csv", skipped)

    unresolved = [row for row in skipped if row.get("reason") == "UNRESOLVED_VARIANT_MAPPING"]
    if unresolved:
        summary = {
            "status": "STOP_UNRESOLVED_MAPPING",
            "planned": len(planned),
            "skipped": len(skipped),
            "unresolved_mapping": len(unresolved),
        }
        (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 5

    sources = {
        market: ensure_source(str(selected[market]["accountId"]))["name"]
        for market in ("CZ", "SK")
    }

    results: List[Dict[str, Any]] = []
    for index, row in enumerate(planned, 1):
        account_id = str(row["merchant_account_id"])
        url = (
            f"{PRODUCTS}/accounts/{quote(account_id, safe='')}/productInputs:insert"
            f"?dataSource={quote(sources[row['market']], safe='')}"
        )
        body = {
            "offerId": row["offer_id"],
            "contentLanguage": row["content_language"],
            "feedLabel": row["feed_label"],
            "productAttributes": {"customLabel3": row["new_custom_label_3"]},
        }
        try:
            request_json("POST", url, body)
            results.append({**row, "status": "ACCEPTED", "error": ""})
        except Exception as exc:
            results.append({**row, "status": "ERROR", "error": str(exc)[:2000]})
        if index % 250 == 0 or index == len(planned):
            errors = sum(r["status"] == "ERROR" for r in results)
            print(f"Upload {index}/{len(planned)} errors={errors}", flush=True)

    write_csv(OUT / "upload_results.csv", results)
    errors = sum(r["status"] == "ERROR" for r in results)
    distribution = dict(sorted(Counter(r["new_custom_label_3"] for r in planned).items()))
    summary = {
        "status": "APPLY_ACCEPTED" if errors == 0 else "APPLY_PARTIAL",
        "source_display_name": SOURCE_DISPLAY_NAME,
        "sources": sources,
        "planned": len(planned),
        "accepted": len(results) - errors,
        "errors": errors,
        "skipped": len(skipped),
        "old_label3_nonempty": sum(bool(r["old_custom_label_3"]) for r in planned),
        "distribution": distribution,
        "custom_label_4": "NOT_WRITTEN",
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if errors == 0 else 6


if __name__ == "__main__":
    raise SystemExit(main())
