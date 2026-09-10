#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote

import google.auth
import google.auth.transport.requests
import requests

ACCOUNT_ID = os.getenv("CZ_MERCHANT_ACCOUNT_ID", "5757276720").strip()
OFFER_ID = os.getenv("TEST_OFFER_ID", "shopify_ZZ_15493147984203_56386003730763").strip()
LANGUAGE = os.getenv("TEST_CONTENT_LANGUAGE", "cs").strip()
FEED_LABEL = os.getenv("TEST_FEED_LABEL", "CZK_105791684939").strip()
OUT = Path("out/forensic-12014980")

PRODUCTS_V1 = "https://merchantapi.googleapis.com/products/v1"
PRODUCTS_V1BETA = "https://merchantapi.googleapis.com/products/v1beta"
DATASOURCES = "https://merchantapi.googleapis.com/datasources/v1"
WATCH = ("2026-08-26", "2026-08-27", "2026-09-10", "2026-09-13")


def credentials_headers() -> Dict[str, str]:
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/content"])
    creds.refresh(google.auth.transport.requests.Request())
    return {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}


def get_json(url: str, headers: Dict[str, str]) -> Dict[str, Any]:
    try:
        r = requests.get(url, headers=headers, timeout=(20, 90))
        try:
            payload = r.json()
        except Exception:
            payload = {"rawText": r.text[:8000]}
        return {
            "statusCode": r.status_code,
            "ok": r.status_code < 400,
            "url": r.url,
            "payload": payload,
        }
    except Exception as exc:
        return {"statusCode": 598, "ok": False, "url": url, "payload": {"error": str(exc)}}


