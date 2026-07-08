# code/config.py
DB = dict(host="127.0.0.1", port=3306, user="root", password="",
          database="fandom_crm", charset="utf8mb4")

# L1 튜닝 (429 나면 WORKERS↓ DELAY↑)
WORKERS     = 5
DELAY       = 0.2
BATCH_LIMIT = None
STOP_ON_429 = 5