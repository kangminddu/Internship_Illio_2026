"""
Instagram 백엔드 API 테스트 — L1(프로필) + L2(포스트)가 로그인 없이 되는지.
목적: "L1·L2는 무료, L3(댓글 작성자)만 유료" 전략이 실제로 성립하는지 검증.

준비:
    pip install requests

실행:
    python insta_backend_test.py <username>
    예: python insta_backend_test.py nike

확인 포인트:
    - 로그인 없이 프로필(L1)이 나오는가?
    - 포스트별 조회/좋아요/댓글'개수'(L2)가 나오는가?
    - 댓글 '작성자'(L3)는 정말 없는가?  ← 이게 유료가 필요한 이유
"""

import sys
import json
import requests

HEADERS = {
    # 인스타 웹앱을 흉내내는 헤더 (로그인 없이 공개 프로필 접근용)
    "x-ig-app-id": "936619743392459",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "*/*",
}


def main(username):
    url = f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}"
    print(f"요청: @{username}\n" + "=" * 50)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
    except Exception as e:
        print(f"✗ 요청 실패: {e}")
        return

    print(f"상태 코드: {resp.status_code}")
    if resp.status_code != 200:
        print("✗ 200이 아님 — 차단되었거나 헤더가 막힘")
        print(resp.text[:300])
        return

    try:
        user = resp.json()["data"]["user"]
    except (KeyError, json.JSONDecodeError):
        print("✗ JSON 파싱 실패 — 응답 구조가 예상과 다름 (차단 가능성)")
        print(resp.text[:300])
        return

    # --- L1: 프로필 ---
    print("\n[L1 프로필]")
    print(f"  이름     : {user.get('full_name')}")
    print(f"  팔로워   : {user.get('edge_followed_by', {}).get('count'):,}")
    print(f"  팔로잉   : {user.get('edge_follow', {}).get('count'):,}")
    print(f"  게시물   : {user.get('edge_owner_to_timeline_media', {}).get('count'):,}")
    print(f"  비즈니스 : {user.get('is_business_account')}")
    print("  ✓ L1 수집 성공 (로그인 없이)")

    # --- L2: 최근 포스트 ---
    print("\n[L2 포스트] — 최근 몇 개")
    edges = user.get("edge_owner_to_timeline_media", {}).get("edges", [])
    if not edges:
        print("  (포스트 없음 또는 못 가져옴)")
    for e in edges[:5]:
        node = e["node"]
        likes = node.get("edge_liked_by", {}).get("count", 0)
        comments = node.get("edge_media_to_comment", {}).get("count", 0)
        views = node.get("video_view_count", "-")
        print(f"  · id={node['id'][:12]}... 좋아요 {likes:,} | "
              f"댓글수 {comments:,} | 조회 {views}")
    if edges:
        print("  ✓ L2 수집 성공 (조회/좋아요/댓글 '개수')")

    # --- L3 확인: 댓글 '작성자'가 있는가? ---
    print("\n[L3 댓글 작성자] — 이게 우리 핵심")
    if edges:
        node = edges[0]["node"]
        comment_edge = node.get("edge_media_to_comment", {})
        # 이 엔드포인트는 count만 주고, 실제 댓글 리스트(edges)는 비어있음
        comment_list = comment_edge.get("edges", [])
        print(f"  댓글 개수(count): {comment_edge.get('count', 0):,}")
        print(f"  실제 댓글 리스트: {len(comment_list)}개")
        if len(comment_list) == 0:
            print("  ✗ 댓글 본문/작성자 없음 → L3는 이 방법으로 불가")
            print("    (댓글 개수만 나옴. 작성자 ID = 유료 API 필요)")
        else:
            print("  ! 댓글 리스트가 나옴 — 예상 밖, 확인 필요")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python insta_backend_test.py <username>")
        print("예시  : python insta_backend_test.py nike")
        sys.exit(1)
    main(sys.argv[1])