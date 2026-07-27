from pathlib import Path
import json

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from instagram.config import context_kwargs

SESSION_DIR = Path("instagram/session")
SESSION_FILE = SESSION_DIR / "instagram.json"


def main():
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:

        browser = p.chromium.launch(
            executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            headless=False,
            slow_mo=100,
        )

        context = browser.new_context(**context_kwargs())

        page = context.new_page()

        # 필요하면 잠시 주석 처리해서 비교 테스트
        Stealth().apply_stealth_sync(page)

        print("=" * 60)
        print("Instagram Login")
        print("=" * 60)

        page.goto(
            "https://www.instagram.com/accounts/login/",
            wait_until="domcontentloaded",
        )

        print()
        print("브라우저에서 직접 로그인하세요.")
        print("2차 인증 및 One Tap 화면까지 모두 완료한 후")
        input("\n로그인이 끝났으면 Enter를 누르세요...")

        # 쿠키가 모두 저장될 시간을 조금 줌
        page.wait_for_timeout(5000)

        cookies = context.cookies()

        print("\n=== 저장될 쿠키 ===")
        print([c["name"] for c in cookies])

        has_session = any(c["name"] == "sessionid" for c in cookies)

        if has_session:
            print("\n✅ sessionid 확인됨")
        else:
            print("\n❌ sessionid가 없습니다. 로그인이 정상적으로 완료되지 않았을 가능성이 있습니다.")

        context.storage_state(path=str(SESSION_FILE))

        print()
        print("세션 저장 완료")
        print(SESSION_FILE.resolve())

        browser.close()


if __name__ == "__main__":
    main()