# tiktok/seed.py
"""
SNS_정보.xlsx(G_/M_ 배치) → creators 생성(없으면) + channels(tiktok) 적재.

유튜브 seed.py와 다른 점
------
유튜브 시드는 자기 엑셀에서 creators를 새로 만든다.
틱톡 시드는 '이미 있는 creator에 채널을 붙이는' 쪽에 가깝다.

    seed_key(엑셀의 '키값')로 creator를 찾고,
    없으면 만들고, 있으면 그대로 두고 채널만 추가한다.

한 크리에이터가 유튜브·인스타·틱톡을 다 가질 수 있으므로
(creator : channel = 1 : N) 이런 구조가 필요하다.
seed_key가 세 플랫폼을 잇는 연결고리다.

creator는 seed_key로 upsert (idempotent), channel은 channel_url_raw UNIQUE로 upsert.
  미리보기:  python -m tiktok.seed --dry-run
  실제적재:  python -m tiktok.seed
"""
import argparse
import re
from collections import Counter

try:
    import pymysql
except ImportError:
    pymysql = None

from tiktok import config

# 엑셀이 youtube 폴더에 있다. 세 플랫폼이 같은 원본 파일을 쓰기 때문.
# (열만 다르다: '유튜브' / 'ChTikTok' / 인스타 열)
DEFAULT_EXCEL = "youtube/SNS_정보.xlsx"
SHEET = "Sheet1"

# 틱톡 핸들 규칙: 영문/숫자/밑줄/점, 2~24자.
# 유튜브와 달리 한글이 안 되므로 정규식으로 검증할 수 있다.
# (유튜브 핸들은 한글·일본어가 들어가 화이트리스트가 무의미했다)
HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{2,24}$")

# 틱톡 단축링크 도메인. 앱에서 '공유'하면 이 형태가 나온다.
# 핸들을 추출할 수 없어 크롤링 대상에서 제외된다.
# ⚠️ 실측 61건. 리다이렉트를 따라가면 실제 URL을 얻을 수 있지만
#    아직 구현하지 않았다. (개선 과제)
SHORTLINK_HOSTS = ("vt.tiktok.com", "vm.tiktok.com", "lite.tiktok.com")


def normalize_tiktok(raw):
    """return (handle, normalized_url, status).

    status가 곧 channel_id_status로 저장되고,
    L1의 대상 선정 조건('handle_only'인 것만)이 된다.

      handle_only     핸들 확보 → 크롤링 가능
      unresolved      틱톡 URL이지만 핸들 추출 실패 (단축링크 등)
      skip_nontiktok  다른 플랫폼 주소
      skip_empty      빈 칸

    유튜브 seed와 달리 사유별로 분리해서 세 가지 상태를 만든다.
    나중에 "왜 이 채널은 수집이 안 됐지?"를 구분할 수 있다.
    """
    if raw is None:
        return (None, None, "skip_empty")
    s = str(raw).strip()
    # 사람이 채운 엑셀에는 빈칸 대신 문자열 "NULL"/"none"/"nan"이 들어온다.
    # (pandas로 저장했다 다시 연 파일에서 흔하다)
    if s == "" or s.upper() == "NULL" or s.lower() in ("none", "nan"):
        return (None, None, "skip_empty")
    low = s.lower()

    # 단축링크는 리다이렉트를 따라가야 핸들을 알 수 있다.
    # 시드 단계에서 네트워크 요청을 하고 싶지 않아 unresolved로 남긴다.
    if any(h in low for h in SHORTLINK_HOSTS):
        return (None, None, "unresolved")

    # 정상 경로: /@핸들
    if "/@" in s:
        # 쿼리스트링·탭 경로를 잘라낸다: /@name/video/123?lang=ko → name
        handle = s.split("/@")[-1].split("/")[0].split("?")[0].strip()
        if HANDLE_RE.match(handle):
            return (handle, "https://www.tiktok.com/@" + handle, "handle_only")
        return (None, None, "unresolved")   # 형식이 안 맞는 핸들

    # URL처럼 보이는데 /@가 없는 경우
    if ("://" in low) or ("www." in low) or (".com" in low) or (".me" in low) or (".net" in low):
        if "tiktok.com" in low:
            return (None, None, "unresolved")      # 틱톡인데 형태 판별 불가
        return (None, None, "skip_nontiktok")      # 인스타/유튜브 등

    # URL이 아니라 핸들만 적힌 경우 ("@name" 또는 "name")
    bare = s.lstrip("@")
    if HANDLE_RE.match(bare):
        return (bare, "https://www.tiktok.com/@" + bare, "handle_only")
    return (None, None, "skip_nontiktok")


