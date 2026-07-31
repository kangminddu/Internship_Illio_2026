# Creator Fandom CRM

크리에이터 섭외 판단에 쓰이는 팬덤 지표를 만들기 위해, YouTube / Instagram / TikTok
세 플랫폼에서 채널·콘텐츠·댓글을 수집하고 파생 지표를 산출하는 Python 크롤링 파이프라인입니다.

2026년 하계 인턴십 과제로 약 5주간 단독 개발했습니다.

```
수집 실적
  YouTube    채널 7,246개 / 콘텐츠 223,392건 (롱폼 105,042 · 쇼츠 118,350)
  Instagram  콘텐츠 17,828건 / 파생지표 209채널
  TikTok     채널 334개
  크리에이터 26,160명 / 채널 11,494개
```

---

## 왜 만들었나

브랜드와 크리에이터를 연결하는 마케팅 업무에서, "이 크리에이터가 실제로 영향력이 있는가"를
판단할 근거가 필요했습니다. 팔로워 수만으로는 부족합니다. 가이드라인의 표현을 빌리면
**"다수의 1회성 참여"와 "소수의 반복 참여"를 구분**해야 합니다.

기존에는 라이브 스트리밍 플랫폼 중심으로 데이터를 관리하고 있었고, 최근 마케팅 수요가
YouTube / Instagram / TikTok으로 확장되면서 이 세 플랫폼의 데이터 기반이 필요해졌습니다.

### 공식 API를 쓰지 않은 이유

프로젝트 초기에는 각 플랫폼의 공식 API로 접근했습니다. 그러나

- 무료 할당량으로는 수천 개 채널을 다룰 수 없었고
- 유료 전환 시 비용이 발생하며
- Instagram / TikTok은 공식 API로 얻을 수 없는 지표가 많았습니다

이 시점에 "유료 대신 무료로, 더 어렵게 만들어보라"는 방향을 제시받아 API 기반 코드를 폐기하고
웹 페이지를 직접 파싱하는 방식으로 전환했습니다. 이 전환이 프로젝트의 성격을 바꿨습니다.
공식 API가 정제해주던 것들 — 레이트 리밋 관리, 재시도 정책, 실패 분류, 응답 구조 변경 대응 —
을 전부 직접 다뤄야 했습니다.

---

## 아키텍처

### 3-Layer 수집 구조

데이터 성격과 수집 비용에 따라 세 계층으로 분리했습니다.

| Layer | 수집 대상 | 갱신 주기 | 요청 비용 |
|---|---|---|---|
| **L1** | 채널 정보 (팔로워, 채널명, 생존 여부) | 주 1회 | 채널당 1~2 |
| **L2** | 콘텐츠 목록·성과 (조회수, 좋아요, 댓글수) | 주 1회 | 채널당 2~30 |
| **L3** | 댓글 → 팬 식별 | 1회 | 콘텐츠당 1 |

계층을 나눈 이유는 두 가지입니다.

**갱신 주기가 다릅니다.** 구독자 수는 계속 변하므로 주기적으로 다시 봐야 하지만,
댓글은 한 번 수집하면 됩니다.

**요청 비용이 8배 차이납니다.** YouTube를 예로 들면, 목록 페이지(L2a)는 채널당 2요청으로
콘텐츠 15개를 얻지만, 개별 영상 페이지(L2b)는 영상당 1요청입니다. 같은 채널의 같은 데이터에
8배의 요청이 필요합니다. 이 차이가 나중에 IP 차단으로 이어졌습니다.

### 파이프라인

```
seed → L1 → L2 → L3 → metric → export
엑셀   채널   콘텐츠  댓글   파생지표  Excel
```

플랫폼별 단계 구성이 조금씩 다릅니다.

```
YouTube  : seed → L1 → email → L2a → L2b → L3 → backfill → metric → export
Instagram: seed → L1 → import → L2 → L3 → metric → export
TikTok   : seed → L1 → L2 → L3 (+ metric/export 별도 실행)
```

- `email` — 유튜브 설명란의 치지직 링크를 따라가 연락처를 보강 (유튜브 전용)
- `import` — 인스타는 크롤 결과를 JSONL로 남기고 별도 단계에서 DB 적재
- `backfill` — 쇼츠 게시일이 L2b에서 채워진 뒤 활동성을 재판정 (유튜브 전용)

