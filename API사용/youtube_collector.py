"""
YouTube 수집기 — 채널(L1) → 영상(L2) → 댓글(L3)을 긁어 PostgreSQL에 적재.

준비물:
    pip install google-api-python-client psycopg2-binary
    export YOUTUBE_API_KEY="..."          # Google Cloud Console에서 발급
    export DATABASE_URL="postgresql://user:pass@host:5432/dbname"

사용:
    python youtube_collector.py https://www.youtube.com/@채널핸들

설정값(아래 CONFIG)만 바꾸면 수집 범위·강도를 조절할 수 있습니다.
쿼터 비용은 코드 하단 QUOTA_COST 표 참고. (현재 일일 기본 한도 10,000 유닛 가정)
"""

import os
import re
import sys
import time
import datetime as dt
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ============================================================
# CONFIG — 나중에 여기만 조절하면 됨
# ============================================================
CONFIG = {
    "video_lookback_months": 6,     # 최근 N개월 영상만 수집 (가이드라인: YouTube 6개월)
    "min_videos": 10,               # 최소 영상 수. 미달이면 기간 2배 확장
    "max_videos": None,             # 영상 상한 (None = 기간 내 전부). 쿼터 아끼려면 숫자 지정
    "comments_per_video": 100,      # 영상당 수집할 최상위 댓글 수 (페이지당 100개)
    "include_replies": False,       # 답글 포함 여부 (대표님 확인 대기 항목)
    "daily_quota_limit": 10000,     # 일일 쿼터 한도 (초과 시 중단)
}

QUOTA_COST = {                      # 호출당 유닛 (YouTube Data API v3 기준)
    "channels.list": 1,
    "playlistItems.list": 1,
    "videos.list": 1,
    "commentThreads.list": 1,
    "search.list": 100,             # 비싸므로 최후의 수단으로만 사용
}

# ============================================================
# 쿼터 추적기 — "차단 없이" 의 핵심
# ============================================================
class QuotaTracker:
    def __init__(self, limit):
        self.used = 0
        self.limit = limit

    def charge(self, call_name):
        cost = QUOTA_COST[call_name]
        if self.used + cost > self.limit:
            raise QuotaExceeded(
                f"쿼터 한도 도달 ({self.used}/{self.limit}). 오늘 수집 중단, 내일 이어서."
            )
        self.used += cost

    def remaining(self):
        return self.limit - self.used

class QuotaExceeded(Exception):
    pass

# ============================================================
# DB 연결
# ============================================================
@contextmanager
def get_db():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# ============================================================
# 파싱 헬퍼
# ============================================================
def parse_duration(iso):
    """ISO 8601 (PT5M30S) → 초. 영상 길이용."""
    if not iso:
        return None
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso)
    if not m:
        return None
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s

def extract_author_id(snippet):
    """authorChannelId는 {'value': 'UC...'} 객체 → 문자열만 추출.
    이걸 PK 삼아 영상 가로질러 묶는 게 중복률/고정 댓글러의 전부."""
    obj = snippet.get("authorChannelId")
    if isinstance(obj, dict):
        return obj.get("value")
    return obj  # 가끔 문자열로 오는 경우 대비

def grapheme_len(text):
    """사람이 보는 글자 수(이모지 단답 vs 문장형 구분용).
    엄밀한 grapheme 클러스터가 필요하면 pip install grapheme 후 grapheme.length() 사용.
    여기선 가벼운 근사: 변이 선택자·결합 문자를 제거한 코드포인트 수."""
    if text is None:
        return 0
    cleaned = re.sub(r"[\uFE00-\uFE0F\u200D]", "", text)
    return len(cleaned)

