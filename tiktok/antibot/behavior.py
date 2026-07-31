# tiktok/antibot/behavior.py
"""
사람처럼 보이는 마우스/스크롤 행동.

왜 필요한가
------
틱톡은 브라우저 지문(stealth가 담당)뿐 아니라 '행동 패턴'도 본다.
    - 마우스가 전혀 안 움직임
    - 페이지 로드 즉시 클릭
    - 정확히 같은 간격으로 스크롤
    - 항상 같은 순서의 동작
이런 건 지문을 아무리 위장해도 봇으로 잡힌다.

★ 이 모듈의 핵심은 random_dwell()이다.
------
초기 코드는 L3에 이런 게 인라인으로 있었다:
    마우스 이동 1회 → 휠 스크롤 → 대기
매 영상마다 똑같은 순서로 똑같은 동작을 했다.
"랜덤 대기"를 넣어도 '동작의 종류와 순서'가 고정이면 패턴이 남는다.

→ 단위 동작을 함수로 쪼개고, 확률적으로 골라 순서를 섞어 실행한다.
  호출할 때마다 조합이 달라진다.

유튜브 크롤러에는 이런 모듈이 없다. requests 기반이라
'행동'이라는 개념 자체가 없기 때문.
"""
import random
import asyncio


DEFAULT_VIEWPORT = {"width": 1280, "height": 720}


def _viewport(page):
    """viewport_size가 None인 환경(persistent context 등) 대비 폴백.

    launch_persistent_context는 viewport를 kwargs로 넘겨도
    page.viewport_size가 None으로 나오는 경우가 있다.
    None이면 아래 마우스 좌표 계산에서 TypeError가 난다.
    """
    return page.viewport_size or DEFAULT_VIEWPORT


async def human_pause(a=0.5, b=2.0):
    """랜덤 대기. 모든 행동의 기본 단위."""
    await asyncio.sleep(random.uniform(a, b))


# -------------------------------------------------------
# 기본 행동 (단위 동작)
#
# 각각을 독립 함수로 쪼갠 이유: random_dwell이 이들을
# 레고 블록처럼 조합할 수 있게 하려고.
# -------------------------------------------------------

