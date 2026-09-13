from flask import Flask, render_template_string, request
import requests
import os
import json
import sqlite3
import threading
import time
import hashlib
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
# SHARED PRODUCT CATALOGUE + SEARCH CACHE
# ============================================================
# IMPORTANT:
# This is a SHARED catalogue for every visitor of this Flask instance.
# The system is NOT tied to Dolo 650 or any hard-coded medicine.
#
# Strategy:
# 1) Exact query cache is checked first.
# 2) If the exact query is not cached, search the shared product catalogue.
#    Example: a previous Dolo 650 discovery may satisfy a later
#    "paracetamol" search because the saved composition contains paracetamol.
# 3) Only when the catalogue cannot answer the query do we call the pharmacy API.
# 4) New API results are saved as reusable product records for ALL users.
# 5) 429/error responses activate provider cooldown and use saved data where possible.
# 6) Per-query locks prevent two simultaneous users from triggering duplicate
#    API calls for the same pharmacy + query.
#
# Local development uses SQLite. Production should move this schema to a
# persistent PostgreSQL/managed DB before launch on Render.

DB_PATH = os.getenv("MEDICOMPARE_DB_PATH", "medicompare.db")
CACHE_TTL_SECONDS = int(os.getenv("MEDICOMPARE_CACHE_TTL", str(6 * 60 * 60)))
STALE_CACHE_MAX_SECONDS = int(
    os.getenv("MEDICOMPARE_STALE_MAX", str(7 * 24 * 60 * 60))
)
DEFAULT_PROVIDER_COOLDOWN_SECONDS = int(
    os.getenv("MEDICOMPARE_PROVIDER_COOLDOWN", str(60 * 60))
)

# Maximum external calls allowed by this application for one provider in one
# local calendar day. This is a safety valve, NOT the provider's official quota.
# Keep it configurable because Parse/provider plans can change.
DEFAULT_DAILY_PROVIDER_CALL_LIMIT = int(
    os.getenv("MEDICOMPARE_DAILY_PROVIDER_CALL_LIMIT", "40")
)

# SQLite concurrency helpers.
db_lock = threading.RLock()
inflight_guard = threading.Lock()
inflight_locks = {}


