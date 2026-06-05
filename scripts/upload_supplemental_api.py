#!/usr/bin/env python3
"""Upload SUPPLEMENTAL_SOURCE_3.tsv to Merchant API as productInputs.

Local inventory remains a file feed. This script handles only the product-level
supplemental attributes: availability, price, sale price, sell on google quantity.

The upload is intentionally resumable through --offset and --limit. Merchant API
can occasionally time out on individual productInputs.insert requests, so network
exceptions are retried and then recorded per row instead of crashing the whole job.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import google.auth
import google.auth.transport.requests
from google.oauth2 import service_account
import requests


DATASOURCES_BASE = "https://merchantapi.googleapis.com/datasources/v1"
PRODUCTS_BASE = "https://merchantapi.googleapis.com/products/v1"
OUT_DIR = Path("out")


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


def merchant_headers() -> Dict[str, str]:
    creds = google_credentials()
    return {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }


def request_json(method: str, url: str, headers: Dict[str, str], *, body: Optional[Dict[str, Any]] = None, max_retries: int = 6) -> Dict[str, Any]:
    last: Dict[str, Any] = {}
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.request(method, url, headers=headers, json=body, timeout=(15, 45))
            try:
                payload = response.json()
            except Exception:
                payload = {"raw_text": response.text}
            result = {
                "ok": response.status_code < 400,
                "status_code": response.status_code,
                "url": response.url,
                "payload": payload,
                "raw_text": response.text[:4000],
                "attempts": attempt,
            }
            last = result
            if response.status_code in (408, 409, 429, 500, 502, 503, 504) and attempt < max_retries:
                wait_seconds = min(90, 2 ** attempt)
                print(f"Transient Merchant API HTTP {response.status_code}; attempt {attempt}/{max_retries}; waiting {wait_seconds}s", flush=True)
                time.sleep(wait_seconds)
                continue
            return result
        except requests.exceptions.RequestException as exc:
            result = {
                "ok": False,
                "status_code": 598,
                "url": url,
                "payload": {"exception": exc.__class__.__name__, "message": str(exc)},
                "raw_text": f"{exc.__class__.__name__}: {str(exc)[:3800]}",
                "attempts": attempt,
            }
            last = result
            if attempt < max_retries:
                wait_seconds = min(90, 2 ** attempt)
                print(f"Transient Merchant API exception {exc.__class__.__name__}; attempt {attempt}/{max_retries}; waiting {wait_seconds}s", flush=True)
                time.sleep(wait_seconds)
                continue
            return result
    return last


def is_expired_token_error(result: Dict[str, Any]) -> bool:
    if result.get("status_code") != 401:
        return False
    text = str(result.get("raw_text") or "")
    payload = result.get("payload") or {}
    return "ACCESS_TOKEN_EXPIRED" in text or "UNAUTHENTICATED" in text or "ACCESS_TOKEN_EXPIRED" in json.dumps(payload)


def list_data_sources(account_id: str, headers: Dict[str, str]) -> List[Dict[str, Any]]:
    url = f"{DATASOURCES_BASE}/accounts/{quote(account_id, safe='')}/dataSources"
    sources: List[Dict[str, Any]] = []
    page_token = ""
    while True:
        page_url = url if not page_token else f"{url}?pageToken={quote(page_token, safe='')}"
        result = request_json("GET", page_url, headers)
        if not result["ok"]:
            raise RuntimeError(f"Could not list data sources: {result['raw_text']}")
        payload = result["payload"]
        sources.extend(payload.get("dataSources") or [])
        page_token = payload.get("nextPageToken") or ""
        if not page_token:
            return sources


def find_supplemental_data_source(account_id: str, headers: Dict[str, str], display_name: str) -> Optional[Dict[str, Any]]:
    for source in list_data_sources(account_id, headers):
        if source.get("displayName") == display_name and source.get("supplementalProductDataSource") is not None:
            return source
    return None


def create_supplemental_data_source(account_id: str, headers: Dict[str, str], display_name: str) -> Dict[str, Any]:
    url = f"{DATASOURCES_BASE}/accounts/{quote(account_id, safe='')}/dataSources"
    body = {
        "displayName": display_name,
        "supplementalProductDataSource": {},
    }
    result = request_json("POST", url, headers, body=body)
    if not result["ok"]:
        raise RuntimeError(f"Could not create supplemental data source: {result['raw_text']}")
    return result["payload"]


def resolve_data_source(account_id: str, headers: Dict[str, str], display_name: str, explicit_name: str = "", create_missing: bool = True) -> Dict[str, Any]:
    if explicit_name:
        if explicit_name.isdigit():
            return {"name": f"accounts/{account_id}/dataSources/{explicit_name}", "displayName": display_name, "explicit": True}
        return {"name": explicit_name, "displayName": display_name, "explicit": True}

    found = find_supplemental_data_source(account_id, headers, display_name)
    if found:
        return found
    if not create_missing:
        raise RuntimeError(f"Supplemental data source {display_name!r} not found")
    return create_supplemental_data_source(account_id, headers, display_name)


def money_to_custom_value(value: str) -> str:
    return str(value or "").strip()


def row_to_product_input(row: Dict[str, str], language: str, feed_label: str) -> Dict[str, Any]:
    custom_attributes = [
        {"name": "availability", "value": row.get("availability", "")},
        {"name": "price", "value": money_to_custom_value(row.get("price", ""))},
        {"name": "sell on google quantity", "value": str(row.get("sell on google quantity", ""))},
    ]
    sale_price = row.get("sale price", "").strip()
    if sale_price:
        custom_attributes.append({"name": "sale price", "value": money_to_custom_value(sale_price)})

    return {
        "offerId": row["id"],
        "contentLanguage": language,
        "feedLabel": feed_label,
        "customAttributes": custom_attributes,
    }


def upload_rows(path: Path, account_id: str, language: str, feed_label: str, data_source_name: str, headers: Dict[str, str], limit: int = 0, offset: int = 0) -> Dict[str, Any]:
    url = f"{PRODUCTS_BASE}/accounts/{quote(account_id, safe='')}/productInputs:insert?dataSource={quote(data_source_name, safe='')}"
    uploaded = 0
    skipped = 0
    processed = 0
    errors: List[Dict[str, Any]] = []
    sample_results: List[Dict[str, Any]] = []
    payload_audit: List[Dict[str, Any]] = []
    started = time.time()
    token_refreshed_at = time.time()

    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for index, row in enumerate(reader, start=1):
            if index <= offset:
                skipped += 1
                continue
            if limit and processed >= limit:
                break
            processed += 1
            body = row_to_product_input(row, language, feed_label)
            payload_audit.append({"row_index": index, "id": row.get("id", ""), "body": json.dumps(body, ensure_ascii=False)})

            # GitHub Workload Identity access tokens are short-lived. A 2,000-row
            # batch can run for more than an hour, so refresh the token periodically
            # and immediately retry once if Merchant reports ACCESS_TOKEN_EXPIRED.
            if processed == 1 or processed % 250 == 1 or (time.time() - token_refreshed_at) > 1800:
                headers = merchant_headers()
                token_refreshed_at = time.time()
                print(f"Refreshed Merchant API token at processed={processed}", flush=True)

            result = request_json("POST", url, headers, body=body, max_retries=6)
            if is_expired_token_error(result):
                print(f"Merchant API token expired at row_index={index}; refreshing and retrying once", flush=True)
                headers = merchant_headers()
                token_refreshed_at = time.time()
                result = request_json("POST", url, headers, body=body, max_retries=6)

            if result["ok"]:
                uploaded += 1
                if len(sample_results) < 5:
                    sample_results.append({"row_index": index, "id": row.get("id", ""), "status_code": result["status_code"], "attempts": result.get("attempts"), "response": result["payload"]})
            else:
                errors.append({"row_index": index, "id": row.get("id", ""), "status_code": result["status_code"], "attempts": result.get("attempts"), "response": result["raw_text"]})
                if len(errors) >= 25:
                    break
            if processed % 100 == 0:
                elapsed = max(1.0, time.time() - started)
                print(f"Supplemental API progress: offset={offset} processed={processed} uploaded={uploaded} errors={len(errors)} rate={processed/elapsed:.2f} rows/s", flush=True)

    write_csv(OUT_DIR / "supplemental_api_payload_audit.csv", payload_audit)
    write_csv(OUT_DIR / "supplemental_api_errors.csv", errors)
    return {
        "offset": offset,
        "limit": limit,
        "skipped_rows": skipped,
        "processed_rows": processed,
        "uploaded_rows": uploaded,
        "errors": len(errors),
        "sample_results": sample_results,
        "data_source": data_source_name,
    }


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--feed-file", default="out/SUPPLEMENTAL_SOURCE_3.tsv")
    parser.add_argument("--account-id", default=os.getenv("GOOGLE_MERCHANT_ID", ""))
    parser.add_argument("--language", default=os.getenv("GOOGLE_LANGUAGE", "cs"))
    parser.add_argument("--feed-label", default=os.getenv("GOOGLE_FEED_LABEL", "CZK_105791684939"))
    parser.add_argument("--data-source", default=os.getenv("SUPPLEMENTAL_API_DATA_SOURCE", ""))
    parser.add_argument("--display-name", default=os.getenv("SUPPLEMENTAL_API_DISPLAY_NAME", "SUPPLEMENTAL_SOURCE_3_API"))
    parser.add_argument("--limit", type=int, default=int(os.getenv("SUPPLEMENTAL_API_UPLOAD_LIMIT", "0") or "0"))
    parser.add_argument("--offset", type=int, default=int(os.getenv("SUPPLEMENTAL_API_UPLOAD_OFFSET", "0") or "0"))
    parser.add_argument("--no-create", action="store_true")
    args = parser.parse_args()

    if not args.account_id:
        raise RuntimeError("Missing GOOGLE_MERCHANT_ID or --account-id")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    headers = merchant_headers()
    data_source = resolve_data_source(args.account_id, headers, args.display_name, args.data_source, create_missing=not args.no_create)
    data_source_name = data_source["name"]
    result = upload_rows(Path(args.feed_file), args.account_id, args.language, args.feed_label, data_source_name, headers, limit=args.limit, offset=args.offset)

    summary = {
        "account_id": args.account_id,
        "language": args.language,
        "feed_label": args.feed_label,
        "data_source": data_source,
        "upload_result": result,
    }
    (OUT_DIR / "supplemental_api_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 1 if result.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
