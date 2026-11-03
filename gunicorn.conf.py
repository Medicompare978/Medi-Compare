# Gunicorn reads this file automatically when it sits next to app.py.
# Fixes "WORKER TIMEOUT" on slow pharmacy searches.
worker_class = "gthread"   # lets several requests run at once
workers = 1                # keep 1 process (free Render instance, shared in-memory state)
threads = 4
timeout = 120              # was 30 s by default
graceful_timeout = 30
keepalive = 5
