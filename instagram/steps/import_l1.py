# -*- coding: utf-8 -*-
"""
steps/import_l1.py  (v3 — 시트 기반 key 재매핑)

L1 결과(output/l1_results.jsonl) → fandom_crm.channels 적재.

■ 적재 규칙
  SUCCESS   → channel_id_status='resolved',  existence='normal'   ← L2 대상
  PRIVATE   → 'resolved',   'private'
  NOT_FOUND → 'not_found',  'deleted'
  그 외(ERROR/TIMEOUT/CHALLENGE)는 미확정이라 적재하지 않음.

channel_activity_status 는 항상 'unknown'. 활동성 판단은 metrics 몫.
"""

import json
from collections import Counter, defaultdict

import pymysql
from openpyxl import load_workbook

try:
    from instagram.config import DB, RESULTS_FILE
    from instagram.reader import DEFAULT_EXCEL, extract_username
except Exception:
    from config import DB, RESULTS_FILE
    from reader import DEFAULT_EXCEL, extract_username


# =========================================================
# 설정
# =========================================================
DRY_RUN = False          # True 면 DB 에 쓰지 않고 집계만 출력
COMMIT_EVERY = 200

STATUS_MAP = {
    "SUCCESS":   ("resolved",  "normal"),
    "PRIVATE":   ("resolved",  "private"),
    "NOT_FOUND": ("not_found", "deleted"),
}

STATUS_PRIORITY = {"SUCCESS": 3, "PRIVATE": 2, "NOT_FOUND": 1}

# IG account_type(숫자) → channels.account_type(varchar)
# TODO: 실제 데이터로 매핑 검증 필요. 모르는 값은 숫자 문자열 그대로 저장.
ACCOUNT_TYPE_MAP = {1: "일반", 2: "크리에이터", 3: "비즈니스"}


SQL_UPSERT_CHANNEL = """
INSERT INTO channels (
    creator_id, platform,
    channel_url_raw, channel_url_normalized,
    channel_id_status, channel_existence_status, channel_activity_status,
    external_channel_id, channel_name,
    account_type, bio, external_link
) VALUES (
    %s, 'instagram',
    %s, %s,
    %s, %s, 'unknown',
    %s, %s,
    %s, %s, %s
)
ON DUPLICATE KEY UPDATE
    creator_id=VALUES(creator_id),
    channel_url_raw=VALUES(channel_url_raw),
    channel_url_normalized=VALUES(channel_url_normalized),
    channel_id_status=VALUES(channel_id_status),
    channel_existence_status=VALUES(channel_existence_status),
    external_channel_id=VALUES(external_channel_id),
    channel_name=VALUES(channel_name),
    account_type=VALUES(account_type),
    bio=VALUES(bio),
    external_link=VALUES(external_link),
    updated_at=CURRENT_TIMESTAMP
"""

SQL_INSERT_CHANNEL_SNAPSHOT = """
INSERT INTO channel_snapshots (
    channel_id,
    captured_at,
    follower_count,
    following_count,
    total_video_count,
    total_like_count,
    total_view_count
)
VALUES (
    %s,
    NOW(),
    %s,
    %s,
    %s,
    NULL,
    NULL
)
ON DUPLICATE KEY UPDATE
    follower_count=VALUES(follower_count),
    following_count=VALUES(following_count),
    total_video_count=VALUES(total_video_count),
    total_like_count=VALUES(total_like_count),
    total_view_count=VALUES(total_view_count);
"""
# =========================================================
# 헬퍼
# =========================================================
def normalize_url(username):
    return f"https://www.instagram.com/{username}/"


def account_type_label(v):
    if v is None:
        return None
    try:
        return ACCOUNT_TYPE_MAP.get(int(v), str(v))
    except (TypeError, ValueError):
        return str(v)


def is_better(new, old):
    """확정 상태 우선, 동순위면 최신 ts."""
    if old is None:
        return True
    pn = STATUS_PRIORITY.get(new.get("status"), 0)
    po = STATUS_PRIORITY.get(old.get("status"), 0)
    if pn != po:
        return pn > po
    return (new.get("ts") or "") >= (old.get("ts") or "")


def load_jsonl():
    if not RESULTS_FILE.exists():
        raise FileNotFoundError(RESULTS_FILE)
    rows = []
    with RESULTS_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def build_username_key_map():
    """
    시트 전체(dedup 없이)를 읽어 username(소문자) → [key, ...] 매핑을 만든다.
    reader.py 가 버린 중복 행의 key 를 되살리기 위한 것.
    """
    wb = load_workbook(DEFAULT_EXCEL, data_only=True)
    ws = wb["Sheet1"]
    m = defaultdict(list)
    for row in ws.iter_rows(min_row=2, values_only=True):
        if len(row) < 3:
            continue
        key, insta = row[0], row[2]
        if not key or insta is None:
            continue
        raw = str(insta).strip()
        if not raw or raw.upper() == "NULL":
            continue
        u = extract_username(raw)
        if not u:
            continue
        k = str(key).strip()
        if k not in m[u.lower()]:
            m[u.lower()].append(k)
    wb.close()
    return m