def safe_int(v):
    """API가 숫자를 문자열로 주거나 필드가 없을 수 있음."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

# ============================================================
# 채널 URL → 채널 ID 변환
# (URL 형태가 @핸들 / /channel/UC... / /c/커스텀 / /user/ 로 제각각)
# ============================================================
def resolve_channel_id(yt, url, quota):
    # 이미 UC... ID가 박힌 URL
    m = re.search(r"/channel/(UC[\w-]+)", url)
    if m:
        return m.group(1)

    # @핸들 형태
    m = re.search(r"/@([\w.-]+)", url)
    if m:
        quota.charge("channels.list")
        resp = yt.channels().list(part="id", forHandle=m.group(1)).execute()
        items = resp.get("items", [])
        if items:
            return items[0]["id"]

    # /c/ 커스텀 URL — 핸들과 동일한 경우가 많아 forHandle 먼저 시도(1유닛),
    # 실패 시에만 search.list 폴백(100유닛). 206개 커스텀 URL 처리용.
    m = re.search(r"/c/([\w.-]+)", url)
    if m:
        custom_name = m.group(1)
        # 1차: forHandle 시도 (저렴)
        quota.charge("channels.list")
        resp = yt.channels().list(part="id", forHandle=custom_name).execute()
        items = resp.get("items", [])
        if items:
            return items[0]["id"]
        # 2차: search.list 폴백 (100유닛 — 최후의 수단)
        try:
            quota.charge("search.list")
            resp = yt.search().list(
                part="snippet", type="channel", q=custom_name, maxResults=1
            ).execute()
            items = resp.get("items", [])
            if items:
                return items[0]["snippet"]["channelId"]
        except QuotaExceeded:
            raise
        except Exception:
            pass

    # /user/레거시 형태
    m = re.search(r"/user/([\w-]+)", url)
    if m:
        quota.charge("channels.list")
        resp = yt.channels().list(part="id", forUsername=m.group(1)).execute()
        items = resp.get("items", [])
        if items:
            return items[0]["id"]

    raise ValueError(f"채널 ID를 못 찾음: {url}")

# ============================================================
# L1 — 채널 통계
# ============================================================
def fetch_channel(yt, channel_id, quota):
    quota.charge("channels.list")
    resp = yt.channels().list(
        part="snippet,statistics,contentDetails", id=channel_id
    ).execute()
    items = resp.get("items", [])
    if not items:
        raise ValueError(f"채널 없음/비공개: {channel_id}")
    ch = items[0]
    uploads_playlist = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    return ch, uploads_playlist

# ============================================================
# L2 영상 목록 — uploads 재생목록을 페이지네이션으로 끝까지
# (search.list=100유닛 대신 playlistItems.list=1유닛으로 저렴하게)
# ============================================================
def fetch_video_ids(yt, uploads_playlist, quota, cfg):
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        days=30 * cfg["video_lookback_months"]
    )
    video_ids, page_token = [], None

    while True:
        quota.charge("playlistItems.list")
        resp = yt.playlistItems().list(
            part="contentDetails",
            playlistId=uploads_playlist,
            maxResults=50,
            pageToken=page_token,
        ).execute()

        for item in resp.get("items", []):
            cd = item["contentDetails"]
            published = cd.get("videoPublishedAt")
            if published:
                pub_dt = dt.datetime.fromisoformat(published.replace("Z", "+00:00"))
                if pub_dt < cutoff:          # 기간 벗어나면 멈춤(최신순 정렬이므로)
                    return _apply_min_max(video_ids, cfg, yt, uploads_playlist, quota, cutoff)
            video_ids.append(cd["videoId"])
            if cfg["max_videos"] and len(video_ids) >= cfg["max_videos"]:
                return video_ids

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return _apply_min_max(video_ids, cfg, yt, uploads_playlist, quota, cutoff)

# ============================================================
# uploads playlist 실패 시 최근 50개 복구
# ============================================================
def fetch_video_ids_search(yt, channel_id, quota):
    quota.charge("search.list")
    resp = yt.search().list(
        part="id",
        channelId=channel_id,
        type="video",
        order="date",
        maxResults=50
    ).execute()
    return [
        item["id"]["videoId"]
        for item in resp.get("items", [])
    ]

def _apply_min_max(video_ids, cfg, yt, uploads_playlist, quota, cutoff):
    """최소 영상 수 미달이면 기간 2배 확장(가이드라인 활동공백 처리)."""
    if len(video_ids) >= cfg["min_videos"]:
        return video_ids
    # 기간을 2배로 늘려 한 번 더 (간단화: 추가 페이지를 더 받음)
    extended_cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        days=60 * cfg["video_lookback_months"]
    )
    extra, page_token = [], None
    while len(video_ids) + len(extra) < cfg["min_videos"]:
        quota.charge("playlistItems.list")
        resp = yt.playlistItems().list(
            part="contentDetails", playlistId=uploads_playlist,
            maxResults=50, pageToken=page_token,
        ).execute()
        for item in resp.get("items", []):
            vid = item["contentDetails"]["videoId"]
            if vid not in video_ids:
                extra.append(vid)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return video_ids + extra

# ============================================================
# L2 영상 통계 — 50개씩 묶어서 호출(쿼터 절약)
# ============================================================
def fetch_videos(yt, video_ids, quota):
    out = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        quota.charge("videos.list")
        resp = yt.videos().list(
            part="snippet,statistics,contentDetails", id=",".join(batch)
        ).execute()
        out.extend(resp.get("items", []))
    return out

# ============================================================
# L3 댓글 — 영상별 페이지네이션. 에러(댓글 비활성 등) 흡수
# ============================================================
def fetch_comments(yt, video_id, quota, cfg):
    comments, page_token, collected = [], None, 0
    while collected < cfg["comments_per_video"]:
        quota.charge("commentThreads.list")
        try:
            resp = yt.commentThreads().list(
                part="snippet,replies" if cfg["include_replies"] else "snippet",
                videoId=video_id,
                maxResults=100,
                pageToken=page_token,
                textFormat="plainText",
            ).execute()
        except HttpError as e:
            if e.resp.status == 403:        # 댓글 비활성/제한 영상
                return None                 # None = "수집 불가" 플래그
            raise

        for thread in resp.get("items", []):
            top = thread["snippet"]["topLevelComment"]
            comments.append((top["id"], top["snippet"], False))
            collected += 1
            if cfg["include_replies"]:
                for reply in thread.get("replies", {}).get("comments", []):
                    comments.append((reply["id"], reply["snippet"], True))

        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return comments

# ============================================================
# DB 적재 (UPSERT — 재수집 시 중복 방지)
# ============================================================
def upsert_creator_and_channel(conn, channel_id, ch, url):
    snip, stats = ch["snippet"], ch.get("statistics", {})
    with conn.cursor() as cur:
        # 1. 이미 등록된 채널인지 확인 (platform + platform_channel_id 기준)
        cur.execute("""
            SELECT id, creator_id
            FROM channels
            WHERE platform = 'youtube'
                AND platform_channel_id = %s
        """, (channel_id,))
        existing = cur.fetchone()

        if existing:
            # 기존 채널 → creator 재사용, 채널 정보만 갱신 (중복 생성 방지)
            ch_pk, creator_id = existing
            cur.execute("""
                UPDATE channels SET
                    nickname=%s, bio=%s, channel_opened_at=%s,
                    platform_channel_id=%s, updated_at=now()
                WHERE id=%s
            """, (snip["title"], snip.get("description"),
                  snip.get("publishedAt"), channel_id, ch_pk))
        else:
            # 신규 채널 → creator 새로 생성 후 channel 생성
            # (주의: 사람-채널 매핑은 채널 기준. 같은 사람이 다른 플랫폼을
            #  운영해도 자동으로 묶지 않음 — 그건 온보딩에서 처리해야 함)
            cur.execute(
                "INSERT INTO creators (name) VALUES (%s) RETURNING id",
                (snip["title"],)
            )
            creator_id = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO channels
                    (creator_id, platform, channel_url, platform_channel_id,
                     nickname, bio, channel_opened_at)
                VALUES (%s,'youtube',%s,%s,%s,%s,%s)
                RETURNING id
            """, (creator_id, url, channel_id, snip["title"],
                  snip.get("description"), snip.get("publishedAt")))
            ch_pk = cur.fetchone()[0]

        # channel_stats는 하루 1회 스냅샷 (snapshot_date UNIQUE로 같은 날 중복 방지)
        cur.execute("""
            INSERT INTO channel_stats
                (channel_id, snapshot_date, follower_count,
                 total_view_count, video_count, raw)
            VALUES (%s, CURRENT_DATE, %s, %s, %s, %s)
            ON CONFLICT (channel_id, snapshot_date) DO NOTHING
        """, (
            ch_pk,
            safe_int(stats.get("subscriberCount")),
            safe_int(stats.get("viewCount")),
            safe_int(stats.get("videoCount")),
            psycopg2.extras.Json(ch)
        ))
    return ch_pk