def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with db_lock:
        conn = get_db_connection()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS products (
                    product_key TEXT PRIMARY KEY,
                    pharmacy TEXT NOT NULL,
                    external_id TEXT,
                    name TEXT NOT NULL,
                    manufacturer TEXT,
                    pack_size TEXT,
                    composition TEXT,
                    price REAL,
                    mrp REAL,
                    discount_percentage REAL,
                    availability TEXT,
                    is_prescription_required INTEGER NOT NULL DEFAULT 0,
                    product_url TEXT,
                    image_url TEXT,
                    raw_json TEXT,
                    first_seen_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_products_pharmacy
                    ON products(pharmacy);
                CREATE INDEX IF NOT EXISTS idx_products_name
                    ON products(name);
                CREATE INDEX IF NOT EXISTS idx_products_composition
                    ON products(composition);
                CREATE INDEX IF NOT EXISTS idx_products_external_id
                    ON products(pharmacy, external_id);

                CREATE TABLE IF NOT EXISTS search_cache (
                    cache_key TEXT PRIMARY KEY,
                    pharmacy TEXT NOT NULL,
                    query TEXT NOT NULL,
                    product_keys_json TEXT NOT NULL,
                    fetched_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_status INTEGER,
                    last_error TEXT,
                    source_type TEXT NOT NULL DEFAULT 'live'
                );

                CREATE INDEX IF NOT EXISTS idx_search_cache_pharmacy_query
                    ON search_cache(pharmacy, query);

                CREATE TABLE IF NOT EXISTS provider_state (
                    pharmacy TEXT PRIMARY KEY,
                    blocked_until REAL NOT NULL DEFAULT 0,
                    last_status INTEGER,
                    last_error TEXT,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS provider_usage (
                    pharmacy TEXT NOT NULL,
                    usage_date TEXT NOT NULL,
                    call_count INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(pharmacy, usage_date)
                );
                """
            )

            # Upgrade databases created by the earlier Step-1 version.
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(search_cache)").fetchall()
            }
            if "source_type" not in columns:
                conn.execute(
                    "ALTER TABLE search_cache ADD COLUMN source_type TEXT NOT NULL DEFAULT 'live'"
                )

            conn.commit()
        finally:
            conn.close()


init_db()


def normalize_query(query):
    """Normalize punctuation/case so equivalent searches share one key."""
    cleaned = "".join(
        ch.lower() if ch.isalnum() or ch.isspace() else " "
        for ch in (query or "")
    )
    return " ".join(cleaned.split())


def make_cache_key(pharmacy, query):
    return f"{pharmacy}:{normalize_query(query)}"


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
                .replace("%", "")
                .strip()
            )

        return float(value)

    except (TypeError, ValueError):
        return default


def parse_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {
            "1", "true", "yes", "y", "required", "prescription"
        }
    return False


def serialize_product(product):
    return json.dumps(product, ensure_ascii=False, separators=(",", ":"))


def make_product_key(pharmacy, product):
    """Stable pharmacy-product key; no medicine is hard-coded."""
    external_id = first_value(
        product,
        "product_id",
        "id",
        "sku",
        "productId",
        "variant_id",
        "variantId",
    )

    if external_id:
        raw = f"{pharmacy}|id|{external_id}"
    else:
        raw = "|".join(
            [
                pharmacy,
                str(first_value(product, "product_name", "name", "medicine_name", "title") or ""),
                str(first_value(product, "pack_size", "pack", "quantity", "packaging") or ""),
                str(first_value(product, "manufacturer", "manufacturer_name", "brand") or ""),
                str(first_value(product, "product_url", "url", "link") or ""),
            ]
        )

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cache_age_label(age_seconds):
    minutes = int(max(0, age_seconds) // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    hours = int(minutes // 60)
    if hours < 24:
        return f"{hours} hr ago"
    days = int(hours // 24)
    return f"{days} day(s) ago"


def get_query_lock(pharmacy, query):
    key = make_cache_key(pharmacy, query)
    with inflight_guard:
        lock = inflight_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            inflight_locks[key] = lock
        return lock


def get_products_by_keys(product_keys):
    if not product_keys:
        return []

    unique_keys = list(dict.fromkeys(product_keys))
    placeholders = ",".join("?" for _ in unique_keys)

    with db_lock:
        conn = get_db_connection()
        try:
            rows = conn.execute(
                f"SELECT * FROM products WHERE product_key IN ({placeholders})",
                unique_keys,
            ).fetchall()
        finally:
            conn.close()

    by_key = {row["product_key"]: row for row in rows}
    products = []

    for key in unique_keys:
        row = by_key.get(key)
        if not row:
            continue

        products.append(
            {
                "name": row["name"],
                "manufacturer": row["manufacturer"],
                "pack_size": row["pack_size"],
                "composition": row["composition"],
                "price": row["price"],
                "mrp": row["mrp"],
                "discount_percentage": row["discount_percentage"],
                "availability": row["availability"],
                "is_prescription_required": bool(row["is_prescription_required"]),
                "product_url": row["product_url"],
                "image_url": row["image_url"],
            }
        )

    return products


def upsert_products(pharmacy, products):
    """Save discovered products into the shared catalogue."""
    if not products:
        return []

    now = time.time()
    product_keys = []

    with db_lock:
        conn = get_db_connection()
        try:
            for product in products:
                if not isinstance(product, dict):
                    continue

                key = product.get("_product_key") or make_product_key(pharmacy, product)
                product_keys.append(key)

                external_id = first_value(
                    product,
                    "product_id",
                    "id",
                    "sku",
                    "productId",
                    "variant_id",
                    "variantId",
                )

                conn.execute(
                    """
                    INSERT INTO products (
                        product_key, pharmacy, external_id, name, manufacturer,
                        pack_size, composition, price, mrp,
                        discount_percentage, availability,
                        is_prescription_required, product_url, image_url,
                        raw_json, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(product_key) DO UPDATE SET
                        external_id = excluded.external_id,
                        name = excluded.name,
                        manufacturer = excluded.manufacturer,
                        pack_size = excluded.pack_size,
                        composition = excluded.composition,
                        price = excluded.price,
                        mrp = excluded.mrp,
                        discount_percentage = excluded.discount_percentage,
                        availability = excluded.availability,
                        is_prescription_required = excluded.is_prescription_required,
                        product_url = excluded.product_url,
                        image_url = excluded.image_url,
                        raw_json = excluded.raw_json,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        key,
                        pharmacy,
                        str(external_id) if external_id is not None else None,
                        str(product.get("name") or "Unnamed medicine"),
                        product.get("manufacturer"),
                        product.get("pack_size"),
                        product.get("composition"),
                        to_float(product.get("price")),
                        to_float(product.get("mrp")),
                        to_float(product.get("discount_percentage")),
                        str(product.get("availability")) if product.get("availability") is not None else None,
                        1 if parse_bool(product.get("is_prescription_required")) else 0,
                        product.get("product_url"),
                        product.get("image_url"),
                        serialize_product(product),
                        now,
                        now,
                    ),
                )

            conn.commit()
        finally:
            conn.close()

    return product_keys


def read_cache(pharmacy, query):
    key = make_cache_key(pharmacy, query)

    with db_lock:
        conn = get_db_connection()
        try:
            row = conn.execute(
                """
                SELECT product_keys_json, fetched_at, last_status,
                       last_error, source_type
                FROM search_cache
                WHERE cache_key = ?
                """,
                (key,),
            ).fetchone()
        finally:
            conn.close()

    if not row:
        return None

    try:
        product_keys = json.loads(row["product_keys_json"])
    except (TypeError, json.JSONDecodeError):
        product_keys = []

    if not isinstance(product_keys, list):
        product_keys = []

    fetched_at = float(row["fetched_at"])
    age_seconds = max(0, time.time() - fetched_at)

    return {
        "products": get_products_by_keys(product_keys),
        "product_keys": product_keys,
        "fetched_at": fetched_at,
        "age_seconds": age_seconds,
        "last_status": row["last_status"],
        "last_error": row["last_error"],
        "source_type": row["source_type"] or "live",
    }


def write_cache(
    pharmacy,
    query,
    product_keys,
    status=200,
    error=None,
    fetched_at=None,
    source_type="live",
):
    key = make_cache_key(pharmacy, query)
    now = time.time()
    fetched_at = float(fetched_at if fetched_at is not None else now)

    with db_lock:
        conn = get_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO search_cache (
                    cache_key, pharmacy, query, product_keys_json,
                    fetched_at, updated_at, last_status, last_error, source_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    product_keys_json = excluded.product_keys_json,
                    fetched_at = excluded.fetched_at,
                    updated_at = excluded.updated_at,
                    last_status = excluded.last_status,
                    last_error = excluded.last_error,
                    source_type = excluded.source_type
                """,
                (
                    key,
                    pharmacy,
                    normalize_query(query),
                    json.dumps(product_keys),
                    fetched_at,
                    now,
                    status,
                    error,
                    source_type,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def update_cache_error(pharmacy, query, status, error):
    key = make_cache_key(pharmacy, query)
    now = time.time()

    with db_lock:
        conn = get_db_connection()
        try:
            conn.execute(
                """
                UPDATE search_cache
                SET updated_at = ?, last_status = ?, last_error = ?
                WHERE cache_key = ?
                """,
                (now, status, error, key),
            )
            conn.commit()
        finally:
            conn.close()


def get_provider_state(pharmacy):
    with db_lock:
        conn = get_db_connection()
        try:
            row = conn.execute(
                """
                SELECT blocked_until, last_status, last_error, updated_at
                FROM provider_state
                WHERE pharmacy = ?
                """,
                (pharmacy,),
            ).fetchone()
        finally:
            conn.close()

    if not row:
        return {
            "blocked_until": 0,
            "last_status": None,
            "last_error": None,
            "updated_at": None,
        }

    return dict(row)


def set_provider_state(pharmacy, status, error=None, cooldown_seconds=0):
    now = time.time()
    blocked_until = now + max(0, cooldown_seconds)

    with db_lock:
        conn = get_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO provider_state (
                    pharmacy, blocked_until, last_status, last_error, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(pharmacy) DO UPDATE SET
                    blocked_until = excluded.blocked_until,
                    last_status = excluded.last_status,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (pharmacy, blocked_until, status, error, now),
            )
            conn.commit()
        finally:
            conn.close()


def provider_is_blocked(pharmacy):
    state = get_provider_state(pharmacy)
    return float(state.get("blocked_until") or 0) > time.time(), state


def get_retry_after_seconds(error_text):
    try:
        payload = json.loads(error_text or "")
        if isinstance(payload, dict):
            value = payload.get("retry_after")
            if value is not None:
                return max(0, int(float(value)))
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return 0


def get_usage_date():
    # Server-local date is sufficient for the local safety valve.
    return time.strftime("%Y-%m-%d")


def get_provider_call_count(pharmacy):
    today = get_usage_date()
    with db_lock:
        conn = get_db_connection()
        try:
            row = conn.execute(
                "SELECT call_count FROM provider_usage WHERE pharmacy = ? AND usage_date = ?",
                (pharmacy, today),
            ).fetchone()
        finally:
            conn.close()
    return int(row["call_count"]) if row else 0


def can_call_provider(pharmacy):
    limit = DEFAULT_DAILY_PROVIDER_CALL_LIMIT
    if limit <= 0:
        return True, 0

    count = get_provider_call_count(pharmacy)
    if count >= limit:
        return False, count
    return True, count


def record_provider_call(pharmacy):
    today = get_usage_date()
    now = time.time()
    with db_lock:
        conn = get_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO provider_usage(pharmacy, usage_date, call_count, updated_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(pharmacy, usage_date) DO UPDATE SET
                    call_count = call_count + 1,
                    updated_at = excluded.updated_at
                """,
                (pharmacy, today, now),
            )
            conn.commit()
        finally:
            conn.close()


def _catalogue_text(product):
    return " ".join(
        str(value or "")
        for value in (
            product.get("name"),
            product.get("composition"),
            product.get("manufacturer"),
            product.get("pack_size"),
        )
    ).lower()


def _query_tokens(query):
    return [token for token in normalize_query(query).split() if token]


def search_shared_catalogue(pharmacy, query, limit=30):
    """Search saved products before touching an external API.

    This is the key difference from the earlier cache-only design.
    A different query can reuse a previously discovered product when the
    saved name/composition strongly matches the query.
    """
    normalized = normalize_query(query)
    tokens = _query_tokens(normalized)
    if not tokens:
        return []

    with db_lock:
        conn = get_db_connection()
        try:
            rows = conn.execute(
                """
                SELECT * FROM products
                WHERE pharmacy = ?
                  AND (
                    lower(name) LIKE ?
                    OR lower(COALESCE(composition, '')) LIKE ?
                    OR lower(COALESCE(manufacturer, '')) LIKE ?
                  )
                ORDER BY last_seen_at DESC
                LIMIT 250
                """,
                (
                    pharmacy,
                    f"%{normalized}%",
                    f"%{normalized}%",
                    f"%{normalized}%",
                ),
            ).fetchall()
        finally:
            conn.close()

    scored = []
    query_compact = normalized.replace(" ", "")

    for row in rows:
        name = normalize_query(row["name"] or "")
        composition = normalize_query(row["composition"] or "")
        manufacturer = normalize_query(row["manufacturer"] or "")
        pack = normalize_query(row["pack_size"] or "")
        combined = " ".join([name, composition, manufacturer, pack])

        score = 0
        if name == normalized:
            score = 100
        elif normalized in name:
            score = 90
        elif normalized in composition:
            score = 82
        elif all(token in name for token in tokens):
            score = 75
        elif all(token in combined for token in tokens):
            score = 65
        else:
            # Compact match helps "dolo650" find "Dolo 650".
            if query_compact and query_compact in name.replace(" ", ""):
                score = 78

        if score >= 65:
            scored.append((score, float(row["last_seen_at"] or 0), row))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)

    product_keys = [row["product_key"] for _, _, row in scored[:limit]]
    return get_products_by_keys(product_keys)


