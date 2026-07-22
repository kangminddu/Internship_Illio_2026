"""
TikTok L2 무료 수집 시도 — 프로필 페이지의 숨겨진 JSON 파싱.
브라우저 없이 requests만으로 영상 목록(L2)을 뽑을 수 있는지 확인.

실행:
    python tiktok_l2_test.py tiktok
"""

import sys
import re
import json
import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.tiktok.com/",
}


def main(username):
    url = f"https://www.tiktok.com/@{username}"

    print(f"요청: {url}")
    print("=" * 60)

    try:
        resp = requests.get(
            url,
            headers=HEADERS,
            timeout=20,
        )
    except Exception as e:
        print("요청 실패:", e)
        return

    print(f"상태 코드: {resp.status_code}")
    print(f"HTML 크기: {len(resp.text):,}자")

    # HTML 저장
    with open("tiktok_response.html", "w", encoding="utf-8") as f:
        f.write(resp.text)

    print("HTML 저장: tiktok_response.html")

    # 숨겨진 JSON 찾기
    m = re.search(
        r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        resp.text,
        re.DOTALL,
    )

    if not m:
        print("\n✗ 숨겨진 JSON 태그 못 찾음")

        print("\n===== 응답 일부 =====")
        print(resp.text[:1000])

        if any(
            s in resp.text.lower()
            for s in ["captcha", "verify", "robot"]
        ):
            print("\n(CAPTCHA 또는 봇 차단 감지)")

        return

    print("\n✓ JSON 태그 발견")

    try:
        data = json.loads(m.group(1))
    except Exception as e:
        print("JSON 파싱 실패:", e)
        return

    print("✓ JSON 파싱 성공\n")

    scope = data.get("__DEFAULT_SCOPE__", {})

    print("===== SCOPE =====")
    for key in scope.keys():
        print(key)

    user_detail = scope.get("webapp.user-detail", {})
    user_info = user_detail.get("userInfo", {})

    if user_info:
        stats = user_info.get("stats", {})
        user = user_info.get("user", {})

        print("\n[L1 프로필]")
        print("닉네임 :", user.get("nickname"))
        print("팔로워 :", stats.get("followerCount"))
        print("좋아요 :", stats.get("heartCount"))
        print("영상 수 :", stats.get("videoCount"))

    item_list = user_detail.get("itemList", [])

    print("\n[L2 영상 목록]")

    if item_list:
        print(f"영상 {len(item_list)}개")

        for item in item_list[:5]:
            stats = item.get("stats", {})

            print(
                f"id={item.get('id')} "
                f"조회={stats.get('playCount')} "
                f"좋아요={stats.get('diggCount')} "
                f"댓글={stats.get('commentCount')}"
            )

    else:
        print("영상 목록 없음")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법:")
        print("python tiktok_l2_test.py tiktok")
        sys.exit(1)

    main(sys.argv[1])