async def mouse_wander(page, moves=None):
    """마우스를 여기저기 자연스럽게 움직인다."""
    vp = _viewport(page)
    width, height = vp["width"], vp["height"]
    # 가장자리는 피한다. 사람은 브라우저 UI 근처로 마우스를 잘 안 보낸다.
    margin = min(100, width // 4, height // 4)

    if moves is None:
        moves = random.randint(1, 4)

    for _ in range(moves):
        await page.mouse.move(
            random.randint(margin, width - margin),
            random.randint(margin, height - margin),
            steps=random.randint(15, 35),
            # ★ steps가 핵심이다.
            #   기본값(1)이면 커서가 순간이동한다 — 명백한 봇 신호.
            #   steps=20이면 Playwright가 그 사이를 20단계로 나눠 이동시켜
            #   mousemove 이벤트가 20번 발생한다. 궤적이 생긴다.
        )
        await human_pause(0.2, 0.7)


async def scroll_down(page, amount=None):
    """아래로 스크롤."""
    if amount is None:
        amount = random.randint(80, 400)
    await page.mouse.wheel(0, amount)
    await human_pause(0.3, 1.2)


async def scroll_up(page, amount=None):
    """위로 스크롤 (사람은 되돌아보기도 함).

    봇은 아래로만 간다. 위로 되돌아가는 건 사람의 특징이다.
    """
    if amount is None:
        amount = random.randint(60, 300)
    await page.mouse.wheel(0, -amount)
    await human_pause(0.3, 1.0)


async def wiggle_scroll(page):
    """아래로 갔다가 살짝 위로 — 망설이는 스크롤.

    뭔가를 지나쳐서 되돌아보는 동작. 사람에게 흔하다.
    """
    await page.mouse.wheel(0, random.randint(150, 400))
    await human_pause(0.2, 0.6)
    await page.mouse.wheel(0, -random.randint(40, 120))
    await human_pause(0.3, 0.9)


async def dwell(page, a=1.5, b=4.0):
    """영상을 보는 척 그냥 머무른다 (아무 동작 없음).

    '아무것도 안 하는 것'도 행동이다.
    계속 뭔가를 하는 것 자체가 부자연스럽다.
    """
    await human_pause(a, b)


async def idle_micro(page):
    """아주 짧게 멈칫 — 화면 보는 척."""
    await human_pause(0.4, 1.2)


# -------------------------------------------------------
# 랜덤 조합 — 매번 다른 행동을 다른 순서로
# -------------------------------------------------------

async def random_dwell(page):
    """영상 페이지에서 사람처럼 '어슬렁거리는' 행동.

    여러 단위 동작을 확률적으로 골라, 랜덤한 순서로 실행한다.
    매 호출마다 조합이 달라져서 규칙적인 봇 패턴을 피한다.
    (마우스만 반복하던 기존 인라인 코드를 대체)

    두 겹의 랜덤성:
      ① 어떤 동작을 할지 — 확률로 후보 선정
      ② 어떤 순서로 할지 — shuffle
    → 이론상 2^6 × 순열 조합. 같은 패턴이 반복될 확률이 매우 낮다.
    """
    actions = []

    # 각 행동을 확률적으로 후보에 넣음 (항상 전부 하지 않음)
    # 확률은 '사람이 그 행동을 할 법한 빈도'에 맞췄다.
    # 마우스 움직임은 거의 항상(0.75), 위로 스크롤은 가끔(0.30).
    if random.random() < 0.75:
        actions.append(lambda: mouse_wander(page))
    if random.random() < 0.55:
        actions.append(lambda: scroll_down(page))
    if random.random() < 0.30:
        actions.append(lambda: scroll_up(page))
    if random.random() < 0.25:
        actions.append(lambda: wiggle_scroll(page))
    if random.random() < 0.45:
        actions.append(lambda: dwell(page, 1.2, 3.5))
    if random.random() < 0.20:
        actions.append(lambda: idle_micro(page))

    # 후보가 하나도 없으면 최소 하나는 하도록 (완전 무행동 방지)
    # 확률상 드물지만(약 1.5%), 아무 동작 없이 바로 클릭하면
    # 그게 오히려 봇 신호다.
    if not actions:
        actions.append(lambda: mouse_wander(page, moves=1))

    # 순서를 섞어서 실행
    random.shuffle(actions)
    for act in actions:
        try:
            await act()
        except Exception:
            pass  # 페이지 닫힘 등은 무시하고 계속
            # 위장 행동이 실패했다고 수집을 중단할 이유는 없다.


# -------------------------------------------------------
# 하위 호환 (기존 이름 유지 — L2 등에서 씀)
#
# random_dwell을 만들기 전부터 L2가 쓰던 함수들.
# 이름을 바꾸면 L2를 고쳐야 해서 그대로 뒀다.
# -------------------------------------------------------

async def arrive(page):
    """페이지 진입 후 사람이 페이지를 읽는 것처럼 잠시 머무른다.

    L2가 채널 페이지를 연 직후 호출한다.
    goto 직후 바로 스크롤하면 로딩도 안 끝났는데 조작하는 셈이라
    부자연스럽다.
    """
    await human_pause(1.2, 3.0)
    await mouse_wander(page, moves=random.randint(2, 5))


async def small_scroll(page):
    """페이지를 조금 훑어보는 행동."""
    for _ in range(random.randint(1, 3)):
        await page.mouse.wheel(0, random.randint(80, 250))
        await human_pause(0.4, 1.2)


async def feed_scroll(page):
    """피드를 크게 한 번 넘기는 행동.

    L2의 영상 목록 스크롤에 쓴다.
    small_scroll(80~250px)과 달리 1500~3500px로 크게 넘긴다.
    영상 목록은 그리드라 한 번에 여러 줄을 넘겨야 다음 배치가 로드된다.
    """
    await page.mouse.wheel(0, random.randint(1500, 3500))
    await human_pause(1.0, 2.5)


async def hover(page, locator):
    """클릭 전 살짝 마우스를 올린다.

    사람은 버튼을 클릭하기 전에 커서를 그 위로 가져간다.
    hover 없이 click()만 부르면 커서가 순간이동한 뒤 클릭하는 셈.

    중심에서 ±6px 어긋난 좌표를 쓰는 이유:
    사람은 버튼 정중앙을 정확히 누르지 않는다.
    """
    try:
        box = await locator.bounding_box()
    except Exception:
        return
    if not box:
        return   # 화면 밖이거나 숨겨진 요소
    await page.mouse.move(
        box["x"] + box["width"] / 2 + random.uniform(-6, 6),
        box["y"] + box["height"] / 2 + random.uniform(-6, 6),
        steps=random.randint(20, 40),
    )
    await human_pause(0.3, 1.0)