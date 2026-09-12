from flask import Flask, render_template_string, request
import requests
import os
from dotenv import load_dotenv

load_dotenv(override=True)

API_KEY = os.getenv("PARSE_API_KEY")

print("API key loaded:", bool(API_KEY))
if API_KEY:
    print("API key length:", len(API_KEY))

app = Flask(__name__)

# ============================================================
# API CONFIG
# ============================================================

APOLLO_SCRAPER_ID = "30394a9d-d3c7-4bd6-803f-34561279285d"
MEDPLUS_SCRAPER_ID = "8285ba5b-0a0a-4525-8706-4fed1d6fc5cc"
PHARMEASY_SCRAPER_ID = "e7c250a1-5824-4027-9f84-c63d92aa5889"

PARSE_BASE_URL = "https://api.parse.bot/scraper"


# ============================================================
# COMMON HELPERS
# ============================================================

def get_products(result):
    if not isinstance(result, dict):
        return []

    data = result.get("data", result)

    if isinstance(data, dict):
        products = data.get("products")
        if isinstance(products, list):
            return products

        results = data.get("results")
        if isinstance(results, list):
            return results

    if isinstance(data, list):
        return data

    return []


def first_value(product, *keys):
    if not isinstance(product, dict):
        return None

    for key in keys:
        value = product.get(key)
        if value is not None and value != "":
            return value

    return None


def to_float(value, default=None):
    try:
        if value is None or value == "":
            return default

        if isinstance(value, str):
            value = (
                value.replace("₹", "")
                .replace(",", "")
                .replace("INR", "")
                .strip()
            )

        return float(value)

    except (TypeError, ValueError):
        return default


# ============================================================
# APOLLO SEARCH
# ============================================================

def search_apollo(query, pincode="110001"):
    url = f"{PARSE_BASE_URL}/{APOLLO_SCRAPER_ID}/search_medicines"

    headers = {
        "X-API-Key": API_KEY
    }

    params = {
        "query": query,
        "pincode": pincode
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=25
        )

        print("Apollo HTTP status:", response.status_code)

        if response.status_code == 200:
            return response.json()

        print("Apollo error:", response.text)
        return {
            "error": response.text,
            "status_code": response.status_code
        }

    except requests.RequestException as e:
        print("Apollo exception:", e)
        return {"error": str(e)}


# ============================================================
# MEDPLUS SEARCH
# ============================================================

def search_medplus(query):
    url = f"{PARSE_BASE_URL}/{MEDPLUS_SCRAPER_ID}/search_medicines"

    headers = {
        "X-API-Key": API_KEY
    }

    params = {
        "page": 1,
        "query": query
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=25
        )

        print("MedPlus HTTP status:", response.status_code)

        if response.status_code == 200:
            return response.json()

        print("MedPlus error:", response.text)
        return {
            "error": response.text,
            "status_code": response.status_code
        }

    except requests.RequestException as e:
        print("MedPlus exception:", e)
        return {"error": str(e)}


# ============================================================
# PHARMEASY SEARCH
# ============================================================

def search_pharmeasy(query):
    url = f"{PARSE_BASE_URL}/{PHARMEASY_SCRAPER_ID}/search_medicines"

    headers = {
        "X-API-Key": API_KEY,
        "Content-Type": "application/json"
    }

    payload = {
        "page": 1,
        "query": query
    }

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=60
        )

        print("PharmEasy HTTP status:", response.status_code)

        if response.status_code == 200:
            return response.json()

        print("PharmEasy error:", response.text)
        return {
            "error": response.text,
            "status_code": response.status_code
        }

    except requests.RequestException as e:
        print("PharmEasy exception:", e)
        return {"error": str(e)}


# ============================================================
# PHARMEASY NORMALIZATION
# ============================================================

def normalize_pharmeasy_products(products):
    normalized = []

    for product in products:
        price = first_value(
            product,
            "price",
            "selling_price",
            "discounted_price",
            "final_price",
            "product_price",
            "sale_price"
        )

        mrp = first_value(
            product,
            "mrp",
            "MRP",
            "mrp_price",
            "maximum_retail_price"
        )

        prescription = first_value(
            product,
            "is_prescription_required",
            "prescription_required",
            "requires_prescription",
            "prescription"
        )

        availability = first_value(
            product,
            "availability",
            "stock_status",
            "in_stock"
        )

        normalized.append({
            "name": first_value(
                product,
                "product_name",
                "name",
                "medicine_name",
                "title"
            ) or "Unnamed medicine",

            "manufacturer": first_value(
                product,
                "manufacturer",
                "manufacturer_name",
                "brand"
            ),

            "pack_size": first_value(
                product,
                "pack_size",
                "pack",
                "quantity",
                "packaging"
            ),

            "composition": first_value(
                product,
                "composition",
                "salt",
                "ingredients"
            ),

            "price": to_float(price, 0),
            "mrp": to_float(mrp),

            "discount_percentage": first_value(
                product,
                "discount_percentage",
                "discount",
                "discount_percent"
            ),

            "availability": availability,
            "is_prescription_required": bool(prescription),

            "product_url": first_value(
                product,
                "product_url",
                "url",
                "link"
            ),

            "image_url": first_value(
                product,
                "image_url",
                "image",
                "imageUrl"
            )
        })

    return normalized


