# Boschino Merchant local inventory automation

This repository validates and uploads Boschino.cz local inventory / local availability data from Shopify to Google Merchant Center.

## What it does

- Fetches Shopify product variants from `vvircm-fz.myshopify.com`.
- Builds Merchant Center offer IDs in the Shopify App API format:

```text
shopify_ZZ_{numeric_product_id}_{numeric_variant_id}
```

- Fetches existing Merchant Center products and uploads only matching offers.
- Generates three local inventory rows per valid offer, one for each verified Google store code.
- Produces audit artifacts for validation before enabling a real upload.

## Verified store codes

```text
06275645225922442974  Praha 8 / Horovo namesti
06824451997053158379  Praha 10 / Francouzska
14326918149907693002  Benatky nad Jizerou
```

## Verified Shopify location mapping

```text
gid://shopify/Location/115128074571 -> 06275645225922442974
gid://shopify/Location/115128107339 -> 06824451997053158379
gid://shopify/Location/115128140107 -> 14326918149907693002
```

## Availability rules

1. Local quantity is greater than zero:
   - `quantity = local_quantity`
   - `availability = limited_availability` for 1-2 pieces
   - `availability = in_stock` for 3+ pieces
   - `pickup_sla = same day`

2. Local quantity is zero, but another mapped Boschino location has stock:
   - `quantity = 0`
   - `availability = in_stock`
   - `pickup_sla = next day`

3. All mapped locations are zero, but the global Merchant offer is in stock:
   - `quantity = 0`
   - `availability = in_stock`
   - `pickup_sla = 6-day`

4. All mapped locations are zero and the global Merchant offer is out of stock:
   - `quantity = 0`
   - `availability = out_of_stock`
   - `pickup_sla` is left blank

## Required GitHub Actions secrets

Add these in GitHub repo settings:

```text
SHOPIFY_ADMIN_TOKEN
SHOPIFY_SHOP_DOMAIN
GOOGLE_MERCHANT_ID
GOOGLE_SERVICE_ACCOUNT_JSON
```

Recommended value:

```text
SHOPIFY_SHOP_DOMAIN=vvircm-fz.myshopify.com
```

`GOOGLE_SERVICE_ACCOUNT_JSON` must be the full service account JSON with access to the relevant Merchant Center account.

## Optional GitHub Actions variables

```text
SHOPIFY_API_VERSION=2025-10
GOOGLE_LANGUAGE=cs
GOOGLE_FEED_LABEL=CZ
```

## Running

The workflow runs nightly at 01:00 UTC and can be started manually from GitHub Actions.

Manual run defaults to dry-run validation. It does not upload data unless `upload=true` is selected.

Recommended first run:

```text
upload=false
```

Recommended first upload test:

```text
upload=true
upload_limit=3
```

Full upload:

```text
upload=true
upload_limit=0
```

## Audit artifacts

Every run uploads an artifact named `merchant-local-inventory-audit` containing:

```text
local_inventory_shopify.tsv
local_inventory_shopify_preview.csv
summary.json
control_sku_8996470703070.csv
skipped_missing_sku.csv
skipped_inactive_product.csv
skipped_not_in_merchant.csv
```

The control SKU is:

```text
8996470703070
```

Expected Merchant offer ID:

```text
shopify_ZZ_15493147984203_56386003730763
```

Expected local quantities:

```text
06275645225922442974  0
06824451997053158379  4
14326918149907693002  105
```
