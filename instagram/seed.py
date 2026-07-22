# -*- coding: utf-8 -*-
"""
seed_instagram.py

SNS_정보.xlsx Sheet1 의 ChInstagram 컬럼 → creators + channels(platform='instagram') 적재.
crawler/seed.py 의 인스타 버전이지만, 결정적으로 다른 점이 하나 있다.

■ seed.py 를 그대로 쓰면 안 되는 이유
seed.py 는 seed_key 를 '행 번호'로 만든다:  seed_key = f"{prefix}_{i}"
그런데 Sheet1 의 '키값' 컬럼은 행 번호가 아니라 외부 마스터 ID 다.
  - Sheet1 은 23728행인데 키값 최대치는 G_236822
  - Sheet1 의 35번째 행의 실제 키값은 G_281
따라서 행 번호로 키를 만들면 G_2210(이미 존재하는 다른 크리에이터)에
엉뚱한 데이터가 붙어 기존 646명이 오염된다.
→ 이 스크립트는 '키값' 컬럼을 그대로 seed_key 로 쓴다.

■ 기존 creators 보호
이미 있는 seed_key 는 nickname/category/agency 를 건드리지 않는다.
(ON DUPLICATE KEY UPDATE 에서 creator_id 만 회수)
신규 생성 시 nickname 은 seed_key 를 임시값으로 넣는다.
  → 기존 관행과 동일. crawler_l1.py 가 유튜브에서
    "UPDATE creators SET nickname=... WHERE nickname LIKE 'G\\_%'" 로 덮어쓰는 패턴.
  → 인스타는 import_l1.py 가 L1 의 full_name 으로 덮어쓰면 된다.

■ username 추출은 reader.py 것을 그대로 재사용
seed 가 만드는 URL 과 L1 이 크롤한 URL 이 반드시 일치해야
import_l1 이 채널을 찾을 수 있다.

사용법:
    python -m instagram.seed_instagram              # 실제 적재
    python -m instagram.seed_instagram --dry-run    # 집계만
"""

import argparse
from collections import defaultdict

import pymysql
from openpyxl import load_workbook

try:
    from instagram.config import DB
    from instagram.reader import DEFAULT_EXCEL, extract_username
except Exception:
    from config import DB
    from reader import DEFAULT_EXCEL, extract_username


SHEET = "Sheet1"
KEY_COL = 0          # A: 키값
INSTA_COL = 2        # C: ChInstagram


# 기존 creator 는 절대 건드리지 않고 creator_id 만 회수한다.
SQL_UPSERT_CREATOR = """
INSERT INTO creators (seed_key, nickname)
VALUES (%s, %s)
ON DUPLICATE KEY UPDATE creator_id=LAST_INSERT_ID(creator_id)
"""

SQL_UPSERT_CHANNEL = """
INSERT INTO channels
  (creator_id, platform, channel_url_raw, channel_url_normalized,
   channel_id_status, channel_existence_status, channel_activity_status, is_primary)
VALUES (%s, 'instagram', %s, %s, 'handle_only', 'unknown', 'unknown', %s)
ON DUPLICATE KEY UPDATE
  channel_url_normalized=VALUES(channel_url_normalized),
  updated_at=CURRENT_TIMESTAMP
"""


def normalize_url(username):
    """L1/import_l1 과 동일한 규칙이어야 한다."""
    return f"https://www.instagram.com/{username}/"


def scan_sheet(excel_path):
    """
    Sheet1 스캔 → [(seed_key, username, raw_url), ...]
    (seed_key, username) 중복은 제거. 완전 동일 행이 12854개라 필수.
    """
    wb = load_workbook(excel_path, read_only=True, data_only=True)
    ws = wb[SHEET]

    pairs = []
    seen = set()
    stats = {"rows": 0, "no_key": 0, "no_insta": 0, "bad_username": 0, "dup": 0}

    for row in ws.iter_rows(min_row=2, values_only=True):
        stats["rows"] += 1
        if len(row) <= INSTA_COL:
            continue

        key = row[KEY_COL]
        insta = row[INSTA_COL]

        if not key or not str(key).strip():
            stats["no_key"] += 1
            continue
        seed_key = str(key).strip()

        if insta is None:
            stats["no_insta"] += 1
            continue
        raw = str(insta).strip()
        if not raw or raw.upper() == "NULL":
            stats["no_insta"] += 1
            continue

        username = extract_username(raw)      # reader.py 와 동일 로직
        if not username:
            stats["bad_username"] += 1
            continue

        sig = (seed_key, username.lower())
        if sig in seen:
            stats["dup"] += 1
            continue
        seen.add(sig)
        pairs.append((seed_key, username, raw))

    wb.close()
    return pairs, stats


