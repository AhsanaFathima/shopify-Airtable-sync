import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# Get secrets from environment variables (do not hardcode sensitive data!)
SHOP = os.environ["SHOPIFY_SHOP"]         # e.g., "yourstore.myshopify.com"
TOKEN = os.environ["SHOPIFY_API_TOKEN"]   # e.g., starts with shpat_
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

# Market display names you expect from Airtable
MARKET_NAMES = {
    "UAE": "UAE",
    "Asia": "Asia",
    "America": "America"
}

# In-memory cache for price lists
CACHED_PRICE_LISTS = None

def shopify_graphql(query, variables=None):
    url = f"https://{SHOP}/admin/api/2024-01/graphql.json"
    headers = {
        "X-Shopify-Access-Token": TOKEN,
        "Content-Type": "application/json"
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    print(f"Sending GraphQL to: {url} | Variables: {variables}", flush=True)
    response = requests.post(url, headers=headers, json=payload)
    print("GraphQL status code:", response.status_code, flush=True)
    print("GraphQL response:", response.text, flush=True)
    response.raise_for_status()
    return response.json()

def get_market_price_lists():
    """Fetch all markets and price lists from Shopify, with caching."""
    global CACHED_PRICE_LISTS
    if CACHED_PRICE_LISTS is not None:
        print("Using cached price lists.", flush=True)
        return CACHED_PRICE_LISTS

    MARKET_QUERY = """
    query ($first: Int!) {
      markets(first: $first) {
        nodes {
          id
          name
          catalogs(first: 5) {
            nodes {
              priceList {
                id
                name
                currency
              }
            }
          }
        }
      }
    }
    """
    result = shopify_graphql(MARKET_QUERY, {"first": 10})
    price_lists = {}
    for market in result["data"]["markets"]["nodes"]:
        name = market["name"]
        for catalog in market["catalogs"]["nodes"]:
            pl = catalog["priceList"]
            if pl:
                price_lists[name] = {"id": pl["id"], "currency": pl["currency"]}
    CACHED_PRICE_LISTS = price_lists
    print("Cached price lists:", CACHED_PRICE_LISTS, flush=True)
    return price_lists

def get_variant_id_by_sku(sku):
    """Find a product variant ID by SKU"""
    GET_VARIANT_QUERY = """
    query ($sku: String!) {
      productVariants(first: 1, query: $sku) {
        nodes {
          id
          sku
          product {
            id
          }
        }
      }
    }
    """
    variant_result = shopify_graphql(GET_VARIANT_QUERY, {"sku": sku})
    print(f"Variant query result for SKU {sku}:", variant_result, flush=True)
    nodes = variant_result["data"]["productVariants"]["nodes"]
    if not nodes:
        print("No variant found for SKU:", sku, flush=True)
        return None, None
    variant_id = nodes[0]["id"]
    product_id = nodes[0]["product"]["id"]
    return variant_id, product_id

def update_price_list(price_list_id, variant_id, amount, currency):
    """Update price for a variant in a specific price list"""
    PRICE_LIST_MUTATION = """
    mutation priceListFixedPricesUpdate($priceListId: ID!, $pricesToAdd: [PriceListPriceInput!]!) {
      priceListFixedPricesUpdate(priceListId: $priceListId, pricesToAdd: $pricesToAdd) {
        pricesAdded {
          variant {
            id
            title
          }
          price {
            amount
            currencyCode
          }
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    prices_to_add = [{
        "variantId": variant_id,
        "price": {
            "amount": str(amount),
            "currencyCode": currency
        }
    }]
    variables = {
        "priceListId": price_list_id,
        "pricesToAdd": prices_to_add
    }
    result = shopify_graphql(PRICE_LIST_MUTATION, variables)
    # Print userErrors if present
    try:
        user_errors = result["data"]["priceListFixedPricesUpdate"]["userErrors"]
        if user_errors:
            print("userErrors in price update:", user_errors, flush=True)
    except Exception as e:
        print("Error extracting userErrors:", e, flush=True)
    return result

def update_variant_details(variant_id, title=None, barcode=None):
    """Update product variant's title and barcode using Shopify REST API"""
    url = f"https://{SHOP}/admin/api/2024-01/variants/{variant_id.split('/')[-1]}.json"
    headers = {
        "X-Shopify-Access-Token": TOKEN,
        "Content-Type": "application/json"
    }
    variant_data = {}
    if barcode:
        variant_data["barcode"] = barcode
    if title:
        variant_data["title"] = title  # Be aware: usually "title" here means the option, not product title
    if not variant_data:
        return None  # Nothing to update
    payload = {"variant": variant_data}
    print(f"Updating variant details for {variant_id}: {payload}", flush=True)
    resp = requests.put(url, headers=headers, json=payload)
    print("Variant update response:", resp.status_code, resp.text, flush=True)
    return resp.json()

def update_product_title(product_id, new_title):
    """Update main product title (not variant) using Shopify REST API"""
    url = f"https://{SHOP}/admin/api/2024-01/products/{product_id.split('/')[-1]}.json"
    headers = {
        "X-Shopify-Access-Token": TOKEN,
        "Content-Type": "application/json"
    }
    payload = {"product": {"title": new_title}}
    print(f"Updating product title for {product_id}: {payload}", flush=True)
    resp = requests.put(url, headers=headers, json=payload)
    print("Product update response:", resp.status_code, resp.text, flush=True)
    return resp.json()

@app.route("/airtable-webhook", methods=["POST"])
def airtable_webhook():
    try:
        secret = request.headers.get("X-Secret-Token")
        print("Secret header:", secret, flush=True)
        if secret != WEBHOOK_SECRET:
            print("Unauthorized!", flush=True)
            return jsonify({"error": "Unauthorized"}), 401

        data = request.json
        print("Received data:", data, flush=True)
        sku = data.get("SKU")
        prices = {
            "UAE": data.get("UAE price"),
            "Asia": data.get("Asia Price"),
            "America": data.get("America Price")
        }
        title = data.get("Title")
        barcode = data.get("Barcode")
        print("SKU:", sku, flush=True)
        print("Prices:", prices, flush=True)
        print("Title:", title, flush=True)
        print("Barcode:", barcode, flush=True)
        if not sku:
            print("SKU missing!", flush=True)
            return jsonify({"error": "SKU missing"}), 400

        # 1. Find the variant ID and product ID by SKU
        variant_id, product_id = get_variant_id_by_sku(sku)
        print("Variant ID:", variant_id, flush=True)
        print("Product ID:", product_id, flush=True)
        if not variant_id:
            print(f"Variant with SKU {sku} not found!", flush=True)
            return jsonify({"error": f"Variant with SKU {sku} not found"}), 404

        # 2. Update variant title and barcode if provided
        if title or barcode:
            update_variant_details(variant_id, title=title, barcode=barcode)

        # 3. (Optional) Update the main product title if provided (uncomment to enable)
        # if title:
        #     update_product_title(product_id, title)

        # 4. Get price list IDs (cached)
        price_lists = get_market_price_lists()
        print("Price lists:", price_lists, flush=True)

        # 5. Update prices per market
        update_results = {}
        for market, price in prices.items():
            print(f"Processing market: {market}, price: {price}", flush=True)
            if price and market in MARKET_NAMES and MARKET_NAMES[market] in price_lists:
                pl_info = price_lists[MARKET_NAMES[market]]
                price_list_id = pl_info["id"]
                currency = pl_info["currency"]
                print(f"Updating price list {price_list_id} for market {market} with price {price} {currency}", flush=True)
                result = update_price_list(price_list_id, variant_id, price, currency)
                print(f"Price update result for {market}:", result, flush=True)
                update_results[market] = result
            else:
                print(f"No update for market: {market} (missing price or price list)", flush=True)

        print("All update results:", update_results, flush=True)
        return jsonify({"status": "success", "results": update_results}), 200
    except Exception as e:
        import traceback
        print("ERROR:", str(e), flush=True)
        print(traceback.format_exc(), flush=True)
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
