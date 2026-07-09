# tiktok/seed.py
# SNS_정보.xlsx(G_/M_ 배치) → creators 생성(없으면) + channels(tiktok) 적재.
# creator는 seed_key로 upsert (idempotent), channel은 channel_url_raw UNIQUE로 upsert.
#   미리보기:  python -m tiktok.seed --dry-run
#   실제적재:  python -m tiktok.seed
import argparse
import re
from collections import Counter

try:
    import pymysql
except ImportError:
    pymysql = None

from tiktok import config

DEFAULT_EXCEL = "code/SNS_정보.xlsx"
SHEET = "Sheet1"
HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{2,24}$")
SHORTLINK_HOSTS = ("vt.tiktok.com", "vm.tiktok.com", "lite.tiktok.com")


def normalize_tiktok(raw):
    """return (handle, normalized_url, status)."""
    if raw is None:
        return (None, None, "skip_empty")
    s = str(raw).strip()
    if s == "" or s.upper() == "NULL" or s.lower() in ("none", "nan"):
        return (None, None, "skip_empty")
    low = s.lower()
    if any(h in low for h in SHORTLINK_HOSTS):
        return (None, None, "unresolved")
    if "/@" in s:
        handle = s.split("/@")[-1].split("/")[0].split("?")[0].strip()
        if HANDLE_RE.match(handle):
            return (handle, "https://www.tiktok.com/@" + handle, "handle_only")
        return (None, None, "unresolved")
    if ("://" in low) or ("www." in low) or (".com" in low) or (".me" in low) or (".net" in low):
        if "tiktok.com" in low:
            return (None, None, "unresolved")
        return (None, None, "skip_nontiktok")
    bare = s.lstrip("@")
    if HANDLE_RE.match(bare):
        return (bare, "https://www.tiktok.com/@" + bare, "handle_only")
    return (None, None, "skip_nontiktok")


def collect(path):
    """엑셀 → creator당 틱톡 채널 1개. dedup by 키값. 적재 대상만 반환."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb[SHEET]
    it = ws.iter_rows(values_only=True)
    header = [(str(c).strip() if c is not None else "") for c in next(it)]
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
            continue
        key = row[ki]
        if key is None or str(key).strip() == "":
            continue
        key = str(key).strip()
        tt = row[ti] if ti < len(row) else None
        handle, norm, status = normalize_tiktok(tt)
        counters[status] += 1
        if status not in ("handle_only", "unresolved"):
            continue
        if key in seen:
            counters["dup_creator"] += 1
            continue
        seen.add(key)
        nickname = (handle if handle else key)[:100]
        records.append(dict(seed_key=key, raw=str(tt).strip()[:768],
                            norm=norm, status=status, nickname=nickname))
    return records, counters


CREATOR_UPSERT = ("INSERT INTO creators (seed_key, nickname, memo) VALUES (%s,%s,%s) "
                  "ON DUPLICATE KEY UPDATE seed_key=seed_key")
CHANNEL_UPSERT = (
    "INSERT INTO channels "
    "(creator_id, platform, channel_url_raw, channel_url_normalized, channel_id_status) "
    "VALUES (%s,'tiktok',%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE "
    "channel_url_normalized=VALUES(channel_url_normalized), "
    "channel_id_status=VALUES(channel_id_status)"
)


def load_creator_map(cur):
    cur.execute("SELECT creator_id, seed_key FROM creators WHERE seed_key IS NOT NULL")
    return {sk: cid for cid, sk in cur.fetchall()}


def main():
    ap = argparse.ArgumentParser(prog="tiktok.seed")
    ap.add_argument("--file", default=DEFAULT_EXCEL)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    records, counters = collect(args.file)

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
            print("  기존 creator 재사용:", len(records) - len(new_keys))
            print("  새로 생성할 creator:", len(new_keys))
            print("=" * 52)

            if args.dry_run:
                print("[DRY-RUN] DB 변경 없음.")
                print("  새 creator 샘플:", [r["seed_key"] for r in new_keys[:5]])
                print("  채널 샘플:")
                for r in records[:4]:
                    print("    %s | %s | %s" % (r["seed_key"], r["status"], r["norm"] or r["raw"]))
                return

            if new_keys:
                cur.executemany(CREATOR_UPSERT,
                                [(r["seed_key"], r["nickname"], "SNS_정보 tiktok seed")
                                 for r in new_keys])
                conn.commit()
            cmap = load_creator_map(cur)
            ch_rows, unmatched = [], 0
            for r in records:
                cid = cmap.get(r["seed_key"])
                if cid is None:
                    unmatched += 1
                    continue
                ch_rows.append((cid, r["raw"], r["norm"], r["status"]))
            cur.executemany(CHANNEL_UPSERT, ch_rows)
            conn.commit()
            print("[COMMIT] creator 생성 %d / channel 적재 %d (매칭실패 %d)"
                  % (len(new_keys), len(ch_rows), unmatched))

            cur.execute("SELECT channel_id_status, COUNT(*) FROM channels "
                        "WHERE platform='tiktok' GROUP BY channel_id_status")
            print("[DB 현황] tiktok 채널:")
            for st, n in cur.fetchall():
                print("  %-14s: %d" % (st, n))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
