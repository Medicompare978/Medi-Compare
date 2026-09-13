from flask import Flask, render_template_string, request, send_file
import requests
import os
import json
import sqlite3
import threading
import time
import hashlib
import difflib
from io import BytesIO

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib import colors
except ImportError:
    A4 = None
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
# persistent PostgreSQL/managed DB before launch on Render so cache/catalogue
# and provider usage are shared across restarts and multiple workers.

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

# App-level outbound rate protection. These are safety settings, not the
# provider's official quota. Tune them to the connected provider plan.
PROVIDER_MIN_INTERVAL_SECONDS = float(
    os.getenv("MEDICOMPARE_PROVIDER_MIN_INTERVAL", "2")
)
PROVIDER_QUEUE_TIMEOUT_SECONDS = int(
    os.getenv("MEDICOMPARE_PROVIDER_QUEUE_TIMEOUT", "120")
)
PROVIDER_QUEUE_MAXSIZE = int(
    os.getenv("MEDICOMPARE_PROVIDER_QUEUE_MAXSIZE", "100")
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



# ============================================================
# PROVIDER REQUEST QUEUE / RATE SHIELD
# ============================================================
# One worker per pharmacy serializes outbound calls. This prevents bursts
# when several users search different medicines at the same time.
# The worker re-checks cooldown + daily safety budget immediately before
# the network request, so queued requests do not consume quota just by
# entering the queue.

class ProviderJob:
    def __init__(self, pharmacy, query, fetch_products):
        self.pharmacy = pharmacy
        self.query = query
        self.fetch_products = fetch_products
        self.done = threading.Event()
        self.result = None


provider_queues = {}
provider_workers_started = set()
provider_queue_guard = threading.Lock()
provider_last_call_at = {}


def get_provider_queue(pharmacy):
    with provider_queue_guard:
        if pharmacy not in provider_queues:
            provider_queues[pharmacy] = queue.Queue(maxsize=PROVIDER_QUEUE_MAXSIZE)
        if pharmacy not in provider_workers_started:
            worker = threading.Thread(
                target=provider_worker,
                args=(pharmacy,),
                name=f"provider-worker-{pharmacy}",
                daemon=True,
            )
            worker.start()
            provider_workers_started.add(pharmacy)
        return provider_queues[pharmacy]


def provider_worker(pharmacy):
    q = provider_queues[pharmacy]
    while True:
        job = q.get()
        try:
            blocked, state = provider_is_blocked(pharmacy)
            if blocked:
                remaining = int(max(0, state.get("blocked_until", 0) - time.time()))
                job.result = {
                    "ok": False,
                    "status_code": 429,
                    "error": state.get("last_error") or f"Provider cooldown active (~{remaining}s)",
                }
                continue

            allowed, count = can_call_provider(pharmacy)
            if not allowed:
                job.result = {
                    "ok": False,
                    "status_code": 429,
                    "error": f"MediCompare provider call safety limit reached ({count})",
                }
                continue

            with provider_queue_guard:
                previous = provider_last_call_at.get(pharmacy, 0)
            wait_seconds = max(
                0,
                PROVIDER_MIN_INTERVAL_SECONDS - (time.time() - previous),
            )
            if wait_seconds:
                time.sleep(wait_seconds)

            # Re-check after the wait because another state change could have
            # happened while this job was sleeping.
            blocked, state = provider_is_blocked(pharmacy)
            if blocked:
                job.result = {
                    "ok": False,
                    "status_code": 429,
                    "error": state.get("last_error") or "Provider cooldown active",
                }
                continue

            allowed, count = can_call_provider(pharmacy)
            if not allowed:
                job.result = {
                    "ok": False,
                    "status_code": 429,
                    "error": f"MediCompare provider call safety limit reached ({count})",
                }
                continue

            # Count only a call that is actually going out.
            record_provider_call(pharmacy)
            print(f"{pharmacy}: QUEUE EXECUTE for '{normalize_query(job.query)}'")
            job.result = job.fetch_products(job.query)
            with provider_queue_guard:
                provider_last_call_at[pharmacy] = time.time()

        except Exception as exc:
            job.result = {
                "ok": False,
                "status_code": 0,
                "error": f"Provider queue worker error: {exc}",
            }
        finally:
            job.done.set()
            q.task_done()


def fetch_through_provider_queue(pharmacy, query, fetch_products):
    try:
        q = get_provider_queue(pharmacy)
        job = ProviderJob(pharmacy, query, fetch_products)
        q.put(job, timeout=PROVIDER_QUEUE_TIMEOUT_SECONDS)
        if not job.done.wait(timeout=PROVIDER_QUEUE_TIMEOUT_SECONDS + 10):
            return {
                "ok": False,
                "status_code": 503,
                "error": "Provider request queue timed out; saved data will be used if available.",
            }
        return job.result or {
            "ok": False,
            "status_code": 503,
            "error": "Provider queue returned no result.",
        }
    except queue.Full:
        return {
            "ok": False,
            "status_code": 503,
            "error": "Provider request queue is busy; saved data will be used if available.",
        }


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

        # IMPORTANT: network call happens OUTSIDE db_lock and inside the
        # provider queue worker. This keeps outbound traffic serialized per
        # pharmacy while allowing unrelated database work to continue.
        result = fetch_through_provider_queue(pharmacy, query, fetch_products)

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




        /* ============================================================
           DIET PDF EXPORT
        ============================================================ */
        .diet-print-only {
            display: none;
        }

        @media print {
            body > * {
                display: none !important;
            }

            #dietPdfPrintArea {
                display: block !important;
                position: static !important;
                width: 100% !important;
                background: white !important;
                color: #1f2f2a !important;
                padding: 28px !important;
                font-family: Arial, sans-serif !important;
            }

            #dietPdfPrintArea h1 {
                margin: 0 0 6px !important;
                color: #087f5b !important;
                font-size: 26px !important;
            }

            #dietPdfPrintArea h2 {
                margin: 22px 0 8px !important;
                color: #087f5b !important;
                font-size: 18px !important;
            }

            #dietPdfPrintArea p {
                margin: 5px 0 !important;
                line-height: 1.5 !important;
            }

            #dietPdfPrintArea .diet-pdf-meta {
                padding: 12px !important;
                border: 1px solid #dce9e3 !important;
                border-radius: 10px !important;
                margin: 14px 0 !important;
            }

            #dietPdfPrintArea .diet-pdf-meal {
                border: 1px solid #dce9e3 !important;
                border-radius: 10px !important;
                padding: 11px !important;
                margin: 8px 0 !important;
                page-break-inside: avoid !important;
            }

            #dietPdfPrintArea .diet-pdf-note {
                margin-top: 20px !important;
                padding-top: 10px !important;
                border-top: 1px solid #dce9e3 !important;
                font-size: 11px !important;
                color: #60736c !important;
            }
        }

        /* ============================================================
           CAMERA SCANNER
        ============================================================ */
        .camera-search-button {
            position: absolute;
            right: 9px;
            top: 50%;
            transform: translateY(-50%);
            width: 40px;
            height: 40px;
            border: 0;
            border-radius: 11px;
            background: #e8f8f2;
            color: #087f5b;
            font-size: 19px;
            cursor: pointer;
            z-index: 3;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: transform .18s ease, background .2s ease;
            -webkit-tap-highlight-color: transparent;
        }

        .camera-search-button:hover {
            background: #d9f3e9;
            transform: translateY(-50%) scale(1.04);
        }

        .camera-search-button:active {
            transform: translateY(-50%) scale(.96);
        }

        .camera-modal {
            position: fixed;
            inset: 0;
            z-index: 3000;
            display: none;
            align-items: center;
            justify-content: center;
            padding: 18px;
            background: rgba(10, 30, 24, .62);
            backdrop-filter: blur(7px);
            -webkit-backdrop-filter: blur(7px);
        }

        .camera-modal.show {
            display: flex;
            animation: cameraFadeIn .2s ease both;
        }

        @keyframes cameraFadeIn {
            from { opacity: 0; }
            to { opacity: 1; }
        }

        .camera-card {
            width: min(520px, 100%);
            max-height: calc(100dvh - 36px);
            overflow: auto;
            background: white;
            border-radius: 24px;
            padding: 18px;
            box-shadow: 0 20px 60px rgba(0,0,0,.25);
            animation: cameraCardIn .25s ease both;
        }

        @keyframes cameraCardIn {
            from { transform: translateY(12px) scale(.98); }
            to { transform: translateY(0) scale(1); }
        }

        .camera-card-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 12px;
        }

        .camera-card-title {
            color: #087f5b;
            font-size: 19px;
            font-weight: 900;
        }

        .camera-close {
            width: 38px;
            height: 38px;
            border: 0;
            border-radius: 12px;
            background: #f0f4f2;
            color: #47615a;
            font-size: 22px;
            cursor: pointer;
        }

        .camera-stage {
            position: relative;
            overflow: hidden;
            width: 100%;
            aspect-ratio: 4 / 3;
            border-radius: 18px;
            background: #10221d;
        }

        .camera-video,
        .camera-photo {
            display: block;
            width: 100%;
            height: 100%;
            object-fit: cover;
        }

        .camera-photo {
            display: none;
        }

        .camera-guide {
            position: absolute;
            left: 9%;
            right: 9%;
            top: 17%;
            bottom: 17%;
            border: 2px dashed rgba(255,255,255,.85);
            border-radius: 16px;
            pointer-events: none;
        }

        .camera-hint {
            margin: 10px 2px 0;
            color: #62736e;
            font-size: 13px;
            line-height: 1.45;
        }

        .camera-error {
            display: none;
            margin-top: 10px;
            padding: 10px 12px;
            border-radius: 12px;
            background: #fff4f4;
            color: #a33a3a;
            font-size: 13px;
            line-height: 1.4;
        }

        .camera-error.show {
            display: block;
        }

        .camera-actions {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 9px;
            margin-top: 14px;
        }

        .camera-action {
            min-height: 46px;
            border: 0;
            border-radius: 13px;
            padding: 10px 14px;
            font: inherit;
            font-weight: 800;
            cursor: pointer;
            background: #e8f8f2;
            color: #087f5b;
        }

        .camera-action.primary {
            background: #087f5b;
            color: white;
        }

        .camera-action:disabled {
            opacity: .45;
            cursor: not-allowed;
        }

        .camera-result {
            display: none;
            margin-top: 12px;
            padding: 11px 13px;
            border-radius: 13px;
            background: #f3faf7;
            color: #45615a;
            font-size: 13px;
            line-height: 1.45;
        }

        .camera-result.show {
            display: block;
        }

        @media (max-width: 560px) {
            .camera-modal {
                padding: 10px;
            }

            .camera-card {
                border-radius: 20px;
                padding: 14px;
            }

            .camera-actions {
                grid-template-columns: 1fr 1fr;
            }
        }

        /* ============================================================
           BOTTOM APP NAVIGATION + SECTION MODES
           Keeps the existing MediCompare visual design intact.
        ============================================================ */
        body {
            padding-bottom: 92px;
        }

        .app-bottom-nav {
            position: fixed;
            left: 50%;
            bottom: 10px;
            transform: translateX(-50%);
            width: min(680px, calc(100% - 20px));
            z-index: 1000;
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 6px;
            padding: 7px;
            background: rgba(255,255,255,0.97);
            border: 1px solid rgba(8,127,91,0.10);
            border-radius: 22px;
            box-shadow: 0 10px 30px rgba(20,70,55,0.18);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
        }

        .app-nav-button {
            border: 0;
            background: transparent;
            color: #5f706b;
            min-height: 66px;
            border-radius: 17px;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 3px;
            cursor: pointer;
            font: inherit;
            font-weight: 700;
            transition: background .25s ease, color .25s ease, transform .18s ease;
            -webkit-tap-highlight-color: transparent;
        }

        .app-nav-button:active {
            transform: scale(.97);
        }

        .app-nav-button .nav-icon {
            font-size: 27px;
            line-height: 1;
        }

        .app-nav-button .nav-label {
            font-size: 14px;
            line-height: 1.1;
        }

        .app-nav-button.active {
            background: #e8f8f2;
            color: #087f5b;
        }

        .app-nav-button.active::after {
            content: "";
            width: 62%;
            height: 4px;
            border-radius: 999px;
            background: #087f5b;
            margin-top: 1px;
        }

        .diet-panel {
            display: none;
            animation: navPanelIn .35s ease both;
        }

        body.diet-mode .diet-panel {
            display: block;
        }

        body.diet-mode .main-content-for-nav {
            display: none;
        }

        body.step-mode .main-content-for-nav > .search-form,
        body.step-mode .main-content-for-nav > .search-status,
        body.step-mode .main-content-for-nav > .search-scan,
        body.step-mode .main-content-for-nav > .search-tip,
        body.step-mode .main-content-for-nav > .warning,
        body.step-mode .main-content-for-nav > .error,
        body.step-mode .main-content-for-nav > .search-title,
        body.step-mode .main-content-for-nav > .pharmacy-section,
        body.step-mode .main-content-for-nav > .empty,
        body.step-mode .main-content-for-nav > .footer-note {
            display: none !important;
        }

        body.step-mode .main-content-for-nav .pre-search-animations {
            display: block !important;
            opacity: 1 !important;
            visibility: visible !important;
            animation: navPanelIn .35s ease both;
        }

        body.step-mode .main-content-for-nav .step-counter,
        body.step-mode .main-content-for-nav .health-tips,
        body.step-mode .main-content-for-nav .price-compare-animation {
            display: flex;
        }

        body.step-mode .main-content-for-nav .step-counter {
            animation: navPanelIn .35s ease both;
        }

        body.step-mode .main-content-for-nav .health-tips,
        body.step-mode .main-content-for-nav .price-compare-animation {
            animation: navPanelIn .42s ease both;
        }

        body.buy-mode .main-content-for-nav {
            display: block;
            animation: navPanelIn .30s ease both;
        }

        @keyframes navPanelIn {
            from { opacity: 0; transform: translateY(8px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .diet-card {
            background: linear-gradient(135deg, #f5fffb, #eef9f5);
            border: 1px solid #d7eee5;
            border-radius: 24px;
            padding: 24px 20px;
            box-shadow: 0 8px 24px rgba(20,70,55,0.07);
            margin-top: 18px;
        }

        .diet-heading {
            text-align: center;
            color: #087f5b;
            font-size: clamp(24px, 5vw, 34px);
            margin: 4px 0 8px;
        }

        .diet-subheading {
            text-align: center;
            color: #687873;
            margin: 0 0 22px;
            font-size: 15px;
        }

        .diet-form-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 14px;
        }

        .diet-field label {
            display: block;
            font-weight: 700;
            color: #31564b;
            margin: 0 0 7px;
        }

        .diet-field input,
        .diet-field select {
            width: 100%;
            border: 1px solid #cfe5dc;
            border-radius: 14px;
            padding: 14px 13px;
            font-size: 16px;
            background: white;
            color: #20312c;
            outline: none;
        }

        .diet-field input:focus,
        .diet-field select:focus {
            border-color: #0ca678;
            box-shadow: 0 0 0 3px rgba(12,166,120,.12);
        }

        .diet-full {
            grid-column: 1 / -1;
        }

        .diet-generate {
            width: 100%;
            margin-top: 18px;
            border: 0;
            border-radius: 15px;
            min-height: 52px;
            background: #087f5b;
            color: white;
            font-size: 17px;
            font-weight: 800;
            cursor: pointer;
        }

        .diet-result {
            display: none;
            margin-top: 20px;
            background: white;
            border: 1px solid #d9ece5;
            border-radius: 20px;
            padding: 20px;
            animation: navPanelIn .35s ease both;
        }

        .diet-result.show {
            display: block;
        }

        .diet-result h3 {
            color: #087f5b;
            text-align: center;
            margin: 0 0 8px;
        }

        .diet-profile {
            text-align: center;
            color: #687873;
            font-size: 14px;
            margin-bottom: 16px;
        }

        .diet-meal {
            padding: 13px 0;
            border-bottom: 1px solid #edf3f0;
        }

        .diet-meal:last-of-type {
            border-bottom: 0;
        }

        .diet-meal strong {
            display: block;
            color: #31564b;
            margin-bottom: 4px;
        }

        .diet-meal span {
            color: #5f706b;
            line-height: 1.5;
        }

        .diet-disclaimer {
            margin-top: 14px;
            padding: 12px;
            border-radius: 12px;
            background: #fff8d9;
            color: #75631b;
            font-size: 12px;
            line-height: 1.45;
        }

        .diet-download {
            width: 100%;
            margin-top: 14px;
            border: 0;
            border-radius: 14px;
            min-height: 48px;
            background: #e8f8f2;
            color: #087f5b;
            font-weight: 800;
            cursor: pointer;
        }

        @media (max-width: 560px) {
            body { padding-bottom: 88px; }
            .app-bottom-nav { bottom: 7px; border-radius: 20px; }
            .app-nav-button { min-height: 62px; }
            .app-nav-button .nav-icon { font-size: 24px; }
            .app-nav-button .nav-label { font-size: 13px; }
            .diet-form-grid { grid-template-columns: 1fr; }
            .diet-full { grid-column: auto; }
            .diet-card { padding: 20px 15px; }
        }


        /* ============================================================
           CAMERA OCR
        ============================================================ */
        .ocr-progress {
            display: none;
            margin-top: 12px;
            padding: 12px 14px;
            border-radius: 14px;
            background: #eef8f4;
            color: #146b58;
            font-size: 14px;
            line-height: 1.45;
        }
        .ocr-progress.show { display: block; }
        .ocr-progress-bar {
            height: 7px;
            margin-top: 8px;
            border-radius: 999px;
            background: #d7ebe4;
            overflow: hidden;
        }
        .ocr-progress-fill {
            width: 0%;
            height: 100%;
            border-radius: inherit;
            background: #0b8668;
            transition: width .2s ease;
        }
        .ocr-text {
            display: none;
            margin-top: 12px;
            padding: 12px 14px;
            border-radius: 14px;
            background: #f6f7f7;
            color: #4d5c58;
            font-size: 13px;
            line-height: 1.5;
            white-space: pre-wrap;
            word-break: break-word;
            max-height: 150px;
            overflow: auto;
        }
        .ocr-text.show { display: block; }
        .ocr-result {
            display: none;
            margin-top: 10px;
            padding: 12px 14px;
            border-radius: 14px;
            background: #e9f7f1;
            color: #0f6653;
            font-weight: 700;
            line-height: 1.45;
        }
        .ocr-result.show { display: block; }
        .ocr-match-list {
            display: none;
            margin-top: 12px;
            gap: 8px;
        }
        .ocr-match-list.show { display: grid; }
        .ocr-match-card {
            padding: 11px 12px;
            border-radius: 14px;
            background: #ffffff;
            border: 1px solid #cfe9df;
        }
        .ocr-match-name { font-weight: 900; color: #0b735a; }
        .ocr-match-meta { margin-top: 3px; font-size: 12px; color: #687773; }
        .ocr-match-button {
            margin-top: 8px;
            border: 0;
            border-radius: 10px;
            padding: 8px 11px;
            background: #0b8668;
            color: #fff;
            font-weight: 800;
            cursor: pointer;
        }

    </style>
    <script src="https://cdn.jsdelivr.net/npm/tesseract.js@5/dist/tesseract.min.js"></script>
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

<div class="main-content-for-nav buy-mode" id="mainContentForNav">

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

        <button
            type="button"
            class="camera-search-button"
            id="cameraOpenButton"
            aria-label="Open camera to scan medicine"
            title="Scan medicine with camera"
        >📷</button>

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


</div>

<!-- ============================================================
     DIET SECTION
============================================================ -->
<div class="diet-panel" id="dietPanel">
    <div class="diet-card">
        <div style="text-align:center;font-size:42px;line-height:1;">🥗</div>
        <h2 class="diet-heading">Healthy Diet Guide</h2>
        <p class="diet-subheading">Enter a few details to generate a simple general healthy eating guide.</p>

        <div class="diet-form-grid">
            <div class="diet-field">
                <label for="dietAge">Age</label>
                <input id="dietAge" type="number" min="1" max="120" placeholder="Enter age">
            </div>

            <div class="diet-field">
                <label for="dietWeight">Body weight (kg)</label>
                <input id="dietWeight" type="number" min="1" max="500" step="0.1" placeholder="Enter weight">
            </div>

            <div class="diet-field diet-full">
                <label for="dietActivity">Activity level</label>
                <select id="dietActivity">
                    <option value="low">🪑 Low — mostly sitting</option>
                    <option value="moderate" selected>🚶 Moderate — regular walking</option>
                    <option value="high">🏃 High — active/exercise</option>
                </select>
            </div>
        </div>

        <button type="button" class="diet-generate" id="dietGenerate">Generate My Diet Chart</button>

        <div class="diet-result" id="dietResult">
            <h3>🥗 Your Daily Wellness Plan</h3>
            <div class="diet-profile" id="dietProfile"></div>

            <div class="diet-meal"><strong>🌅 Breakfast</strong><span id="dietBreakfast"></span></div>
            <div class="diet-meal"><strong>🍎 Mid-Morning</strong><span>One fruit or a small handful of unsalted nuts.</span></div>
            <div class="diet-meal"><strong>🍛 Lunch</strong><span id="dietLunch"></span></div>
            <div class="diet-meal"><strong>☕ Evening</strong><span>Fruit, sprouts, curd or another light snack.</span></div>
            <div class="diet-meal"><strong>🌙 Dinner</strong><span id="dietDinner"></span></div>
            <div class="diet-meal"><strong>💧 Hydration</strong><span>Drink water regularly through the day and with meals according to your thirst and routine.</span></div>

            <div class="diet-disclaimer">⚠️ This is a general healthy-diet guide, not a medical or therapeutic diet prescription. Individual nutritional needs can vary. If you have a medical condition, are pregnant, or need a therapeutic diet, consult a qualified healthcare professional or registered dietitian.</div>
            <button type="button" class="diet-download" id="dietDownload">⬇️ Download Your Diet Chart</button>
        </div>
    </div>
</div>

</main>



<!-- ============================================================
     DIET PDF PRINT AREA
============================================================ -->
<div id="dietPdfPrintArea" class="diet-print-only"></div>

<!-- ============================================================
     CAMERA SCANNER
============================================================ -->
<div class="camera-modal" id="cameraModal" aria-hidden="true">
    <div class="camera-card" role="dialog" aria-modal="true" aria-labelledby="cameraTitle">

        <div class="camera-card-header">
            <div class="camera-card-title" id="cameraTitle">📷 Scan Medicine</div>
            <button type="button" class="camera-close" id="cameraCloseButton" aria-label="Close camera">×</button>
        </div>

        <div class="camera-stage">
            <video
                class="camera-video"
                id="cameraVideo"
                autoplay
                playsinline
                muted
            ></video>

            <img
                class="camera-photo"
                id="cameraPhoto"
                alt="Captured medicine photo"
            >

            <div class="camera-guide" aria-hidden="true"></div>
        </div>

        <div class="camera-hint" id="cameraHint">
            Place the medicine strip or box inside the guide and keep it steady.
        </div>

        <div class="camera-error" id="cameraError" role="alert"></div>

        <div class="camera-actions">
            <button type="button" class="camera-action primary" id="cameraCaptureButton">
                📸 Capture
            </button>
            <button type="button" class="camera-action" id="cameraRetakeButton" disabled>
                🔄 Retake
            </button>
            <button type="button" class="camera-action primary" id="cameraUsePhotoButton" disabled>
                ✅ Use Photo
            </button>
            <button type="button" class="camera-action" id="cameraCloseButton2">
                Close
            </button>
        </div>

        <div class="camera-result" id="cameraResult">
            Photo captured. Tap <strong>Use Photo</strong> to read the medicine text.
        </div>

        <div class="ocr-progress" id="ocrProgress" aria-live="polite">
            <div id="ocrProgressText">Reading medicine text…</div>
            <div class="ocr-progress-bar"><div class="ocr-progress-fill" id="ocrProgressFill"></div></div>
        </div>

        <div class="ocr-result" id="ocrResult"></div>
        <div class="ocr-match-list" id="ocrMatchList"></div>
        <div class="ocr-text" id="ocrText"></div>

        <canvas id="cameraCanvas" style="display:none;"></canvas>
    </div>
</div>


<!-- ============================================================
     BOTTOM NAVIGATION
============================================================ -->
<nav class="app-bottom-nav" aria-label="MediCompare sections">
    <button type="button" class="app-nav-button active" id="navBuyMedi" data-section="buy">
        <span class="nav-icon">💊</span>
        <span class="nav-label">BuyMedi</span>
    </button>
    <button type="button" class="app-nav-button" id="navStepCounter" data-section="step">
        <span class="nav-icon">👟</span>
        <span class="nav-label">Step Counter</span>
    </button>
    <button type="button" class="app-nav-button" id="navDiet" data-section="diet">
        <span class="nav-icon">🍎</span>
        <span class="nav-label">Diet</span>
    </button>
</nav>

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
    // DIET PDF DOWNLOAD
    // Downloads a real server-generated PDF. No browser print dialog.
    // ============================================================
    function downloadDietChartAsPDF() {
        const age = Number(dietAge.value);
        const weight = Number(dietWeight.value);
        const activity = dietActivity.value || "moderate";

        if (!Number.isFinite(age) || age < 1 || age > 120 ||
            !Number.isFinite(weight) || weight <= 0 || weight > 500) {
            alert("Please generate your diet chart first.");
            return;
        }

        const params = new URLSearchParams({
            age: String(age),
            weight: String(weight),
            activity: activity
        });

        window.location.href = "/download-diet-pdf?" + params.toString();
    }


    // ============================================================
    // CAMERA SCANNER
    // Camera + OCR: permission -> live preview -> capture -> OCR
    // -> detected medicine query. User confirms the OCR text before search.
    // ============================================================
    const cameraOpenButton = document.getElementById("cameraOpenButton");
    const cameraModal = document.getElementById("cameraModal");
    const cameraVideo = document.getElementById("cameraVideo");
    const cameraPhoto = document.getElementById("cameraPhoto");
    const cameraCanvas = document.getElementById("cameraCanvas");
    const cameraCaptureButton = document.getElementById("cameraCaptureButton");
    const cameraRetakeButton = document.getElementById("cameraRetakeButton");
    const cameraUsePhotoButton = document.getElementById("cameraUsePhotoButton");
    const cameraCloseButton = document.getElementById("cameraCloseButton");
    const cameraCloseButton2 = document.getElementById("cameraCloseButton2");
    const cameraError = document.getElementById("cameraError");
    const cameraResult = document.getElementById("cameraResult");
    const cameraHint = document.getElementById("cameraHint");

    let cameraStream = null;
    let capturedPhotoDataUrl = "";

    function showCameraError(message) {
        cameraError.textContent = message;
        cameraError.classList.add("show");
    }

    function clearCameraError() {
        cameraError.textContent = "";
        cameraError.classList.remove("show");
    }

    function stopCameraStream() {
        if (cameraStream) {
            cameraStream.getTracks().forEach(function (track) {
                track.stop();
            });
            cameraStream = null;
        }
        cameraVideo.srcObject = null;
    }

    function resetCameraView() {
        capturedPhotoDataUrl = "";
        cameraPhoto.removeAttribute("src");
        cameraPhoto.style.display = "none";
        cameraVideo.style.display = "block";
        cameraCaptureButton.disabled = false;
        cameraRetakeButton.disabled = true;
        cameraUsePhotoButton.disabled = true;
        cameraResult.classList.remove("show");
        if (typeof clearOcrOutput === "function") {
            clearOcrOutput();
        }
        cameraHint.textContent =
            "Place the medicine strip or box inside the guide and keep it steady.";
    }

    async function openCamera() {
        clearCameraError();
        resetCameraView();

        if (!window.isSecureContext) {
            showCameraError(
                "Camera access needs HTTPS. Open the HTTPS MediCompare website on your phone."
            );
            cameraModal.classList.add("show");
            cameraModal.setAttribute("aria-hidden", "false");
            return;
        }

        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            showCameraError(
                "Camera access is not supported by this browser. Try Chrome on Android."
            );
            cameraModal.classList.add("show");
            cameraModal.setAttribute("aria-hidden", "false");
            return;
        }

        cameraModal.classList.add("show");
        cameraModal.setAttribute("aria-hidden", "false");

        try {
            cameraStream = await navigator.mediaDevices.getUserMedia({
                video: {
                    facingMode: { ideal: "environment" },
                    width: { ideal: 1280 },
                    height: { ideal: 720 }
                },
                audio: false
            });

            cameraVideo.srcObject = cameraStream;
            await cameraVideo.play();
            cameraHint.textContent =
                "Camera ready. Keep the medicine name and strength clearly visible.";
        } catch (error) {
            console.error("Camera access error:", error);

            let message =
                "Could not access the camera. Please allow camera permission and try again.";

            if (error && error.name === "NotAllowedError") {
                message =
                    "Camera permission was denied. Allow camera access in your browser settings and try again.";
            } else if (error && error.name === "NotFoundError") {
                message =
                    "No camera was found on this device.";
            } else if (error && error.name === "NotReadableError") {
                message =
                    "The camera is busy in another app. Close that app and try again.";
            }

            showCameraError(message);
        }
    }

    function closeCamera() {
        stopCameraStream();
        cameraModal.classList.remove("show");
        cameraModal.setAttribute("aria-hidden", "true");
    }

    function capturePhoto() {
        if (!cameraStream || !cameraVideo.videoWidth || !cameraVideo.videoHeight) {
            showCameraError("Camera is not ready yet. Please wait a moment and try again.");
            return;
        }

        const maxWidth = 1280;
        const scale = Math.min(1, maxWidth / cameraVideo.videoWidth);
        cameraCanvas.width = Math.round(cameraVideo.videoWidth * scale);
        cameraCanvas.height = Math.round(cameraVideo.videoHeight * scale);

        const context = cameraCanvas.getContext("2d");
        context.drawImage(
            cameraVideo,
            0,
            0,
            cameraCanvas.width,
            cameraCanvas.height
        );

        capturedPhotoDataUrl = cameraCanvas.toDataURL("image/jpeg", 0.88);
        cameraPhoto.src = capturedPhotoDataUrl;
        cameraVideo.style.display = "none";
        cameraPhoto.style.display = "block";

        cameraCaptureButton.disabled = true;
        cameraRetakeButton.disabled = false;
        cameraUsePhotoButton.disabled = false;
        cameraResult.classList.add("show");
        cameraHint.textContent =
            "Check the photo. Retake it if the medicine name, strength or pack size is unclear.";

        // The live camera is no longer needed after capture.
        stopCameraStream();
    }

    cameraOpenButton.addEventListener("click", openCamera);
    cameraCaptureButton.addEventListener("click", capturePhoto);
    cameraRetakeButton.addEventListener("click", openCamera);

    const ocrProgress = document.getElementById("ocrProgress");
    const ocrProgressText = document.getElementById("ocrProgressText");
    const ocrProgressFill = document.getElementById("ocrProgressFill");
    const ocrResult = document.getElementById("ocrResult");
    const ocrText = document.getElementById("ocrText");

    function setOcrProgress(percent, message) {
        ocrProgress.classList.add("show");
        ocrProgressFill.style.width = Math.max(0, Math.min(100, percent)) + "%";
        ocrProgressText.textContent = message;
    }

    function clearOcrOutput() {
        ocrProgress.classList.remove("show");
        ocrProgressFill.style.width = "0%";
        ocrProgressText.textContent = "Reading medicine text…";
        ocrResult.textContent = "";
        ocrResult.classList.remove("show");
        ocrText.textContent = "";
        ocrText.classList.remove("show");
        const matchList = document.getElementById("ocrMatchList");
        if (matchList) { matchList.innerHTML = ""; matchList.classList.remove("show"); }
    }

    function cleanOcrLine(line) {
        return String(line || "")
            .replace(/[|{}\[\]<>~`^*_#=]+/g, " ")
            .replace(/\s+/g, " ")
            .trim();
    }

    function normalizeOCRMedicineText(text) {
        let clean = cleanOcrLine(text)
            .replace(/[|]/g, "I")
            .replace(/[“”‘’]/g, "")
            .replace(/\b(\d+)\s*[oO]\s*(mg|mcg|µg|ug|g|ml|iu)\b/gi, "$1 0 $2")
            .replace(/\s+/g, " ")
            .trim();

        // Repair common OCR confusions inside medicine-like words only.
        clean = clean.split(/\s+/).map(function (token) {
            const upper = token.toUpperCase();
            if (/[A-Z]/.test(upper) && /\d/.test(upper) && token.length >= 3) {
                return token
                    .replace(/0/g, "O")
                    .replace(/1/g, "I")
                    .replace(/5/g, "S")
                    .replace(/8/g, "B");
            }
            return token;
        }).join(" ");

        // Join OCR's frequent single-letter splitting: D O L O 650 -> DOLO 650.
        clean = clean.replace(/\b([A-Za-z])\s+([A-Za-z])\s+([A-Za-z])\s+([A-Za-z])\b/g, "$1$2$3$4");
        return clean.trim();
    }

    function scoreMedicineLine(line) {
        const clean = normalizeOCRMedicineText(line);
        if (!clean || clean.length < 3 || clean.length > 60) return -99;

        const letters = (clean.match(/[A-Za-z]/g) || []).length;
        const digits = (clean.match(/\d/g) || []).length;
        const words = clean.split(/\s+/).filter(Boolean);
        const strengthPattern = /\b\d+(?:\.\d+)?\s*(?:mg|mcg|µg|ug|g|ml|iu)\b/i;
        const formPattern = /\b(tablet|tablets|tab|capsule|capsules|syrup|drops|injection|cream|gel|ointment|suspension)\b/i;
        const noisePattern = /^(mrp|batch|lot|mfg|exp|expiry|manufactured|marketed|composition|warning|store|schedule|keep|dosage|for|use|each|price|ref|code|arr|cl|ew|ri|on)\b/i;

        let score = 0;
        if (strengthPattern.test(clean)) score += 14;
        if (formPattern.test(clean)) score += 3;
        if (letters >= 4) score += 2;
        if (digits >= 1) score += 2;
        if (words.length <= 5) score += 2;
        if (clean.length <= 28) score += 3;
        if (/^[A-Z][A-Z0-9+\-/. ]{2,35}$/i.test(clean)) score += 1;
        if (noisePattern.test(clean)) score -= 14;
        if (/[^A-Za-z0-9 .+\-/()%µ]/.test(clean)) score -= 3;
        return score;
    }

    function extractMedicineCandidates(text) {
        const rawLines = String(text || "")
            .split(/\r?\n/)
            .map(cleanOcrLine)
            .filter(function (line) { return line.length >= 3 && line.length <= 100; });

        const candidates = [];

        rawLines.forEach(function (line, index) {
            const normalized = normalizeOCRMedicineText(line);
            candidates.push({ line: normalized, score: scoreMedicineLine(normalized), index: index });

            // Also test compact adjacent token groups because OCR may split
            // the brand and strength across separate lines/words.
            const tokens = normalized.split(/\s+/).filter(Boolean);
            for (let size = 2; size <= Math.min(5, tokens.length); size++) {
                for (let j = 0; j + size <= tokens.length; j++) {
                    const group = tokens.slice(j, j + size).join(" ");
                    candidates.push({ line: group, score: scoreMedicineLine(group) + 1, index: index + j / 100 });
                }
            }
        });

        return candidates
            .filter(function (item) { return item.score >= 5; })
            .sort(function (a, b) { return b.score - a.score || a.index - b.index; });
    }

    function pickMedicineQuery(text) {
        const candidates = extractMedicineCandidates(text);
        return candidates.length ? candidates[0].line.slice(0, 80) : "";
    }

    function prepareOCRImages(dataUrl) {
        return new Promise(function (resolve) {
            const image = new Image();
            image.onload = function () {
                const sourceWidth = image.naturalWidth || 1200;
                const sourceHeight = image.naturalHeight || 800;
                const maxWidth = 2000;
                const scale = Math.min(1.9, maxWidth / Math.max(1, sourceWidth));
                const width = Math.max(1000, Math.round(sourceWidth * scale));
                const height = Math.max(700, Math.round(sourceHeight * scale));

                function render(mode, cropType) {
                    let sx = 0, sy = 0, sw = width, sh = height;
                    if (cropType === "center") {
                        sx = Math.round(width * 0.05); sy = Math.round(height * 0.15);
                        sw = Math.round(width * 0.90); sh = Math.round(height * 0.70);
                    } else if (cropType === "middle") {
                        sx = Math.round(width * 0.03); sy = Math.round(height * 0.28);
                        sw = Math.round(width * 0.94); sh = Math.round(height * 0.44);
                    } else if (cropType === "wide") {
                        sx = Math.round(width * 0.02); sy = Math.round(height * 0.08);
                        sw = Math.round(width * 0.96); sh = Math.round(height * 0.84);
                    }

                    const canvas = document.createElement("canvas");
                    canvas.width = sw; canvas.height = sh;
                    const ctx = canvas.getContext("2d", { willReadFrequently: true });
                    ctx.imageSmoothingEnabled = true;
                    ctx.imageSmoothingQuality = "high";
                    ctx.drawImage(image, sx, sy, sw, sh, 0, 0, sw, sh);

                    if (mode === "original") return canvas.toDataURL("image/jpeg", 0.97);

                    const imageData = ctx.getImageData(0, 0, sw, sh);
                    const data = imageData.data;
                    for (let i = 0; i < data.length; i += 4) {
                        const gray = 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
                        let value;
                        if (mode === "gray") {
                            value = Math.max(0, Math.min(255, ((gray - 128) * 2.05) + 128));
                        } else if (mode === "threshold") {
                            value = gray > 168 ? 255 : 0;
                        } else if (mode === "adaptive") {
                            // A lightweight local-ish threshold that handles shadows better than one global cutoff.
                            value = gray > 145 ? 255 : (gray < 105 ? 0 : 255);
                        } else {
                            value = 255 - gray;
                        }
                        data[i] = data[i + 1] = data[i + 2] = value;
                    }
                    ctx.putImageData(imageData, 0, 0);
                    return canvas.toDataURL("image/png");
                }

                resolve([
                    { name: "full-original", data: render("original", "wide"), psm: "6" },
                    { name: "center-original", data: render("original", "center"), psm: "7" },
                    { name: "center-gray", data: render("gray", "center"), psm: "7" },
                    { name: "center-threshold", data: render("threshold", "center"), psm: "7" },
                    { name: "middle-gray", data: render("gray", "middle"), psm: "7" },
                    { name: "middle-adaptive", data: render("adaptive", "middle"), psm: "11" },
                    { name: "center-inverted", data: render("invert", "center"), psm: "7" }
                ]);
            };
            image.onerror = function () {
                resolve([{ name: "original", data: dataUrl, psm: "6" }]);
            };
            image.src = dataUrl;
        });
    }

    async function runMedicineOCR() {
        if (!capturedPhotoDataUrl) return;

        clearOcrOutput();
        cameraUsePhotoButton.disabled = true;
        cameraRetakeButton.disabled = true;
        cameraHint.textContent = "Enhancing the photo and reading the medicine label. Keep this window open…";
        cameraResult.textContent = "OCR is analysing the captured photo…";
        cameraResult.classList.add("show");

        // Do not leave an older server-side search visually mixed with a new scan.
        document.querySelectorAll(".search-title, .pharmacy-section, .compare-prices-section").forEach(function (el) {
            el.dataset.ocrHidden = "1";
            el.style.display = "none";
        });

        if (!window.Tesseract) {
            setOcrProgress(0, "OCR engine could not be loaded. Check your internet connection and try again.");
            cameraUsePhotoButton.disabled = false;
            cameraRetakeButton.disabled = false;
            return;
        }

        try {
            const images = await prepareOCRImages(capturedPhotoDataUrl);
            const outputs = [];

            for (let i = 0; i < images.length; i++) {
                const pass = images[i];
                const baseProgress = Math.round((i / images.length) * 82);
                setOcrProgress(baseProgress, "OCR pass " + (i + 1) + " of " + images.length + "…");

                const result = await Tesseract.recognize(pass.data, "eng", {
                    tessedit_pageseg_mode: pass.psm,
                    preserve_interword_spaces: "1",
                    tessedit_char_whitelist: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-+/.()%µ ",
                    logger: function (info) {
                        if (typeof info.progress === "number") {
                            const local = Math.round(info.progress * 16);
                            const total = Math.min(96, baseProgress + local);
                            const label = info.status === "recognizing text" ? "Recognizing medicine text…" : "Preparing OCR…";
                            setOcrProgress(total, label + " (" + (i + 1) + "/" + images.length + ")");
                        }
                    }
                });

                const passText = result && result.data && result.data.text ? result.data.text.trim() : "";
                if (passText) {
                    outputs.push({
                        text: passText,
                        confidence: result && result.data && Number.isFinite(result.data.confidence) ? result.data.confidence : 0,
                        pass: pass.name
                    });
                }
            }

            outputs.sort(function (a, b) { return b.confidence - a.confidence; });
            const combinedText = outputs.map(function (item) { return item.text; }).join("\n");

            const candidateMap = {};
            outputs.forEach(function (item) {
                extractMedicineCandidates(item.text).slice(0, 12).forEach(function (candidate) {
                    const normalized = normalizeOCRMedicineText(candidate.line);
                    if (!normalized) return;
                    const key = normalized.toUpperCase();
                    if (!candidateMap[key]) candidateMap[key] = { text: normalized, score: 0, hits: 0, bestConfidence: 0 };
                    candidateMap[key].score += candidate.score + Math.min(10, item.confidence / 10);
                    candidateMap[key].hits += 1;
                    candidateMap[key].bestConfidence = Math.max(candidateMap[key].bestConfidence, item.confidence);
                });
            });

            const votedCandidates = Object.keys(candidateMap).map(function (key) { return candidateMap[key]; }).sort(function (a, b) {
                return b.hits - a.hits || b.score - a.score || b.bestConfidence - a.bestConfidence;
            });

            const top = votedCandidates[0];
            const localQuery = top ? top.text.slice(0, 80) : pickMedicineQuery(combinedText);

            ocrText.textContent = combinedText || "No readable text was found.";
            ocrText.classList.add("show");

            // First try the saved shared catalogue. This can correct OCR errors such as
            // D0LO / 6S0 -> DOLO 650 without spending a new pharmacy API call.
            let catalogueMatches = [];
            try {
                const response = await fetch("/api/ocr-match", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ text: combinedText })
                });
                if (response.ok) {
                    const data = await response.json();
                    catalogueMatches = Array.isArray(data.matches) ? data.matches : [];
                }
            } catch (matchError) {
                console.warn("OCR catalogue matching unavailable:", matchError);
            }

            const matchList = document.getElementById("ocrMatchList");
            matchList.innerHTML = "";
            matchList.classList.remove("show");

            if (catalogueMatches.length) {
                catalogueMatches.slice(0, 3).forEach(function (match, index) {
                    const card = document.createElement("div");
                    card.className = "ocr-match-card";
                    const name = document.createElement("div");
                    name.className = "ocr-match-name";
                    name.textContent = match.name;
                    const meta = document.createElement("div");
                    meta.className = "ocr-match-meta";
                    meta.textContent = [match.composition, match.pack_size, match.manufacturer].filter(Boolean).join(" • ");
                    const button = document.createElement("button");
                    button.type = "button";
                    button.className = "ocr-match-button";
                    button.textContent = index === 0 ? "Use this medicine" : "Use this result";
                    button.addEventListener("click", function () {
                        medicineInput.value = match.name;
                        updateClearButton();
                        closeCamera();
                        medicineInput.focus();
                    });
                    card.appendChild(name);
                    if (meta.textContent) card.appendChild(meta);
                    card.appendChild(button);
                    matchList.appendChild(card);
                });
                matchList.classList.add("show");
                ocrResult.textContent = "Possible medicine found from your saved pharmacy catalogue. Please verify the exact name, strength and pack size.";
                ocrResult.classList.add("show");
                cameraHint.textContent = "Choose the correct catalogue result. We will not search until you confirm it.";
                cameraResult.textContent = "OCR matched a catalogue medicine — choose the correct result.";
                setOcrProgress(100, "OCR + catalogue matching complete");
                return;
            }

            // If no catalogue match exists, show the OCR text only when it is useful.
            // Never push weak/gibberish OCR directly into the medicine search.
            const medicineQuery = localQuery;
            const strongEnough = !!top && top.score >= 9 && (top.hits >= 2 || top.bestConfidence >= 55);
            if (!medicineQuery || !strongEnough) {
                ocrResult.textContent = "Could not confidently identify a medicine from the available catalogue. Please retake a sharp, well-lit photo.";
                ocrResult.classList.add("show");
                cameraHint.textContent = "Tip: keep the medicine name large, straight-on and free from glare.";
                setOcrProgress(100, "OCR finished — no confident catalogue match");
                return;
            }

            medicineInput.value = medicineQuery;
            updateClearButton();
            ocrResult.textContent = "OCR text: " + medicineQuery + " — please correct/verify it before searching.";
            ocrResult.classList.add("show");
            cameraHint.textContent = "No saved catalogue match was found. Verify the OCR text before searching.";
            cameraResult.textContent = "OCR completed — please verify the result.";
            setOcrProgress(100, "OCR complete");

            setTimeout(function () {
                closeCamera();
                medicineInput.focus();
            }, 900);
        } catch (error) {
            console.error("OCR error:", error);
            setOcrProgress(0, "OCR failed. Please retake a clearer medicine photo and try again.");
            cameraHint.textContent = "Use a sharp, well-lit photo with the medicine name and strength facing the camera.";
        } finally {
            cameraUsePhotoButton.disabled = false;
            cameraRetakeButton.disabled = false;
        }
    }

    cameraUsePhotoButton.addEventListener("click", runMedicineOCR);

    cameraCloseButton.addEventListener("click", closeCamera);
    cameraCloseButton2.addEventListener("click", closeCamera);

    cameraModal.addEventListener("click", function (event) {
        if (event.target === cameraModal) {
            closeCamera();
        }
    });

    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && cameraModal.classList.contains("show")) {
            closeCamera();
        }
    });


    // ============================================================
    // STEP COUNTER
    // Walking detector v10.1.
    // Hybrid detector: conservative startup, responsive counting after
    // walking is confirmed.

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

    // Walking detector v10.1.
    // Goal: keep v9's walking responsiveness, but separate walking confirmation
    // from the per-step counter. Once walking is confirmed, do NOT require
    // every new step to match the previous 2 intervals perfectly.
    const GRAVITY_ALPHA = 0.88;
    const SIGNAL_ALPHA = 0.38;
    const NOISE_ALPHA = 0.02;

    // Candidate sensitivity.
    const MIN_DYNAMIC_PEAK = 0.18;
    const PEAK_MARGIN = 0.12;
    const CONFIRMED_MIN_DYNAMIC_PEAK = 0.10;
    const CONFIRMED_PEAK_MARGIN = 0.07;

    // Normal human walking cadence range. These are broad intentionally.
    const STEP_MIN_INTERVAL_MS = 280;
    const STEP_MAX_INTERVAL_MS = 1400;

    // Four candidates protect startup from a few random phone movements.
    // Only the newest confirmed candidate is counted, so startup adds just
    // one-step latency instead of backfilling all the earlier movement.
    const WALK_CONFIRM_CANDIDATES = 4;
    const WALK_START_INTERVAL_TOLERANCE = 0.42;
    const WALK_START_AMPLITUDE_TOLERANCE = 0.80;
    const WALK_START_TIMEOUT_MS = 1800;

    // Once walking is established, use a rolling cadence baseline instead of
    // demanding that every pair of steps be almost identical.
    const WALK_BREAK_TIMEOUT_MS = 1500;
    const CADENCE_HISTORY = 6;
    const CADENCE_MIN_RATIO = 0.45;
    const CADENCE_MAX_RATIO = 1.75;
    const DUPLICATE_INTERVAL_FACTOR = 0.42;

    // Peak-shape gate. A step candidate should be a real local pulse, not just
    // a tiny slow drift of the phone.
    const MIN_PEAK_SHARPNESS = 0.045;
    const PEAK_SHARPNESS_RATIO = 0.16;

    // Very large isolated spikes are not allowed to immediately establish
    // walking unless they participate in the repeated cadence pattern.
    const MAX_START_AMPLITUDE_RATIO = 4.5;

    let candidateTimes = [];
    let candidateAmplitudes = [];
    let confirmedWalking = false;
    let lastCandidateTime = 0;
    let lastConfirmedTime = 0;
    let walkingIntervals = [];
    let walkingAmplitudes = [];

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
        walkingIntervals = [];
        walkingAmplitudes = [];
        signalPeakReference = 0;
    }

    function average(values) {
        if (!values.length) return 0;
        return values.reduce((a, b) => a + b, 0) / values.length;
    }

    function median(values) {
        if (!values.length) return 0;
        const sorted = values.slice().sort((a, b) => a - b);
        const middle = Math.floor(sorted.length / 2);
        return sorted.length % 2
            ? sorted[middle]
            : (sorted[middle - 1] + sorted[middle]) / 2;
    }

    function cadenceIsRegular(times) {
        if (times.length < 3) return false;

        const intervals = [];
        for (let i = 1; i < times.length; i++) {
            const interval = times[i] - times[i - 1];
            if (interval >= STEP_MIN_INTERVAL_MS && interval <= STEP_MAX_INTERVAL_MS) {
                intervals.push(interval);
            }
        }

        if (intervals.length < 2) return false;

        const base = median(intervals.slice(-3));
        if (!base) return false;

        return intervals.slice(-2).every(function (interval) {
            return Math.abs(interval - base) / base <= WALK_START_INTERVAL_TOLERANCE;
        });
    }

    function amplitudesAreWalkingLike(amplitudes) {
        if (amplitudes.length < 3) return false;

        const recent = amplitudes.slice(-4);
        const base = median(recent);
        if (base <= 0) return false;

        const min = Math.min(...recent);
        const max = Math.max(...recent);
        return (max - min) / base <= WALK_START_AMPLITUDE_TOLERANCE;
    }

    function pushLimited(list, value, maxLength) {
        list.push(value);
        if (list.length > maxLength) list.shift();
    }

    function confirmWalking(now) {
        confirmedWalking = true;
        walkingIntervals = [];
        walkingAmplitudes = candidateAmplitudes.slice(-4);

        for (let i = Math.max(1, candidateTimes.length - 3);
             i < candidateTimes.length; i++) {
            pushLimited(
                walkingIntervals,
                candidateTimes[i] - candidateTimes[i - 1],
                CADENCE_HISTORY
            );
        }

        // Count the current walking peak immediately. Earlier peaks were only
        // evidence for confirmation. This avoids the large v8/v10-style delay.
        addConfirmedSteps(1);
        lastConfirmedTime = now;
    }

    function registerCandidate(now, amplitude) {
        if (lastCandidateTime) {
            const interval = now - lastCandidateTime;

            if (interval < STEP_MIN_INTERVAL_MS) return;

            if (interval > STEP_MAX_INTERVAL_MS) {
                resetWalkingDetector();
            }
        }

        // While sitting/holding the phone, a few random movements should not
        // start a walking session. Keep startup conservative and require a
        // repeated cadence + reasonably consistent pulse size.
        if (!confirmedWalking && candidateTimes.length) {
            const previousAmplitude = candidateAmplitudes[candidateAmplitudes.length - 1];
            const interval = now - lastCandidateTime;

            if (interval < STEP_MIN_INTERVAL_MS || interval > STEP_MAX_INTERVAL_MS) {
                candidateTimes = [];
                candidateAmplitudes = [];
                lastCandidateTime = 0;
            } else if (candidateAmplitudes.length >= 2 && previousAmplitude > 0) {
                const amplitudeRatio = amplitude / previousAmplitude;
                if (amplitudeRatio > MAX_START_AMPLITUDE_RATIO ||
                    amplitudeRatio < (1 / MAX_START_AMPLITUDE_RATIO)) {
                    return;
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

        if (!confirmedWalking &&
            candidateTimes.length >= WALK_CONFIRM_CANDIDATES &&
            cadenceIsRegular(candidateTimes) &&
            amplitudesAreWalkingLike(candidateAmplitudes)) {
            confirmWalking(now);
            return;
        }

        if (!confirmedWalking) return;

        // From here on, the detector is intentionally permissive. Real walking
        // is not perfectly metronomic, so use a rolling median cadence instead
        // of v9's strict two-interval comparison.
        if (candidateTimes.length >= 2) {
            const interval = now - candidateTimes[candidateTimes.length - 2];

            if (interval < STEP_MIN_INTERVAL_MS || interval > STEP_MAX_INTERVAL_MS) {
                return;
            }

            const cadenceBase = median(walkingIntervals.slice(-CADENCE_HISTORY));

            if (cadenceBase > 0) {
                if (interval < cadenceBase * DUPLICATE_INTERVAL_FACTOR) return;

                const ratio = interval / cadenceBase;
                if (ratio < CADENCE_MIN_RATIO || ratio > CADENCE_MAX_RATIO) {
                    return;
                }
            }

            pushLimited(walkingIntervals, interval, CADENCE_HISTORY);
            pushLimited(walkingAmplitudes, amplitude, CADENCE_HISTORY);

            // Only infer one missed step when the gap is clearly close to 2x
            // the established cadence. Never infer from a vague long gap.
            if (cadenceBase > 0) {
                const ratio = interval / cadenceBase;
                if (ratio >= 1.75 && ratio <= 2.25) {
                    addConfirmedSteps(2);
                } else {
                    addConfirmedSteps(1);
                }
            } else {
                addConfirmedSteps(1);
            }

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

        // Track quiet/background movement slowly so normal walking peaks do
        // not immediately raise their own threshold.
        const quietSignal = Math.min(filteredSignal, noiseFloor + 0.30);
        noiseFloor =
            NOISE_ALPHA * quietSignal +
            (1 - NOISE_ALPHA) * noiseFloor;

        const isPeak =
            previousSignal > previousPreviousSignal &&
            previousSignal >= filteredSignal;

        if (isPeak) {
            const amplitude = previousSignal;
            const dynamicHeight = amplitude - noiseFloor;
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

            // Local peak sharpness rejects slow hand/phone drifts while keeping
            // ordinary walking pulses responsive.
            const localLow = Math.min(previousPreviousSignal, filteredSignal);
            const peakSharpness = previousSignal - localLow;
            const requiredSharpness = Math.max(
                MIN_PEAK_SHARPNESS,
                dynamicHeight * PEAK_SHARPNESS_RATIO
            );

            if (dynamicHeight >= minimumPeak &&
                amplitude >= adaptiveThreshold &&
                peakSharpness >= requiredSharpness) {
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

    // ============================================================
    // BOTTOM NAVIGATION
    // ============================================================
    const navBuyMedi = document.getElementById("navBuyMedi");
    const navStepCounter = document.getElementById("navStepCounter");
    const navDiet = document.getElementById("navDiet");
    const dietPanel = document.getElementById("dietPanel");
    const mainContentForNav = document.getElementById("mainContentForNav");

    function setActiveNav(button) {
        [navBuyMedi, navStepCounter, navDiet].forEach(function (item) {
            item.classList.toggle("active", item === button);
        });
    }

    function scrollToTopOfContent() {
        window.scrollTo({ top: 0, behavior: "smooth" });
    }

    navBuyMedi.addEventListener("click", function () {
        document.body.classList.remove("step-mode", "diet-mode");
        document.body.classList.add("buy-mode");
        mainContentForNav.classList.remove("buy-mode");
        void mainContentForNav.offsetWidth;
        mainContentForNav.classList.add("buy-mode");
        setActiveNav(navBuyMedi);
        scrollToTopOfContent();
    });

    navStepCounter.addEventListener("click", function () {
        document.body.classList.remove("buy-mode", "diet-mode");
        document.body.classList.add("step-mode");
        setActiveNav(navStepCounter);
        scrollToTopOfContent();
    });

    navDiet.addEventListener("click", function () {
        document.body.classList.remove("buy-mode", "step-mode");
        document.body.classList.add("diet-mode");
        setActiveNav(navDiet);
        scrollToTopOfContent();
    });

    // ============================================================
    // DIET GUIDE
    // General wellness guidance only; no therapeutic prescription.
    // ============================================================
    const dietAge = document.getElementById("dietAge");
    const dietWeight = document.getElementById("dietWeight");
    const dietActivity = document.getElementById("dietActivity");
    const dietGenerate = document.getElementById("dietGenerate");
    const dietResult = document.getElementById("dietResult");
    const dietProfile = document.getElementById("dietProfile");
    const dietBreakfast = document.getElementById("dietBreakfast");
    const dietLunch = document.getElementById("dietLunch");
    const dietDinner = document.getElementById("dietDinner");
    const dietDownload = document.getElementById("dietDownload");

    let lastDietText = "";

    const dietGuides = {
        low: {
            label: "Low activity",
            breakfast: "Oats or idli/upma with fruit; choose a balanced portion.",
            lunch: "Rice or roti with dal, plenty of vegetables and curd.",
            dinner: "Roti with vegetables and dal, or a light balanced meal."
        },
        moderate: {
            label: "Moderate activity",
            breakfast: "Idli, dosa, oats or upma with fruit and a protein-rich side.",
            lunch: "Rice or roti with dal/protein, vegetables and curd.",
            dinner: "Roti or rice with vegetables and dal/protein; keep the meal balanced."
        },
        high: {
            label: "High activity",
            breakfast: "Oats/idli/upma with fruit plus a protein-rich food such as curd, eggs or sprouts.",
            lunch: "Rice or roti with a good protein source, vegetables and curd.",
            dinner: "A balanced meal with roti/rice, vegetables and a protein source."
        }
    };

    dietGenerate.addEventListener("click", function () {
        const age = Number(dietAge.value);
        const weight = Number(dietWeight.value);
        const activity = dietActivity.value;

        if (!Number.isFinite(age) || age < 1 || age > 120) {
            dietAge.focus();
            dietAge.setCustomValidity("Please enter an age from 1 to 120.");
            dietAge.reportValidity();
            dietAge.setCustomValidity("");
            return;
        }

        if (!Number.isFinite(weight) || weight <= 0 || weight > 500) {
            dietWeight.focus();
            dietWeight.setCustomValidity("Please enter a valid body weight in kg.");
            dietWeight.reportValidity();
            dietWeight.setCustomValidity("");
            return;
        }

        const guide = dietGuides[activity] || dietGuides.moderate;
        dietProfile.textContent = "Age: " + age + " • Weight: " + weight + " kg • Activity: " + guide.label;
        dietBreakfast.textContent = guide.breakfast;
        dietLunch.textContent = guide.lunch;
        dietDinner.textContent = guide.dinner;
        dietResult.classList.add("show");

        lastDietText =
            "MediCompare — General Healthy Diet Guide\n\n" +
            "Age: " + age + "\n" +
            "Body weight: " + weight + " kg\n" +
            "Activity: " + guide.label + "\n\n" +
            "Breakfast: " + guide.breakfast + "\n\n" +
            "Mid-Morning: One fruit or a small handful of unsalted nuts.\n\n" +
            "Lunch: " + guide.lunch + "\n\n" +
            "Evening: Fruit, sprouts, curd or another light snack.\n\n" +
            "Dinner: " + guide.dinner + "\n\n" +
            "Hydration: Drink water regularly through the day and with meals according to your thirst and routine.\n\n" +
            "Disclaimer: General healthy-diet guide only; not a medical or therapeutic diet prescription.";
    });

    // Download button gets a real server-generated PDF file.
    dietDownload.addEventListener("click", downloadDietChartAsPDF);

</script>

</body>
</html>
"""
# ============================================================
# DIET PDF DOWNLOAD
# Generates an actual PDF on the server so Android/iOS/desktop
# browsers all download the same .pdf file without window.print().
# ============================================================
@app.get("/download-diet-pdf")
def download_diet_pdf():
    if A4 is None:
        return "PDF support is not installed on the server. Please install reportlab.", 500

    try:
        age = float(request.args.get("age", ""))
        weight = float(request.args.get("weight", ""))
    except (TypeError, ValueError):
        return "Please generate your diet chart first.", 400

    if not (1 <= age <= 120) or not (0 < weight <= 500):
        return "Invalid age or weight.", 400

    activity = request.args.get("activity", "moderate")
    guides = {
        "low": {
            "label": "Low activity",
            "breakfast": "Oats or idli/upma with fruit; choose a balanced portion.",
            "lunch": "Rice or roti with dal, plenty of vegetables and curd.",
            "dinner": "Roti with vegetables and dal, or a light balanced meal."
        },
        "moderate": {
            "label": "Moderate activity",
            "breakfast": "Idli, dosa, oats or upma with fruit and a protein-rich side.",
            "lunch": "Rice or roti with dal/protein, vegetables and curd.",
            "dinner": "Roti or rice with vegetables and dal/protein; keep the meal balanced."
        },
        "high": {
            "label": "High activity",
            "breakfast": "Oats/idli/upma with fruit plus a protein-rich food such as curd, eggs or sprouts.",
            "lunch": "Rice or roti with a good protein source, vegetables and curd.",
            "dinner": "A balanced meal with roti/rice, vegetables and a protein source."
        }
    }
    guide = guides.get(activity, guides["moderate"])

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=40, leftMargin=40,
        topMargin=42, bottomMargin=42, title="MediCompare Healthy Diet Guide"
    )
    styles = getSampleStyleSheet()
    title = ParagraphStyle("DietTitle", parent=styles["Title"], alignment=TA_CENTER, fontSize=20, spaceAfter=6)
    subtitle = ParagraphStyle("DietSubtitle", parent=styles["Normal"], alignment=TA_CENTER, fontSize=10, spaceAfter=18)
    heading = ParagraphStyle("DietHeading", parent=styles["Heading3"], fontSize=12, spaceAfter=5)
    body = ParagraphStyle("DietBody", parent=styles["BodyText"], fontSize=10, leading=15, spaceAfter=4)
    note = ParagraphStyle("DietNote", parent=styles["BodyText"], fontSize=8.5, leading=12, spaceBefore=12)

    story = [
        Paragraph("MediCompare", title),
        Paragraph("Healthy Diet Guide", heading),
        Paragraph("General healthy eating guide generated from your entered details.", subtitle),
        Paragraph("Your Profile", heading),
        Paragraph(f"Age: {age:g} &nbsp;&nbsp; | &nbsp;&nbsp; Weight: {weight:g} kg &nbsp;&nbsp; | &nbsp;&nbsp; Activity: {guide['label']}", body),
        Spacer(1, 10)
    ]

    rows = [
        [Paragraph("Meal", heading), Paragraph("Suggested guide", heading)],
        ["Breakfast", guide["breakfast"]],
        ["Mid-Morning", "One fruit or a small handful of unsalted nuts."],
        ["Lunch", guide["lunch"]],
        ["Evening", "Fruit, sprouts, curd or another light snack."],
        ["Dinner", guide["dinner"]],
        ["Hydration", "Drink water regularly through the day and with meals according to your thirst and routine."],
    ]
    table = Table(rows, colWidths=[105, 390], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#eeeeee")),
        ("GRID", (0,0), (-1,-1), 0.5, colors.HexColor("#cccccc")),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTNAME", (0,1), (0,-1), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,-1), 9.5),
        ("LEADING", (0,0), (-1,-1), 13),
        ("LEFTPADDING", (0,0), (-1,-1), 8),
        ("RIGHTPADDING", (0,0), (-1,-1), 8),
        ("TOPPADDING", (0,0), (-1,-1), 8),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
    ]))
    story.append(table)
    story.append(Paragraph(
        "<b>Important:</b> This is a general wellness guide, not a medical or therapeutic diet prescription. "
        "Individual nutritional needs can vary. For medical conditions or therapeutic diets, consult a qualified healthcare professional or registered dietitian.",
        note
    ))

    doc.build(story)
    buffer.seek(0)
    return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name="MediCompare_Diet_Chart.pdf")


# ============================================================
# HOME / SEARCH
# ============================================================


# ============================================================
# OCR -> SHARED CATALOGUE MATCHING
# ============================================================
@app.route("/api/ocr-match", methods=["POST"])
def ocr_match():
    """Match noisy OCR text against products already discovered in our shared catalogue.

    This endpoint does NOT call pharmacy APIs. It only uses saved catalogue data,
    so OCR retries do not consume Parse credits.
    """
    payload = request.get_json(silent=True) or {}
    raw_text = str(payload.get("text") or "").strip()
    if not raw_text:
        return {"matches": []}

    # Keep useful OCR lines and short phrases, while dropping obvious metadata noise.
    lines = []
    for raw_line in raw_text.splitlines():
        line = " ".join(str(raw_line).split())
        if 3 <= len(line) <= 80:
            lines.append(line)

    if not lines:
        lines = [raw_text[:80]]

    with db_lock:
        conn = get_db_connection()
        try:
            rows = conn.execute(
                """
                SELECT name, manufacturer, pack_size, composition,
                       price, mrp, pharmacy, product_url
                FROM products
                WHERE name IS NOT NULL AND trim(name) <> ''
                ORDER BY last_seen_at DESC
                LIMIT 3000
                """
            ).fetchall()
        finally:
            conn.close()

    def compact(value):
        return "".join(ch for ch in normalize_query(value) if ch.isalnum())

    def similarity(a, b):
        a = compact(a)
        b = compact(b)
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        if a in b or b in a:
            return min(0.98, 0.82 + 0.16 * min(len(a), len(b)) / max(len(a), len(b)))
        return difflib.SequenceMatcher(None, a, b).ratio()

    results = []
    seen = set()
    for row in rows:
        name = str(row["name"] or "").strip()
        name_norm = normalize_query(name)
        if not name_norm:
            continue

        best = 0.0
        best_line = ""
        for line in lines:
            # Compare the full line and its individual words/short groups.
            candidates = [line]
            tokens = normalize_query(line).split()
            candidates.extend(tokens)
            for size in (2, 3):
                for j in range(max(0, len(tokens) - size + 1)):
                    candidates.append(" ".join(tokens[j:j + size]))

            for candidate in candidates:
                score = similarity(candidate, name)
                if score > best:
                    best = score
                    best_line = candidate

            # Composition can rescue generic brand OCR when the OCR captured the
            # active ingredient instead of the brand name.
            composition = str(row["composition"] or "")
            if composition:
                comp_score = similarity(line, composition)
                best = max(best, comp_score * 0.96)

        if best < 0.68:
            continue

        key = name_norm
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "name": name,
            "manufacturer": row["manufacturer"],
            "pack_size": row["pack_size"],
            "composition": row["composition"],
            "price": row["price"],
            "mrp": row["mrp"],
            "pharmacy": row["pharmacy"],
            "product_url": row["product_url"],
            "score": round(best, 3),
            "ocr_fragment": best_line,
        })

    results.sort(key=lambda item: item["score"], reverse=True)
    return {"matches": results[:8]}


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
