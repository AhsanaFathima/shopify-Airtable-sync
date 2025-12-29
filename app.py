import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

print("🚀 Flask app starting...", flush=True)

# ---------- ENV ----------
SHOP = os.environ["SHOPIFY_SHOP"]
TOKEN = os.environ["SHOPIFY_API_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-07")

# ---------- MARKET MAPPING ----------
MARKET_NAMES = {
    "UAE": "United Arab Emirates",
    "Asia": "Asia Market with 55 rate",
    "America": "America catlog",
}

# ---------- CACHE ----------
CACHED_PRICE_LISTS = None

# ---------- HELPERS ----------
def _json_headers():
    return {
        "X-Shopify-Access-Token": TOKEN,
        "Content-Type": "application/json",
    }

def _graphql_url():
    return f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"

def _rest_url(path):
    return f"https://{SHOP}/admin/api/{API_VERSION}/{path}"

def _to_number(x):
    try:
        return float(x) if x not in (None, "") else None
    except Exception:
        return None

# ---------- GRAPHQL ----------
def shopify_graphql(query, variables=None):
    resp = requests.post(
        _graphql_url(),
        headers=_json_headers(),
        json={"query": query, "variables": variables},
    )
    resp.raise_for_status()
    return resp.json()

# ---------- PRICE LISTS ----------
def get_market_price_lists():
    global CACHED_PRICE_LISTS

    if CACHED_PRICE_LISTS:
        return CACHED_PRICE_LISTS

    QUERY = """
    query {
      catalogs(first: 20, type: MARKET) {
        nodes {
          title
          status
          priceList {
            id
            currency
          }
        }
      }
    }
    """

    res = shopify_graphql(QUERY)
    price_lists = {}

    for c in res.get("data", {}).get("catalogs", {}).get("nodes", []):
        if c.get("status") == "ACTIVE" and c.get("priceList"):
            price_lists[c["title"]] = {
                "id": c["priceList"]["id"],
                "currency": c["priceList"]["currency"],
            }

    print("📊 Price lists:", price_lists, flush=True)
    CACHED_PRICE_LISTS = price_lists
    return price_lists

# ---------- VARIANT ----------
def get_variant_product_and_inventory_by_sku(sku):
    QUERY = """
    query ($q: String!) {
      productVariants(first: 1, query: $q) {
        nodes {
          id
        }
      }
    }
    """

    res = shopify_graphql(QUERY, {"q": f"sku:{sku}"})
    nodes = res.get("data", {}).get("productVariants", {}).get("nodes", [])

    if not nodes:
        return None, None, None

    variant_gid = nodes[0]["id"]
    variant_id = variant_gid.split("/")[-1]

    r = requests.get(_rest_url(f"variants/{variant_id}.json"), headers=_json_headers())
    r.raise_for_status()
    inventory_item_id = r.json()["variant"]["inventory_item_id"]

    return variant_gid, variant_id, inventory_item_id

# ---------- CURRENT SHOPIFY PRICES ----------
def get_variant_default_price(variant_id):
    r = requests.get(_rest_url(f"variants/{variant_id}.json"), headers=_json_headers())
    r.raise_for_status()
    return float(r.json()["variant"]["price"])

def get_price_list_price(price_list_id, variant_gid):
    QUERY = """
    query ($pl: ID!, $vid: ID!) {
      priceList(id: $pl) {
        prices(first: 1, query: $vid) {
          nodes {
            price { amount }
          }
        }
      }
    }
    """
    try:
        res = shopify_graphql(QUERY, {"pl": price_list_id, "vid": variant_gid})
        price_list = res.get("data", {}).get("priceList")
        if not price_list:
            return None

        nodes = price_list.get("prices", {}).get("nodes", [])
        if not nodes:
            return None

        return float(nodes[0]["price"]["amount"])
    except Exception as e:
        print("⚠️ Failed to read price list price:", e, flush=True)
        return None

# ---------- UPDATE ----------
def update_variant_default_price(variant_id, price):
    print(f"💲 Updating UAE default price → {price}", flush=True)
    requests.put(
        _rest_url(f"variants/{variant_id}.json"),
        headers=_json_headers(),
        json={"variant": {"id": int(variant_id), "price": str(price)}},
    ).raise_for_status()

def update_price_list(price_list_id, variant_gid, price, currency):
    print(f"➡️ Updating price list {price_list_id} → {price}", flush=True)

    MUTATION = """
    mutation ($pl: ID!, $prices: [PriceListPriceInput!]!) {
      priceListFixedPricesAdd(priceListId: $pl, prices: $prices) {
        userErrors { message }
      }
    }
    """

    shopify_graphql(
        MUTATION,
        {
            "pl": price_list_id,
            "prices": [{
                "variantId": variant_gid,
                "price": {"amount": str(price), "currencyCode": currency},
            }],
        },
    )

# ---------- ROUTES ----------
@app.route("/", methods=["GET"])
def home():
    return "Airtable-Shopify Sync Running", 200

@app.route("/airtable-webhook", methods=["POST"])
def airtable_webhook():
    print("\n🔔 WEBHOOK HIT", flush=True)

    secret = (request.headers.get("X-Secret-Token") or "").strip()
    expected = (WEBHOOK_SECRET or "").strip()

    print("🔑 Secret received:", repr(secret), flush=True)
    print("🔑 Secret expected:", repr(expected), flush=True)

    if secret != expected:
        print("❌ Unauthorized", flush=True)
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json or {}
    sku = data.get("SKU")

    prices = {
        "UAE": _to_number(data.get("UAE price")),
        "Asia": _to_number(data.get("Asia Price")),
        "America": _to_number(data.get("America Price")),
    }

    if not sku:
        return jsonify({"error": "SKU missing"}), 400

    variant_gid, variant_id, inventory_item_id = get_variant_product_and_inventory_by_sku(sku)
    if not variant_gid:
        return jsonify({"error": "Variant not found"}), 404

    # ✅ UAE default price (auto detect)
    if prices["UAE"] is not None:
        old_price = get_variant_default_price(variant_id)
        if old_price != prices["UAE"]:
            update_variant_default_price(variant_id, prices["UAE"])
        else:
            print("⏭️ UAE unchanged", flush=True)

    price_lists = get_market_price_lists()

    # ✅ Market prices (auto detect)
    for market, new_price in prices.items():
        if new_price is None:
            continue

        market_name = MARKET_NAMES.get(market)
        if not market_name:
            continue

        pl = price_lists.get(market_name)
        if not pl:
            continue

        old_price = get_price_list_price(pl["id"], variant_gid)
        print(f"🔎 {market} | Old: {old_price} | New: {new_price}", flush=True)

        if old_price == new_price:
            print(f"⏭️ {market} unchanged", flush=True)
            continue

        update_price_list(pl["id"], variant_gid, new_price, pl["currency"])

    print("🎉 SYNC COMPLETE", flush=True)
    return jsonify({"status": "success"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
