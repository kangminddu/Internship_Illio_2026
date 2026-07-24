# config.py

import os
EXPORT_DIR = "output"
os.makedirs(EXPORT_DIR, exist_ok=True)

# 유튜브는 자신의 계정 필요없음 데이터베이스만 집어넣기
DB = dict(host="", port="", user="", password="",
          database="fandom_crm", charset="utf8mb4")

# ── L1 (requests, 가벼움 → 병렬 세게 OK) ──
L1_WORKERS = 5
L1_DELAY   = 0.2

# ── L2a (requests) ──
L2A_WORKERS = 5
L2A_DELAY   = 0.3

# ── L2b (requests, watch page) ──
L2B_WORKERS = 10
L2B_DELAY   = 0.3
L2_RECENT_MONTHS=6
# ── L3 (Playwright, 무거움 → 살살) ──
L3_VIDEOS_PER_CHANNEL = 10
L3_MAX_SCROLLS = 20
L3_COMMENT_LIMIT = 30
L3_WORKERS = 3
# ── 공통 ──
BATCH_LIMIT = None
STOP_ON_429 = 5

# -- EMAIL (치지직 보강 수집) --
CHZZK_USER_AGENT = "Mozilla/5.0"
CHZZK_TIMEOUT = 15
CHZZK_DELAY = 1.0