def upsert_videos(conn, ch_pk, videos, comments_disabled_ids):
    with conn.cursor() as cur:
        for v in videos:
            snip, stats, cd = v["snippet"], v.get("statistics", {}), v["contentDetails"]
            cur.execute("""
                INSERT INTO videos
                    (channel_id, platform_video_id, published_at, title,
                     duration_seconds, category_id, view_count, like_count,
                     comment_count, comments_disabled, raw, last_updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                ON CONFLICT (channel_id, platform_video_id) DO UPDATE SET
                    view_count=EXCLUDED.view_count,
                    like_count=EXCLUDED.like_count,
                    comment_count=EXCLUDED.comment_count,
                    last_updated_at=now()
                RETURNING id
            """, (ch_pk, v["id"], snip.get("publishedAt"), snip.get("title"),
                  parse_duration(cd.get("duration")), snip.get("categoryId"),
                  safe_int(stats.get("viewCount")), safe_int(stats.get("likeCount")),
                  safe_int(stats.get("commentCount")),
                  v["id"] in comments_disabled_ids,
                  psycopg2.extras.Json(v)))
            v["_pk"] = cur.fetchone()[0]

def upsert_comments(conn, video_pk, comments):
    if not comments:
        return
    with conn.cursor() as cur:
        for comment_id, snip, is_reply in comments:
            cur.execute("""
                INSERT INTO comments
                    (video_id, platform_comment_id, author_id, author_name,
                     text, text_length, like_count, is_reply, published_at, raw)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (video_id, platform_comment_id) DO NOTHING
            """, (video_pk, comment_id, extract_author_id(snip),
                  snip.get("authorDisplayName"), snip.get("textDisplay"),
                  grapheme_len(snip.get("textDisplay")),
                  safe_int(snip.get("likeCount")), is_reply,
                  snip.get("publishedAt"), psycopg2.extras.Json(snip)))

