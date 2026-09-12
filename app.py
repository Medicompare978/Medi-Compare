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
            position: relative;
            overflow: hidden;
            background: linear-gradient(135deg, #087f5b 0%, #0ca678 55%, #12b886 100%);
            color: white;
            padding: 38px 16px 55px;
            text-align: center;
            box-shadow: 0 5px 18px rgba(0,0,0,0.14);
        }

        .header::before,
        .header::after {
            content: "";
            position: absolute;
            border-radius: 50%;
            background: rgba(255,255,255,0.08);
            pointer-events: none;
        }

        .header::before {
            width: 220px;
            height: 220px;
            left: -90px;
            top: -120px;
        }

        .header::after {
            width: 280px;
            height: 280px;
            right: -120px;
            bottom: -190px;
        }

        .header-content {
            position: relative;
            z-index: 2;
        }

        .logo {
            font-size: 42px;
            margin-bottom: 5px;
            animation: logoFloat 3s ease-in-out infinite;
        }

        .header h1 {
            margin: 0;
            font-size: 38px;
            font-weight: 900;
            letter-spacing: -0.8px;
        }

        .header .tagline {
            margin: 9px 0 0;
            font-size: 18px;
            font-weight: 700;
        }

        .header .subtitle {
            margin: 7px 0 0;
            font-size: 14px;
            opacity: 0.92;
        }

        .floating-pill {
            position: absolute;
            font-size: 27px;
            opacity: 0.7;
            z-index: 1;
            animation: pillFloat 4s ease-in-out infinite;
        }

        .pill-one {
            left: 13%;
            top: 38px;
        }

        .pill-two {
            right: 14%;
            top: 75px;
            animation-delay: 1.2s;
        }

        .pill-three {
            left: 24%;
            bottom: 12px;
            animation-delay: 2s;
        }

        @keyframes pillFloat {
            0%, 100% {
                transform: translateY(0) rotate(-8deg);
            }

            50% {
                transform: translateY(-12px) rotate(8deg);
            }
        }

        @keyframes logoFloat {
            0%, 100% {
                transform: translateY(0);
            }

            50% {
                transform: translateY(-4px);
            }
        }

        .container {
            width: min(1180px, calc(100% - 28px));
            margin: 22px auto 40px;
        }

        .search-form {
            position: relative;
            z-index: 5;
            display: flex;
            gap: 10px;
            margin: -30px auto 22px;
            padding: 10px;
            max-width: 1050px;
            background: white;
            border-radius: 18px;
            box-shadow: 0 8px 28px rgba(0,0,0,0.12);
        }

        .search-wrapper {
            position: relative;
            flex: 1;
            min-width: 0;
        }

        .search-wrapper::before {
            content: "🔍";
            position: absolute;
            left: 15px;
            top: 50%;
            transform: translateY(-50%);
            font-size: 17px;
            opacity: 0.65;
            pointer-events: none;
            z-index: 2;
        }

        .search-form input {
            width: 100%;
            flex: 1;
            min-width: 0;
            border: 2px solid #e4ebe8;
            border-radius: 13px;
            padding: 15px 44px 15px 44px;
            font-size: 16px;
            outline: none;
            background: #fbfdfc;
            transition: all 0.25s ease;
        }

        .search-form input:focus {
            border-color: #0ca678;
            background: white;
            box-shadow: 0 0 0 4px rgba(12,166,120,0.10);
            transform: translateY(-1px);
        }

        .search-form button {
            border: 0;
            border-radius: 13px;
            padding: 0 27px;
            background: #087f5b;
            color: white;
            font-size: 16px;
            font-weight: 800;
            cursor: pointer;
            transition: all 0.2s ease;
        }

        .search-form button:hover {
            background: #066a4c;
            transform: translateY(-2px);
            box-shadow: 0 6px 14px rgba(8,127,91,0.25);
        }

        .search-form button:active {
            transform: translateY(0) scale(0.98);
        }

        .clear-search {
            position: absolute;
            right: 12px;
            top: 50%;
            transform: translateY(-50%);
            width: 30px;
            height: 30px;
            border: 0;
            border-radius: 50%;
            background: #eef4f1;
            color: #52615b;
            font-size: 18px;
            line-height: 1;
            cursor: pointer;
            display: none;
            align-items: center;
            justify-content: center;
            transition: all 0.2s ease;
            z-index: 3;
        }

        .clear-search:hover {
            background: #dce9e4;
            transform: translateY(-50%) scale(1.08);
        }

        .search-wrapper.has-value .clear-search {
            display: flex;
        }

        .search-status {
            display: none;
            align-items: center;
            justify-content: center;
            gap: 9px;
            max-width: 1050px;
            margin: 0 auto 20px;
            padding: 13px 16px;
            border-radius: 13px;
            background: #eefaf6;
            border: 1px solid #ccece1;
            color: #087f5b;
            font-size: 13px;
            font-weight: 700;
        }

        .search-status.active {
            display: flex;
        }

        .loading-dots {
            display: inline-flex;
            gap: 4px;
        }

        .loading-dots span {
            width: 6px;
            height: 6px;
            border-radius: 50%;
            background: #0ca678;
            animation: loadingPulse 1s infinite ease-in-out;
        }

        .loading-dots span:nth-child(2) {
            animation-delay: 0.15s;
        }

        .loading-dots span:nth-child(3) {
            animation-delay: 0.30s;
        }

        @keyframes loadingPulse {
            0%, 80%, 100% {
                opacity: 0.3;
                transform: scale(0.8);
            }

            40% {
                opacity: 1;
                transform: scale(1.15);
            }
        }

        .search-tip {
            max-width: 1050px;
            margin: -10px auto 20px;
            padding: 0 4px;
            color: #708079;
            font-size: 12px;
            text-align: left;
        }

        .health-tips {
            max-width: 1050px;
            margin: 16px auto 22px;
            min-height: 125px;
            padding: 17px 20px 15px;
            border-radius: 17px;
            border: 1px solid #d9eee6;
            background: linear-gradient(135deg, #f5fffb, #eef9f5);
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 5px;
            color: #17634f;
            overflow: hidden;
            position: relative;
            box-shadow: 0 5px 18px rgba(17, 88, 67, 0.06);
        }

        .health-tip-icon {
            font-size: 48px;
            line-height: 1;
            width: auto;
            text-align: center;
            flex: 0 0 auto;
            position: relative;
            z-index: 2;
            filter: drop-shadow(0 5px 7px rgba(23, 99, 79, 0.14));
            animation: tipIconBig 2.2s ease-in-out infinite;
        }

        .health-tip-text {
            font-size: 14px;
            font-weight: 700;
            letter-spacing: 0.1px;
            text-align: center;
            line-height: 1.45;
            position: relative;
            z-index: 2;
            animation: tipTextIn 0.55s ease both;
        }

        .health-tip-label {
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 1.5px;
            font-weight: 800;
            color: #5a8a7b;
            order: -1;
            margin: 0 0 2px;
        }

        .health-tip-glow {
            position: absolute;
            width: 90px;
            height: 90px;
            border-radius: 50%;
            background: rgba(33, 177, 131, 0.08);
            right: -25px;
            top: -35px;
            animation: tipGlow 4s ease-in-out infinite;
            pointer-events: none;
        }

        @keyframes tipIconBig {
            0%, 100% {
                transform: translateY(0) scale(1) rotate(0deg);
            }
            35% {
                transform: translateY(-8px) scale(1.08) rotate(-4deg);
            }
            65% {
                transform: translateY(-3px) scale(1.03) rotate(3deg);
            }
        }

        @keyframes tipTextIn {
            from { opacity: 0; transform: translateY(8px); }
            to { opacity: 1; transform: translateY(0); }
        }

        @keyframes tipGlow {
            0%, 100% { transform: scale(0.9); opacity: 0.6; }
            50% { transform: scale(1.12); opacity: 1; }
        }

        @media (max-width: 600px) {
            .health-tips {
                margin: 14px 0 18px;
                min-height: 112px;
                padding: 14px 13px 12px;
            }

            .health-tip-icon {
                font-size: 42px;
            }

            .health-tip-text {
                font-size: 12px;
                line-height: 1.4;
            }

            .health-tip-label {
                display: none;
            }
        }

        @media (prefers-reduced-motion: reduce) {
            .health-tip-icon,
            .health-tip-text,
            .health-tip-glow {
                animation: none !important;
            }
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

        /* ====================================================
           STEP 2 - RESULT & CARD ANIMATIONS
           ==================================================== */

        .search-title {
            animation: resultTitleIn 0.55s ease both;
        }

        .pharmacy-section {
            animation: sectionIn 0.65s ease both;
        }

        .pharmacy-section:nth-of-type(2) {
            animation-delay: 0.12s;
        }

        .pharmacy-section:nth-of-type(3) {
            animation-delay: 0.24s;
        }

        .pharmacy-header {
            position: relative;
        }

        .pharmacy-header h2 {
            transition: transform 0.25s ease;
        }

        .pharmacy-header:hover h2 {
            transform: translateX(5px);
        }

        .products .card {
            opacity: 0;
            animation: cardIn 0.55s ease forwards;
        }

        .products .card:nth-child(1) { animation-delay: 0.05s; }
        .products .card:nth-child(2) { animation-delay: 0.12s; }
        .products .card:nth-child(3) { animation-delay: 0.19s; }
        .products .card:nth-child(4) { animation-delay: 0.26s; }
        .products .card:nth-child(5) { animation-delay: 0.33s; }
        .products .card:nth-child(6) { animation-delay: 0.40s; }
        .products .card:nth-child(7) { animation-delay: 0.47s; }
        .products .card:nth-child(8) { animation-delay: 0.54s; }

        .card {
            background: white;
            border: 1px solid #e0e7e4;
            border-radius: 16px;
            overflow: hidden;
            box-shadow: 0 3px 12px rgba(0,0,0,0.06);
            display: flex;
            flex-direction: column;
            min-width: 0;
            transition:
                transform 0.28s ease,
                box-shadow 0.28s ease,
                border-color 0.28s ease;
        }

        .card:hover {
            transform: translateY(-7px);
            box-shadow: 0 12px 30px rgba(0,0,0,0.12);
            border-color: #b8dcd0;
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
            transition: transform 0.35s ease;
        }

        .card:hover .product-image {
            transform: scale(1.06);
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
            transition: transform 0.25s ease;
            display: inline-block;
        }

        .card:hover .price {
            transform: scale(1.05);
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

        .buy-button {
            transition:
                transform 0.2s ease,
                box-shadow 0.2s ease,
                background 0.2s ease;
        }

        .buy-button:hover {
            background: #066a4c;
            transform: translateY(-2px);
            box-shadow: 0 7px 16px rgba(8,127,91,0.24);
        }

        .buy-button:active {
            transform: translateY(0) scale(0.98);
        }

        .empty {
            background: white;
            border: 1px dashed #cbd5d1;
            border-radius: 14px;
            padding: 24px;
            color: #65716c;
            text-align: center;
            animation: emptyIn 0.55s ease both;
        }

        @keyframes resultTitleIn {
            from {
                opacity: 0;
                transform: translateY(12px);
            }

            to {
                opacity: 1;
                transform: translateY(0);
            }
        }

        @keyframes sectionIn {
            from {
                opacity: 0;
                transform: translateY(18px);
            }

            to {
                opacity: 1;
                transform: translateY(0);
            }
        }

        @keyframes cardIn {
            from {
                opacity: 0;
                transform: translateY(24px) scale(0.97);
            }

            to {
                opacity: 1;
                transform: translateY(0) scale(1);
            }
        }

        @keyframes emptyIn {
            from {
                opacity: 0;
                transform: translateY(10px);
            }

            to {
                opacity: 1;
                transform: translateY(0);
            }
        }

        .footer-note {
            margin-top: 30px;
            color: #6b7772;
            text-align: center;
            font-size: 12px;
            line-height: 1.5;
        }

        @media (prefers-reduced-motion: reduce) {
            *,
            *::before,
            *::after {
                animation-duration: 0.01ms !important;
                animation-iteration-count: 1 !important;
                transition-duration: 0.01ms !important;
                scroll-behavior: auto !important;
            }
        }

        @media (max-width: 600px) {
            .header {
                padding: 30px 14px 48px;
            }

            .header h1 {
                font-size: 30px;
            }

            .header .tagline {
                font-size: 16px;
            }

            .header .subtitle {
                font-size: 13px;
            }

            .logo {
                font-size: 36px;
            }

            .floating-pill {
                font-size: 22px;
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
                padding: 13px 42px 13px 42px;
            }

            .search-form {
                flex-direction: column;
            }

            .search-form button {
                min-height: 46px;
            }

            .search-tip {
                margin-top: -8px;
                padding: 0 2px;
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

    <span class="floating-pill pill-one">💊</span>
    <span class="floating-pill pill-two">💊</span>
    <span class="floating-pill pill-three">💊</span>

    <div class="header-content">

        <div class="logo">💊</div>

        <h1>MediCompare</h1>

        <div class="tagline">
            Compare. Save. Choose Better.
        </div>

        <div class="subtitle">
            Compare medicine prices across trusted online pharmacies
        </div>

    </div>

</header>

<main class="container">

<form method="GET" class="search-form" id="searchForm">

    <div class="search-wrapper{% if searched_medicine %} has-value{% endif %}" id="searchWrapper">

        <input
            type="search"
            name="medicine"
            id="medicineInput"
            value="{{ searched_medicine }}"
            placeholder="Search medicine (e.g. Dolo 650)"
            autocomplete="off"
            required
        >

        <button
            type="button"
            class="clear-search"
            id="clearSearch"
            aria-label="Clear search"
        >×</button>

    </div>

    <button type="submit">
        Search
    </button>

</form>

<div class="search-status" id="searchStatus" aria-live="polite">
    <span>🔎</span>
    <span>Searching pharmacies...</span>
    <span class="loading-dots">
        <span></span><span></span><span></span>
    </span>
</div>

<div class="search-tip">
    💡 Tip: Include the medicine name and strength for more accurate results.
</div>


<div class="warning">

    ⚠️ <strong>Important:</strong>

    MediCompare is a price/catalogue comparison tool,
    not medical advice.

    Check the exact medicine name, strength,
    composition and pack size before buying.

    Prescription medicines should only be used as
    directed by a qualified healthcare professional.

</div>

<div class="health-tips" id="healthTips" aria-live="polite">
    <div class="health-tip-glow"></div>
    <span class="health-tip-icon" id="healthTipIcon">💧</span>
    <span class="health-tip-label">Healthy Habit</span>
    <span class="health-tip-text" id="healthTipText">Stay hydrated — drink enough water throughout the day.</span>
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

<script>
    const searchForm = document.getElementById("searchForm");
    const medicineInput = document.getElementById("medicineInput");
    const searchWrapper = document.getElementById("searchWrapper");
    const clearSearch = document.getElementById("clearSearch");
    const searchStatus = document.getElementById("searchStatus");

    function updateClearButton() {
        if (medicineInput.value.trim()) {
            searchWrapper.classList.add("has-value");
        } else {
            searchWrapper.classList.remove("has-value");
        }
    }

    medicineInput.addEventListener("input", updateClearButton);

    clearSearch.addEventListener("click", function () {
        medicineInput.value = "";
        updateClearButton();
        medicineInput.focus();
    });

    searchForm.addEventListener("submit", function () {
        if (!medicineInput.value.trim()) {
            return;
        }

        searchStatus.classList.add("active");
    });

    updateClearButton();

    const healthTips = [
        ["💧", "Stay hydrated — drink enough water throughout the day."],
        ["🧼", "Wash your hands before eating and after coming from outside."],
        ["🥗", "Choose a balanced diet with plenty of fruits and vegetables."],
        ["😴", "Good sleep supports your body’s natural recovery."],
        ["🚶", "A little daily movement can help you stay active and healthy."],
        ["🩺", "Take medicines only as directed by a qualified healthcare professional."]
    ];

    let healthTipIndex = 0;
    const healthTipIcon = document.getElementById("healthTipIcon");
    const healthTipText = document.getElementById("healthTipText");

    function showHealthTip(index) {
        healthTipText.style.animation = "none";
        healthTipIcon.style.animation = "none";

        void healthTipText.offsetWidth;
        void healthTipIcon.offsetWidth;

        healthTipIcon.textContent = healthTips[index][0];
        healthTipText.textContent = healthTips[index][1];

        healthTipText.style.animation = "tipTextIn 0.55s ease both";
        healthTipIcon.style.animation = "tipIconBig 2.2s ease-in-out infinite";
    }

    setInterval(function () {
        healthTipIndex = (healthTipIndex + 1) % healthTips.length;
        showHealthTip(healthTipIndex);
    }, 4500);
</script>

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
