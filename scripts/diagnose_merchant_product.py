#!/usr/bin/env python3
"""Diagnose Merchant product resource names for local inventory uploads.

This script is read-only. It calls Merchant API products.get with many resource
name candidates and reports.search with several query variants, then writes all
responses to out/merchant_product_diagnostics.json.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

import google.auth
import google.auth.transport.requests
from google.oauth2 import service_account
import requests


OUT_DIR = Path("out")
PRODUCTS_BASE = "https://merchantapi.googleapis.com/products/v1"
REPORTS_BASE = "https://merchantapi.googleapis.com/reports/v1"


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


def headers() -> Dict[str, str]:
    creds = google_credentials()
    return {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}


def request_json(method: str, url: str, hdrs: Dict[str, str], *, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        response = requests.request(method, url, headers=hdrs, json=body, timeout=60)
        try:
            payload = response.json()
        except Exception:
            payload = {"raw_text": response.text[:4000]}
        return {
            "status_code": response.status_code,
            "ok": response.status_code < 400,
            "url": response.url,
            "response": payload,
            "raw_text": response.text[:4000],
        }
    except Exception as exc:
        return {"status_code": 598, "ok": False, "url": url, "response": {}, "raw_text": str(exc)[:4000]}


def b64url_no_padding(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def unique(values: Iterable[str]) -> List[str]:
    return list(dict.fromkeys([value for value in values if value]))


def product_key_candidates(language: str, feed_label: str, offer_id: str, variant_gid: str, extra_feed_labels: List[str]) -> List[str]:
    feed_labels = unique([feed_label, "CZ", "CZK_105791684939", *extra_feed_labels])
    languages = unique([language, "cs", "en"])
    offer_ids = unique([offer_id, variant_gid, b64url_no_padding(offer_id), b64url_no_padding(variant_gid) if variant_gid else ""])
    prefixes = ["", "online~", "local~"]

    keys: List[str] = []
    for prefix in prefixes:
        for lang in languages:
            for label in feed_labels:
                for oid in offer_ids:
                    if prefix:
                        keys.append(f"{prefix}{lang}~{label}~{oid}")
                    else:
                        keys.append(f"{lang}~{label}~{oid}")
    return unique(keys)


def products_get_diagnostics(account_id: str, hdrs: Dict[str, str], candidates: List[str]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for product_key in candidates:
        parent = f"accounts/{account_id}/products/{product_key}"
        # Encode product_key as one path segment. This matters for gid://... candidates.
        url = f"{PRODUCTS_BASE}/accounts/{quote(account_id, safe='')}/products/{quote(product_key, safe='')}"
        result = request_json("GET", url, hdrs)
        results.append({"product_key": product_key, "parent": parent, **result})
        if result.get("ok"):
            break
    return results


def report_queries(offer_id: str, gtin: str, sku: str, variant_gid: str) -> List[Dict[str, str]]:
    select_sets = [
        "product_view.id, product_view.offer_id, product_view.title, product_view.gtin, product_view.language_code, product_view.feed_label, product_view.channel",
        "product_view.id, product_view.title, product_view.brand, product_view.gtin, product_view.language_code, product_view.feed_label, product_view.channel",
        "product_view.id, product_view.offer_id, product_view.title",
        "product_view.id, product_view.title",
    ]
    filters = [
        ("offer_id", f"product_view.offer_id = '{offer_id}'"),
        ("id_contains_offer", f"product_view.id LIKE '%{offer_id}%'"),
        ("gtin", f"product_view.gtin = '{gtin}'"),
        ("id_contains_variant_gid", f"product_view.id LIKE '%{variant_gid}%'"),
        ("title_contains_part", "product_view.title LIKE '%12014980%'"),
        ("title_contains_cerpadlo", "product_view.title LIKE '%čerpadlo%'"),
        ("sku_as_id", f"product_view.id LIKE '%{sku}%'"),
    ]
    queries: List[Dict[str, str]] = []
    for select in select_sets:
        for name, where in filters:
            queries.append({"name": f"{name}__{abs(hash(select))}", "query": f"SELECT {select} FROM product_view WHERE {where} LIMIT 10"})
    return queries


def reports_search_diagnostics(account_id: str, hdrs: Dict[str, str], queries: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    url = f"{REPORTS_BASE}/accounts/{quote(account_id, safe='')}/reports:search"
    for item in queries:
        result = request_json("POST", url, hdrs, body={"query": item["query"], "pageSize": 10})
        results.append({"name": item["name"], "query": item["query"], **result})
        if result.get("ok") and (result.get("response") or {}).get("results"):
            # Keep going, but mark that this query found rows.
            results[-1]["found_rows"] = True
        else:
            results[-1]["found_rows"] = False
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account-id", default=os.getenv("GOOGLE_MERCHANT_ID", ""))
    parser.add_argument("--language", default=os.getenv("GOOGLE_LANGUAGE", "cs"))
    parser.add_argument("--feed-label", default=os.getenv("GOOGLE_FEED_LABEL", "CZ"))
    parser.add_argument("--offer-id", default="shopify_ZZ_15493147984203_56386003730763")
    parser.add_argument("--variant-gid", default="gid://shopify/ProductVariant/56386003730763")
    parser.add_argument("--gtin", default="4054905492008")
    parser.add_argument("--sku", default="8996470703070")
    parser.add_argument("--extra-feed-label", action="append", default=[])
    args = parser.parse_args()

    if not args.account_id:
        raise RuntimeError("Missing --account-id or GOOGLE_MERCHANT_ID")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    hdrs = headers()
    candidates = product_key_candidates(args.language, args.feed_label, args.offer_id, args.variant_gid, args.extra_feed_label)
    product_get = products_get_diagnostics(args.account_id, hdrs, candidates)
    reports = reports_search_diagnostics(args.account_id, hdrs, report_queries(args.offer_id, args.gtin, args.sku, args.variant_gid))

    diagnostics = {
        "account_id": args.account_id,
        "language": args.language,
        "feed_label": args.feed_label,
        "offer_id": args.offer_id,
        "variant_gid": args.variant_gid,
        "gtin": args.gtin,
        "sku": args.sku,
        "candidate_count": len(candidates),
        "products_get": product_get,
        "reports_search": reports,
        "products_get_matches": [row for row in product_get if row.get("ok")],
        "reports_matches": [row for row in reports if row.get("found_rows")],
    }

    (OUT_DIR / "merchant_product_diagnostics.json").write_text(json.dumps(diagnostics, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({
        "products_get_matches": len(diagnostics["products_get_matches"]),
        "reports_matches": len(diagnostics["reports_matches"]),
        "candidate_count": len(candidates),
        "output": "out/merchant_product_diagnostics.json",
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
