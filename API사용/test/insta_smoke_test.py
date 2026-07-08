"""
Instagram 스모크 테스트 — 댓글 작성자 ID가 나오는지 확인 (DB 안 건드림).
핵심 질문: comment.owner.username / comment.owner.userid 가 실제로 나오는가?
          (YouTube의 authorChannelId에 해당 — 중복률/고정 댓글러의 재료)

준비:
    pip install instaloader

실행:
    python insta_smoke_test.py <채널_username>
    예: python insta_smoke_test.py nike

⚠️ 반드시 부계정으로. 주 계정 쓰지 마세요 (차단 위험).
   비밀번호는 실행 시 터미널로 입력받습니다 (화면에 안 보임).
"""

import sys
import getpass
import instaloader


def main(target_username):
    L = instaloader.Instaloader(
        download_pictures=False,   # 우리는 사진 다운로드가 목적이 아님
        download_videos=False,
        download_comments=True,    # 댓글은 받아야 함
        save_metadata=False,
    )

    # --- 부계정 로그인 ---
    login_user = input("인스타 부계정 아이디: ").strip()
    password = getpass.getpass("비밀번호(화면에 안 보임): ")  # 입력 숨김
    try:
        L.login(login_user, password)
        print(f"✓ 로그인 성공: {login_user}\n")
    except Exception as e:
        print(f"✗ 로그인 실패: {type(e).__name__}: {e}")
        print("  2단계 인증(2FA)이 걸려 있으면 코드 입력이 필요할 수 있어요.")
        return

    # --- 대상 채널 프로필 (L1) ---
    print(f"[1] 프로필 정보 (L1): @{target_username}")
    try:
        profile = instaloader.Profile.from_username(L.context, target_username)
        print(f"    닉네임   : {profile.full_name}")
        print(f"    팔로워   : {profile.followers:,}")
        print(f"    게시물   : {profile.mediacount:,}")
        print(f"    계정유형 : {'비즈니스' if profile.is_business_account else '일반'}\n")
    except Exception as e:
        print(f"    ✗ 프로필 실패: {type(e).__name__}: {e}")
        return

    # --- 최근 포스트 1개 (L2) ---
    print("[2] 최근 포스트 (L2) — 1개만")
    try:
        post = next(profile.get_posts())
        print(f"    shortcode: {post.shortcode}")
        print(f"    좋아요   : {post.likes:,}")
        print(f"    댓글수   : {post.comments:,}")
        print(f"    유형     : {post.typename}\n")
    except Exception as e:
        print(f"    ✗ 포스트 실패: {type(e).__name__}: {e}")
        return

    # --- 댓글 + 작성자 ID (L3) ★ 핵심 관문 ★ ---
    print("[3] 댓글 (L3) — 5개만  ★ 진짜 확인하려는 것 ★")
    try:
        count = 0
        for comment in post.get_comments():
            owner = comment.owner
            # comment.owner 에서 username 과 고유 userid 추출
            uid = getattr(owner, "userid", None)
            uname = getattr(owner, "username", None)
            text = (comment.text or "").replace("\n", " ")[:40]
            print(f"    · userid={uid} | @{uname} | {text}")
            count += 1
            if count >= 5:
                break

        if count == 0:
            print("    (댓글이 없거나 못 가져옴)")
        else:
            print(f"\n    ✓ 댓글 작성자 ID 추출 성공! (userid + username)")
            print("      → YouTube의 authorChannelId처럼 중복률/고정 댓글러 계산 가능")
    except Exception as e:
        print(f"    ✗ 댓글 실패: {type(e).__name__}: {e}")
        print("      login_required / feedback_required 면 인스타가 봇으로 의심하는 것")
        return

    print("\n" + "=" * 50)
    print("✓ 테스트 통과 — Instagram도 댓글 작성자까지 수집 가능")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python insta_smoke_test.py <채널_username>")
        print("예시  : python insta_smoke_test.py nike")
        sys.exit(1)
    main(sys.argv[1])