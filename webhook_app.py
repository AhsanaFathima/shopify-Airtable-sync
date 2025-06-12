from flask import Flask, request, jsonify
import os
import requests

app = Flask(__name__)

# Dummy Shopify function: Replace with your logic
def update_shopify_prices(sku, prices):
    # Here, implement the Shopify API call using requests
    print(f"Updating SKU {sku} with prices: {prices}")
    # Example: requests.put(...)
    return True

@app.route('/airtable-webhook', methods=['POST'])
def airtable_webhook():
    # Security check (optional)
    secret = request.headers.get('X-Secret-Token')
    if secret != os.environ.get('WEBHOOK_SECRET'):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json
    print("Webhook received:", data)
    
    # Expecting structure with SKU and price fields
    sku = data.get('SKU') or data.get('fields', {}).get('SKU')
    prices = {
        'UAE': data.get('UAE price') or data.get('fields', {}).get('UAE price'),
        'Asia': data.get('Asia Price') or data.get('fields', {}).get('Asia Price'),
        'America': data.get('America Price') or data.get('fields', {}).get('America Price'),
    }
    if sku:
        update_shopify_prices(sku, prices)
        return jsonify({"status": "success"}), 200
    else:
        return jsonify({"error": "SKU missing"}), 400

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)

