import asyncio
import json

from playwright.async_api import async_playwright

URL = "https://www.youtube.com/@mkbhd/videos"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0.0.0 Safari/537.36"
)


# --------------------------------------------------
# 영상 추출
# --------------------------------------------------
def extract_videos(obj):

    if isinstance(obj, dict):

        if "videoRenderer" in obj:
            yield obj["videoRenderer"]

        if "lockupViewModel" in obj:
            yield obj["lockupViewModel"]

        for v in obj.values():
            yield from extract_videos(v)

    elif isinstance(obj, list):

        for v in obj:
            yield from extract_videos(v)


# --------------------------------------------------
# continuation 추출
# --------------------------------------------------
def extract_continuation(obj):

    if isinstance(obj, dict):

        if "continuationCommand" in obj:

            cmd = obj["continuationCommand"]

            token = cmd.get("token")

            if token:
                yield token

        for v in obj.values():
            yield from extract_continuation(v)

    elif isinstance(obj, list):

        for v in obj:
            yield from extract_continuation(v)


# --------------------------------------------------
# 영상 파싱
# --------------------------------------------------
def parse_video(video):

    try:

        md = video["metadata"]["lockupMetadataViewModel"]

        rows = (
            md["metadata"]
            ["contentMetadataViewModel"]
            ["metadataRows"]
        )

        views = ""
        published = ""

        if rows:

            parts = rows[0]["metadataParts"]

            if len(parts) >= 1:
                views = parts[0]["text"]["content"]

            if len(parts) >= 2:
                published = parts[1]["text"]["content"]

        return {
            "video_id": video.get("contentId"),
            "title": md["title"]["content"],
            "views": views,
            "published": published,
            "url": f"https://youtube.com/watch?v={video.get('contentId')}"
        }

    except Exception:
        return None


async def main():

    async with async_playwright() as p:

        browser = await p.chromium.launch(headless=False)

        context = await browser.new_context(
            user_agent=UA,
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            viewport={"width": 1920, "height": 1080},
        )

        page = await context.new_page()

        # --------------------------------------------------
        # Request
        # --------------------------------------------------
        async def on_request(req):

            if "youtubei/v1/browse" not in req.url:
                return

            print("=" * 80)
            print("REQUEST")
            print(req.url)
            print(req.method)

            # post_data는 gzip이라 출력하지 않음

        page.on("request", on_request)

        # --------------------------------------------------
        # Response
        # --------------------------------------------------
        async def on_response(res):

            if "youtubei/v1/browse" not in res.url:
                return

            print("\n" + "=" * 80)
            print("Browse Response")
            print("status =", res.status)

            data = await res.json()

            videos = list(extract_videos(data))
            tokens = list(extract_continuation(data))

            print()
            print("=" * 60)
            print("CONTINUATION TOKENS")
            print("=" * 60)

            if tokens:

                for i, token in enumerate(tokens):
                    print(f"{i+1}. {token[:80]}...")

            else:

                print("없음")

            print()
            print("=" * 60)
            print("영상 :", len(videos))
            print("=" * 60)

            for v in videos:

                info = parse_video(v)

                if info:
                    print(info)

        page.on("response", on_response)

        print("YouTube 접속")

        await page.goto(
            URL,
            wait_until="networkidle"
        )

        # --------------------------------------------------
        # Session 정보
        # --------------------------------------------------
        ytcfg = await page.evaluate("""
        () => ({
            apiKey: window.ytcfg.get("INNERTUBE_API_KEY"),
            clientVersion: window.ytcfg.get("INNERTUBE_CLIENT_VERSION"),
            visitorData: window.ytcfg.get("VISITOR_DATA")
        })
        """)

        print()
        print("=" * 60)
        print("SESSION")
        print("=" * 60)
        print(json.dumps(ytcfg, indent=2, ensure_ascii=False))
        print("=" * 60)

        await page.wait_for_timeout(3000)

        last_height = 0

        for i in range(40):

            await page.mouse.wheel(0, 4000)

            await page.wait_for_timeout(1000)

            height = await page.evaluate(
                "() => document.documentElement.scrollHeight"
            )

            print(f"\nScroll {i+1}  height={height}")

            if height == last_height:
                print("더 이상 증가 없음")
                break

            last_height = height

        print("완료")

        await browser.close()


asyncio.run(main())