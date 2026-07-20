# tiktok/antibot/behavior.py

import random
import asyncio


DEFAULT_VIEWPORT = {"width": 1280, "height": 720}


def _viewport(page):
    """viewport_size가 None인 환경(persistent context 등) 대비 폴백."""
    return page.viewport_size or DEFAULT_VIEWPORT


async def human_pause(a=0.5, b=2.0):
    """랜덤 대기."""
    await asyncio.sleep(random.uniform(a, b))


# -------------------------------------------------------
# 기본 행동 (단위 동작)
# -------------------------------------------------------

async def mouse_wander(page, moves=None):
    """마우스를 여기저기 자연스럽게 움직인다."""
    vp = _viewport(page)
    width, height = vp["width"], vp["height"]
    margin = min(100, width // 4, height // 4)

    if moves is None:
        moves = random.randint(1, 4)

    for _ in range(moves):
        await page.mouse.move(
            random.randint(margin, width - margin),
            random.randint(margin, height - margin),
            steps=random.randint(15, 35),
        )
        await human_pause(0.2, 0.7)


async def scroll_down(page, amount=None):
    """아래로 스크롤."""
    if amount is None:
        amount = random.randint(80, 400)
    await page.mouse.wheel(0, amount)
    await human_pause(0.3, 1.2)


async def scroll_up(page, amount=None):
    """위로 스크롤 (사람은 되돌아보기도 함)."""
    if amount is None:
        amount = random.randint(60, 300)
    await page.mouse.wheel(0, -amount)
    await human_pause(0.3, 1.0)


async def wiggle_scroll(page):
    """아래로 갔다가 살짝 위로 — 망설이는 스크롤."""
    await page.mouse.wheel(0, random.randint(150, 400))
    await human_pause(0.2, 0.6)
    await page.mouse.wheel(0, -random.randint(40, 120))
    await human_pause(0.3, 0.9)


async def dwell(page, a=1.5, b=4.0):
    """영상을 보는 척 그냥 머무른다 (아무 동작 없음)."""
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
    """
    actions = []

    # 각 행동을 확률적으로 후보에 넣음 (항상 전부 하지 않음)
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
    if not actions:
        actions.append(lambda: mouse_wander(page, moves=1))

    # 순서를 섞어서 실행
    random.shuffle(actions)
    for act in actions:
        try:
            await act()
        except Exception:
            pass  # 페이지 닫힘 등은 무시하고 계속


# -------------------------------------------------------
# 하위 호환 (기존 이름 유지 — L2 등에서 씀)
# -------------------------------------------------------

async def arrive(page):
    """페이지 진입 후 사람이 페이지를 읽는 것처럼 잠시 머무른다."""
    await human_pause(1.2, 3.0)
    await mouse_wander(page, moves=random.randint(2, 5))


async def small_scroll(page):
    """페이지를 조금 훑어보는 행동."""
    for _ in range(random.randint(1, 3)):
        await page.mouse.wheel(0, random.randint(80, 250))
        await human_pause(0.4, 1.2)


async def feed_scroll(page):
    """피드를 크게 한 번 넘기는 행동."""
    await page.mouse.wheel(0, random.randint(1500, 3500))
    await human_pause(1.0, 2.5)


async def hover(page, locator):
    """클릭 전 살짝 마우스를 올린다."""
    try:
        box = await locator.bounding_box()
    except Exception:
        return
    if not box:
        return
    await page.mouse.move(
        box["x"] + box["width"] / 2 + random.uniform(-6, 6),
        box["y"] + box["height"] / 2 + random.uniform(-6, 6),
        steps=random.randint(20, 40),
    )
    await human_pause(0.3, 1.0)