def get_catalogue_latest_timestamp(pharmacy, products):
    """Return a conservative freshness timestamp for catalogue-derived results."""
    if not products:
        return None

    # We need the most recent matching product timestamp, not current time.
    names = {str(product.get("name") or "") for product in products}
    if not names:
        return None

    placeholders = ",".join("?" for _ in names)
    with db_lock:
        conn = get_db_connection()
        try:
            row = conn.execute(
                f"""
                SELECT MAX(last_seen_at) AS latest
                FROM products
                WHERE pharmacy = ? AND name IN ({placeholders})
                """,
                [pharmacy, *names],
            ).fetchone()
        finally:
            conn.close()

    return float(row["latest"]) if row and row["latest"] else None


def cached_search(pharmacy, query, fetch_products):
    """Shared catalogue + cache + quota protection.

    Priority order:
      A. Fresh exact-query cache.
      B. Shared product catalogue match (even if the wording is different).
      C. One controlled provider API call.
      D. Stale saved data on provider error/429/call-budget exhaustion.

    The external API is therefore NOT the first stop for every user's query.
    """
    cached = read_cache(pharmacy, query)

    if cached and cached["products"] and cached["age_seconds"] < CACHE_TTL_SECONDS:
        source = "cache"
        if cached.get("source_type") == "catalogue":
            source = "catalogue"
        print(
            f"{pharmacy}: CACHE HIT for '{normalize_query(query)}' "
            f"({cache_age_label(cached['age_seconds'])})"
        )
        return cached["products"], cached["fetched_at"], source

    # Search the shared catalogue BEFORE calling the external API.
    catalogue_products = search_shared_catalogue(pharmacy, query)
    if catalogue_products:
        latest = get_catalogue_latest_timestamp(pharmacy, catalogue_products)
        product_keys = []

        # Re-create keys from the normalized DB-backed products is difficult
        # without the external id, so find them by a stable name/fields match.
        # We instead cache the query through a dedicated lookup below.
        with db_lock:
            conn = get_db_connection()
            try:
                for product in catalogue_products:
                    row = conn.execute(
                        """
                        SELECT product_key FROM products
                        WHERE pharmacy = ?
                          AND name = ?
                          AND COALESCE(pack_size, '') = COALESCE(?, '')
                        ORDER BY last_seen_at DESC
                        LIMIT 1
                        """,
                        (pharmacy, product.get("name"), product.get("pack_size")),
                    ).fetchone()
                    if row:
                        product_keys.append(row["product_key"])
            finally:
                conn.close()

        write_cache(
            pharmacy,
            query,
            product_keys,
            status=200,
            error=None,
            fetched_at=latest or time.time(),
            source_type="catalogue",
        )
        print(
            f"{pharmacy}: CATALOGUE HIT for '{normalize_query(query)}' "
            f"({len(catalogue_products)} saved products; no API call)"
        )
        return catalogue_products, latest, "catalogue"

    # Only the SAME pharmacy + SAME query is serialized. Different medicines
    # or pharmacies can proceed independently.
    query_lock = get_query_lock(pharmacy, query)
    with query_lock:
        # Another user may have filled cache/catalogue while we waited.
        cached = read_cache(pharmacy, query)
        if cached and cached["products"] and cached["age_seconds"] < CACHE_TTL_SECONDS:
            source = "cache"
            if cached.get("source_type") == "catalogue":
                source = "catalogue"
            print(
                f"{pharmacy}: CACHE HIT after lock for '{normalize_query(query)}' "
                f"({cache_age_label(cached['age_seconds'])})"
            )
            return cached["products"], cached["fetched_at"], source

        catalogue_products = search_shared_catalogue(pharmacy, query)
        if catalogue_products:
            latest = get_catalogue_latest_timestamp(pharmacy, catalogue_products)
            product_keys = []
            with db_lock:
                conn = get_db_connection()
                try:
                    for product in catalogue_products:
                        row = conn.execute(
                            """
                            SELECT product_key FROM products
                            WHERE pharmacy = ?
                              AND name = ?
                              AND COALESCE(pack_size, '') = COALESCE(?, '')
                            ORDER BY last_seen_at DESC
                            LIMIT 1
                            """,
                            (pharmacy, product.get("name"), product.get("pack_size")),
                        ).fetchone()
                        if row:
                            product_keys.append(row["product_key"])
                finally:
                    conn.close()

            write_cache(
                pharmacy,
                query,
                product_keys,
                fetched_at=latest or time.time(),
                source_type="catalogue",
            )
            print(
                f"{pharmacy}: CATALOGUE HIT after lock for '{normalize_query(query)}' "
                f"({len(catalogue_products)} saved products; no API call)"
            )
            return catalogue_products, latest, "catalogue"

        blocked, state = provider_is_blocked(pharmacy)
        if blocked:
            remaining = int(max(0, state["blocked_until"] - time.time()))
            print(
                f"{pharmacy}: PROVIDER COOLDOWN active; ~{remaining}s remaining "
                f"for '{normalize_query(query)}'"
            )
            if cached and cached["age_seconds"] <= STALE_CACHE_MAX_SECONDS:
                return cached["products"], cached["fetched_at"], "stale"
            return [], None, "blocked"

        allowed, count = can_call_provider(pharmacy)
        if not allowed:
            print(
                f"{pharmacy}: DAILY APP SAFETY LIMIT reached ({count} calls); "
                f"no API call for '{normalize_query(query)}'"
            )
            if cached and cached["age_seconds"] <= STALE_CACHE_MAX_SECONDS:
                return cached["products"], cached["fetched_at"], "stale"
            return [], None, "budget"

        # IMPORTANT: network call happens OUTSIDE db_lock.
        # This avoids blocking unrelated users/searches on SQLite.
        record_provider_call(pharmacy)
        result = fetch_products(query)

        if result.get("ok"):
            products = result.get("products", [])
            product_keys = upsert_products(pharmacy, products)
            fetched_at = time.time()
            write_cache(
                pharmacy,
                query,
                product_keys,
                status=200,
                error=None,
                fetched_at=fetched_at,
                source_type="live",
            )
            set_provider_state(pharmacy, 200, None, 0)
            print(
                f"{pharmacy}: LIVE FETCH saved for '{normalize_query(query)}' "
                f"({len(products)} products; {len(product_keys)} catalogue records)"
            )
            return products, fetched_at, "live"

        status_code = int(result.get("status_code") or 0)
        error_text = str(result.get("error") or "unknown error")

        if status_code == 429:
            retry_after = get_retry_after_seconds(error_text)
            cooldown = max(DEFAULT_PROVIDER_COOLDOWN_SECONDS, retry_after)
            set_provider_state(pharmacy, 429, error_text, cooldown)
            print(
                f"{pharmacy}: HTTP 429; provider cooldown for {cooldown}s"
            )
        else:
            set_provider_state(pharmacy, status_code or 0, error_text, 0)

        if cached:
            update_cache_error(pharmacy, query, status_code or None, error_text)

        if cached and cached["age_seconds"] <= STALE_CACHE_MAX_SECONDS:
            print(
                f"{pharmacy}: using SAVED DATA for '{normalize_query(query)}' "
                f"({cache_age_label(cached['age_seconds'])})"
            )
            return cached["products"], cached["fetched_at"], "stale"

    return [], None, "error"


# ============================================================
# COMMON API HELPERS
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