### 데이터베이스 (16 tables)

```
creators ─┬─ channels ─┬─ channel_snapshots   팔로워 시계열
          │            ├─ channel_metrics     산출 지표
          │            └─ contents ─┬─ content_snapshots  조회수/좋아요 시계열
          │                         └─ comments ── fans
          ├─ creator_emails
          └─ advocacy_snapshots
crawl_logs (독립)
```

**원본과 파생을 분리했습니다.** `contents`·`content_snapshots`는 수집한 그대로이고,
`channel_metrics`는 그것을 가공한 값입니다. 지표 공식이 바뀌어도 재크롤링 없이
다시 계산할 수 있습니다.

**시점 데이터는 스냅샷으로 쌓습니다.** 구독자 수나 조회수는 시점마다 다르므로
덮어쓰지 않고 `(channel_id, captured_at)` 유니크로 누적합니다.

**세 플랫폼이 같은 테이블을 공유**하고 `platform` 컬럼으로 구분합니다.
크로스 플랫폼 분석(같은 크리에이터의 YouTube/Instagram 팔로워 비교)이 가능한 대신,
플랫폼별 고유 필드는 NULL이 많습니다.

---

## 플랫폼별 기술 차이

세 플랫폼은 데이터를 주는 방식이 전혀 다릅니다. 이것이 크롤러 구조를 결정했습니다.

| | 데이터 위치 | 수집 방식 | 로그인 | 요청 간격 |
|---|---|---|---|---|
| **YouTube** | HTML 내 `ytInitialData` (댓글만 XHR) | requests (L3만 Playwright) | 불필요 | 1.2초 |
| **TikTok** | HTML(SSR) + XHR 혼재 | Playwright | L2/L3만 | 1~3초 |
| **Instagram** | GraphQL 응답 (게시물 페이지는 SSR) | Playwright | 필수 | 8~15초 |

### YouTube — HTML에 박힌 JSON

```html
<script>var ytInitialData = {"contents":{...}};</script>
```

화면에 표시될 모든 데이터가 JSON으로 통째로 들어 있습니다. 정규식으로 이 부분만 뽑아
`json.loads`하면 되므로 DOM 파싱이 필요 없습니다. 세 플랫폼 중 가장 다루기 쉬웠습니다.

예외는 댓글입니다. 초기 HTML에 없고 스크롤해야 `youtubei/v1/next` API가 호출되는 구조라
L3만 Playwright를 씁니다.

### TikTok — SSR/CSR 혼재

같은 URL이라도 요청마다 서버사이드 렌더링을 해줄 때와 안 해줄 때가 있습니다.
SSR이면 HTML에 데이터가 박혀 오고, 아니면 `/api/user/detail/` XHR로만 옵니다.
그래서 파서가 두 경로를 모두 봅니다.

### Instagram — GraphQL 가로채기

HTML에 데이터가 거의 없습니다. React가 GraphQL로 전부 가져오므로
`page.on("response")`로 응답을 가로채는 것이 유일한 경로입니다.

문제는 `/graphql/query` 하나로 수십 종류의 쿼리가 오간다는 점입니다.
요청 헤더의 `x-fb-friendly-name`으로 식별해야 하는데, **같은 데이터인데도 계정에 따라
다른 쿼리 이름이 옵니다.** (아래 트러블슈팅 참고)

---

## 핵심 설계

### 1. SQL 쿼리 하나로 구현한 Resume

수천 개 채널을 수 시간에 걸쳐 수집하므로 중단은 필연입니다.
각 단계가 `crawl_logs`에 성공·실패를 기록하고, 대상 선정 쿼리가 그것을 참조합니다.

```sql
SELECT channel_id, ...
FROM channels
WHERE platform = 'youtube'
  AND channel_id_status <> 'duplicate'
  AND channel_existence_status NOT IN ('deleted', 'suspended')
  AND channel_id NOT IN (
      SELECT channel_id FROM crawl_logs
      WHERE layer = 'L1' AND status = 'success'
        AND attempted_at >= NOW() - INTERVAL 7 DAY
  )
```

