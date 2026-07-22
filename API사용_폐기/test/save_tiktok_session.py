from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False
    )

    context = browser.new_context()

    page = context.new_page()

    page.goto("https://www.tiktok.com")

    print("카카오 로그인 완료")
    print("그리고 아무 영상을 하나 누르세요.")
    print("댓글까지 보이게 만든 뒤 30초 정도 기다리세요.")

    input("\n30초 기다린 뒤 엔터")

    page.wait_for_timeout(30000)

    context.storage_state(
        path="code/tiktok.json"
    )

    print("저장 완료")

    input("\n브라우저 종료")

    browser.close()