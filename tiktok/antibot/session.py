# tiktok/antibot/session.py
"""브라우저 생명주기 관리 계층 (얇게).

역할은 딱 하나: "프록시 문제에 강하게, 페이지를 열어서 넘겨준다."
captcha 판단 / 파싱 / DB / stats / 중단 정책은 전부 호출부(l1/l2/l3)의 몫.

재시도 우선순위 (실험으로 검증된 순서)
------
1. 같은 프록시로 goto 2~3회 재시도   → 일시적 네트워크 오류 회복
2. 재시도 모두 실패 = 죽은 프록시     → dead 처리 후 rotate
3. 새 브라우저/컨텍스트로 같은 URL 재시도
4. 모든 프록시 소진                   → ProxyExhausted

동시성 (L1 병렬에서 검증된 규칙)
------
워커들이 BrowserSession 하나를 공유한다. 한 워커가 프록시를 rotate하며
context를 갈아끼우는 순간, 다른 워커가 옛 context(또는 교체 중의 None)로
new_page를 호출하면 TargetClosedError / AttributeError가 난다.
→ "현재 context로 페이지 만들기"와 "context 교체"를 같은 Lock으로 배타 처리.
→ 페이지 생성 시 (generation, page)를 원자적으로 스냅샷해서 반환.
"""

import asyncio

from tiktok.antibot import browser
from tiktok.antibot import manager


class ProxyExhausted(RuntimeError):
    """rotate를 상한까지 돌려도 살아있는 프록시가 없을 때."""


class BrowserSession:
    def __init__(self, playwright, *,
                 goto_retry=3,
                 rotate_max=5,
                 retry_wait=1.5,
                 goto_timeout=30000):
        self._pw = playwright
        self._goto_retry = goto_retry
        self._rotate_max = rotate_max
        self._retry_wait = retry_wait
        self._goto_timeout = goto_timeout

        self._browser = None
        self._context = None
        self._generation = 0
        self._lock = asyncio.Lock()  # context 교체 ↔ 페이지 생성 배타

    # ---- 생명주기 -------------------------------------------------------

    async def start(self):
        async with self._lock:
            self._browser, self._context = await browser.create_context(self._pw)
            self._generation += 1
        return self

    async def close(self):
        async with self._lock:
            await self._close_unlocked()

    async def _close_unlocked(self):
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
        self._browser = None
        self._context = None

    async def _new_page_snapshot(self):
        """Lock 안에서 현재 context로 페이지를 만들고 (generation, page) 반환.

        이 호출이 Lock을 쥐고 있는 동안에는 rotate가 context를 못 바꾸므로,
        '옛 context/None으로 new_page' 레이스가 원천 차단된다.
        """
        async with self._lock:
            if self._context is None:
                # 재생성 실패 등으로 세션이 비어있음 → 프록시 에러로 취급해
                # 바깥 루프가 rotate를 시도하게 한다.
                raise _SessionDown()
            gen = self._generation
            page = await browser.new_page(self._context)
            return gen, page

    async def _rotate_and_recreate(self, dead_generation):
        """프록시 dead → rotate → 브라우저 재생성. 세대로 중복 방지.

        여러 워커가 같은 죽은 프록시로 동시에 도달해도, Lock 안에서 세대를
        비교해 최초 1회만 실제 재생성한다. 나머지는 새 세션을 그대로 잇는다.
        """
        async with self._lock:
            if self._generation != dead_generation:
                return  # 다른 워커가 이미 새 브라우저를 만듦

            manager.fail_and_rotate()  # dead + kill_chrome + rotate

            if manager.available_count() == 0:
                await self._close_unlocked()
                raise ProxyExhausted("살아있는 proxy가 없습니다.")

            await self._close_unlocked()
            self._browser, self._context = await browser.create_context(self._pw)
            self._generation += 1
            print(f"[session] rotate 후 재생성 (gen={self._generation})")

    # ---- 페이지 작업 ----------------------------------------------------

    async def run_with_page(self, url, work, *, goto_kwargs=None):
        """새 페이지로 url을 열고 work(page)를 실행해 결과를 반환.

        1층: 같은 프록시로 goto를 goto_retry회까지 재시도(일시 오류 회복).
        2층: 그래도 실패하면 rotate + 재생성 후 같은 url 재시도(rotate_max회).
        프록시와 무관한 예외(파서 오류 등)는 그대로 올려 호출부가 처리.
        """
        goto_kwargs = goto_kwargs or {
            "wait_until": "domcontentloaded",
            "timeout": self._goto_timeout,
        }

        rotate_count = 0
        while True:
            # --- 1층: 같은 프록시로 goto 재시도 ---
            gen = self._generation
            goto_ok = False
            last_exc = None
            page = None

            try:
                for attempt in range(self._goto_retry + 1):  # 0=최초
                    try:
                        gen, page = await self._new_page_snapshot()
                    except _SessionDown as e:
                        last_exc = e
                        break  # context 없음 → 2층(rotate)으로
                    

                    try:
                        await page.goto(url, **goto_kwargs)
                        goto_ok = True
                        break
                    except Exception as e:
                        msg = str(e)
                        print(
                            f"[goto] gen={gen} "
                            f"attempt={attempt+1}/{self._goto_retry+1} "
                            f"url={url}\n"
                            f"      {type(e).__name__}: {msg}"
                        )
                        
                        if "ERR_ABORTED" in msg or "frame was detached" in msg:
                            raise _RetryGeneration()
                        if manager.is_auth_error(e):
                            raise RuntimeError(
                                "Proxy 인증 오류입니다. \n"
                                "username/password 또는 persistent profile을 확인하세요."
                            ) from e
                        # 프록시 무관 실패(파서/타임아웃 아님)면 그대로 올림
                        if not manager.is_proxy_error(e):
                            raise
                        last_exc = e
                    finally:
                        if not goto_ok and page is not None:
                            try:
                                await page.close()
                            except Exception:
                                pass
                            page = None

                    if attempt < self._goto_retry:
                        await asyncio.sleep(self._retry_wait)

                if goto_ok:
                    try:
                        return await work(page)
                    finally:
                        if page is not None:
                            try:
                                await page.close()
                            except Exception:
                                pass

            except _SessionDown:
                pass  # 아래 2층으로
            
            except _RetryGeneration:
                        # 다른 worker가 이미 브라우저를 재생성함
                        # rotate하지 말고 현재 URL만 새 generation으로 다시 시도
                        continue
            # --- 2층: 죽은 프록시 판정 → rotate + 재생성 ---
            rotate_count += 1
            if rotate_count > self._rotate_max:
                raise ProxyExhausted(
                    f"rotate {self._rotate_max}회 초과, url={url}"
                ) from last_exc

            print(
                f"[session] proxy dead 판정 "
                f"(goto {self._goto_retry}회 실패): "
                f"{type(last_exc).__name__}: {last_exc}"
            )
            await self._rotate_and_recreate(dead_generation=gen)
            # while → 새 프록시/브라우저로 같은 url 처음부터


class _SessionDown(Exception):
    """context가 일시적으로 없음(교체 실패 직후 등). 내부 신호용."""
    
    
class _RetryGeneration(Exception):
    """다른 worker가 브라우저를 재생성했으므로 현재 URL만 다시 시도."""
    