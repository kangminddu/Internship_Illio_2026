# 프로젝트 개요

현재 TikTok 크롤러를 Python + Playwright 기반으로 개발하고 있습니다.

기존에는 HTML 내부의 SIGI_STATE나 NEXT_DATA를 파싱하여 데이터를 수집했지만, TikTok의 웹 구조가 변경되면서 더 이상 해당 객체들이 존재하지 않습니다.

그래서 현재는 브라우저가 실제로 호출하는 내부 API를 분석하여 안정적으로 데이터를 수집하는 방식으로 변경하려고 합니다.

---

# 목표

다음과 같은 프로필 페이지에서

https://www.tiktok.com/@nba

다음 정보를 안정적으로 수집하고 싶습니다.

- 사용자 정보
- 영상 목록
- 영상 메타데이터
- 이후 댓글, 통계 등 확장 가능하도록 설계

---

# 개발 환경

- Python
- Playwright (async)
- macOS
- Safari User-Agent
- 비로그인 상태
- Chromium 사용

---

# 현재 방식

Playwright에서 모든 Network Response를 감시하고 있습니다.

예를 들어

```python
async def on_response(response):

    print("="*120)
    print(response.status, response.url)

    headers = response.headers
    print("Content-Type :", headers.get("content-type"))
    print("Content-Length:", headers.get("content-length"))

    try:
        body = await response.text()
        print("Body Length :", len(body))
        print(body[:2000])
    except Exception as e:
        print("Body 읽기 실패 :", e)

page.on("response", on_response)