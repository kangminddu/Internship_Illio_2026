# -*- coding: utf-8 -*-
"""
reader.py (개선판)

youtube/SNS_정보.xlsx 의 Sheet1 에서 Instagram 컬럼(C)만 읽어
username 리스트를 만든다. DB와 무관하며 아래 형태를 반환한다.

[
    {
        "key": "G_35",
        "username": "fancim_review",
        "url": "https://www.instagram.com/fancim_review/",
        "raw_url": "<원본 셀 값>"
    },
    ...
]

컬럼: A=key, B=TikTok, C=Instagram, D=YouTube
"""

import re
from pathlib import Path
from urllib.parse import urlparse

from openpyxl import load_workbook


# =========================================================
# 설정
# =========================================================
# 엑셀 기본 경로: reader.py 위치 기준으로 계산 (실행 위치와 무관하게 동작)
#   instagram/reader.py  ->  ../youtube/SNS_정보.xlsx
DEFAULT_EXCEL = Path(__file__).resolve().parent.parent / "youtube" / "SNS_정보.xlsx"

# username 으로 오면 안 되는 예약 경로 (실제 프로필이 아님)
INVALID_PATHS = {
    "", "accounts", "explore", "reels", "reel", "stories", "story",
    "p", "tv", "channel", "redirect", "invites", "invite",
    "share", "s", "_u", "web", "direct", "directory",
    "about", "legal", "privacy", "terms", "developer", "developers",
    "api", "oauth", "help", "press", "business", "creators",
}

# Instagram username 규칙: 영문/숫자/밑줄/마침표, 1~30자
USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")

# True면 위 규칙에 안 맞는 username을 버린다.
# 기존(관대한) 동작을 그대로 쓰고 싶으면 False 로 두세요.
VALIDATE_USERNAME = True


# =========================================================
# username 추출
# =========================================================
def extract_username(url, validate=None):
    """
    Instagram URL -> username

    https://www.instagram.com/nike/         -> nike
    https://www.instagram.com/nike/?hl=ko   -> nike
    https://instagram.com/Nike              -> nike   (소문자 정규화)
    https://www.instagram.com/p/XXptr/      -> ""      (게시글 링크)
    "우리 인스타 놀러오세요"                 -> ""      (URL 아님)
    """
    if validate is None:
        validate = VALIDATE_USERNAME

    if not url:
        return ""

    url = str(url).strip()

    # URL이 아닌 공유 문구 제거
    if "instagram.com" not in url.lower():
        return ""

    # 스킴 보정
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)

    # instagram 도메인만 허용
    if "instagram.com" not in parsed.netloc.lower():
        return ""

    path = parsed.path.strip("/")
    if not path:
        return ""

    username = path.split("/")[0].strip().lower()

    # 예약 경로 제거
    if username in INVALID_PATHS:
        return ""

    # 형식 검증 (선택)
    if validate and not USERNAME_RE.match(username):
        return ""

    return username


# =========================================================
# 엑셀 로드
# =========================================================
def load_instagram_urls(excel_path=None, sheet_name="Sheet1", verbose=True):
    """
    Sheet1의 Instagram 컬럼(C)만 읽어 중복 없는 username 리스트를 반환.
    """
    excel_path = Path(excel_path) if excel_path else DEFAULT_EXCEL

    if not excel_path.exists():
        raise FileNotFoundError(f"엑셀을 찾을 수 없습니다: {excel_path}")

    wb = load_workbook(excel_path, data_only=True)
    if sheet_name not in wb.sheetnames:
        wb.close()
        raise ValueError(f"'{sheet_name}' 시트가 없습니다. 존재하는 시트: {wb.sheetnames}")
    ws = wb[sheet_name]

    result = []
    seen = set()
    stats = {
        "total": 0,
        "empty_or_null": 0,
        "no_username": 0,
        "duplicate": 0,
        "collected": 0,
    }

    for row in ws.iter_rows(min_row=2, values_only=True):
        stats["total"] += 1

        # C열보다 짧은 행 방어
        if len(row) < 3:
            stats["empty_or_null"] += 1
            continue

        key = row[0]
        instagram = row[2]

        if instagram is None:
            stats["empty_or_null"] += 1
            continue

        raw = str(instagram).strip()
        if not raw or raw.upper() == "NULL":
            stats["empty_or_null"] += 1
            continue

        username = extract_username(raw)
        if not username:
            stats["no_username"] += 1
            continue

        # 같은 계정은 한 번만 (소문자 기준)
        if username in seen:
            stats["duplicate"] += 1
            continue

        seen.add(username)
        result.append(
            {
                "key": key,
                "username": username,
                "url": f"https://www.instagram.com/{username}/",
                "raw_url": raw,
            }
        )
        stats["collected"] += 1

    wb.close()

    if verbose:
        print(
            "[reader] 총 {total}행 | 빈값/NULL {empty_or_null} | "
            "username없음 {no_username} | 중복 {duplicate} | "
            "최종수집 {collected}".format(**stats)
        )

    return result


# l1.py 호환용 별칭 (l1.py의 load_rows에서 get_instagram_rows()로 호출)
get_instagram_rows = load_instagram_urls


if __name__ == "__main__":
    rows = load_instagram_urls()
    print(f"총 {len(rows)}개")
    for row in rows[:10]:
        print(row)