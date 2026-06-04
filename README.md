# HappyBaby

Single-product store page configured for CJ Dropshipping SKU `CJYD233025008HS`.

## Setup

1. Open `.env`.
2. Fill in your CJ API credentials:
   - `CJ_API_KEY` (recommended by CJ)
   - or `CJ_ACCESS_TOKEN` (if you already have a valid token)
3. Fill in Stripe settings in `.env`:
   - `STRIPE_SECRET_KEY`
   - `STRIPE_PUBLISHABLE_KEY`
   - `STRIPE_WEBHOOK_SECRET`
   - `STRIPE_CURRENCY`
   - `STRIPE_PRICE_STANDARD_SEK`
   - `STRIPE_PRICE_NURSERY_SEK`
4. Keep `CJ_PRODUCT_SKU` as `CJYD233025008HS` to stay locked to that one CJ product.
5. Run `python3 server.py` and open `http://localhost:8000/happy_baby_product_store.html`.

## Files

- `happy_baby_product_store.html` - product page that renders live CJ image/title/description/variants
- `.env` - your private CJ credentials and SKU
- `.env.example` - template for environment variables
- `server.py` - local API proxy that fetches product data from CJ