# ============================================================
# APOLLO SEARCH
# ============================================================

def search_apollo(query, pincode="110001"):
    url = f"{PARSE_BASE_URL}/{APOLLO_SCRAPER_ID}/search_medicines"
    headers = {"X-API-Key": API_KEY}
    params = {"query": query, "pincode": pincode}

    try:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=25,
        )

        print("Apollo HTTP status:", response.status_code)

        if response.status_code == 200:
            return response.json()

        print("Apollo error:", response.text[:500])
        return {"error": response.text, "status_code": response.status_code}

    except requests.RequestException as e:
        print("Apollo exception:", e)
        return {"error": str(e), "status_code": 0}


# ============================================================
# MEDPLUS SEARCH
# ============================================================

def search_medplus(query):
    url = f"{PARSE_BASE_URL}/{MEDPLUS_SCRAPER_ID}/search_medicines"
    headers = {"X-API-Key": API_KEY}
    params = {"page": 1, "query": query}

    try:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=25,
        )

        print("MedPlus HTTP status:", response.status_code)

        if response.status_code == 200:
            return response.json()

        print("MedPlus error:", response.text[:500])
        return {"error": response.text, "status_code": response.status_code}

    except requests.RequestException as e:
        print("MedPlus exception:", e)
        return {"error": str(e), "status_code": 0}


# ============================================================
# PHARMEASY SEARCH
# ============================================================

def search_pharmeasy(query):
    url = f"{PARSE_BASE_URL}/{PHARMEASY_SCRAPER_ID}/search_medicines"
    headers = {
        "X-API-Key": API_KEY,
        "Content-Type": "application/json",
    }
    payload = {"page": 1, "query": query}

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=60,
        )

        print("PharmEasy HTTP status:", response.status_code)

        if response.status_code == 200:
            return response.json()

        print("PharmEasy error:", response.text[:500])
        return {"error": response.text, "status_code": response.status_code}

    except requests.RequestException as e:
        print("PharmEasy exception:", e)
        return {"error": str(e), "status_code": 0}


# ============================================================
# PHARMEASY NORMALIZATION
# ============================================================

def normalize_pharmeasy_products(products):
    normalized = []

    for product in products:
        if not isinstance(product, dict):
            continue

        price = first_value(
            product,
            "price",
            "selling_price",
            "discounted_price",
            "final_price",
            "product_price",
            "sale_price",
        )

        mrp = first_value(
            product,
            "mrp",
            "MRP",
            "mrp_price",
            "maximum_retail_price",
        )

        prescription = first_value(
            product,
            "is_prescription_required",
            "prescription_required",
            "requires_prescription",
            "prescription",
        )

        availability = first_value(
            product,
            "availability",
            "stock_status",
            "in_stock",
        )

        image_value = first_value(
            product,
            "image_url",
            "image",
            "imageUrl",
            "image_url_1",
            "thumbnail",
            "thumbnail_url",
        )

        if isinstance(image_value, dict):
            image_value = first_value(
                image_value,
                "url",
                "src",
                "image_url",
            )

        normalized.append(
            {
                "name": first_value(
                    product,
                    "product_name",
                    "name",
                    "medicine_name",
                    "title",
                ) or "Unnamed medicine",
                "manufacturer": first_value(
                    product,
                    "manufacturer",
                    "manufacturer_name",
                    "brand",
                ),
                "pack_size": first_value(
                    product,
                    "pack_size",
                    "pack",
                    "quantity",
                    "packaging",
                ),
                "composition": first_value(
                    product,
                    "composition",
                    "salt",
                    "ingredients",
                ),
                "price": to_float(price, 0),
                "mrp": to_float(mrp),
                "discount_percentage": first_value(
                    product,
                    "discount_percentage",
                    "discount",
                    "discount_percent",
                ),
                "availability": availability,
                "is_prescription_required": parse_bool(prescription),
                "product_url": first_value(
                    product,
                    "product_url",
                    "url",
                    "link",
                ),
                "image_url": image_value,
            }
        )

    return normalized


# ============================================================
# CACHE-AWARE PHARMACY FETCHERS
# ============================================================

def fetch_apollo_products(query):
    result = search_apollo(query)
    if isinstance(result, dict) and result.get("status") == "success":
        return {"ok": True, "products": get_products(result)}

    return {
        "ok": False,
        "error": result.get("error", "Apollo API failed") if isinstance(result, dict) else "Apollo API failed",
        "status_code": result.get("status_code", 0) if isinstance(result, dict) else 0,
    }


def fetch_medplus_products(query):
    result = search_medplus(query)
    if isinstance(result, dict) and result.get("status") == "success":
        return {"ok": True, "products": get_products(result)}

    return {
        "ok": False,
        "error": result.get("error", "MedPlus API failed") if isinstance(result, dict) else "MedPlus API failed",
        "status_code": result.get("status_code", 0) if isinstance(result, dict) else 0,
    }


def fetch_pharmeasy_products(query):
    result = search_pharmeasy(query)
    if isinstance(result, dict) and result.get("status") == "success":
        return {
            "ok": True,
            "products": normalize_pharmeasy_products(get_products(result)),
        }

    return {
        "ok": False,
        "error": result.get("error", "PharmEasy API failed") if isinstance(result, dict) else "PharmEasy API failed",
        "status_code": result.get("status_code", 0) if isinstance(result, dict) else 0,
    }