def load_creator_map(conn):
    """seed_key → creator_id"""
    with conn.cursor() as cur:
        cur.execute("SELECT seed_key, creator_id FROM creators "
                    "WHERE seed_key IS NOT NULL")
        return {r[0]: r[1] for r in cur.fetchall()}


def dedup_by_username(rows):
    best = {}
    no_username = 0
    for r in rows:
        u = (r.get("username") or "").strip().strip("/")
        if not u:
            no_username += 1
            continue
        k = u.lower()
        if is_better(r, best.get(k)):
            r["_username"] = u
            best[k] = r
    return best, no_username


# =========================================================
# 메인
# =========================================================
def main():
    rows = load_jsonl()
    best, no_username = dedup_by_username(rows)
    status_counts = Counter(r.get("status") for r in best.values())

    print("=" * 58)
    print(f"JSONL 총 라인      : {len(rows)}")
    print(f"유니크 username    : {len(best)}")
    if no_username:
        print(f"username 없음(스킵): {no_username}")
    for st, c in status_counts.most_common():
        mark = "적재" if st in STATUS_MAP else "제외"
        print(f"  {st:<16} {c:>6}  ({mark})")
    print("=" * 58)

    print("시트에서 username→key 후보 매핑 생성 중...")
    uname_keys = build_username_key_map()
    print(f"  시트 인스타 username: {len(uname_keys)}개")

    conn = pymysql.connect(**DB)
    try:
        creator_map = load_creator_map(conn)
        print(f"  creators 로드: {len(creator_map)}명")
        print("=" * 58)

        upserted = skipped = 0
        no_creator = 0
        rescued = 0           # jsonl key 로는 실패했는데 시트 후보 key 로 살린 건수
        rescued_ex, missing_ex = [], []

        with conn.cursor() as cur:
            for i, row in enumerate(best.values(), 1):
                status = row.get("status")
                if status not in STATUS_MAP:
                    skipped += 1
                    continue

                username = row["_username"]
                jkey = row.get("key")

                # 후보 key: jsonl 의 key 를 먼저, 그 다음 시트에서 나온 모든 key
                candidates = []
                if jkey:
                    candidates.append(str(jkey).strip())
                for k in uname_keys.get(username.lower(), []):
                    if k not in candidates:
                        candidates.append(k)

                creator_id = None
                used_key = None
                for k in candidates:
                    if k in creator_map:
                        creator_id = creator_map[k]
                        used_key = k
                        break

                if creator_id is None:
                    no_creator += 1
                    if len(missing_ex) < 5:
                        missing_ex.append(f"@{username}({jkey})")
                    continue

                if used_key != (str(jkey).strip() if jkey else None):
                    rescued += 1
                    if len(rescued_ex) < 5:
                        rescued_ex.append(f"@{username} {jkey}→{used_key}")

                id_status, existence = STATUS_MAP[status]

                if not DRY_RUN:
                    cur.execute(SQL_UPSERT_CHANNEL, (
                        creator_id,
                        row.get("url") or normalize_url(username),
                        normalize_url(username),
                        id_status,
                        existence,
                        row.get("user_id"),
                        row.get("nickname"),
                        account_type_label(row.get("account_type")),
                        row.get("biography"),
                        row.get("external_url"),
                    ))
                    cur.execute("""
                        SELECT channel_id
                        FROM channels
                        WHERE platform='instagram'
                        AND channel_url_normalized=%s
                    """, (normalize_url(username),))

                    channel_id = cur.fetchone()[0]
                    cur.execute(
                        SQL_INSERT_CHANNEL_SNAPSHOT,
                        (
                            channel_id,
                            row.get("followers"),
                            row.get("following"),
                            row.get("media_count"),
                        ),
                    )
                    if i % COMMIT_EVERY == 0:
                        conn.commit()
                upserted += 1

        if not DRY_RUN:
            conn.commit()
    finally:
        conn.close()

    print(f"적재(upsert)   : {upserted}" + ("  [DRY_RUN, 미반영]" if DRY_RUN else ""))
    print(f"  └ 시트 key 로 복구: {rescued}")
    if rescued_ex:
        print(f"     예시: {', '.join(rescued_ex)}")
    print(f"건너뜀(미확정) : {skipped}")
    print(f"creator 없음   : {no_creator}")
    if missing_ex:
        print(f"     예시: {', '.join(missing_ex)}")
    print("=" * 58)


if __name__ == "__main__":
    main()