상태 파일도 체크포인트도 없습니다. **"최근 7일 내 성공 기록이 없는 채널"** 이 곧 재개 조건입니다.
몇 번을 실행하든 결과가 같고(멱등성), 언제 멈춰도 다시 실행하면 이어서 진행됩니다.

`attempted_at` 조건은 가이드라인의 "L1 주 1회 갱신" 요구에 대응합니다.
이 조건이 없으면 한 번 성공한 채널은 영원히 건너뛰어 시계열이 더 이상 쌓이지 않습니다.

### 2. "데이터 없음"과 "가져오지 못함"의 구분

이 프로젝트에서 가장 반복해서 만난 문제입니다.

네트워크 오류로 목록을 못 가져왔는데 "이 채널은 콘텐츠가 0개"로 처리하면 어떻게 될까요.
그 채널은 비활성으로 분류되고, 지표 계산에서 빠지고, resume 로직 때문에 다시 수집되지도 않습니다.
**활발한 크리에이터가 조용히 사라집니다.**

파이썬에서 `None`과 `[]`는 둘 다 falsy이므로 `if not items:`로는 구분되지 않습니다.
명시적으로 반환 규약을 정의했습니다.

```python
def fetch_tab(crawl_url, tab, parser):
    """items=None 이면 실패, items=[] 는 '정상인데 콘텐츠 없음'."""
```

```python
if videos is None or shorts is None:
    record_failure(...)   # 활동 상태를 건드리지 않는다 → 다음 실행에서 재시도
    break
```

`inactive` 판정은 **"두 탭 모두 HTTP 200 + 정말 콘텐츠 0개"** 일 때만 내립니다.

실패도 확정 여부에 따라 나눕니다.

```python
def classify_existence(r):
    if sig == "channel_banned":   return "suspended", "channel_banned"   # 확정
    if code == 404:               return "deleted",   "http_404"         # 확정
    if code == 403:               return "unknown",   "http_403"         # 미확정 → 재시도
    if et == "retriable_timeout": return "unknown",   et                 # 미확정 → 재시도
```

확정된 실패는 영구 제외, 불확실한 실패는 다음 실행에서 재시도합니다.

### 3. 전역 Rate Limiting

플랫폼별로 차단 기준이 달라 각각 다른 전략을 씁니다. YouTube의 경우 세 가지를 동시에 관리합니다.

```python
class RateController:
    self._next_at       # ① 요청 간 최소 간격 + 지터
    self._pause_until   # ② 429 지수 백오프 (전 워커 공동 정지)
    self._warmup_left   # ③ 휴식 복귀 후 slow start
```

**전역이라는 점이 핵심입니다.** 워커마다 `sleep`을 걸면 워커 4개일 때 실제 속도는 4배가 되어,
병렬을 늘릴수록 차단 위험이 커집니다. 인스턴스 하나를 모든 워커가 공유하고 lock으로
직렬화하므로 워커 수와 무관하게 IP 기준 속도가 고정됩니다.

세 번째가 특히 반직관적이었습니다. **초당 속도가 아무리 느려도 8시간을 연속으로 요청하면
차단됩니다.** 그래서 2,000요청마다 35분씩 쉬며 세션을 잘게 끊습니다.

```python
jitter = random.uniform(0, interval * 0.3)
```

정확히 1.2초마다 요청하면 기계적으로 규칙적이라 봇으로 탐지됩니다. 랜덤을 섞습니다.

---

## 트러블슈팅

프로젝트에서 실제로 겪고 해결한 문제들입니다.

### 시간대 9시간 오차 — 에러 없이 진행된 데이터 오염

서버가 UTC로 설정되어 있었고 크롤러도 UTC를 명시해 저장하고 있었습니다.
DB의 모든 시각이 실제보다 9시간 이르게 들어갔지만, **에러도 나지 않고 데이터도 다 있어서
화면상으로는 아무 이상이 없었습니다.**

Instagram 원본 게시물의 시각과 DB 값을 하나씩 대조하고서야 발견했습니다.

원인은 pymysql이 datetime을 전송할 때 tzinfo를 버리고 숫자만 문자열로 만드는 것이었습니다.

