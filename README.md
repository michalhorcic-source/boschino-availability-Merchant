# Boschino Supplemental Source 3 feed

This repository publishes one product-level Google Merchant Center supplemental feed for Boschino.cz:

```text
SUPPLEMENTAL_SOURCE_3.tsv
```

Local inventory is intentionally out of scope for this repository and will be handled in a separate repository.

## What it does

- Fetches Shopify product variants from `vvircm-fz.myshopify.com`.
- Builds Merchant Center offer IDs in the Shopify App API format:

```text
shopify_ZZ_{numeric_product_id}_{numeric_variant_id}
```

- Generates `SUPPLEMENTAL_SOURCE_3.tsv` for Google Merchant Center.
- Publishes the feed to GitHub Pages for Merchant scheduled fetch.
- Does not upload products through the Merchant API.
- Does not generate or publish a local inventory feed.

## Public URLs

```text
https://michalhorcic-source.github.io/boschino-availability-Merchant/SUPPLEMENTAL_SOURCE_3.tsv
https://michalhorcic-source.github.io/boschino-availability-Merchant/summary.json
```

## Feed columns

```text
id
availability
price
sale price
sell on google quantity
```

## Availability rules

A variant is exported only when it has a SKU and the Shopify product status is `ACTIVE`.

The feed sets:

```text
availability = in_stock
```

when at least one of these is true:

- Shopify `availableForSale` is `true`.
- Shopify `inventoryQuantity` or product `totalInventory` is greater than zero.
- Shopify availability metafields indicate that the item is available or in stock.

Otherwise the feed sets:

```text
availability = out_of_stock
```

## Price rules

- `price` is the normal Merchant price in `CZK`.
- `sale price` is filled only when Shopify `compareAtPrice` is higher than current `price`.
- `sell on google quantity` is the maximum of Shopify variant `inventoryQuantity` and product `totalInventory`.

## Required GitHub Actions secrets

```text
SHOPIFY_ADMIN_ACCESS_TOKEN
SHOPIFY_SHOP
```

Recommended value:

```text
SHOPIFY_SHOP=vvircm-fz.myshopify.com
```

## Optional GitHub Actions variables

```text
SHOPIFY_API_VERSION=2025-10
```

## Running

The workflow runs once per day from GitHub Actions and can also be started manually.

Current schedule:

```text
15 23 * * * UTC
```

This is shortly after midnight in Czech time during summer time. GitHub Actions schedules use UTC.

## Output artifacts

Every run uploads an artifact named:

```text
supplemental-source-3-feed
```

It contains:

```text
SUPPLEMENTAL_SOURCE_3.tsv
supplemental_source_3_preview.csv
control_variant_56264244887883_supplemental.csv
skipped_missing_sku.csv
skipped_inactive_product.csv
summary.json
```

## Control product

```text
shopify_ZZ_15464863891787_56264244887883
```

This control row is exported to:

```text
control_variant_56264244887883_supplemental.csv
```