# ============================================================
# HTML / CSS
# ============================================================

HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">

    <title>MediCompare</title>

    <style>
        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            font-family: Arial, Helvetica, sans-serif;
            background: #f4f7f6;
            color: #17221f;
        }

        .header {
            background: linear-gradient(135deg, #087f5b, #0ca678);
            color: white;
            padding: 26px 16px 30px;
            text-align: center;
            box-shadow: 0 4px 14px rgba(0,0,0,0.12);
        }

        .header h1 {
            margin: 0;
            font-size: 32px;
            font-weight: 800;
        }

        .header p {
            margin: 8px 0 0;
            opacity: 0.95;
            font-size: 14px;
        }

        .container {
            width: min(1180px, calc(100% - 28px));
            margin: 22px auto 40px;
        }

        .search-form {
            display: flex;
            gap: 10px;
            margin-bottom: 18px;
        }

        .search-form input {
            flex: 1;
            min-width: 0;
            border: 1px solid #cbd5d1;
            border-radius: 12px;
            padding: 14px 15px;
            font-size: 16px;
            outline: none;
            background: white;
        }

        .search-form input:focus {
            border-color: #0ca678;
            box-shadow: 0 0 0 3px rgba(12,166,120,0.12);
        }

        .search-form button {
            border: 0;
            border-radius: 12px;
            padding: 0 22px;
            background: #087f5b;
            color: white;
            font-size: 16px;
            font-weight: 700;
            cursor: pointer;
        }

        .search-form button:hover {
            background: #066a4c;
        }

        .warning {
            background: #fff8db;
            border: 1px solid #f0d56a;
            color: #6b5600;
            border-radius: 12px;
            padding: 12px 14px;
            font-size: 13px;
            margin-bottom: 22px;
        }

        .error {
            background: #fff0f0;
            border: 1px solid #ef9a9a;
            color: #9b1c1c;
            border-radius: 12px;
            padding: 12px 14px;
            margin-bottom: 18px;
        }

        .search-title {
            margin: 0 0 18px;
            font-size: 22px;
        }

        .pharmacy-section {
            margin-top: 28px;
        }

        .pharmacy-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            margin-bottom: 12px;
        }

        .pharmacy-header h2 {
            margin: 0;
            font-size: 21px;
        }

        .count {
            color: #64706c;
            font-size: 13px;
        }

        .products {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(245px, 1fr));
            gap: 15px;
        }

        .card {
            background: white;
            border: 1px solid #e0e7e4;
            border-radius: 16px;
            overflow: hidden;
            box-shadow: 0 3px 12px rgba(0,0,0,0.06);
            display: flex;
            flex-direction: column;
            min-width: 0;
        }

        .product-image-box {
            height: 190px;
            background: #fafafa;
            display: flex;
            align-items: center;
            justify-content: center;
            border-bottom: 1px solid #edf1ef;
            padding: 12px;
        }

        .product-image {
            width: 100%;
            height: 100%;
            object-fit: contain;
        }

        .image-unavailable {
            color: #8a9691;
            font-size: 13px;
            display: flex;
            align-items: center;
            justify-content: center;
            width: 100%;
            height: 100%;
        }

        .card-body {
            padding: 15px;
            display: flex;
            flex-direction: column;
            flex: 1;
        }

        .product-name {
            font-size: 16px;
            font-weight: 800;
            line-height: 1.35;
            margin-bottom: 9px;
        }

        .manufacturer {
            color: #596761;
            font-size: 13px;
            margin-bottom: 8px;
        }

        .meta {
            color: #53615b;
            font-size: 13px;
            line-height: 1.55;
            margin-bottom: 8px;
        }

        .price-row {
            display: flex;
            align-items: baseline;
            gap: 8px;
            flex-wrap: wrap;
            margin-top: auto;
            padding-top: 8px;
        }

        .price {
            font-size: 23px;
            font-weight: 900;
            color: #087f5b;
        }

        .mrp {
            color: #8b9691;
            text-decoration: line-through;
            font-size: 13px;
        }

        .discount {
            color: #087f5b;
            font-size: 12px;
            font-weight: 700;
        }

        .badge {
            display: inline-block;
            width: fit-content;
            margin-top: 9px;
            padding: 5px 8px;
            border-radius: 999px;
            font-size: 11px;
            font-weight: 700;
        }

        .badge.rx {
            background: #fff0f0;
            color: #a61e1e;
        }

        .badge.stock {
            background: #e8f8f2;
            color: #087f5b;
        }

        .badge.out {
            background: #f1f3f2;
            color: #65716c;
        }

        .buy-button {
            display: block;
            text-align: center;
            text-decoration: none;
            margin-top: 13px;
            padding: 11px 12px;
            border-radius: 10px;
            background: #087f5b;
            color: white;
            font-weight: 800;
            font-size: 14px;
        }

        .buy-button:hover {
            background: #066a4c;
        }

        .empty {
            background: white;
            border: 1px dashed #cbd5d1;
            border-radius: 14px;
            padding: 24px;
            color: #65716c;
            text-align: center;
        }

        .footer-note {
            margin-top: 30px;
            color: #6b7772;
            text-align: center;
            font-size: 12px;
            line-height: 1.5;
        }

        @media (max-width: 600px) {
            .header h1 {
                font-size: 27px;
            }

            .container {
                width: min(100% - 18px, 1180px);
                margin-top: 14px;
            }

            .search-form {
                gap: 8px;
            }

            .search-form input {
                font-size: 15px;
                padding: 13px;
            }

            .search-form button {
                padding: 0 16px;
                font-size: 15px;
            }

            .products {
                grid-template-columns: 1fr;
            }

            .product-image-box {
                height: 210px;
            }
        }
    </style>