def format_cache_meta(fetched_at, source):
    if fetched_at is None:
        if source == "blocked":
            return {"label": "⏳ Provider temporarily limited", "source": source}
        if source == "budget":
            return {"label": "⏳ Provider call limit protected", "source": source}
        return {"label": "Price data unavailable", "source": source}

    age_seconds = max(0, time.time() - float(fetched_at))

    if source == "live":
        label = "🟢 Live • checked just now"
    elif source == "cache":
        label = f"🕒 Saved • checked {cache_age_label(age_seconds)}"
    elif source == "catalogue":
        label = f"📚 Catalogue • checked {cache_age_label(age_seconds)}"
    elif source == "stale":
        label = "⚠️ Saved data • last checked " f"{cache_age_label(age_seconds)}"
    else:
        label = "Price data unavailable"

    return {"label": label, "source": source}


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

        /* Pre-search animations live above the results area.
           Once a medicine has been searched, remove this whole block
           so the results occupy the same space immediately. */
        .pre-search-animations {
            transition: opacity 0.22s ease, transform 0.22s ease;
        }

        .pre-search-animations.hide-on-results {
            display: none !important;
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
            margin: 8px auto 16px;
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


        /* ============================================================
           STEP COUNTER
           ============================================================ */

        .step-counter {
            max-width: 1050px;
            margin: 0 auto 12px;
            padding: 16px 20px;
            border-radius: 17px;
            border: 1px solid #d9eee6;
            background: linear-gradient(135deg, #f8fffc, #eefaf5);
            box-shadow: 0 5px 18px rgba(17, 88, 67, 0.05);
            display: flex;
            align-items: center;
            gap: 18px;
            position: relative;
            overflow: hidden;
        }

        .step-counter::before {
            content: "";
            position: absolute;
            width: 120px;
            height: 120px;
            border-radius: 50%;
            right: -45px;
            top: -60px;
            background: rgba(12,166,120,0.08);
            pointer-events: none;
        }

        .step-icon {
            width: 62px;
            height: 62px;
            flex: 0 0 62px;
            border-radius: 18px;
            background: #e0f6ed;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 34px;
            animation: stepFloat 2.4s ease-in-out infinite;
            position: relative;
            z-index: 1;
        }

        .step-content {
            min-width: 0;
            flex: 1;
            position: relative;
            z-index: 1;
        }

        .step-label {
            color: #5a8a7b;
            font-size: 10px;
            font-weight: 800;
            text-transform: uppercase;
            letter-spacing: 1.4px;
            margin-bottom: 2px;
        }

        .step-title-row {
            display: flex;
            align-items: baseline;
            gap: 8px;
            flex-wrap: wrap;
        }

        .step-count {
            color: #087f5b;
            font-size: 28px;
            line-height: 1;
            font-weight: 900;
            letter-spacing: -0.5px;
        }

        .step-goal-text {
            color: #708079;
            font-size: 12px;
            font-weight: 700;
        }

        .step-progress {
            height: 7px;
            width: 100%;
            margin-top: 9px;
            border-radius: 99px;
            background: #dcefe8;
            overflow: hidden;
        }

        .step-progress-bar {
            height: 100%;
            width: 0%;
            border-radius: 99px;
            background: #0ca678;
            transition: width 0.35s ease;
        }

        .step-status {
            margin-top: 6px;
            color: #708079;
            font-size: 11px;
            min-height: 15px;
        }

        .step-actions {
            display: flex;
            flex-direction: column;
            gap: 7px;
            flex: 0 0 auto;
            position: relative;
            z-index: 1;
        }

        .step-button {
            border: 0;
            border-radius: 11px;
            padding: 10px 15px;
            background: #087f5b;
            color: white;
            font-size: 12px;
            font-weight: 800;
            cursor: pointer;
            transition: all 0.2s ease;
            white-space: nowrap;
        }

        .step-button:hover {
            background: #066a4c;
            transform: translateY(-1px);
            box-shadow: 0 5px 12px rgba(8,127,91,0.20);
        }

        .step-button.secondary {
            background: #e5f2ed;
            color: #17634f;
        }

        .step-button.secondary:hover {
            background: #d6ebe3;
            box-shadow: none;
        }

        @keyframes stepFloat {
            0%, 100% { transform: translateY(0); }
            50% { transform: translateY(-5px); }
        }

        @media (max-width: 600px) {
            .step-counter {
                padding: 13px 14px;
                gap: 11px;
                align-items: flex-start;
            }

            .step-icon {
                width: 48px;
                height: 48px;
                flex-basis: 48px;
                border-radius: 14px;
                font-size: 27px;
            }

            .step-count {
                font-size: 23px;
            }

            .step-goal-text {
                font-size: 11px;
            }

            .step-button {
                padding: 9px 11px;
                font-size: 11px;
            }

            .step-actions {
                margin-left: auto;
            }
        }

        @media (prefers-reduced-motion: reduce) {
            .step-icon {
                animation: none !important;
            }
        }

        .search-scan {
            max-width: 1050px;
            margin: 0 auto 20px;
            padding: 16px 20px;
            border-radius: 16px;
            border: 1px solid #d9eee6;
            background: #f7fffc;
            display: none;
            align-items: center;
            gap: 14px;
            overflow: hidden;
            position: relative;
        }

        .search-scan.active {
            display: flex;
            animation: scanBoxIn 0.35s ease both;
        }

        .scan-icon {
            width: 44px;
            height: 44px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            background: #e2f7ef;
            font-size: 23px;
            flex: 0 0 44px;
            animation: scanPulse 1.3s ease-in-out infinite;
        }

        .scan-content {
            min-width: 0;
            flex: 1;
        }

        .scan-title {
            color: #17634f;
            font-size: 14px;
            font-weight: 800;
            margin-bottom: 5px;
        }

        .scan-subtitle {
            color: #708079;
            font-size: 12px;
            margin-bottom: 9px;
        }

        .scan-track {
            height: 5px;
            width: 100%;
            border-radius: 99px;
            background: #dcefe8;
            overflow: hidden;
            position: relative;
        }

        .scan-line {
            position: absolute;
            left: -25%;
            top: 0;
            width: 25%;
            height: 100%;
            border-radius: 99px;
            background: #16a878;
            animation: scanMove 1.35s ease-in-out infinite;
        }

        .scan-dots {
            display: inline-flex;
            gap: 4px;
            margin-left: 3px;
        }

        .scan-dots span {
            width: 4px;
            height: 4px;
            border-radius: 50%;
            background: #16a878;
            animation: scanDot 1s infinite ease-in-out;
        }

        .scan-dots span:nth-child(2) {
            animation-delay: 0.15s;
        }

        .scan-dots span:nth-child(3) {
            animation-delay: 0.3s;
        }

        @keyframes scanBoxIn {
            from {
                opacity: 0;
                transform: translateY(8px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }

        @keyframes scanPulse {
            0%, 100% {
                transform: scale(1);
            }
            50% {
                transform: scale(1.1);
            }
        }

        @keyframes scanMove {
            0% {
                left: -25%;
            }
            100% {
                left: 100%;
            }
        }

        @keyframes scanDot {
            0%, 80%, 100% {
                opacity: 0.25;
                transform: translateY(0);
            }
            40% {
                opacity: 1;
                transform: translateY(-2px);
            }
        }

        @media (max-width: 600px) {
            .search-scan {
                padding: 13px 14px;
                gap: 10px;
            }

            .scan-icon {
                width: 38px;
                height: 38px;
                flex-basis: 38px;
                font-size: 20px;
            }

            .scan-title {
                font-size: 13px;
            }

            .scan-subtitle {
                font-size: 11px;
            }
        }

        @media (prefers-reduced-motion: reduce) {
            .scan-icon,
            .scan-line,
            .scan-dots span {
                animation: none !important;
            }
        }

        .price-compare-animation {
            max-width: 1050px;
            margin: 0 auto 12px;
            min-height: 96px;
            padding: 15px 20px;
            border-radius: 17px;
            border: 1px solid #e1eee9;
            background: linear-gradient(135deg, #fbfffd, #f1faf6);
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 16px;
            position: relative;
            overflow: hidden;
            box-shadow: 0 5px 18px rgba(17, 88, 67, 0.05);
        }

        .price-orbit {
            position: relative;
            width: 76px;
            height: 60px;
            flex: 0 0 76px;
        }

        .rupee-coin {
            position: absolute;
            width: 34px;
            height: 34px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 18px;
            font-weight: 900;
            background: #e2f7ef;
            border: 1px solid #c9eadd;
            color: #14785c;
            box-shadow: 0 4px 10px rgba(20, 120, 92, 0.10);
        }

        .rupee-one {
            left: 0;
            top: 13px;
            animation: rupeeFloatOne 2.6s ease-in-out infinite;
        }

        .rupee-two {
            right: 0;
            top: 3px;
            animation: rupeeFloatTwo 2.6s ease-in-out infinite;
        }

        .rupee-three {
            left: 23px;
            bottom: 0;
            animation: rupeeFloatThree 2.6s ease-in-out infinite;
        }

        .price-compare-copy {
            position: relative;
            z-index: 2;
        }

        .price-compare-title {
            color: #17634f;
            font-size: 15px;
            font-weight: 850;
            margin-bottom: 5px;
        }

        .price-compare-subtitle {
            color: #708079;
            font-size: 12px;
            line-height: 1.45;
        }

        .price-spark {
            position: absolute;
            width: 180px;
            height: 180px;
            border-radius: 50%;
            right: -80px;
            top: -80px;
            background: rgba(33, 177, 131, 0.07);
            animation: priceSpark 4s ease-in-out infinite;
            pointer-events: none;
        }

        @keyframes rupeeFloatOne {
            0%, 100% { transform: translate(0, 0) rotate(0deg); }
            50% { transform: translate(-3px, -7px) rotate(-5deg); }
        }

        @keyframes rupeeFloatTwo {
            0%, 100% { transform: translate(0, 0) rotate(0deg); }
            50% { transform: translate(3px, 6px) rotate(5deg); }
        }

        @keyframes rupeeFloatThree {
            0%, 100% { transform: translate(0, 0) rotate(0deg); }
            50% { transform: translate(0, -6px) rotate(-4deg); }
        }

        @keyframes priceSpark {
            0%, 100% { transform: scale(0.9); opacity: 0.55; }
            50% { transform: scale(1.12); opacity: 1; }
        }

        @media (max-width: 600px) {
            .price-compare-animation {
                min-height: 92px;
                padding: 13px 14px;
                gap: 10px;
            }

            .price-orbit {
                width: 64px;
                height: 54px;
                flex-basis: 64px;
                transform: scale(0.88);
            }

            .price-compare-title {
                font-size: 13px;
            }

            .price-compare-subtitle {
                font-size: 11px;
            }
        }

        @media (prefers-reduced-motion: reduce) {
            .rupee-coin,
            .price-spark {
                animation: none !important;
            }
        }

        .pharmacy-section {
            position: relative;
            overflow: hidden;
        }

        .pharmacy-section::before {
            content: "";
            position: absolute;
            top: 0;
            left: -35%;
            width: 35%;
            height: 2px;
            background: #20a879;
            opacity: 0;
            pointer-events: none;
        }

        .pharmacy-section:hover::before {
            opacity: 1;
            animation: pharmacySweep 1.1s ease-out;
        }

        .pharmacy-header h2 {
            transition: transform 0.25s ease;
        }

        .pharmacy-section:hover .pharmacy-header h2 {
            transform: translateX(3px);
        }

        .pharmacy-section .count {
            transition: transform 0.25s ease, box-shadow 0.25s ease;
        }

        .pharmacy-section:hover .count {
            transform: scale(1.04);
            box-shadow: 0 3px 10px rgba(23, 99, 79, 0.08);
        }

        @keyframes pharmacySweep {
            0% {
                left: -35%;
            }
            100% {
                left: 100%;
            }
        }

        @media (prefers-reduced-motion: reduce) {
            .pharmacy-section::before {
                animation: none !important;
            }

            .pharmacy-section:hover .pharmacy-header h2,
            .pharmacy-section:hover .count {
                transform: none;
            }
        }

        .warning {
            background: #fff8db;
            border: 1px solid #f0d56a;
            color: #6b5600;
            border-radius: 12px;
            padding: 11px 15px;
            font-size: 12px;
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

        .pharmacy-meta {
            display: flex;
            flex-direction: column;
            align-items: flex-end;
            gap: 3px;
            text-align: right;
        }

        .cache-time {
            color: #73817b;
            font-size: 10px;
            line-height: 1.3;
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

<div class="search-scan" id="searchScan" aria-live="polite">
    <div class="scan-icon">🔍</div>

    <div class="scan-content">
        <div class="scan-title">
            Checking pharmacy catalogues
            <span class="scan-dots">
                <span></span><span></span><span></span>
            </span>
        </div>

        <div class="scan-subtitle">
            Looking for matching medicines and available prices...
        </div>

        <div class="scan-track">
            <div class="scan-line"></div>
        </div>
    </div>
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

<div class="pre-search-animations{% if searched_medicine %} hide-on-results{% endif %}" id="preSearchAnimations">


    <div class="step-counter" id="stepCounter">
        <div class="step-icon" aria-hidden="true">🚶</div>

        <div class="step-content">
            <div class="step-label">Daily Activity</div>

            <div class="step-title-row">
                <span class="step-count" id="stepCount">0</span>
                <span class="step-goal-text">/ 10,000 steps</span>
            </div>

            <div class="step-progress" aria-label="Daily step goal progress">
                <div class="step-progress-bar" id="stepProgressBar"></div>
            </div>

            <div class="step-status" id="stepStatus">
                Use the HTTPS MediCompare site on your phone to enable motion counting.
            </div>
        </div>

        <div class="step-actions">
            <button type="button" class="step-button" id="stepStartButton">
                Start Counting
            </button>
            <button type="button" class="step-button secondary" id="stepResetButton">
                Reset
            </button>
        </div>
    </div>

    <div class="health-tips" id="healthTips" aria-live="polite">
        <div class="health-tip-glow"></div>
        <span class="health-tip-icon" id="healthTipIcon">💧</span>
        <span class="health-tip-label">Healthy Habit</span>
        <span class="health-tip-text" id="healthTipText">Stay hydrated — drink enough water throughout the day.</span>
    </div>

    <div class="price-compare-animation">
        <div class="price-spark"></div>

        <div class="price-orbit" aria-hidden="true">
            <div class="rupee-coin rupee-one">₹</div>
            <div class="rupee-coin rupee-two">₹</div>
            <div class="rupee-coin rupee-three">₹</div>
        </div>

        <div class="price-compare-copy">
            <div class="price-compare-title">Compare prices. Choose smarter. 💚</div>
            <div class="price-compare-subtitle">
                Always compare the exact medicine, strength and pack size before choosing a lower-priced option.
            </div>
        </div>
    </div>

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

<section class="pharmacy-section pharmacy-result-reveal">

    <div class="pharmacy-header">

        <h2>🏥 Apollo Pharmacy</h2>

        <div class="pharmacy-meta">
            <span class="count">
                {{ apollo_products|length }} result(s)
            </span>
            {% if cache_meta.apollo %}
                <span class="cache-time">{{ cache_meta.apollo.label }}</span>
            {% endif %}
        </div>

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

<section class="pharmacy-section pharmacy-result-reveal">

    <div class="pharmacy-header">

        <h2>💚 MedPlus</h2>

        <div class="pharmacy-meta">
            <span class="count">
                {{ medplus_products|length }} result(s)
            </span>
            {% if cache_meta.medplus %}
                <span class="cache-time">{{ cache_meta.medplus.label }}</span>
            {% endif %}
        </div>

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

<section class="pharmacy-section pharmacy-result-reveal">

    <div class="pharmacy-header">

        <h2>💙 PharmEasy</h2>

        <div class="pharmacy-meta">
            <span class="count">
                {{ pharmeasy_products|length }} result(s)
            </span>
            {% if cache_meta.pharmeasy %}
                <span class="cache-time">{{ cache_meta.pharmeasy.label }}</span>
            {% endif %}
        </div>

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

    MediCompare may use saved search results to reduce
    repeated pharmacy catalogue requests.

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

        const searchScan = document.getElementById("searchScan");
        if (searchScan) {
            searchScan.classList.add("active");
        }
    });

    updateClearButton();


    // ============================================================
    // STEP COUNTER
    // Walking detector v10.
    // V10 preserves V9's seated-movement protection while improving
    // cadence validation and missed-step recovery.

    const STEP_GOAL = 10000;
    const stepCountElement = document.getElementById("stepCount");
    const stepProgressBar = document.getElementById("stepProgressBar");
    const stepStatus = document.getElementById("stepStatus");
    const stepStartButton = document.getElementById("stepStartButton");
    const stepResetButton = document.getElementById("stepResetButton");

    const stepStorageKey = "medicompare_steps_" +
        new Date().toISOString().slice(0, 10);

    let stepCount = parseInt(localStorage.getItem(stepStorageKey) || "0", 10);
    if (!Number.isFinite(stepCount) || stepCount < 0) stepCount = 0;

    let displayedStepCount = stepCount;
    let stepCounting = false;
    let uiUpdateTimer = null;

    // Sensor processing state.
    let gravityX = 0;
    let gravityY = 0;
    let gravityZ = 0;
    let gravityInitialized = false;
    let filteredSignal = 0;
    let previousSignal = 0;
    let previousPreviousSignal = 0;
    let detectorReady = false;

    // Adaptive signal/noise state.
    let noiseFloor = 0;
    let signalPeakReference = 0;
    let lastMotionAt = 0;

    // Walking detector.
    const GRAVITY_ALPHA = 0.88;
    const SIGNAL_ALPHA = 0.50;
    const NOISE_ALPHA = 0.020;

    // Conservative while starting; more sensitive after walking is confirmed.
    const MIN_DYNAMIC_PEAK = 0.22;
    const PEAK_MARGIN = 0.16;
    const CONFIRMED_MIN_DYNAMIC_PEAK = 0.18;
    const CONFIRMED_PEAK_MARGIN = 0.12;

    const STEP_MIN_INTERVAL_MS = 300;
    const STEP_MAX_INTERVAL_MS = 1450;
    const WALK_CONFIRM_CANDIDATES = 4;
    const WALK_INTERVAL_TOLERANCE = 0.28;
    const WALK_AMPLITUDE_TOLERANCE = 0.45;
    const WALK_START_TIMEOUT_MS = 1800;
    const WALK_BREAK_TIMEOUT_MS = 1250;

    const DUPLICATE_INTERVAL_FACTOR = 0.42;

    let candidateTimes = [];
    let candidateAmplitudes = [];
    let confirmedWalking = false;
    let lastCandidateTime = 0;
    let lastConfirmedTime = 0;

    function updateStepDisplay(force = false) {
        if (!force && displayedStepCount === stepCount) return;
        displayedStepCount = stepCount;
        stepCountElement.textContent = displayedStepCount.toLocaleString();
        const progress = Math.min(100, (displayedStepCount / STEP_GOAL) * 100);
        stepProgressBar.style.width = progress + "%";
    }

    function saveSteps() {
        localStorage.setItem(stepStorageKey, String(stepCount));
    }

    function addConfirmedSteps(numberOfSteps) {
        if (numberOfSteps <= 0) return;
        stepCount += numberOfSteps;
        saveSteps();
        // Keep the value available immediately; the visible UI timer handles
        // the normal refresh cadence.
    }

    function startUiUpdateTimer() {
        stopUiUpdateTimer();
        uiUpdateTimer = setInterval(function () {
            updateStepDisplay(true);
        }, 1500);
    }

    function stopUiUpdateTimer() {
        if (uiUpdateTimer) {
            clearInterval(uiUpdateTimer);
            uiUpdateTimer = null;
        }
    }

    function resetWalkingDetector() {
        candidateTimes = [];
        candidateAmplitudes = [];
        confirmedWalking = false;
        lastCandidateTime = 0;
        lastConfirmedTime = 0;
        signalPeakReference = 0;
    }

    function cadenceIsRegular(times) {
        if (times.length < WALK_CONFIRM_CANDIDATES) return false;

        const intervals = [];
        for (let i = 1; i < times.length; i++) {
            intervals.push(times[i] - times[i - 1]);
        }

        const recent = intervals.slice(-3);
        if (recent.length < 3) return false;

        const sorted = [...recent].sort((a, b) => a - b);
        const median = sorted[Math.floor(sorted.length / 2)];

        if (median < STEP_MIN_INTERVAL_MS ||
            median > STEP_MAX_INTERVAL_MS) {
            return false;
        }

        return recent.every(value =>
            Math.abs(value - median) / median <= WALK_INTERVAL_TOLERANCE
        );
    }

    function amplitudesAreWalkingLike(amplitudes) {
        if (amplitudes.length < WALK_CONFIRM_CANDIDATES) return false;

        const recent = amplitudes.slice(-4);
        const sorted = [...recent].sort((a, b) => a - b);
        const median = sorted[Math.floor(sorted.length / 2)];

        if (!Number.isFinite(median) || median < MIN_DYNAMIC_PEAK) {
            return false;
        }

        return recent.every(value =>
            Math.abs(value - median) / median <= WALK_AMPLITUDE_TOLERANCE
        );
    }

    function registerCandidate(now, amplitude) {
        let missedSteps = 0;

        if (lastCandidateTime) {
            const interval = now - lastCandidateTime;

            if (interval < STEP_MIN_INTERVAL_MS) return;

            if (interval > STEP_MAX_INTERVAL_MS) {
                resetWalkingDetector();
            } else if (confirmedWalking && candidateTimes.length >= 3) {
                const recentIntervals = [];

                for (
                    let i = Math.max(1, candidateTimes.length - 4);
                    i < candidateTimes.length;
                    i++
                ) {
                    recentIntervals.push(
                        candidateTimes[i] - candidateTimes[i - 1]
                    );
                }

                if (recentIntervals.length) {
                    const sortedIntervals =
                        [...recentIntervals].sort((a, b) => a - b);
                    const cadence =
                        sortedIntervals[
                            Math.floor(sortedIntervals.length / 2)
                        ];

                    if (!Number.isFinite(cadence)) return;

                    // Reject a second peak from the same phone swing.
                    if (interval < cadence * DUPLICATE_INTERVAL_FACTOR) {
                        return;
                    }

                    // Active walking must stay close to the established
                    // cadence. Timing alone is not enough.
                    if (interval < cadence * 0.72 ||
                        interval > cadence * 1.32) {
                        return;
                    }

                    // The new peak must remain reasonably strong compared
                    // with the recent walking peaks. This is the key
                    // seated-movement protection.
                    const recentAmplitudes =
                        candidateAmplitudes.slice(-4);

                    if (recentAmplitudes.length >= 3) {
                        const sortedAmplitudes =
                            [...recentAmplitudes].sort((a, b) => a - b);
                        const amplitudeMedian =
                            sortedAmplitudes[
                                Math.floor(sortedAmplitudes.length / 2)
                            ];

                        if (!Number.isFinite(amplitudeMedian) ||
                            amplitude <
                                Math.max(
                                    CONFIRMED_MIN_DYNAMIC_PEAK,
                                    amplitudeMedian * 0.52
                                )) {
                            return;
                        }
                    }

                    // Recover one missed step only for a very clear
                    // approximately-2x cadence gap.
                    const cadenceRatio = interval / cadence;

                    if (cadenceRatio >= 1.62 &&
                        cadenceRatio <= 2.42) {
                        missedSteps = 1;
                    }
                }
            }
        }

        candidateTimes.push(now);
        candidateAmplitudes.push(amplitude);
        lastCandidateTime = now;

        if (candidateTimes.length > 8) {
            candidateTimes.shift();
            candidateAmplitudes.shift();
        }

        // Startup remains strict: four rhythmic, similar-strength peaks
        // are required before the detector enters active walking mode.
        if (!confirmedWalking &&
            candidateTimes.length >= WALK_CONFIRM_CANDIDATES &&
            cadenceIsRegular(candidateTimes) &&
            amplitudesAreWalkingLike(candidateAmplitudes) &&
            candidateAmplitudes
                .slice(-4)
                .every(value => value >= MIN_DYNAMIC_PEAK)) {

            confirmedWalking = true;

            // Current peak is the first counted walking step.
            addConfirmedSteps(1);
            lastConfirmedTime = now;
            return;
        }

        // Once confirmed, count every validated walking peak.
        if (confirmedWalking &&
            candidateTimes.length >= 4 &&
            cadenceIsRegular(candidateTimes)) {

            addConfirmedSteps(1 + missedSteps);
            lastConfirmedTime = now;
        }
    }

    function chooseAcceleration(event) {
        const linear = event.acceleration;
        const gravityIncluded = event.accelerationIncludingGravity;

        function validVector(value) {
            return value &&
                Number.isFinite(Number(value.x)) &&
                Number.isFinite(Number(value.y)) &&
                Number.isFinite(Number(value.z));
        }

        if (validVector(linear)) {
            const lm = Math.sqrt(
                Number(linear.x) ** 2 +
                Number(linear.y) ** 2 +
                Number(linear.z) ** 2
            );
            // Some Android/browser implementations expose a nominal linear
            // vector but leave it effectively empty. Fall back in that case.
            if (lm > 0.08) return { vector: linear, linear: true };
        }

        if (validVector(gravityIncluded)) {
            return { vector: gravityIncluded, linear: false };
        }

        return null;
    }

    function handleMotion(event) {
        if (!stepCounting) return;

        const selected = chooseAcceleration(event);
        if (!selected) return;

        const x = Number(selected.vector.x) || 0;
        const y = Number(selected.vector.y) || 0;
        const z = Number(selected.vector.z) || 0;
        let signalMagnitude;

        if (selected.linear) {
            signalMagnitude = Math.sqrt(x * x + y * y + z * z);
        } else {
            if (!gravityInitialized) {
                gravityX = x;
                gravityY = y;
                gravityZ = z;
                gravityInitialized = true;
                lastMotionAt = performance.now();
                return;
            }

            gravityX = GRAVITY_ALPHA * gravityX + (1 - GRAVITY_ALPHA) * x;
            gravityY = GRAVITY_ALPHA * gravityY + (1 - GRAVITY_ALPHA) * y;
            gravityZ = GRAVITY_ALPHA * gravityZ + (1 - GRAVITY_ALPHA) * z;

            const lx = x - gravityX;
            const ly = y - gravityY;
            const lz = z - gravityZ;
            signalMagnitude = Math.sqrt(lx * lx + ly * ly + lz * lz);
        }

        const now = performance.now();
        lastMotionAt = now;

        if (!detectorReady) {
            filteredSignal = signalMagnitude;
            previousPreviousSignal = filteredSignal;
            previousSignal = filteredSignal;
            detectorReady = true;
            noiseFloor = signalMagnitude;
            return;
        }

        filteredSignal =
            SIGNAL_ALPHA * signalMagnitude +
            (1 - SIGNAL_ALPHA) * filteredSignal;

        // Slowly follow the quiet/background movement. Large transient peaks
        // should not immediately raise the threshold.
        const quietSignal = Math.min(filteredSignal, noiseFloor + 0.35);
        noiseFloor =
            NOISE_ALPHA * quietSignal +
            (1 - NOISE_ALPHA) * noiseFloor;

        const isPeak =
            previousSignal > previousPreviousSignal &&
            previousSignal >= filteredSignal;

        if (isPeak) {
            const amplitude = previousSignal;
            const dynamicHeight = amplitude - noiseFloor;

            // Adaptive threshold: easy enough for normal walking, but requires
            // a real transient above the current background movement.
            const minimumPeak = confirmedWalking
                ? CONFIRMED_MIN_DYNAMIC_PEAK
                : MIN_DYNAMIC_PEAK;

            const peakMargin = confirmedWalking
                ? CONFIRMED_PEAK_MARGIN
                : PEAK_MARGIN;

            const adaptiveThreshold = Math.max(
                minimumPeak,
                peakMargin + noiseFloor
            );

            if (dynamicHeight >= minimumPeak &&
                amplitude >= adaptiveThreshold) {
                registerCandidate(now, dynamicHeight);
                signalPeakReference = dynamicHeight;
            }
        }

        previousPreviousSignal = previousSignal;
        previousSignal = filteredSignal;

        if (confirmedWalking &&
            lastConfirmedTime > 0 &&
            now - lastConfirmedTime > WALK_BREAK_TIMEOUT_MS) {
            resetWalkingDetector();
        }

        if (!confirmedWalking &&
            lastCandidateTime > 0 &&
            now - lastCandidateTime > WALK_START_TIMEOUT_MS) {
            resetWalkingDetector();
        }
    }

    async function startStepCounter() {
        try {
            if (!window.isSecureContext) {
                stepStatus.textContent =
                    "Motion counting needs HTTPS. Open MediCompare using its HTTPS address (or localhost).";
                return;
            }

            if (typeof DeviceMotionEvent === "undefined") {
                stepStatus.textContent =
                    "Motion sensors are unavailable in this browser. Try Chrome on Android.";
                return;
            }

            if (typeof DeviceMotionEvent.requestPermission === "function") {
                const permission = await DeviceMotionEvent.requestPermission();
                if (permission !== "granted") {
                    stepStatus.textContent =
                        "Motion permission was not granted. Please allow it and try again.";
                    return;
                }
            }

            gravityX = 0;
            gravityY = 0;
            gravityZ = 0;
            gravityInitialized = false;
            filteredSignal = 0;
            previousSignal = 0;
            previousPreviousSignal = 0;
            detectorReady = false;
            noiseFloor = 0;
            signalPeakReference = 0;
            lastMotionAt = 0;
            resetWalkingDetector();
            stepCounting = true;

            window.addEventListener("devicemotion", handleMotion, { passive: true });
            startUiUpdateTimer();

            stepStartButton.textContent = "Pause";
            stepStatus.textContent =
                "Calibrating motion… walk normally. Walking rhythm will be confirmed first.";
        } catch (error) {
            console.error("Step counter permission/error:", error);
            stepStatus.textContent =
                "Motion access could not be started. Please use the HTTPS MediCompare site.";
        }
    }

    function pauseStepCounter() {
        stepCounting = false;
        window.removeEventListener("devicemotion", handleMotion);
        stopUiUpdateTimer();
        updateStepDisplay(true);
        stepStartButton.textContent = "Start Counting";
        stepStatus.textContent =
            "Paused. Your steps are saved on this device.";
    }

    stepStartButton.addEventListener("click", function () {
        if (stepCounting) pauseStepCounter();
        else startStepCounter();
    });

    stepResetButton.addEventListener("click", function () {
        stepCount = 0;
        displayedStepCount = 0;
        gravityX = 0;
        gravityY = 0;
        gravityZ = 0;
        gravityInitialized = false;
        filteredSignal = 0;
        previousSignal = 0;
        previousPreviousSignal = 0;
        detectorReady = false;
        noiseFloor = 0;
        signalPeakReference = 0;
        resetWalkingDetector();
        saveSteps();
        updateStepDisplay(true);

        if (stepCounting) {
            stepStatus.textContent =
                "Reset complete. Walking detection is active…";
        } else {
            stepStatus.textContent =
                "Counter reset. Start counting when you're ready.";
        }
    });

    updateStepDisplay(true);

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

    searched_medicine = request.args.get("medicine", "").strip()

    apollo_products = []
    medplus_products = []
    pharmeasy_products = []

    cache_meta = {
        "apollo": None,
        "medplus": None,
        "pharmeasy": None,
    }

    error_message = ""

    if searched_medicine:

        if not API_KEY:
            error_message = (
                "PARSE_API_KEY was not found. Check your .env file."
            )
        else:
            # Each pharmacy has its own shared cache + catalogue records.
            # A first-time query may use the provider API. Repeated queries
            # from any user use the same saved data until refresh is needed.

            (
                apollo_products,
                apollo_checked_at,
                apollo_source,
            ) = cached_search(
                "apollo",
                searched_medicine,
                fetch_apollo_products,
            )
            cache_meta["apollo"] = format_cache_meta(
                apollo_checked_at,
                apollo_source,
            )

            (
                medplus_products,
                medplus_checked_at,
                medplus_source,
            ) = cached_search(
                "medplus",
                searched_medicine,
                fetch_medplus_products,
            )
            cache_meta["medplus"] = format_cache_meta(
                medplus_checked_at,
                medplus_source,
            )

            (
                pharmeasy_products,
                pharmeasy_checked_at,
                pharmeasy_source,
            ) = cached_search(
                "pharmeasy",
                searched_medicine,
                fetch_pharmeasy_products,
            )
            cache_meta["pharmeasy"] = format_cache_meta(
                pharmeasy_checked_at,
                pharmeasy_source,
            )

            if (
                not apollo_products
                and not medplus_products
                and not pharmeasy_products
            ):
                error_message = (
                    "No results were returned. Try a more specific medicine "
                    "name or strength."
                )

    return render_template_string(
        HTML,
        searched_medicine=searched_medicine,
        apollo_products=apollo_products,
        medplus_products=medplus_products,
        pharmeasy_products=pharmeasy_products,
        cache_meta=cache_meta,
        error_message=error_message,
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    print()
    print("MediCompare is starting...")
    print("Laptop: http://127.0.0.1:5000")
    print("LAN:    http://10.27.159.184:5000")
    print("Database:", os.path.abspath(DB_PATH))
    print("Cache TTL (hours):", round(CACHE_TTL_SECONDS / 3600, 2))
    print("Stale data max (days):", round(STALE_CACHE_MAX_SECONDS / 86400, 2))
    print()

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True,
    )