def collect(path):
    """엑셀 → creator당 틱톡 채널 1개. dedup by 키값. 적재 대상만 반환."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True)   # 대용량 대비
    ws = wb[SHEET]
    it = ws.iter_rows(values_only=True)
    header = [(str(c).strip() if c is not None else "") for c in next(it)]

    # 유튜브 seed는 별칭 사전으로 유연하게 찾는데, 여기는 정확한 이름을 요구한다.
    # 헤더가 없으면 DB를 건드리기 전에 중단한다.
    # (수천 행을 넣고 나서 "엉뚱한 열을 읽었다"를 발견하면 늦다)
    try:
        ki = header.index("키값")
        ti = header.index("ChTikTok")
    except ValueError:
        raise SystemExit("헤더에서 '키값'/'ChTikTok' 못 찾음: %r" % header)

    seen = set()
    records = []
    counters = Counter()
    for row in it:
        if ki >= len(row):
            continue      # 행 길이가 짧으면(뒤쪽이 전부 빈칸) 건너뜀
        key = row[ki]
        if key is None or str(key).strip() == "":
            continue
        key = str(key).strip()
        tt = row[ti] if ti < len(row) else None
        handle, norm, status = normalize_tiktok(tt)
        counters[status] += 1   # 전체 분류 집계 (skip 포함)

        # unresolved도 적재한다. 나중에 단축링크를 풀면 살릴 수 있으므로
        # DB에 흔적을 남겨둔다. (skip_* 는 아예 안 넣음)
        if status not in ("handle_only", "unresolved"):
            continue

        # 같은 키값이 여러 행에 나오면 첫 번째만 쓴다.
        # (creator당 틱톡 채널 1개 전제)
        if key in seen:
            counters["dup_creator"] += 1
            continue
        seen.add(key)

        # 임시 닉네임. L1이 실제 채널명을 받으면 갱신된다.
        nickname = (handle if handle else key)[:100]
        records.append(dict(seed_key=key, raw=str(tt).strip()[:768],
                            norm=norm, status=status, nickname=nickname))
    return records, counters


# ON DUPLICATE KEY UPDATE seed_key=seed_key
# → "아무것도 안 바꾼다"는 관용구. 중복이면 조용히 무시한다.
#   틱톡 시드는 기존 creator를 건드리면 안 된다.
#   유튜브 시드가 이미 넣어둔 닉네임·소속사를 덮어쓰면 안 되기 때문.
#   (유튜브 seed는 반대로 VALUES(...)로 갱신한다 — 자기가 주인이라서)
CREATOR_UPSERT = ("INSERT INTO creators (seed_key, nickname, memo) VALUES (%s,%s,%s) "
                  "ON DUPLICATE KEY UPDATE seed_key=seed_key")

# channel도 마찬가지. channel_url_raw UNIQUE에 걸리면 그대로 둔다.
# 재실행해도 L1이 채워둔 channel_name/bio 등이 날아가지 않는다.
CHANNEL_UPSERT = (
    "INSERT INTO channels "
    "(creator_id, platform, channel_url_raw, channel_url_normalized, channel_id_status) "
    "VALUES (%s,'tiktok',%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE "
    "channel_id = channel_id"
)


def load_creator_map(cur):
    """seed_key → creator_id 매핑을 통째로 가져온다.

    행마다 SELECT를 날리지 않고 한 번에 읽어 메모리에 둔다.
    수천 행이면 쿼리 수천 번 vs 한 번의 차이가 크다.
    """
    cur.execute("SELECT creator_id, seed_key FROM creators WHERE seed_key IS NOT NULL")
    return {sk: cid for cid, sk in cur.fetchall()}


def main():
    ap = argparse.ArgumentParser(prog="tiktok.seed")
    ap.add_argument("--file", default=DEFAULT_EXCEL)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    records, counters = collect(args.file)

    # 분류 결과를 먼저 보여준다.
    # 이게 곧 시드 엑셀의 품질 리포트다.
    # (unresolved가 많으면 = 단축링크가 많다 = 엑셀 관리 방식을 바꿔야 한다)
    print("=" * 52)
    print("[분류] handle_only=%d unresolved=%d skip_nontiktok=%d skip_empty=%d dup_creator=%d"
          % (counters.get("handle_only", 0), counters.get("unresolved", 0),
             counters.get("skip_nontiktok", 0), counters.get("skip_empty", 0),
             counters.get("dup_creator", 0)))
    print("[적재 대상] 고유 creator(틱톡 보유):", len(records))

    if pymysql is None:
        raise SystemExit("pymysql 필요: pip install pymysql")

    conn = pymysql.connect(**config.DB)
    try:
        with conn.cursor() as cur:
            cmap = load_creator_map(cur)
            new_keys = [r for r in records if r["seed_key"] not in cmap]
            # 기존 재사용 / 신규 생성을 구분해 보여준다.
            # 유튜브 시드를 먼저 돌렸다면 대부분 '재사용'으로 나와야 정상이다.
            print("  기존 creator 재사용:", len(records) - len(new_keys))
            print("  새로 생성할 creator:", len(new_keys))
            print("=" * 52)

            # ★ --dry-run: DB를 건드리기 전에 결과를 확인한다.
            #   시드는 수천 행을 한 번에 넣는 작업이라 되돌리기 어렵다.
            #   유튜브 seed에는 없는 옵션 (틱톡을 만들면서 추가)
            if args.dry_run:
                print("[DRY-RUN] DB 변경 없음.")
                print("  새 creator 샘플:", [r["seed_key"] for r in new_keys[:5]])
                print("  채널 샘플:")
                for r in records[:4]:
                    print("    %s | %s | %s" % (r["seed_key"], r["status"], r["norm"] or r["raw"]))
                return

            # ── 1단계: creator 먼저 (channels.creator_id가 FK) ──
            # executemany로 한 번에 넣는다. 행마다 execute하면 수천 번 왕복.
            if new_keys:
                cur.executemany(CREATOR_UPSERT,
                                [(r["seed_key"], r["nickname"], "SNS_정보 tiktok seed")
                                 for r in new_keys])
                conn.commit()

            # ── 2단계: creator_id를 다시 읽어온다 ──
            # 방금 INSERT한 것들의 PK가 필요한데, executemany로는
            # 개별 lastrowid를 알 수 없어 매핑을 새로 로드한다.
            cmap = load_creator_map(cur)
            ch_rows, unmatched = [], 0
            for r in records:
                cid = cmap.get(r["seed_key"])
                if cid is None:
                    unmatched += 1   # 있으면 안 되는 케이스. 집계해서 드러낸다.
                    continue
                ch_rows.append((cid, r["raw"], r["norm"], r["status"]))
            cur.executemany(CHANNEL_UPSERT, ch_rows)
            conn.commit()
            print("[COMMIT] creator 생성 %d / channel 적재 %d (매칭실패 %d)"
                  % (len(new_keys), len(ch_rows), unmatched))

            # 적재 후 DB 현황을 바로 보여준다.
            # 따로 SQL을 치지 않아도 "지금 뭐가 들어갔나"를 알 수 있고,
            # 이 값이 곧 L1의 대상 규모다(handle_only 개수).
            cur.execute("SELECT channel_id_status, COUNT(*) FROM channels "
                        "WHERE platform='tiktok' GROUP BY channel_id_status")
            print("[DB 현황] tiktok 채널:")
            for st, n in cur.fetchall():
                print("  %-14s: %d" % (st, n))
    except Exception:
        conn.rollback()   # 중간에 실패하면 전부 되돌린다
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()