def find_channel(conn, platform_channel_id):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id
            FROM channels
            WHERE platform = 'youtube'
              AND platform_channel_id = %s
        """, (platform_channel_id,))
        row = cur.fetchone()
        return row[0] if row else None

def get_existing_video_ids(conn, channel_pk):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT platform_video_id FROM videos WHERE channel_id = %s",
            (channel_pk,)
        )
        return {row[0] for row in cur.fetchall()}

# ============================================================
# 활동 상태 갱신 (가이드라인: active / inactive / archived)
#   0~179일   → active
#   180~364일 → inactive
#   365일+    → archived
# ============================================================
def update_channel_activity(conn, channel_pk):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT MAX(published_at)
            FROM videos
            WHERE channel_id = %s
        """, (channel_pk,))
        last_upload = cur.fetchone()[0]

        if last_upload is None:
            status = "archived"
        else:
            now = dt.datetime.now(dt.timezone.utc)
            if last_upload.tzinfo is None:
                last_upload = last_upload.replace(tzinfo=dt.timezone.utc)
            inactive_days = (now - last_upload).days
            if inactive_days >= 365:
                status = "archived"
            elif inactive_days >= 180:
                status = "inactive"
            else:
                status = "active"

        cur.execute("""
            UPDATE channels
            SET last_upload_at = %s, status = %s, updated_at = now()
            WHERE id = %s
        """, (last_upload, status, channel_pk))
    return status

