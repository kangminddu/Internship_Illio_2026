from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False
    )

    context = browser.new_context(
        storage_state="tiktok.json"
    )

    page = context.new_page()

    page.goto(
        "https://www.tiktok.com",
        wait_until="networkidle"
    )

    page.wait_for_timeout(5000)

    print(page.locator("body").inner_text()[:2000])

    input("\n엔터")

    browser.close()