import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sparesdat_shopify_readonly_inventory.py"
spec = importlib.util.spec_from_file_location("sparesdat_inventory", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class FakeClient:
    version = "2026-10"

    def __init__(self, shop=mod.EXPECTED_SHOP, scopes=None):
        self.shop = shop
        self.scopes = scopes if scopes is not None else [
            "read_products", "read_metaobject_definitions", "read_metaobjects", "read_files"
        ]
        self.operations = []

    def query(self, op, graph_query, variables=None):
        self.operations.append((op, graph_query, variables))
        if op == "identity":
            return {"shop": {"id": "gid://shopify/Shop/1", "myshopifyDomain": self.shop, "name": "Boschino"},
                    "currentAppInstallation": {"accessScopes": [{"handle": s} for s in self.scopes]}}
        if op == "products_count":
            return {"productsCount": {"count": 51881, "precision": "EXACT"}}
        if op == "metaobject_definitions":
            return {"metaobjectDefinitions": {"nodes": [{"type": "spotrebic", "name": "Appliance",
                        "metaobjectsCount": 1500, "fieldDefinitions": [{"key": "pdf_files", "type": {"name": "list.file_reference"}}]}],
                        "pageInfo": {"hasNextPage": False, "endCursor": None}}}
        if op == "product_metafield_definitions":
            return {"metafieldDefinitions": {"nodes": [{"namespace": "custom", "key": "compatible_appliances", "type": {"name": "json"}}],
                        "pageInfo": {"hasNextPage": False, "endCursor": None}}}
        if op == "appliance_sample":
            return {"metaobjects": {"nodes": [{"id": "gid://shopify/Metaobject/2", "handle": "bosch-mcm3200w-01",
                        "fields": [{"key": "pdf_files"}, {"key": "brand"}]}],
                        "pageInfo": {"hasNextPage": True}}}
        if op == "files_sample":
            return {"files": {"nodes": [{"id": "gid://shopify/GenericFile/3", "__typename": "GenericFile"}],
                              "pageInfo": {"hasNextPage": True}}}
        raise AssertionError("Unexpected operation: " + op)


class InventoryTests(unittest.TestCase):
    def test_store_is_exact_host_only(self):
        for invalid in ("miele.applias.cz", "https://vvircm-fz.myshopify.com", "shop.myshopify.com", "", "vvircm-fz.myshopify.com.evil.tld"):
            with self.assertRaises(mod.InventoryError):
                mod.ShopifyAdminClient(invalid, "fake-nonsecret-token")

    def test_no_token_fails_before_api(self):
        with self.assertRaises(mod.InventoryError):
            mod.ShopifyAdminClient(mod.EXPECTED_SHOP, "")

    def test_shop_identity_mismatch_stops(self):
        fake = FakeClient(shop="some-other-shop.myshopify.com")
        with self.assertRaisesRegex(mod.InventoryError, "WRONG_SHOP_IDENTITY"):
            mod.inventory(fake)
        self.assertEqual(len(fake.operations), 1)

    def test_readonly_queries_and_report(self):
        fake = FakeClient()
        report = mod.inventory(fake)
        self.assertEqual(report["status"], "COMPLETE")
        self.assertEqual(report["sections"]["products"]["count"]["count"], 51881)
        self.assertEqual(report["sections"]["metaobject_definitions"]["definitions"][0]["fields"][0]["key"], "pdf_files")
        self.assertEqual(report["sections"]["generic_files_sample"]["sampled"], 1)
        self.assertNotIn("secret", repr(report).lower())
        for operation, query, variables in fake.operations:
            self.assertTrue(query.strip().startswith("query "))
            self.assertNotIn("mutation", query.lower())

    def test_missing_optional_scope_is_reported_not_guessed(self):
        fake = FakeClient(scopes=["read_products"])
        report = mod.inventory(fake)
        self.assertEqual(report["status"], "PARTIAL")
        self.assertEqual(report["sections"]["generic_files_sample"]["status"], "NO_SCOPE")
        self.assertNotIn("files_sample", [x[0] for x in fake.operations])

    def test_pagination_requires_progress(self):
        class BadClient:
            def query(self, *args, **kwargs):
                return {"metaobjectDefinitions": {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": None}}}
        with self.assertRaisesRegex(mod.InventoryError, "BAD_CURSOR"):
            mod.page_definitions(BadClient(), "metaobject_definitions", mod.META_DEFS, "metaobjectDefinitions")

    def test_query_blocks_mutation(self):
        client = mod.ShopifyAdminClient(mod.EXPECTED_SHOP, "fake-nonsecret-token", opener=lambda *a, **kw: None)
        with self.assertRaisesRegex(mod.InventoryError, "QUERY_ONLY_REQUIRED"):
            client.query("bad", "mutation { productDelete(input: {id: \"1\"}) { userErrors {message} } }")


if __name__ == "__main__":
    unittest.main()
