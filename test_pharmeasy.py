import os
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("PARSE_API_KEY")

SCRAPER_ID = "e7c250a1-5824-4027-9f84-c63d92aa5889"
ENDPOINT = "search_medicines"

url = f"https://api.parse.bot/scraper/{SCRAPER_ID}/{ENDPOINT}"

headers = {
    "X-API-Key": API_KEY,
    "Content-Type": "application/json"
}

data = {
    "page": 1,
    "query": "paracetamol"
}

print("Testing PharmEasy API...")
print("API key loaded:", bool(API_KEY))

response = requests.post(
    url,
    headers=headers,
    json=data,
    timeout=60
)

print("Status:", response.status_code)
print(response.text)