# ============================================================
# 메인 — 한 채널 1회 수집
# ============================================================
def collect_channel(url, yt=None, quota=None):
    # 배치에서 여러 채널을 돌 때는 yt·quota를 공유(쿼터를 누적으로 세기 위함).
    # 단독 실행이면 새로 만든다.
    if yt is None:
        yt = build("youtube", "v3", developerKey=os.environ["YOUTUBE_API_KEY"])
    if quota is None:
        quota = QuotaTracker(CONFIG["daily_quota_limit"])

    print(f"[1/5] 채널 ID 변환: {url}")
    channel_id = resolve_channel_id(yt, url, quota)

    # 증분 수집: 이미 수집한 영상은 댓글 재수집 건너뛰기 위해 기존 영상 ID 조회
    existing_video_ids = set()
    with get_db() as conn:
        existing_channel_pk = find_channel(conn, channel_id)
        if existing_channel_pk:
            existing_video_ids = get_existing_video_ids(conn, existing_channel_pk)

    print(f"[2/5] 채널 통계 수집 (L1): {channel_id}")
    ch, uploads = fetch_channel(yt, channel_id, quota)

    print(f"[3/5] 영상 목록 수집: 최근 {CONFIG['video_lookback_months']}개월")
    try:
        video_ids = fetch_video_ids(yt, uploads, quota, CONFIG)
    except HttpError:
        print("      → playlist 실패 → search.list 폴백")
        video_ids = fetch_video_ids_search(yt, channel_id, quota)
    print(f"      → 영상 {len(video_ids)}개")

    new_video_ids = [vid for vid in video_ids if vid not in existing_video_ids]

    print("[4/5] 영상 통계 수집 (L2)")
    videos = fetch_videos(yt, video_ids, quota)

    new_video_id_set = set(new_video_ids)
    print(f"[5/5] 댓글 수집 (L3) — 신규 영상 {len(new_video_ids)}개")

    comments_by_video = {}
    disabled = set()
    for v in videos:
        if v["id"] not in new_video_id_set:   # 증분: 기존 영상은 댓글 재수집 안 함
            continue
        result = fetch_comments(yt, v["id"], quota, CONFIG)
        if result is None:
            disabled.add(v["id"])
        else:
            comments_by_video[v["id"]] = result

    print(f"      → 적재 시작 (쿼터 사용: {quota.used}/{quota.limit})")

    with get_db() as conn:
        ch_pk = upsert_creator_and_channel(conn, channel_id, ch, url)
        upsert_videos(conn, ch_pk, videos, disabled)
        for v in videos:
            if v["id"] in comments_by_video:
                upsert_comments(conn, v["_pk"], comments_by_video[v["id"]])
        status = update_channel_activity(conn, ch_pk)

    total_comments = sum(len(c) for c in comments_by_video.values())

    # 영상이 있으면 최근 업로드 날짜 계산 (없으면 None)
    latest_upload = None
    if videos:
        latest_upload = max(
            dt.datetime.fromisoformat(
                v["snippet"]["publishedAt"].replace("Z", "+00:00")
            )
            for v in videos
        ).date()

    print(
        f"\n완료 ✓  영상 {len(videos)}개 · 댓글 {total_comments}개 · "
        f"댓글비활성 {len(disabled)}개 · 최근업로드 {latest_upload} · "
        f"상태 {status} · 쿼터 {quota.used}유닛"
    )

    return {
        "channel_id": channel_id,
        "videos": len(videos),
        "comments": total_comments,
        "disabled": len(disabled),
        "quota_used": quota.used,
        "status": status,
        "last_upload": latest_upload,
        "new_videos": len(new_video_ids),
    }

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python youtube_collector.py <채널 URL>")
        sys.exit(1)
    try:
        collect_channel(sys.argv[1])
    except QuotaExceeded as e:
        print(f"\n[중단] {e}")
    except (ValueError, HttpError) as e:
        print(f"\n[오류] {e}")