```python
datetime.now(timezone.utc)   # 2026-07-28 01:00:00+00:00
# pymysql이 만드는 문자열 → '2026-07-28 01:00:00'  (오프셋 소실)
# MySQL(+09:00)이 해석    → 2026-07-28 01:00 KST
# 실제 시각               → 2026-07-28 10:00 KST
```

서버 시간대를 KST로 변경하고, 기존 데이터를 보정하고, 세 플랫폼 코드를 전부 수정한 뒤
재수집하여 실제 게시일과 대조 검증했습니다.

### TikTok 실패율 급증 — 통제 변수 9개 배제

7월 13일에는 354개 채널을 18분에 무결점으로 수집했습니다(분당 18~20건).
그런데 16일 뒤 **같은 코드가 30~50% 실패**하기 시작했습니다.

원인을 좁히기 위해 변수를 하나씩 통제하며 배제했습니다.

| 변수 | 결과 |
|---|---|
| CPU / 메모리 | 무관 (사용률 21%, 여유 2.2GB) |
| IP 3종 (EC2 / 가정용 / 모바일 핫스팟) | 무관 (실패율 동일) |
| 로그인 상태 | **유의미** (아래 참고) |
| 브라우저 컨텍스트 재사용 | 무관 (재사용 50% vs 매번 새로 20%) |
| 캐시 / 쿠키 삭제 | 무관 |
| 동시성 (worker 1~3) | 무관 |
| 요청 간격 (3초 ~ 30초) | 무관 |
| 리소스 차단 | 무관 (60% vs 50%) |
| headless / 실행 옵션 | 무관 (완전 기본 설정에서도 실패) |

성공·실패 HTML을 직접 대조한 결과, 실패 시에는 TikTok이 서버사이드 렌더링을 하지 않고
껍데기만 내려주고 있었습니다. `/api/user/detail/`도 HTTP 200에 빈 본문이었고,
화면에는 "문제가 발생했습니다"가 표시됐습니다.
TikTok 공식 문서도 이 화면을 서버 일시 오류로 안내합니다.

**통제 불가능한 실패**로 판단하고 재시도로 흡수하되, 실패 유형을 구분해 기록하도록 했습니다.

```python
if SCRIPT_ID not in html:
    etype = "server_error" if SERVER_ERROR_TEXT in html else "blocked"
    log_result(conn, cid, url, "failed", etype, ...)   # channels는 건드리지 않음
```

조사 과정에서 **로그인 상태가 오히려 불리하다**는 것도 발견했습니다.
L1은 공개 프로필만 읽는데 L2/L3와 브라우저 프로필을 공유하면서 불필요한 로그인 상태를
가져갔습니다. TikTok은 로그인 계정이 단시간에 수백 개 프로필을 조회하면 계정 단위로
제한을 걸어 데이터를 주지 않습니다.

```
비로그인 성공 : 384KB, 프로필 데이터가 HTML에 렌더링됨
로그인 실패   : 102KB, 로그인 UI만 있고 데이터 없음
```

L1을 비로그인으로 전환하니 실패가 크게 줄었습니다. **인증이 항상 유리한 것은 아니었습니다.**

### Instagram 6,500건 오분류 — 200 응답으로 위장한 차단

레이트 리밋에 걸리면 Instagram은 빈 페이지를 돌려줍니다.
**HTTP 200이고 URL도 그대로이며 리다이렉트도 없습니다.**

기존 코드는 이것을 "댓글 0개"로 오인해 6,500건을 헛돌며 `empty`로 기록했습니다.
로그상으로는 모두 정상이라 며칠간 발견하지 못했고, resume 로직이 그것을 영구 제외했습니다.

3중 감지로 해결했습니다.

```python
# ① HTTP 4xx/5xx
if resp and resp.status >= 400:
    _mark_blocked(f"http_{resp.status}")

# ② goto 예외 (ERR_HTTP_RESPONSE_CODE_FAILURE 등)
if _is_block_error(e):
    _mark_blocked(f"goto_error:{type(e).__name__}")

# ③ 정상 페이지라면 반드시 있어야 할 구조의 부재
if not result["blocked"] and not captured["comments"]:
    if not result["html"] or COMMENTS_ROOT not in result["html"]:
        _mark_blocked("no_ssr_payload")
```

