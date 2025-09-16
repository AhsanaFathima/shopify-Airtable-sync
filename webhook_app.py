import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ---------- Env ----------
SHOP = os.environ["SHOPIFY_SHOP"]             # e.g., "yourstore.myshopify.com"
TOKEN = os.environ["SHOPIFY_API_TOKEN"]       # e.g., "shpat_..."
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
PREFERRED_LOCATION_ID = os.getenv("SHOPIFY_LOCATION_ID")  # optional; if unset we'll use the primary location

# Map your Airtable markets to Shopify Market names (from your logs)
MARKET_NAMES = {
    "UAE": "United Arab Emirates",
    "Asia": "Asia Market",
    "America": "America & Australia Market",
}

# ---------- Caches ----------
CACHED_PRICE_LISTS = None
CACHED_PRIMARY_LOCATION_ID = None

def _json_headers():
    return {"X-Shopify-Access-Token": TOKEN, "Content-Type": "application/json"}

# ---------- GraphQL helper ----------
def shopify_graphql(query, variables=None):
    url = f"https://{SHOP}/admin/api/2024-01/graphql.json"
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    print(f"\n[GQL] POST {url}\nVars: {variables}", flush=True)
    resp = requests.post(url, headers=_json_headers(), json=payload)
    print("[GQL] Status:", resp.status_code, flush=True)
    print("[GQL] Body:", resp.text, flush=True)
    resp.raise_for_status()
    return resp.json()