</head>

<body>

<header class="header">
    <h1>💊 MediCompare</h1>
    <p>Compare medicine prices across online pharmacies</p>
</header>

<main class="container">

<form method="GET" class="search-form">

    <input
        type="search"
        name="medicine"
        value="{{ searched_medicine }}"
        placeholder="Search medicine (e.g. Dolo 650)"
        autocomplete="off"
        required
    >

    <button type="submit">
        Search
    </button>

</form>


<div class="warning">

    ⚠️ <strong>Important:</strong>

    MediCompare is a price/catalogue comparison tool,
    not medical advice.

    Check the exact medicine name, strength,
    composition and pack size before buying.

    Prescription medicines should only be used as
    directed by a qualified healthcare professional.

</div>


{% if error_message %}

<div class="error">
    {{ error_message }}
</div>

{% endif %}


{% if searched_medicine %}

<h2 class="search-title">
    Results for "{{ searched_medicine }}"
</h2>


<!-- ========================================================
     APOLLO
     ======================================================== -->

<section class="pharmacy-section">

    <div class="pharmacy-header">

        <h2>🏥 Apollo Pharmacy</h2>

        <span class="count">
            {{ apollo_products|length }} result(s)
        </span>

    </div>


    {% if apollo_products %}

    <div class="products">

        {% for product in apollo_products %}

        <article class="card">

            {% if product.image_url %}

            <div class="product-image-box">

                <img
                    src="{{ product.image_url }}"
                    alt="{{ product.name }}"
                    class="product-image"
                    loading="lazy"
                    onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';"
                >

                <div
                    class="image-unavailable"
                    style="display:none;"
                >
                    Image unavailable
                </div>

            </div>

            {% endif %}


            <div class="card-body">

                <div class="product-name">
                    {{ product.name or "Unnamed medicine" }}
                </div>


                {% if product.manufacturer %}

                <div class="manufacturer">
                    Manufacturer:
                    {{ product.manufacturer }}
                </div>

                {% endif %}


                <div class="meta">

                    {% if product.pack_size %}
                        📦 Pack: {{ product.pack_size }}<br>
                    {% endif %}

                    {% if product.tags %}
                        🧪 {{ product.tags|join(", ") }}<br>
                    {% endif %}

                    {% if product.delivery_time %}
                        🚚 {{ product.delivery_time }}
                    {% endif %}

                </div>


                <div class="price-row">

                    <span class="price">
                        ₹{{ "%.2f"|format(product.price|float) }}
                    </span>


                    {% if product.mrp
                          and product.mrp|float >
                              product.price|float %}

                    <span class="mrp">
                        ₹{{ "%.2f"|format(product.mrp|float) }}
                    </span>

                    {% endif %}


                    {% if product.discount_percentage %}

                    <span class="discount">
                        {{ product.discount_percentage }}% OFF
                    </span>

                    {% endif %}

                </div>


                {% if product.is_prescription_required %}

                <span class="badge rx">
                    Prescription required
                </span>

                {% endif %}


                {% if product.availability %}

                <span class="badge stock">
                    {{ product.availability }}
                </span>

                {% endif %}

            </div>

        </article>

        {% endfor %}

    </div>


    {% else %}

    <div class="empty">
        No Apollo results found for this search.
    </div>

    {% endif %}