def paged_datasources(headers: Dict[str, str]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    token = ""
    while True:
        url = f"{DATASOURCES}/accounts/{quote(ACCOUNT_ID, safe='')}/dataSources?pageSize=100"
        if token:
            url += "&pageToken=" + quote(token, safe="")
        page = get_json(url, headers)
        if not page["ok"]:
            raise RuntimeError(f"dataSources.list failed: {page}")
        payload = page["payload"]
        items.extend(payload.get("dataSources") or [])
        token = str(payload.get("nextPageToken") or "")
        if not token:
            return items


def b64url(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def product_input_candidates() -> List[str]:
    plain = f"{LANGUAGE}~{FEED_LABEL}~{OFFER_ID}"
    beta = f"online~{LANGUAGE}~{FEED_LABEL}~{OFFER_ID}"
    return [plain, b64url(plain), beta, b64url(beta)]


def processed_product_candidates() -> List[str]:
    plain = f"{LANGUAGE}~{FEED_LABEL}~{OFFER_ID}"
    beta = f"online~{LANGUAGE}~{FEED_LABEL}~{OFFER_ID}"
    return [plain, b64url(plain), beta, b64url(beta)]


def source_kind(source: Dict[str, Any]) -> str:
    for key in (
        "primaryProductDataSource",
        "supplementalProductDataSource",
        "localInventoryDataSource",
        "regionalInventoryDataSource",
    ):
        if source.get(key) is not None:
            return key
    return "other"


def recurse_watch(value: Any, path: str = "") -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    if isinstance(value, dict):
        for k, v in value.items():
            p = f"{path}.{k}" if path else k
            found.extend(recurse_watch(v, p))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            found.extend(recurse_watch(v, f"{path}[{i}]"))
    elif isinstance(value, (str, int, float)):
        text = str(value)
        hits = [needle for needle in WATCH if needle in text]
        if hits:
            found.append({"path": path, "value": text, "matched": hits})
    return found


def fetch_file_row(fetch_uri: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {"fetchUri": fetch_uri, "offerMatches": [], "dateHits": []}
    try:
        with requests.get(fetch_uri, stream=True, timeout=(20, 180)) as r:
            result["statusCode"] = r.status_code
            if r.status_code >= 400:
                result["error"] = r.text[:1000]
                return result
            for line_no, raw in enumerate(r.iter_lines(decode_unicode=True), start=1):
                line = raw or ""
                if OFFER_ID in line:
                    result["offerMatches"].append({"line": line_no, "text": line[:5000]})
                if any(x in line for x in ("2026-08-26", "2026-08-27")):
                    result["dateHits"].append({"line": line_no, "text": line[:2000]})
                    if len(result["dateHits"]) >= 50:
                        result["dateHitsTruncated"] = True
                if len(result["offerMatches"]) >= 20 and len(result["dateHits"]) >= 50:
                    break
    except Exception as exc:
        result["error"] = str(exc)
    return result


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    headers = credentials_headers()

    sources = paged_datasources(headers)
    full_sources: List[Dict[str, Any]] = []
    for source in sources:
        name = str(source.get("name") or "")
        full = get_json(f"{DATASOURCES}/{name}", headers) if name else {"ok": False, "payload": {}}
        payload = full.get("payload") if full.get("ok") else source
        full_sources.append(payload if isinstance(payload, dict) else source)

    product_gets: List[Dict[str, Any]] = []
    for base in (PRODUCTS_V1, PRODUCTS_V1BETA):
        for key in processed_product_candidates():
            result = get_json(
                f"{base}/accounts/{quote(ACCOUNT_ID, safe='')}/products/{quote(key, safe='')}",
                headers,
            )
            product_gets.append({"api": base.rsplit('/', 1)[-1], "key": key, **result})

    # ProductInput GET is not exposed by the current generated REST client, but
    # ProductInput resource docs still reference GetProductInput. Probe GET only;
    # every request remains read-only and non-2xx responses are retained as evidence.
    input_gets: List[Dict[str, Any]] = []
    for source in full_sources:
        source_name = str(source.get("name") or "")
        if not source_name:
            continue
        for base in (PRODUCTS_V1, PRODUCTS_V1BETA):
            for key in product_input_candidates():
                url = (
                    f"{base}/accounts/{quote(ACCOUNT_ID, safe='')}/productInputs/{quote(key, safe='')}"
                    f"?dataSource={quote(source_name, safe='')}"
                )
                result = get_json(url, headers)
                input_gets.append({
                    "source": source_name,
                    "displayName": source.get("displayName"),
                    "sourceKind": source_kind(source),
                    "api": base.rsplit('/', 1)[-1],
                    "key": key,
                    **result,
                })

    file_checks: List[Dict[str, Any]] = []
    seen_uris = set()
    for source in full_sources:
        file_input = source.get("fileInput") or {}
        uri = str(((file_input.get("fetchSettings") or {}).get("fetchUri") or "")).strip()
        if not uri or uri in seen_uris:
            continue
        seen_uris.add(uri)
        check = fetch_file_row(uri)
        check["source"] = source.get("name")
        check["displayName"] = source.get("displayName")
        file_checks.append(check)

    evidence: List[Dict[str, Any]] = []
    for bucket_name, rows in (("processedProduct", product_gets), ("productInputProbe", input_gets)):
        for row in rows:
            hits = recurse_watch(row.get("payload"))
            if hits:
                evidence.append({
                    "bucket": bucket_name,
                    "source": row.get("source"),
                    "displayName": row.get("displayName"),
                    "api": row.get("api"),
                    "statusCode": row.get("statusCode"),
                    "url": row.get("url"),
                    "hits": hits,
                })

    for row in file_checks:
        old_date_offer_lines = [
            item for item in row.get("offerMatches", [])
            if "2026-08-26" in item.get("text", "") or "2026-08-27" in item.get("text", "")
        ]
        if old_date_offer_lines:
            evidence.append({
                "bucket": "fileSourceOfferRow",
                "source": row.get("source"),
                "displayName": row.get("displayName"),
                "hits": old_date_offer_lines,
            })

    compact_sources = [
        {
            "name": s.get("name"),
            "id": str(s.get("name") or "").split("/")[-1],
            "displayName": s.get("displayName"),
            "input": s.get("input"),
            "kind": source_kind(s),
            "fileInput": s.get("fileInput"),
            "defaultRule": (s.get("primaryProductDataSource") or {}).get("defaultRule"),
        }
        for s in full_sources
    ]

    payload = {
        "accountId": ACCOUNT_ID,
        "offerId": OFFER_ID,
        "language": LANGUAGE,
        "feedLabel": FEED_LABEL,
        "watchDates": list(WATCH),
        "sources": compact_sources,
        "processedProductGets": product_gets,
        "productInputGetProbes": input_gets,
        "fileSourceChecks": file_checks,
        "dateEvidence": evidence,
    }
    (OUT / "forensic.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=== DATASOURCES ===")
    print(json.dumps(compact_sources, ensure_ascii=False, indent=2))
    print("=== OLD DATE EVIDENCE ===")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    print("=== PRODUCT INPUT GET SUCCESSES ===")
    print(json.dumps([
        {
            "displayName": r.get("displayName"),
            "source": r.get("source"),
            "api": r.get("api"),
            "key": r.get("key"),
            "payload": r.get("payload"),
        }
        for r in input_gets if r.get("ok")
    ], ensure_ascii=False, indent=2))

    # This is a diagnostic workflow: absence of old-date evidence is a valid result.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