세 번째가 핵심입니다. 눈에 보이는 신호가 전혀 없을 때는
**"정상이라면 있어야 할 것이 없다"** 로 판별하는 수밖에 없었습니다.
댓글이 0개인 게시물도 `comments__connection` 구조 자체는 존재합니다.

### Instagram GraphQL 쿼리 이름 — 193개 계정 누락

게시물이 0개로 기록된 계정 193개를 조사해보니 실제로는 게시물이 있었습니다.

Instagram은 요청 헤더의 `friendly-name`으로 쿼리를 구분하는데,
**게시물이 적어 스크롤이 발생하지 않는 계정에는 다른 이름의 쿼리가 나갑니다.**
페이지네이션이 없으니 pagination 쿼리가 오지 않은 것입니다.
크롤러가 응답을 받고도 알아보지 못한 셈입니다.

```python
L2_POSTS_QUERY_NAMES = {
    "PolarisProfilePostsQuery",                       # 소형 계정
    "PolarisProfilePostsTabContentQuery",
    "PolarisProfilePostsTabContentQuery_connection",   # 페이지네이션 발생 시
}
```

재발 방지 장치를 넣었습니다. "게시물 0개"인데 목록 쿼리를 하나도 잡지 못했고
등록되지 않은 쿼리 이름이 관측되면 경고를 출력하고, 실행 종료 시 미등록 이름을
빈도순으로 요약합니다.

```
[L2] 미등록 friendly-name (config 확인 필요):
         42회  PolarisProfileXXXQuery
```

또한 릴스가 그리드에 노출되지 않는 계정이 **725개 중 365개(50%)** 였습니다.
릴스 탭을 항상 별도로 방문하도록 변경했고, 같은 게시물이 두 탭에서 서로 다른 필드를
주기 때문에(그리드는 게시일·캡션, 릴스 탭은 조회수) 중복 시 병합하도록 했습니다.

```python
if eid in captured["seen"]:
    prev = captured["by_id"].get(eid)
    for k, v in p.items():
        if v is not None and prev.get(k) is None:
            prev[k] = v      # 빈 칸만 채운다
    continue
```

### YouTube IP 차단 — 구조적 한계 확인

L1과 L2a는 429 없이 완료했습니다(채널 7,246개, 콘텐츠 223,392건, 약 14시간).
그러나 L2a 완료 직후 L2b를 시작하자 **5초 만에 429를 7회 받고 중단**됐고,
결국 서버 IP가 차단됐습니다.

```
requests           → 429
Playwright Chromium → 429 (Google sorry 페이지)
실제 설치된 Chrome  → 429
동일 코드, 가정용 IP → 200 + 정상 데이터
```

세 가지 클라이언트로 확인한 결과 **클라이언트 종류가 아니라 IP 단위 판정**이며,
며칠이 지나도 해제되지 않았습니다.

원인은 요청 구조입니다.

```
L2a : 채널당 2요청 → 콘텐츠 15개 확보
L2b : 영상당 1요청 → 콘텐츠 1개      = 8배
```

목록 페이지와 개별 영상 페이지에 적용되는 제한도 다릅니다.
**한 단계에서 검증된 안전 마진을 다른 단계에 그대로 적용할 수 없었습니다.**

다만 백오프와 자동 중단이 설계대로 작동해 데이터 오염 없이 멈췄고,
resume 덕분에 손실도 없었습니다.

### 그 외

<details>
<summary>Shorts 게시일 부재로 인한 활동성 판정 왜곡</summary>

YouTube 쇼츠 탭에는 업로드 날짜가 표시되지 않습니다(조회수만 있음).
따라서 쇼츠의 게시일은 개별 영상 페이지(L2b)에서만 알 수 있는데,
활동성 판정은 L2a 시점에 이루어집니다.

실측 결과 쇼츠 118,350건이 전부 `published_at` NULL이었고,
활동성이 롱폼만 반영해 판정된 상태였습니다.
"1년 이상 미업로드"로 분류된 2,220개 중 **1,395개가 쇼츠 보유 채널**이었습니다.

게다가 그렇게 분류되면 L2b 대상에서 제외되어 쇼츠 게시일을 영영 받지 못하고
재판정도 불가능한 순환에 갇힙니다.