</section>


<!-- ========================================================
     MEDPLUS
     ======================================================== -->

<section class="pharmacy-section">

    <div class="pharmacy-header">

        <h2>💚 MedPlus</h2>

        <span class="count">
            {{ medplus_products|length }} result(s)
        </span>

    </div>


    {% if medplus_products %}

    <div class="products">

        {% for product in medplus_products %}

        <article class="card">

            {% if product.image_url %}

            <div class="product-image-box">

                <img
                    src="{{ product.image_url }}"
                    alt="{{ product.product_name }}"
                    class="product-image"
                    loading="lazy"
                    onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';"
                >

                <div
                    class="image-unavailable"
                    style="display:none;"
                >
                    Image unavailable
                </div>

            </div>

            {% endif %}


            <div class="card-body">

                <div class="product-name">
                    {{ product.product_name or "Unnamed medicine" }}
                </div>


                {% if product.manufacturer %}

                <div class="manufacturer">
                    Manufacturer:
                    {{ product.manufacturer }}
                </div>

                {% endif %}


                <div class="meta">

                    {% if product.pack_size %}
                        📦 Pack: {{ product.pack_size }}<br>
                    {% endif %}

                    {% if product.composition %}
                        🧪 {{ product.composition }}<br>
                    {% endif %}

                    {% if product.is_in_stock is defined %}

                        {% if product.is_in_stock %}
                            🟢 In stock
                        {% else %}
                            🔴 Out of stock
                        {% endif %}

                    {% endif %}

                </div>


                <div class="price-row">

                    {% if product.price is defined
                          and product.price is not none %}

                    <span class="price">
                        ₹{{ "%.2f"|format(product.price|float) }}
                    </span>

                    {% elif product.regular_price is defined
                            and product.regular_price is not none %}

                    <span class="price">
                        ₹{{ "%.2f"|format(product.regular_price|float) }}
                    </span>

                    {% elif product.mrp is defined
                            and product.mrp is not none %}

                    <span class="price">
                        ₹{{ "%.2f"|format(product.mrp|float) }}
                    </span>

                    {% endif %}


                    {% if product.mrp is defined
                          and product.mrp is not none %}

                    <span class="mrp">
                        MRP ₹{{ "%.2f"|format(product.mrp|float) }}
                    </span>

                    {% endif %}


                    {% if product.discount_percentage %}

                    <span class="discount">
                        {{ product.discount_percentage }}% OFF
                    </span>

                    {% endif %}

                </div>


                {% if product.is_prescription_required %}

                <span class="badge rx">
                    Prescription required
                </span>

                {% endif %}


                {% if product.product_url %}

                <a
                    href="{{ product.product_url }}"
                    target="_blank"
                    rel="noopener noreferrer"
                    class="buy-button"
                >
                    Buy on MedPlus ↗
                </a>

                {% endif %}

            </div>

        </article>

        {% endfor %}

    </div>


    {% else %}

    <div class="empty">
        No MedPlus results found for this search.
    </div>

    {% endif %}

</section>


<!-- ========================================================
     PHARMEASY
     ======================================================== -->

