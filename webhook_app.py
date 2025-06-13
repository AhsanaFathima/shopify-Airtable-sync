import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# Load secrets from environment
SHOP = os.environ["SHOPIFY_SHOP"]  # e.g., "yourstore.myshopify.com"
TOKEN = os.environ["SHOPIFY_API_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

# Map your markets to their price list IDs and currencies
# You should run the MARKET_QUERY once (see code below) to get these IDs and fill them in here:
MARKET_PRICE_LISTS = {
    "UAE": {"id": "gid://shopify/PriceList/1234567890", "currency": "AED"},
    "Asia": {"id": "gid://shopify/PriceList/2345678901", "currency": "USD"},
    "America": {"id": "gid://shopify/PriceList/3456789012", "currency": "USD"},
}

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

@app.route("/airtable-webhook", methods=["POST"])
def airtable_webhook():
    # Check webhook security
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

    # Find variant by SKU
    variant_result = shopify_graphql(GET_VARIANT_QUERY, {"sku": sku})
    nodes = variant_result["data"]["productVariants"]["nodes"]
    if not nodes:
        return jsonify({"error": f"Variant with SKU {sku} not found"}), 404
    variant_id = nodes[0]["id"]

    # For each market, update price if value provided
    update_results = {}
    for market, price in prices.items():
        if price and market in MARKET_PRICE_LISTS:
            price_list_id = MARKET_PRICE_LISTS[market]["id"]
            currency = MARKET_PRICE_LISTS[market]["currency"]
            prices_to_add = [{
                "variantId": variant_id,
                "price": {
                    "amount": str(price),
                    "currencyCode": currency
                }
            }]
            variables = {
                "priceListId": price_list_id,
                "pricesToAdd": prices_to_add
            }
            result = shopify_graphql(PRICE_LIST_MUTATION, variables)
            update_results[market] = result

    return jsonify({"status": "success", "results": update_results}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