세 겹으로 대응했습니다.
1. 게시일 미상 쇼츠가 10개 이상이면 `dormant`로 단정하지 않음
2. `dormant` 채널도 L2b가 쇼츠만 수집하도록 분기
3. 로그 레이어를 `L2b_shorts`로 분리해 재판정 후 롱폼도 수집되도록

근본 원인은 **판정에 필요한 데이터가 L2b에서 나오는데 판정은 L2a에서 한다**는 것입니다.
L2b가 대상 선정에 활동성을 사용하므로 순환 참조가 발생하고,
그래서 잠정 판정 → backfill 확정의 2단계 구조가 되었습니다.
</details>

<details>
<summary>channel_metrics가 0행이던 문제</summary>

지표 테이블이 비어 있었는데, AUTO_INCREMENT는 12,000을 넘어 있었습니다.
그동안의 INSERT가 전부 실패했다는 흔적이었습니다.

원인이 둘이었습니다.
1. `UNIQUE KEY(channel_id)`가 채널당 1행만 허용 → 재계산 시 INSERT 실패
2. 활동성이 전부 `unknown`이라 지표 계산 대상 필터에 하나도 걸리지 않음

인덱스를 제거하고, 이미 수집된 데이터로 활동성을 소급 재판정하는
`backfill_activity.py`를 만들었습니다. 재크롤링 없이 DB만 읽고 씁니다.

```python
from youtube.crawler.crawler_l2a import classify_activity   # 판정 로직 재사용
```

다만 이때 대상을 "유튜브 채널 전체"로 잡으면, 아직 수집되지 않은 채널까지
`inactive`로 덮입니다. `classify_activity()`는 근거가 없으면 `inactive`를 반환하기 때문입니다.
**로직을 재사용해도 전제 조건까지 함께 오지는 않습니다.**

```sql
AND EXISTS (SELECT 1 FROM contents c WHERE c.channel_id = ch.channel_id)
```
</details>

<details>
<summary>동시성 — TOCTOU 대응</summary>

시드 엑셀에 같은 채널이 `@handle`과 `/c/name` 두 형태로 들어오면 별개 행이 되지만,
L1이 열어보면 같은 UC ID입니다. `channels`에 UNIQUE 제약이 있어 두 번째 UPDATE가
1062 에러로 크래시했습니다.

사전 검사만으로는 막을 수 없습니다. 워커 4개가 동시에 돌기 때문입니다.

```
워커 A: SELECT → 중복 없음 확인
워커 B: SELECT → 중복 없음 확인   (A가 아직 커밋하지 않음)
워커 A: UPDATE → 성공
워커 B: UPDATE → 1062 에러
```

검사(check)와 사용(use) 사이에 상태가 바뀌는 TOCTOU 문제입니다.
사전 검사는 최적화이고, **실제 보증은 DB 제약이 한다**는 원칙으로
`IntegrityError`를 잡아 duplicate로 처리하는 2차 방어를 두었습니다.
</details>

---

## 산출 지표

가이드라인에 정의된 파생 지표를 산출합니다.

| 지표 | 산식 |
|---|---|
| 활성 팬덤 비율 (VPF) | 평균 조회수 ÷ 팔로워 × 100 |
| 참여율 (ER) | (평균 좋아요 + 평균 댓글) ÷ 분모 × 100 |
| 충성도 (Loyalty) | (평균 댓글 × 10 + 평균 좋아요) ÷ 분모 |
| 업로드 빈도 | 기간 내 콘텐츠 수 ÷ 주 |
| 댓글 작성자 중복률 | 2개 이상 콘텐츠에 댓글 단 계정 ÷ 전체 계정 × 100 |
| 고정 댓글러 수 | 수집 콘텐츠의 절반 이상에 댓글 단 계정 수 |
| 평균 댓글 길이 | 전체 댓글 길이 합 ÷ 댓글 수 |

**ER 분모가 플랫폼마다 다릅니다.** YouTube와 TikTok은 평균 조회수를 쓰지만,
Instagram은 피드/캐러셀에 조회수를 제공하지 않으므로 팔로워 수를 씁니다.
가이드라인도 이렇게 규정하고 있습니다.

