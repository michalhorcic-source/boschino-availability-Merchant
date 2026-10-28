#!/usr/bin/env python3
"""Bounded, read-only Shopify Admin API inventory for CMF Sparesdat.

No mutations, media downloads, customer records, product values, or credentials
are written to the report. Run only in an approved Boschino GitHub Actions job.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

EXPECTED_SHOP = "vvircm-fz.myshopify.com"
DEFAULT_API_VERSION = "2026-07"
MAX_DEFINITION_PAGES = 10

IDENTITY = """query SparesdatShopIdentity {
  shop { id myshopifyDomain name }
  currentAppInstallation { accessScopes { handle } }
}"""
PRODUCT_COUNT = """query SparesdatProductCount {
  productsCount(limit: null) { count precision }
}"""
META_DEFS = """query SparesdatMetaobjectDefinitions($cursor: String) {
  metaobjectDefinitions(first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes { id type name metaobjectsCount fieldDefinitions { key type { name } } }
  }
}"""
PRODUCT_METAFIELDS = """query SparesdatProductMetafieldDefinitions($cursor: String) {
  metafieldDefinitions(first: 100, after: $cursor, ownerType: PRODUCT) {
    pageInfo { hasNextPage endCursor }
    nodes { namespace key type { name } }
  }
}"""
APPLIANCE_SAMPLE = """query SparesdatApplianceSample {
  metaobjects(type: "spotrebic", first: 3) {
    pageInfo { hasNextPage }
    nodes { id handle fields { key } }
  }
}"""
LOCALES = """query SparesdatLocales {
  shopLocales { locale primary published }
}"""
TRANSLATIONS_SAMPLE = """query SparesdatTranslationSample($locale: String!) {
  translatableResources(first: 3, resourceType: PRODUCT) {
    nodes { resourceId translations(locale: $locale) { key outdated } }
  }
}"""
FILES_SAMPLE = """query SparesdatFileSample {
  files(first: 10, query: "media_type:GENERIC_FILE") {
    pageInfo { hasNextPage }
    nodes { id __typename }
  }
}"""


class InventoryError(RuntimeError):
    pass


def check_shop(domain):
    if not domain or domain.strip().lower() != EXPECTED_SHOP:
        raise InventoryError("WRONG_SHOP_CONFIG")
    return EXPECTED_SHOP


def check_version(version):
    if not re.fullmatch(r"20\d{2}-(01|04|07|10)", version or ""):
        raise InventoryError("INVALID_API_VERSION")
    return version


class ShopifyAdminClient:
    def __init__(self, domain, token, api_version=DEFAULT_API_VERSION, opener=urlopen, sleeper=time.sleep):
        self.domain = check_shop(domain)
        self.version = check_version(api_version)
        if not token or not token.strip():
            raise InventoryError("MISSING_TOKEN")
        self._token = token.strip()
        self._opener = opener
        self._sleep = sleeper
        self._url = f"https://{self.domain}/admin/api/{self.version}/graphql.json"

    def query(self, op, graph_query, variables=None):
        if not graph_query.lstrip().startswith("query ") or re.search(r"\bmutation\b", graph_query, re.I):
            raise InventoryError("QUERY_ONLY_REQUIRED")
        body = json.dumps({"query": graph_query, "variables": variables or {}}).encode("utf-8")
        request = Request(
            self._url, data=body, method="POST",
            headers={"Content-Type": "application/json", "X-Shopify-Access-Token": self._token},
        )
        for attempt in range(4):
            try:
                with self._opener(request, timeout=30) as response:
                    payload = json.loads(response.read())
                if payload.get("errors"):
                    # Do not print untrusted API error text or echo request headers.
                    raise InventoryError(f"GRAPHQL_ERROR_{op}")
                if not isinstance(payload.get("data"), dict):
                    raise InventoryError(f"MISSING_DATA_{op}")
                return payload["data"]
            except HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    self._sleep(min(8, 2 ** attempt))
                    continue
                raise InventoryError(f"HTTP_{e.code}_{op}") from None
            except (URLError, TimeoutError, ValueError, json.JSONDecodeError):
                raise InventoryError(f"NETWORK_OR_JSON_ERROR_{op}") from None
        raise InventoryError(f"RETRIES_EXHAUSTED_{op}")


def page_definitions(client, operation, graph_query, root_key):
    cursor = None
    entries = []
    pages = 0
    truncated = False
    seen = set()
    for _ in range(MAX_DEFINITION_PAGES):
        data = client.query(operation, graph_query, {"cursor": cursor})
        conn = data.get(root_key) or {}
        entries.extend(conn.get("nodes") or [])
        pages += 1
        page_info = conn.get("pageInfo") or {}
        if not page_info.get("hasNextPage", False):
            break
        next_cursor = page_info.get("endCursor")
        if not next_cursor or next_cursor in seen:
            raise InventoryError(f"BAD_CURSOR_{operation}")
        seen.add(next_cursor)
        cursor = next_cursor
    else:
        truncated = True
    return entries, pages, truncated


def inventory(client):
    identity = client.query("identity", IDENTITY)
    shop = identity.get("shop") or {}
    if shop.get("myshopifyDomain", "").lower() != EXPECTED_SHOP:
        raise InventoryError("WRONG_SHOP_IDENTITY")
    scopes = set(x.get("handle") for x in (identity.get("currentAppInstallation") or {}).get("accessScopes", []) if x.get("handle"))
    relevant = ("read_products", "read_metaobject_definitions", "read_metaobjects", "read_files", "read_locales", "read_markets_home", "read_translations")
    result = {
        "report_type": "sparesdat-shopify-readonly-inventory-v1",
        "verified_shop": EXPECTED_SHOP,
        "shop_id": shop.get("id"),
        "api_version": client.version,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "access_scope_check": {k: k in scopes for k in relevant},
        "sections": {},
        "limitations": [
            "This is a bounded API inventory, not a complete media/asset export.",
            "No PDF licenses or redistribution rights have been verified.",
            "No Shopify mutations, products, or file bytes were requested.",
        ],
    }
    sections = result["sections"]

    def optional(name, required_scope, action):
        acceptable_scopes = (required_scope,) if isinstance(required_scope, str) else tuple(required_scope)
        if not any(scope in scopes for scope in acceptable_scopes):
            sections[name] = {"status": "NO_SCOPE", "required_scopes_any": list(acceptable_scopes)}
            return
        try:
            outcome = action()
            status = ("SKIPPED" if outcome.get("sample_status") == "NO_LOCALE_INVENTORY"
                      else "TRUNCATED" if outcome.get("truncated") else "OK")
            sections[name] = {"status": status, **outcome}
        except InventoryError as exc:
            sections[name] = {"status": "QUERY_FAILED", "error_code": str(exc)}

    optional("products", "read_products", lambda: {
        "count": client.query("products_count", PRODUCT_COUNT)["productsCount"]
    })

    def meta_defs():
        values, pages, truncated = page_definitions(client, "metaobject_definitions", META_DEFS, "metaobjectDefinitions")
        return {"definitions_seen": len(values), "pages": pages, "truncated": truncated,
                "definitions": [
                    {"type": d.get("type"), "name": d.get("name"), "metaobjects_count": d.get("metaobjectsCount"),
                     "fields": [{"key": f.get("key"), "type": (f.get("type") or {}).get("name")}
                                for f in (d.get("fieldDefinitions") or [])]}
                    for d in values
                ]}
    optional("metaobject_definitions", "read_metaobject_definitions", meta_defs)

    def metafields():
        values, pages, truncated = page_definitions(client, "product_metafield_definitions", PRODUCT_METAFIELDS, "metafieldDefinitions")
        return {"definitions_seen": len(values), "pages": pages, "truncated": truncated,
                "definitions": [{"namespace": d.get("namespace"), "key": d.get("key"),
                                 "type": (d.get("type") or {}).get("name")} for d in values]}
    optional("product_metafield_definitions", "read_products", metafields)

    def appliance():
        data = client.query("appliance_sample", APPLIANCE_SAMPLE).get("metaobjects") or {}
        nodes = data.get("nodes") or []
        return {"sampled": len(nodes), "more_available": bool((data.get("pageInfo") or {}).get("hasNextPage")),
                "samples": [{"id": n.get("id"), "handle": n.get("handle"),
                             "field_keys": sorted({f.get("key") for f in n.get("fields", []) if f.get("key")})} for n in nodes]}
    optional("appliance_metaobjects_sample", "read_metaobjects", appliance)

    def files():
        data = client.query("files_sample", FILES_SAMPLE).get("files") or {}
        nodes = data.get("nodes") or []
        return {"sampled": len(nodes), "more_available": bool((data.get("pageInfo") or {}).get("hasNextPage")),
                "ids": [{"id": n.get("id"), "type": n.get("__typename")} for n in nodes]}
    optional("generic_files_sample", "read_files", files)

    def locales():
        values = client.query("shop_locales", LOCALES).get("shopLocales") or []
        return {"locales": sorted(
            [{"code": x.get("locale"), "primary": bool(x.get("primary")), "published": bool(x.get("published"))}
             for x in values], key=lambda x: x["code"] or ""
        )}
    optional("shop_locales", ("read_locales", "read_markets_home"), locales)

    def translations():
        known = sections.get("shop_locales") or {}
        if known.get("status") != "OK":
            return {"sample_status": "NO_LOCALE_INVENTORY", "sampled_locales": []}
        codes = [x["code"] for x in known.get("locales", []) if not x.get("primary") and x.get("code")]
        sample = []
        for locale in codes[:3]:
            nodes = (client.query("translation_sample", TRANSLATIONS_SAMPLE, {"locale": locale})
                     .get("translatableResources") or {}).get("nodes") or []
            sample.append({"locale": locale, "sampled_products": len(nodes),
                           "translation_keys": sum(len(n.get("translations") or []) for n in nodes),
                           "outdated_keys": sum(1 for n in nodes for v in (n.get("translations") or [])
                                                if v.get("outdated"))})
        return {"sample_status": "SAMPLE_ONLY", "sampled_locales": sample,
                "not_full_coverage": True}
    optional("translations_sample", "read_translations", translations)

    result["status"] = ("BOUNDED_COMPLETE" if all(v.get("status") == "OK" for v in sections.values()) else "PARTIAL")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="out/sparesdat-admin-inventory/summary.json")
    args = parser.parse_args(argv)
    try:
        client = ShopifyAdminClient(
            os.getenv("SHOPIFY_SHOP", ""), os.getenv("SHOPIFY_ADMIN_ACCESS_TOKEN", ""),
            os.getenv("SHOPIFY_API_VERSION", DEFAULT_API_VERSION),
        )
        report = inventory(client)
        dest = Path(args.out)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        # Never log report values or server errors; only safe status and section names.
        print("Verified Boschino Admin API store; status=" + report["status"])
        for name, entry in report["sections"].items():
            print(f"{name}: {entry.get('status')}")
    except InventoryError as exc:
        print("Shopify inventory halted: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