<section class="pharmacy-section">

    <div class="pharmacy-header">

        <h2>💙 PharmEasy</h2>

        <span class="count">
            {{ pharmeasy_products|length }} result(s)
        </span>

    </div>


    {% if pharmeasy_products %}

    <div class="products">

        {% for product in pharmeasy_products %}

        <article class="card">

            {% if product.image_url %}

            <div class="product-image-box">

                <img
                    src="{{ product.image_url }}"
                    alt="{{ product.name }}"
                    class="product-image"
                    loading="lazy"
                    onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';"
                >

                <div
                    class="image-unavailable"
                    style="display:none;"
                >
                    Image unavailable
                </div>

            </div>

            {% endif %}


            <div class="card-body">

                <div class="product-name">
                    {{ product.name or "Unnamed medicine" }}
                </div>


                {% if product.manufacturer %}

                <div class="manufacturer">
                    Manufacturer:
                    {{ product.manufacturer }}
                </div>

                {% endif %}


                <div class="meta">

                    {% if product.pack_size %}
                        📦 Pack: {{ product.pack_size }}<br>
                    {% endif %}

                    {% if product.composition %}
                        🧪 {{ product.composition }}<br>
                    {% endif %}

                    {% if product.availability is not none %}

                        {% if product.availability is boolean %}

                            {% if product.availability %}
                                🟢 In stock
                            {% else %}
                                🔴 Out of stock
                            {% endif %}

                        {% else %}

                            🚚 {{ product.availability }}

                        {% endif %}

                    {% endif %}

                </div>


                <div class="price-row">

                    <span class="price">
                        ₹{{ "%.2f"|format(product.price|float) }}
                    </span>


                    {% if product.mrp
                          and product.mrp|float >
                              product.price|float %}

                    <span class="mrp">
                        MRP ₹{{ "%.2f"|format(product.mrp|float) }}
                    </span>

                    {% endif %}


                    {% if product.discount_percentage %}

                    <span class="discount">
                        {{ product.discount_percentage }}% OFF
                    </span>

                    {% endif %}

                </div>


                {% if product.is_prescription_required %}

                <span class="badge rx">
                    Prescription required
                </span>

                {% endif %}


                {% if product.product_url %}

                <a
                    href="{{ product.product_url }}"
                    target="_blank"
                    rel="noopener noreferrer"
                    class="buy-button"
                >
                    Buy on PharmEasy ↗
                </a>

                {% endif %}

            </div>

        </article>

        {% endfor %}

    </div>


    {% else %}

    <div class="empty">
        No PharmEasy results found for this search.
    </div>

    {% endif %}

</section>


{% else %}

<div class="empty">
    Search for a medicine to compare available
    catalogue results.
</div>

{% endif %}


<div class="footer-note">

    Prices and availability can change.

    Always verify the final price, stock status,
    exact strength and pack size on the pharmacy
    website before purchasing.

</div>


</main>

</body>
</html>
"""


# ============================================================
# HOME / SEARCH
# ============================================================

@app.route("/", methods=["GET"])
def home():

    searched_medicine = request.args.get(
        "medicine",
        ""
    ).strip()

    apollo_products = []
    medplus_products = []
    pharmeasy_products = []

    error_message = ""

    if searched_medicine:

        if not API_KEY:

            error_message = (
                "PARSE_API_KEY was not found. "
                "Check your .env file."
            )

        else:

            # ---------------- Apollo ----------------

            apollo_result = search_apollo(
                searched_medicine
            )

            if (
                isinstance(apollo_result, dict)
                and apollo_result.get("status")
                == "success"
            ):

                apollo_products = get_products(
                    apollo_result
                )

            elif (
                isinstance(apollo_result, dict)
                and apollo_result.get("error")
            ):

                print(
                    "Apollo result error:",
                    apollo_result["error"]
                )


            # ---------------- MedPlus ----------------

            medplus_result = search_medplus(
                searched_medicine
            )

            if (
                isinstance(medplus_result, dict)
                and medplus_result.get("status")
                == "success"
            ):

                medplus_products = get_products(
                    medplus_result
                )

            elif (
                isinstance(medplus_result, dict)
                and medplus_result.get("error")
            ):

                print(
                    "MedPlus result error:",
                    medplus_result["error"]
                )


            # ---------------- PharmEasy ----------------

            pharmeasy_result = search_pharmeasy(
                searched_medicine
            )

            if (
                isinstance(pharmeasy_result, dict)
                and pharmeasy_result.get("status")
                == "success"
            ):

                raw_products = get_products(
                    pharmeasy_result
                )

                pharmeasy_products = (
                    normalize_pharmeasy_products(
                        raw_products
                    )
                )

            elif (
                isinstance(pharmeasy_result, dict)
                and pharmeasy_result.get("error")
            ):

                print(
                    "PharmEasy result error:",
                    pharmeasy_result["error"]
                )


            if (
                not apollo_products
                and not medplus_products
                and not pharmeasy_products
            ):

                error_message = (
                    "No results were returned. "
                    "Try a more specific medicine "
                    "name or strength."
                )


    return render_template_string(

        HTML,

        searched_medicine=searched_medicine,

        apollo_products=apollo_products,

        medplus_products=medplus_products,

        pharmeasy_products=pharmeasy_products,

        error_message=error_message
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    print()
    print("MediCompare is starting...")
    print("Laptop: http://127.0.0.1:5000")
    print("LAN:    http://10.27.159.184:5000")
    print()

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True
    )