# ---------- Markets / Price Lists ----------
def get_market_price_lists():
    """Fetch markets & attached price lists (catalogs)."""
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
          catalogs(first: 10) {
            nodes {
              id
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
    result = shopify_graphql(MARKET_QUERY, {"first": 20})
    if "data" not in result or "markets" not in result["data"]:
        print("ERROR: Could not find data.markets in result", flush=True)
        print("Raw result:", result, flush=True)
        return {}

    price_lists = {}
    print("\nDEBUG: --- Shopify Market Catalogs/PriceLists ---", flush=True)
    for market in result["data"]["markets"]["nodes"]:
        mname = market["name"]
        for catalog in market["catalogs"]["nodes"]:
            pl = catalog.get("priceList")
            print(f"  Market: {mname} | Catalog: {catalog.get('id')}", flush=True)
            if pl:
                print(f"    PriceList: {pl['name']} (ID: {pl['id']}, Currency: {pl['currency']})", flush=True)
                price_lists[mname] = {"id": pl["id"], "currency": pl["currency"]}
            else:
                print("    No price list attached.", flush=True)

    CACHED_PRICE_LISTS = price_lists
    print("DEBUG: price_lists mapping used for updates:", price_lists, flush=True)
    return price_lists

# ---------- Variants / Products ----------
def get_variant_product_and_inventory_by_sku(sku):
    """
    Return (variant_gid, product_gid, variant_numeric_id, inventory_item_id).
    We fetch the inventory_item_id via REST GET /variants/{id}.json
    """
    GET_VARIANT_QUERY = """
    query ($sku: String!) {
      productVariants(first: 1, query: $sku) {
        nodes {
          id
          sku
          product { id }
        }
      }
    }
    """
    res = shopify_graphql(GET_VARIANT_QUERY, {"sku": sku})
    nodes = res.get("data", {}).get("productVariants", {}).get("nodes", [])
    if not nodes:
        print("No variant found for SKU:", sku, flush=True)
        return None, None, None, None

    variant_gid = nodes[0]["id"]
    product_gid = nodes[0]["product"]["id"]
    variant_numeric = variant_gid.split("/")[-1]

    # REST: fetch inventory_item_id
    url = f"https://{SHOP}/admin/api/2024-01/variants/{variant_numeric}.json"
    r = requests.get(url, headers=_json_headers())
    print("[REST] GET variant:", r.status_code, r.text, flush=True)
    r.raise_for_status()
    inventory_item_id = r.json()["variant"]["inventory_item_id"]

    return variant_gid, product_gid, variant_numeric, inventory_item_id

def update_variant_default_price(variant_id_num, price, compare_at_price=None):
    """Update the default/base price and optional compare_at_price (REST)."""
    url = f"https://{SHOP}/admin/api/2024-01/variants/{variant_id_num}.json"
    variant_data = {"id": int(variant_id_num), "price": str(price)}
    if compare_at_price is not None:
        variant_data["compare_at_price"] = str(compare_at_price)
    payload = {"variant": variant_data}
    print(f"[REST] PUT default price {url} payload={payload}", flush=True)
    resp = requests.put(url, headers=_json_headers(), json=payload)
    print("[REST] default price resp:", resp.status_code, resp.text, flush=True)
    resp.raise_for_status()
    return resp.json()

def update_variant_details(variant_gid, title=None, barcode=None):
    """Update variant title/barcode (REST)."""
    if not (title or barcode):
        return None
    var_num = variant_gid.split("/")[-1]
    url = f"https://{SHOP}/admin/api/2024-01/variants/{var_num}.json"
    vdata = {"id": int(var_num)}
    if title:   vdata["title"] = title
    if barcode: vdata["barcode"] = barcode
    payload = {"variant": vdata}
    print(f"[REST] PUT variant details {url} payload={payload}", flush=True)
    resp = requests.put(url, headers=_json_headers(), json=payload)
    print("[REST] variant details resp:", resp.status_code, resp.text, flush=True)
    resp.raise_for_status()
    return resp.json()

def update_product_title(product_gid, new_title):
    """Update main product title (REST)."""
    pid = product_gid.split("/")[-1]
    url = f"https://{SHOP}/admin/api/2024-01/products/{pid}.json"
    payload = {"product": {"id": int(pid), "title": new_title}}
    print(f"[REST] PUT product title {url} payload={payload}", flush=True)
    resp = requests.put(url, headers=_json_headers(), json=payload)
    print("[REST] product title resp:", resp.status_code, resp.text, flush=True)
    resp.raise_for_status()
    return resp.json()

# ---------- Metafields ----------
def set_metafield(owner_id_gid, namespace, key, mtype, value):
    """Upsert metafield via GraphQL metafieldsSet."""
    METAFIELDS_SET = """
    mutation metafieldsSet($metafields: [MetafieldsSetInput!]!) {
      metafieldsSet(metafields: $metafields) {
        metafields { id namespace key type value }
        userErrors { field message }
      }
    }
    """
    variables = {
        "metafields": [{
            "ownerId": owner_id_gid,
            "namespace": namespace,
            "key": key,
            "type": mtype,
            "value": str(value)
        }]
    }
    print(f"Setting metafield {namespace}.{key}={value} on {owner_id_gid}", flush=True)
    result = shopify_graphql(METAFIELDS_SET, variables)
    try:
        errs = result["data"]["metafieldsSet"]["userErrors"]
        if errs:
            print("Metafield userErrors:", errs, flush=True)
    except Exception as e:
        print("Error reading metafield userErrors:", e, flush=True)
    return result

# ---------- Inventory ----------
def get_primary_location_id():
    """Return chosen (env) or primary location id (cached)."""
    global CACHED_PRIMARY_LOCATION_ID
    if PREFERRED_LOCATION_ID:
        return PREFERRED_LOCATION_ID
    if CACHED_PRIMARY_LOCATION_ID:
        return CACHED_PRIMARY_LOCATION_ID

    url = f"https://{SHOP}/admin/api/2024-01/locations.json"
    r = requests.get(url, headers=_json_headers())
    print("[REST] GET locations:", r.status_code, r.text, flush=True)
    r.raise_for_status()
    locs = r.json().get("locations", [])
    if not locs:
        raise RuntimeError("No locations found on store.")
    primary = next((l for l in locs if l.get("primary")), None)
    chosen = primary or locs[0]
    CACHED_PRIMARY_LOCATION_ID = str(chosen["id"])
    print("Using location:", CACHED_PRIMARY_LOCATION_ID, "| name:", chosen.get("name"), flush=True)
    return CACHED_PRIMARY_LOCATION_ID

def set_inventory_absolute(inventory_item_id, location_id, quantity):
    """Set absolute quantity at a location (REST inventory_levels/set)."""
    url = f"https://{SHOP}/admin/api/2024-01/inventory_levels/set.json"
    payload = {
        "inventory_item_id": int(inventory_item_id),
        "location_id": int(location_id),
        "available": int(quantity)
    }
    print(f"[REST] POST set inventory {url} payload={payload}", flush=True)
    resp = requests.post(url, headers=_json_headers(), json=payload)
    print("[REST] set inventory resp:", resp.status_code, resp.text, flush=True)
    resp.raise_for_status()
    return resp.json()

# ---------- Price Lists (fixed price + optional compare_at) ----------
def update_price_list(price_list_id, variant_gid, price_amount, currency, compare_at_amount=None):
    """
    Upsert fixed price (and optional compareAtPrice) in a price list via GraphQL.
    """
    MUT = """
    mutation priceListFixedPricesUpdate(
      $priceListId: ID!,
      $pricesToAdd: [PriceListPriceInput!]!,
      $variantIdsToDelete: [ID!]!
    ) {
      priceListFixedPricesUpdate(
        priceListId: $priceListId,
        pricesToAdd: $pricesToAdd,
        variantIdsToDelete: $variantIdsToDelete
      ) {
        pricesAdded {
          variant { id title }
          price { amount currencyCode }
          compareAtPrice { amount currencyCode }
        }
        userErrors { field message }
      }
    }
    """
    price_input = {
        "variantId": variant_gid,
        "price": {"amount": str(price_amount), "currencyCode": currency}
    }
    if compare_at_amount is not None:
        price_input["compareAtPrice"] = {"amount": str(compare_at_amount), "currencyCode": currency}

    variables = {
        "priceListId": price_list_id,
        "pricesToAdd": [price_input],
        "variantIdsToDelete": []
    }
    res = shopify_graphql(MUT, variables)
    try:
        errs = res["data"]["priceListFixedPricesUpdate"]["userErrors"]
        if errs:
            print("userErrors:", errs, flush=True)
    except Exception as e:
        print("Could not read userErrors:", e, flush=True)
    return res

# ---------- Flask ----------
@app.route("/", methods=["GET"])
def home():
    return "Airtable-Shopify Sync Webhook is running!", 200

@app.route("/airtable-webhook", methods=["POST"])
def airtable_webhook():
    try:
        # Security
        secret = request.headers.get("X-Secret-Token")
        print("Secret header:", secret, flush=True)
        if secret != WEBHOOK_SECRET:
            print("Unauthorized!", flush=True)
            return jsonify({"error": "Unauthorized"}), 401

        # Payload
        data = request.json or {}
        print("Received data:", data, flush=True)

        sku = data.get("SKU")
        prices = {
            "UAE": data.get("UAE price"),
            "Asia": data.get("Asia Price"),
            "America": data.get("America Price")
        }
        uae_compare_price = data.get("UAE Comparison Price")
        qty_abs = data.get("Qty given in shopify")
        title = data.get("Title")
        barcode = data.get("Barcode")
        size_value = data.get("Size")

        print("SKU:", sku, flush=True)
        print("Prices:", prices, flush=True)
        print("UAE Comparison Price:", uae_compare_price, flush=True)
        print("Qty given in shopify:", qty_abs, flush=True)
        print("Title:", title, flush=True)
        print("Barcode:", barcode, flush=True)
        print("Size:", size_value, flush=True)

        if not sku:
            return jsonify({"error": "SKU missing"}), 400

        # Variant/Product + inventory item id
        variant_gid, product_gid, variant_num, inventory_item_id = get_variant_product_and_inventory_by_sku(sku)
        if not variant_gid:
            return jsonify({"error": f"Variant with SKU {sku} not found"}), 404
        print("variant_gid:", variant_gid, "product_gid:", product_gid, "variant_num:", variant_num, "inventory_item_id:", inventory_item_id, flush=True)

        # Variant details
        if title or barcode:
            update_variant_details(variant_gid, title=title, barcode=barcode)
        if title:
            update_product_title(product_gid, title)

        # Default/base price = UAE price (+ optional compare_at from Airtable)
        if prices.get("UAE") is not None:
            update_variant_default_price(variant_num, prices["UAE"], compare_at_price=uae_compare_price)

        # Metafield custom.size on variant
        if size_value is not None and str(size_value) != "":
            set_metafield(
                owner_id_gid=variant_gid,
                namespace="custom",
                key="size",
                mtype="single_line_text_field",
                value=str(size_value)
            )

        # Inventory absolute quantity at a location
        inventory_update = None
        if qty_abs is not None:
            loc_id = get_primary_location_id()
            inventory_update = set_inventory_absolute(inventory_item_id, loc_id, qty_abs)

        # Price lists per market (+ compare_at on UAE list only)
        price_lists = get_market_price_lists()
        print("Price lists:", price_lists, flush=True)

        price_updates = {}
        for market_key, amount in prices.items():
            if amount is None:
                continue
            mname = MARKET_NAMES.get(market_key)
            if not mname or mname not in price_lists:
                print(f"No price list for market {market_key}", flush=True)
                continue
            pl = price_lists[mname]
            compare_amt = uae_compare_price if market_key == "UAE" and uae_compare_price is not None else None
            print(f"Updating PL={pl['id']} Market={market_key} price={amount} {pl['currency']} compare_at={compare_amt}", flush=True)
            res = update_price_list(pl["id"], variant_gid, amount, pl["currency"], compare_at_amount=compare_amt)
            price_updates[market_key] = res

        return jsonify({
            "status": "success",
            "variant_id": variant_gid,
            "product_id": product_gid,
            "inventory_update": inventory_update,
            "price_list_updates": price_updates
        }), 200

    except Exception as e:
        import traceback
        print("ERROR:", str(e), flush=True)
        print(traceback.format_exc(), flush=True)
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
