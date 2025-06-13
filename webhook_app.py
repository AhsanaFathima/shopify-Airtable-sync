import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

SHOP = os.environ["fragrantsouq.com"]  # e.g., "yourstore.myshopify.com"
TOKEN = os.environ["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"]
WEBHOOK_SECRET = os.environ["1985447a1574b629ae70dd9f8a1df56f"]

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
    response = requests.post(url, headers=headers, json=payload)
    response.raise_for_status()
    return response.json()

def get_market_price_lists():
    """Fetch all markets and price lists from Shopify, with caching."""
    global CACHED_PRICE_LISTS
    if CACHED_PRICE_LISTS is not None:
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
    return price_lists

def get_variant_id_by_sku(sku):
    """Find a product variant ID by SKU"""
    GET_VARIANT_QUERY = """
    query ($sku: String!) {
      productVariants(first: 1, query: $sku) {
        nodes {
          id
          sku
        }
      }
    }
    """
    variant_result = shopify_graphql(GET_VARIANT_QUERY, {"sku": sku})
    nodes = variant_result["data"]["productVariants"]["nodes"]
    if not nodes:
        return None
    return nodes[0]["id"]

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
    return shopify_graphql(PRICE_LIST_MUTATION, variables)

@app.route("/airtable-webhook", methods=["POST"])
def airtable_webhook():
    # Security
    secret = request.headers.get("X-Secret-Token")
    if secret != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json
    sku = data.get("SKU")
    prices = {
        "UAE": data.get("UAE price"),
        "Asia": data.get("Asia Price"),
        "America": data.get("America Price")
    }
    if not sku:
        return jsonify({"error": "SKU missing"}), 400

    # 1. Find the variant ID by SKU
    variant_id = get_variant_id_by_sku(sku)
    if not variant_id:
        return jsonify({"error": f"Variant with SKU {sku} not found"}), 404

    # 2. Get price list IDs (cached)
    price_lists = get_market_price_lists()

    # 3. Update prices per market
    update_results = {}
    for market, price in prices.items():
        if price and market in MARKET_NAMES and MARKET_NAMES[market] in price_lists:
            pl_info = price_lists[MARKET_NAMES[market]]
            price_list_id = pl_info["id"]
            currency = pl_info["currency"]
            result = update_price_list(price_list_id, variant_id, price, currency)
            update_results[market] = result

    return jsonify({"status": "success", "results": update_results}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
