from pathlib import Path
from playwright.sync_api import sync_playwright

SESSION_DIR = Path("instagram/session")
SESSION_FILE = SESSION_DIR / "instagram.json"


def main():

    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=False,
            slow_mo=100,
        )

        context = browser.new_context(
            viewport={"width": 1400, "height": 900}
        )

        page = context.new_page()

        print("=" * 60)
        print("Instagram Login")
        print("=" * 60)

        page.goto(
            "https://www.instagram.com/accounts/login/",
            wait_until="networkidle"
        )

        print()
        print("브라우저에서 직접 로그인하세요.")
        print("2차 인증까지 완료한 후")
        input("\n로그인이 끝났으면 Enter를 누르세요...")

        page.goto(
            "https://www.instagram.com/",
            wait_until="networkidle"
        )

        context.storage_state(path=str(SESSION_FILE))

        print()
        print(f"세션 저장 완료")
        print(SESSION_FILE.resolve())

        browser.close()


if __name__ == "__main__":
    main()