def main(excel_path=None, dry_run=False):
    excel_path = excel_path or DEFAULT_EXCEL
    pairs, stats = scan_sheet(excel_path)

    usernames = {u.lower() for _, u, _ in pairs}
    keys = {k for k, _, _ in pairs}

    # 한 계정을 여러 creator 가 가리키는 경우 (채널은 1행만 생김 → 마지막이 이김)
    owners = defaultdict(set)
    for k, u, _ in pairs:
        owners[u.lower()].add(k)
    multi = {u: ks for u, ks in owners.items() if len(ks) > 1}

    print("=" * 62)
    print(f"엑셀: {excel_path}")
    print(f"전체 행                 : {stats['rows']}")
    print(f"  키값 없음             : {stats['no_key']}")
    print(f"  인스타 없음/NULL      : {stats['no_insta']}")
    print(f"  username 추출 실패    : {stats['bad_username']}")
    print(f"  (키값,계정) 중복 제거 : {stats['dup']}")
    print("-" * 62)
    print(f"적재 대상 (키값,계정) 쌍: {len(pairs)}")
    print(f"  유니크 seed_key       : {len(keys)}")
    print(f"  유니크 인스타 계정    : {len(usernames)}")
    if multi:
        print(f"  ⚠️ 한 계정을 여러 키값이 가리킴: {len(multi)}건 (채널 1행만 생성됨)")
        for u, ks in list(multi.items())[:5]:
            print(f"     @{u}: {sorted(ks)}")
    print("=" * 62)

    if dry_run:
        print("DRY-RUN → DB 미반영. 종료.")
        return

    conn = pymysql.connect(**DB, autocommit=False)
    created_creator = existing_creator = 0
    ch_upserted = 0
    primary_done = set()

    try:
        with conn.cursor() as cur:
            # 기존 creators 파악 (신규 생성 수를 정확히 세기 위해)
            cur.execute("SELECT seed_key FROM creators WHERE seed_key IS NOT NULL")
            before = {r[0] for r in cur.fetchall()}

            for i, (seed_key, username, raw) in enumerate(pairs, 1):
                # creator: 없으면 생성(임시 nickname=seed_key), 있으면 id 만 회수
                cur.execute(SQL_UPSERT_CREATOR, (seed_key, seed_key))
                creator_id = cur.lastrowid
                if not creator_id:
                    cur.execute("SELECT creator_id FROM creators WHERE seed_key=%s",
                                (seed_key,))
                    creator_id = cur.fetchone()[0]

                if seed_key in before:
                    existing_creator += 1
                else:
                    created_creator += 1
                    before.add(seed_key)

                is_primary = 0 if creator_id in primary_done else 1
                primary_done.add(creator_id)

                cur.execute(SQL_UPSERT_CHANNEL, (
                    creator_id, raw, normalize_url(username), is_primary,
                ))
                ch_upserted += 1

                if i % 500 == 0:
                    conn.commit()
                    print(f"  ... {i}/{len(pairs)}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"creator 신규 생성 : {created_creator}")
    print(f"creator 기존 사용 : {existing_creator}")
    print(f"channels upsert   : {ch_upserted}")
    print("=" * 62)
    print("다음: python -m instagram.steps.import_l1  (L1 크롤 결과를 채널에 반영)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=None, help="엑셀 경로 (기본: reader.DEFAULT_EXCEL)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    main(args.file, args.dry_run)