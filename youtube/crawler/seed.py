"""
범용 Seed: 엑셀(유튜브 URL 포함) → creators/channels 적재.
- 컬럼을 '위치'가 아니라 '헤더 이름'으로 찾음 (별칭 사전).
- 엑셀 경로/시트를 CLI 인자로 받음.
- URL 검증/정규화는 lib/youtube_url_filter.py 로 일원화
  (percent-encoded 한글 handle, 구형 custom URL, /videos/ 꼬리 모두 처리)

사용법:
  python -m youtube.crawler.seed --file 파일.xlsx
  python -m youtube.crawler.seed --file 파일.xlsx --sheet "시트명"
"""
import re
import argparse
from datetime import datetime
from openpyxl import load_workbook
import pymysql
from youtube.config import DB
from youtube.crawler.lib.youtube_url_filter import normalize_youtube_channel_url

# ─────────────────────────────────────────────
# 컬럼 별칭 사전
# ─────────────────────────────────────────────
COLUMN_ALIASES = {
    "youtube":  ["유튜브", "youtubeurl", "youtube", "유튜브url", "youtube_url", "유튜브 url"],
    "nickname": ["닉네임", "상품명2", "상품명 2", "이름", "크리에이터", "키값"],
    "agency":   ["소속사", "소속", "agency"],
    "category": ["구분", "카테고리", "category"],
    "birthday": ["생일", "birthday"],
    "debut":    ["데뷔", "데뷔일", "debut"],
}


def normalize_header(h):
    if h is None:
        return ""
    return str(h).strip().replace(" ", "").lower()


def build_column_map(header_row):
    norm_headers = [normalize_header(h) for h in header_row]
    colmap = {}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            na = normalize_header(alias)
            if na in norm_headers:
                colmap[field] = norm_headers.index(na)
                break
    return colmap


def classify_status(u):
    if re.search(r"/channel/UC[\w-]{22}", u): return "resolved"
    if "/@" in u: return "handle_only"
    if "/c/" in u: return "custom_only"
    if "/user/" in u: return "user_legacy"
    return "unresolved"   # 구형 custom URL(youtube.com/이름)도 여기로 → 크롤링 시 UC로 해소


def clean(v):
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "-", "NULL"):
        return None
    return s


def to_date(v):
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    return None


def get(row, colmap, field):
    idx = colmap.get(field)
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def main(xlsx_path, sheet_name=None, key_prefix="SD"):
    wb = load_workbook(xlsx_path, read_only=True)

    if sheet_name and sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
    else:
        ws = wb[wb.sheetnames[0]]
        if sheet_name:
            print(f"⚠️  시트 '{sheet_name}' 없음 → 첫 시트 '{ws.title}' 사용")

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        print("❌ 빈 시트")
        return

    header = rows[0]
    colmap = build_column_map(header)

    print(f"시트: {ws.title} | 데이터 {len(rows)-1}행")
    print(f"인식된 컬럼: {colmap}")
    if "youtube" not in colmap:
        print("❌ '유튜브' 컬럼을 못 찾음. 헤더:", header)
        print("   → COLUMN_ALIASES['youtube']에 실제 헤더명을 추가하세요.")
        return
    if "nickname" not in colmap:
        print("❌ '닉네임' 컬럼을 못 찾음. 헤더:", header)
        return
    print()

    conn = pymysql.connect(**DB, autocommit=True)
    inserted_ch = 0
    creator_only = 0
    skipped = 0
    skip_reasons = {}   # 사유별 집계 (마지막에 요약 출력)

    with conn.cursor() as cur:
        for i, row in enumerate(rows[1:], 1):
            nickname = clean(get(row, colmap, "nickname"))
            youtube  = clean(get(row, colmap, "youtube"))
            agency   = clean(get(row, colmap, "agency"))
            category = clean(get(row, colmap, "category"))
            debut    = to_date(get(row, colmap, "debut"))
            birthday = to_date(get(row, colmap, "birthday"))

            if not nickname:
                skipped += 1
                continue

            seed_key = f"{key_prefix}_{i}"

            cur.execute("""
                INSERT INTO creators (seed_key, nickname, category, agency, debut_date, birthday)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    nickname=VALUES(nickname), category=VALUES(category),
                    agency=VALUES(agency), debut_date=VALUES(debut_date),
                    birthday=VALUES(birthday)
            """, (seed_key, nickname, category, agency, debut, birthday))

            if not youtube:
                creator_only += 1
                continue

            # ── URL 검증 + 정규화 (한 번에) ──
            url_norm, skip_reason = normalize_youtube_channel_url(youtube)
            if not url_norm:
                print(f"⚠️ Skip [{skip_reason}] : {youtube}")
                skip_reasons[skip_reason] = skip_reasons.get(skip_reason, 0) + 1
                creator_only += 1
                continue

            cur.execute("SELECT creator_id FROM creators WHERE seed_key=%s", (seed_key,))
            creator_id = cur.fetchone()[0]

            status = classify_status(url_norm)
            uc = None
            m = re.search(r"(UC[\w-]{22})", url_norm)
            if m:
                uc = m.group(1)

            cur.execute("""
                INSERT INTO channels
                  (creator_id, platform, channel_url_raw, channel_url_normalized,
                   channel_id_status, external_channel_id, is_primary)
                VALUES (%s, 'youtube', %s, %s, %s, %s, 1)
                ON DUPLICATE KEY UPDATE
                  channel_url_normalized=VALUES(channel_url_normalized),
                  channel_id_status=VALUES(channel_id_status)
            """, (creator_id, youtube, url_norm, status, uc))
            inserted_ch += 1

    conn.close()
    print(f"\ncreators 총: {inserted_ch + creator_only}")
    print(f"  ├ 유튜브 채널 생성: {inserted_ch}")
    print(f"  └ creators만(유튜브 없음/부적합): {creator_only}")
    print(f"닉네임 없어 skip: {skipped}")
    if skip_reasons:
        print("URL skip 사유별 집계:")
        for reason, cnt in sorted(skip_reasons.items(), key=lambda x: -x[1]):
            print(f"  - {reason}: {cnt}건")
    print("완료. python main.py --l1 로 시작.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="엑셀 → DB seed")
    parser.add_argument("--file", required=True, help="엑셀 파일 경로")
    parser.add_argument("--sheet", default=None, help="시트명 (없으면 첫 시트)")
    parser.add_argument("--prefix", default="SD", help="seed_key 접두사 (기본 SD)")
    args = parser.parse_args()
    main(args.file, args.sheet, args.prefix)