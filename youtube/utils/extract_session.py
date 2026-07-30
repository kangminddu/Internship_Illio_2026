# utils/extract_session.py
"""
유튜브 내부 API(InnerTube) 구조 탐색용 조사 스크립트.

★ 파이프라인의 일부가 아니다. main.py가 이 파일을 실행하지 않는다.
   "유튜브가 데이터를 어떻게 내려주는가"를 알아내려고 만든 일회성 도구다.

왜 필요했나
------
크롤러를 짜려면 먼저 이걸 알아야 했다:
  1) 영상 목록 데이터가 HTML에 있나, API로 오나?
  2) API라면 어떤 엔드포인트이고 응답 구조는 어떻게 생겼나?
  3) 스크롤할 때 다음 페이지를 어떻게 요청하나? (continuation token)
  4) API를 직접 호출하려면 어떤 인증값이 필요한가? (ytcfg)

브라우저를 headless=False로 띄우고 네트워크를 전부 들여다보면서
이 답을 찾았다. 여기서 알아낸 구조가 youtube_parser.py의
parse_l2_videos / find_first 설계로 이어졌다.

무엇을 알아냈나
------
  - 영상 목록은 초기 HTML의 ytInitialData에 있고,
    스크롤하면 youtubei/v1/browse API가 추가분을 준다
  - 응답 구조가 lockupViewModel / videoRenderer 두 형태로 섞여 있다
    (유튜브가 UI를 바꾸는 중이라 페이지마다 다르다)
    → 그래서 파서가 두 키를 모두 찾도록 만들었다
  - continuation token으로 다음 페이지를 요청한다
    → 다만 최종 크롤러는 이 방식을 안 쓴다. L2a는 첫 페이지만 읽고
      끝낸다(가이드라인이 최대 15개만 요구하므로 무한 스크롤이 불필요)
  - ytcfg에 API 키/클라이언트 버전/visitorData가 들어있다
    → API를 직접 호출하는 방식도 검토했지만, 이 값들이 세션마다 바뀌고
      요청 서명이 필요해서 채택하지 않았다. HTML의 ytInitialData를
      정규식으로 뽑는 쪽이 훨씬 단순했다.

실행: python -m youtube.utils.extract_session
"""
import asyncio
import json

from playwright.async_api import async_playwright

# 조사 대상은 아무 채널이나 상관없다. mkbhd는 영상이 많아
# 무한 스크롤과 continuation 동작을 관찰하기 좋아서 골랐다.
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
    """중첩 JSON에서 영상 항목을 전부 뽑아낸다.

    두 키를 모두 찾는 이유:
      videoRenderer      구버전 레이아웃
      lockupViewModel    신버전 레이아웃
    유튜브가 UI를 교체하는 중이라 페이지/시점마다 다르게 온다.
    조사 단계에서 이걸 발견해서, 파서도 양쪽을 대비하게 만들었다.

    yield from 재귀: 경로를 모르니 트리 전체를 훑는다.
    (youtube_parser.find_first와 같은 발상. 다만 여기는 '전부' 수집)
    """
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
    """다음 페이지 요청용 토큰을 찾는다.

    유튜브 무한 스크롤의 작동 방식:
      1회 응답에 영상 30개 + 다음 페이지용 continuation token이 함께 온다.
      스크롤하면 그 토큰으로 youtubei/v1/browse를 다시 호출한다.

    → 토큰만 알면 브라우저 없이 API를 반복 호출해 전체 영상을 긁을 수 있다.
      실제로 그 방식도 검토했지만, 요청에 서명이 필요하고 인증값이
      세션마다 바뀌어서 유지보수 비용이 크다고 판단해 채택하지 않았다.
      (틱톡 L2는 반대로 이 방식을 쓴다 — /api/post/item_list/ XHR 가로채기)
    """
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
    """영상 항목 하나에서 제목/조회수/게시일을 꺼낸다.

    ★ 이 함수가 youtube_parser.parse_l2_videos의 원형이다.
      여기서 알아낸 경로:
        metadata → lockupMetadataViewModel → metadata
                 → contentMetadataViewModel → metadataRows

      그리고 metadataRows[0].metadataParts에 조회수와 게시일이
      '순서대로' 들어있다는 것도 여기서 확인했다.

      다만 최종 파서는 인덱스(parts[0], parts[1]) 대신
      텍스트 내용("조회수"가 포함됐나)으로 판별하도록 바꿨다.
      언어 설정이나 레이아웃에 따라 순서가 뒤바뀔 수 있어서.

    try/except로 통째로 감싼 이유: 조사용이라 구조가 안 맞는 항목이
    섞여 있어도 그냥 넘기고 나머지를 보는 게 목적이다.
    (실제 크롤러였다면 실패 원인을 남겨야 한다)
    """
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

        # headless=False. 조사 스크립트라 브라우저를 직접 봐야 한다.
        # (크롤러는 반대로 headless=True — 렌더링 부담을 줄이려고)
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
            """youtubei/v1/browse 요청을 관찰한다.
            어떤 URL로, 어떤 메서드로 부르는지 확인이 목적."""
            if "youtubei/v1/browse" not in req.url:
                return

            print("=" * 80)
            print("REQUEST")
            print(req.url)
            print(req.method)

            # post_data는 gzip이라 출력하지 않음
            # (요청 본문에 continuation token과 클라이언트 정보가 들어있는데,
            #  압축돼 있어 그냥 찍으면 깨진다)

        page.on("request", on_request)

        # --------------------------------------------------
        # Response
        # --------------------------------------------------
        async def on_response(res):
            """browse API 응답을 파싱해서 구조를 확인한다.

            page.on("response")로 API 응답을 가로채는 이 패턴이
            나중에 crawler_l3.py(youtubei/v1/next로 댓글 수집)와
            틱톡 L2(/api/post/item_list/)에서 그대로 쓰인다.
            """
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
                    print(f"{i+1}. {token[:80]}...")   # 토큰이 매우 길어 앞부분만

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
            wait_until="networkidle"    # 조사용이라 네트워크가 완전히 멎을 때까지 대기
        )

        # --------------------------------------------------
        # Session 정보
        # --------------------------------------------------
        # ytcfg에 InnerTube API를 직접 호출하는 데 필요한 값들이 들어있다.
        #   INNERTUBE_API_KEY      API 키
        #   INNERTUBE_CLIENT_VERSION  클라이언트 버전 (헤더에 넣어야 함)
        #   VISITOR_DATA           방문자 식별자
        #
        # → 이 값들로 브라우저 없이 API를 직접 부르는 방식을 검토했다.
        #   훨씬 빠르고 가볍지만, 값이 세션마다 바뀌고 요청 서명이 필요해
        #   유지보수 비용이 크다고 판단해 채택하지 않았다.
        #   대신 HTML의 ytInitialData를 정규식으로 뽑는 단순한 길을 택했다.
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

        # --------------------------------------------------
        # 무한 스크롤 관찰
        # --------------------------------------------------
        # 스크롤할 때마다 browse API가 호출되는지, 언제 멈추는지 확인.
        # scrollHeight가 안 늘어나면 바닥에 닿은 것 = 더 이상 영상이 없다.
        #
        # 이 '높이 변화 없음 = 종료' 패턴이 crawler_l3.py의 댓글 스크롤에서
        # '댓글 수가 안 늘어남 = 종료'로 응용된다.
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


# ⚠️ if __name__ 가드가 없다. import하면 브라우저가 뜬다.
#    조사용 일회성 스크립트라 그대로 뒀다.
asyncio.run(main())