이상치 처리도 플랫폼별로 다릅니다.

```python
def trimmed_mean(values, trim_ratio=0.1, min_sample=3):
    """상하위 10% 절사 평균. 표본이 부족하면 단순 평균, 0이면 None."""
```

크리에이터의 콘텐츠 성과는 편차가 극심합니다. 100만 조회 영상 하나가 나머지 14개의
평균을 통째로 왜곡합니다. `None`을 0으로 치환하지 않고 평균에서 제외하는 것도 중요합니다.
**"값이 0"과 "값을 모름"은 다릅니다.**

콘텐츠 유형도 분리해 계산합니다.

```
롱폼 / 쇼츠  ×  광고 / 일반  =  4분할
```

"이 크리에이터가 광고 콘텐츠에서도 성과를 유지하는가"가 섭외 판단의 핵심이기 때문입니다.

---

## 프로젝트 구조

```
Internship_Illio_2026/
│
├── youtube/
│   ├── main.py                    파이프라인 오케스트레이터 (subprocess)
│   ├── config.py                  설정 (git 제외)
│   ├── crawler/
│   │   ├── seed.py                엑셀 → DB 적재
│   │   ├── crawler_l1_parallel.py 채널 정보 (ThreadPool)
│   │   ├── crawler_l2a.py         영상/쇼츠 목록 + 활동성 판정
│   │   ├── crawler_l2.py          영상 개별 페이지 (L2b)
│   │   ├── crawler_l3.py          댓글 (Playwright)
│   │   ├── chzzk_email.py         치지직 연락처 보강
│   │   └── lib/
│   │       ├── youtube_parser.py  HTML → dict (L1/L2a/L2b 공용)
│   │       ├── rate_control.py    전역 속도 제어
│   │       └── youtube_url_filter.py
│   ├── metrics/
│   ├── export/
│   └── backfill_activity.py       활동성 소급 재판정
│
├── instagram/
│   ├── main.py
│   ├── login.py                   세션 생성 (storage_state)
│   ├── reader.py                  엑셀 파싱 (DB 무관)
│   ├── steps/
│   │   ├── l1.py                  프로필 → JSONL
│   │   ├── import_l1.py           JSONL → DB
│   │   ├── l2.py                  게시물 + 릴스
│   │   └── l3.py                  댓글 + 미디어 메타 보강
│   ├── lib/                       GraphQL 파서
│   ├── metric/
│   └── export/
│
├── tiktok/
│   ├── main.py                    asyncio.run 직접 호출
│   ├── login.py                   세션 생성 (persistent profile)
│   ├── steps/l1.py, l2.py, l3.py
│   ├── antibot/
│   │   ├── browser.py             컨텍스트 생성 (옵션 일치 보장)
│   │   ├── stealth.py             지문 위조
│   │   ├── behavior.py            마우스/스크롤 흉내
│   │   ├── not_found.py           계정 없음 판별 (statusCode 기반)
│   │   └── captcha.py             CAPTCHA 감지
│   ├── metrics/
│   └── export/
│
└── sql/fandom_crm_schema.sql
```

플랫폼별로 완전히 독립적입니다. 공유하는 것은 DB 스키마뿐이고 코드는 서로 import하지 않습니다.
세 플랫폼의 HTML 구조, 인증 방식, 봇 대응이 전부 달라 공통화가 어려웠습니다.

---

## 실행

### 요구 사항

- Python 3.11+
- MySQL 8.0+ / MariaDB 10.5+
- Google Chrome (Instagram / TikTok 로그인)

```bash
pip install -r requirements.txt
playwright install
mysql -u root -p < sql/fandom_crm_schema.sql
```

각 플랫폼 디렉터리에 `config.py`를 작성합니다 (`config_example.py` 참고).

### 실행

```bash
# 시드 적재
python -m youtube.main --file SNS_정보.xlsx
python -m instagram.seed
python -m tiktok.seed --dry-run    # 미리보기
python -m tiktok.seed

# 로그인 (Instagram / TikTok)
python -m instagram.login
python -m tiktok.login

# 단계별 실행
python -m youtube.main --l1
python -m youtube.main --from l2a     # l2a부터 끝까지
python -m youtube.main                # 전체

python -m instagram.main --l1
python -m tiktok.main --l1 --limit 10 # 테스트
```

### 주요 옵션

| 옵션 | 설명 |
|---|---|
| `--l1` / `--l2` / `--l3` | 특정 단계만 실행 |
| `--from <stage>` | 해당 단계부터 끝까지 |
| `--limit N` | 대상 N개만 (테스트) |
| `--channel <handle>` | 단일 채널 (디버그) |
| `--all` | 이미 성공한 대상도 재수집 (TikTok) |

### 진행 확인

```sql
SELECT ch.platform, cl.layer, cl.status, COUNT(*)
FROM crawl_logs cl JOIN channels ch ON ch.channel_id = cl.channel_id
GROUP BY 1, 2, 3;

SELECT platform, channel_activity_status, COUNT(*)
FROM channels GROUP BY 1, 2;
```

---

## 알려진 한계

발견했으나 해결하지 못한 것들을 기록합니다.

**IP 차단 (YouTube L2b)** — 영상 개별 페이지 수집은 단일 IP로 불가능합니다.
프록시 로테이션 또는 수집 범위 축소(활동 채널만 + 영상 수 15→5)가 필요합니다.
후자를 적용하면 요청량이 100,000건에서 7,500건으로 줄어듭니다.

**channel_metrics의 시계열 설계와 코드 불일치** — 유니크 인덱스를 제거해 시계열이
가능해졌으나, 계산 코드는 매번 DELETE 후 재생성하고 조회 코드는 `MAX()`로 집계합니다.
Instagram만 "오늘 계산분만 삭제"로 실제 시계열을 쌓습니다.

**활동성 판정 시점의 플랫폼 간 불일치** — YouTube는 L2a 잠정 + backfill 확정,
TikTok은 L2에서 확정, Instagram은 지표 계산 시점입니다.
Instagram은 임시 위치이며, 그 결과 L2 이후 유입된 채널 269개가 `unknown`으로 남아 있습니다.

**Loyalty Score 스케일 불일치** — YouTube와 Instagram에 `×100`이 초과 적용되어
TikTok과 값의 스케일이 다릅니다. 플랫폼 간 직접 비교가 불가능합니다.
수정 시 기존 산출물과 100배 차이가 나므로 결정이 필요합니다.

**주 1회 갱신 요구 미충족** — 가이드라인은 L1/L2의 주 1회 갱신을 요구하나
현재 속도로는 전량 1회 순회에 약 7일이 소요됩니다. 갱신 주기 차등이 필요합니다.

**미구현 영역** — 멀티 플랫폼 팔로워 편차, 외부 커뮤니티 규모, 커뮤니티 언급량은
별도 크롤러가 필요하고 공개 데이터 원칙과 충돌 소지가 있어 구현하지 않았습니다.

**코드 정리** — 동일한 rate limiter가 세 벌 존재합니다(L1 자체 구현 / `rate_control.py` /
L3 asyncio 버전). `antibot/` 의 프록시 로테이션 모듈 400여 줄은 호출되지 않습니다.
`metrics`, `export` 모듈은 `main()` 함수와 진입점 가드가 없어 import 시 즉시 실행됩니다.

---

## 배운 것

가장 크게 배운 것은 코드를 작성하는 방법이 아니라 **데이터를 의심하는 방법**이었습니다.

이 프로젝트에서 발견한 문제 대부분은 "정상으로 보이는 상태"에서 진행되고 있었습니다.
시간대가 9시간 어긋난 채 저장되고 있었고, 지표 테이블이 비어 있었고,
레이트 리밋에 걸린 요청이 "댓글 0개"로 기록되고 있었습니다.
에러 로그에는 아무것도 남지 않았습니다.

**에러 없이 실행되는 코드가 정상 동작하는 코드는 아니며,
결과를 원본과 대조해 검증하기 전까지는 아무것도 확신할 수 없다**는 것을 몸으로 익혔습니다.

그리고 크롤러에서 실패를 어떻게 기록하느냐가 데이터 신뢰도를 결정한다는 것도 배웠습니다.
"데이터가 없다"와 "가져오지 못했다"를 구분하지 못하면, 실패가 조용히 데이터로 굳어집니다.