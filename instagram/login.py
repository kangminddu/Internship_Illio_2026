# instagram/login.py
"""
Instagram 세션 생성 스크립트.

    python -m instagram.login

브라우저가 뜨면 직접 로그인하고, 2차 인증과 One Tap 화면까지
전부 넘긴 뒤 터미널에서 Enter를 누른다.
이후 크롤러는 저장된 세션 파일을 재사용한다.

★ 틱톡과 세션 저장 방식이 다르다
------
    틱톡   : persistent profile (프로필 디렉터리 통째로 재사용)
    인스타 : storage_state (쿠키/로컬스토리지를 JSON으로 export)  ← 이 파일

    틱톡은 쿠키 외에 IndexedDB·캐시·디바이스 지문까지 함께 봐서
    JSON만 옮기면 "다른 브라우저에서 로그인했다"로 판정됐다.
    인스타는 sessionid 쿠키가 핵심이라 JSON으로 충분하다.

    JSON 방식의 이점:
      - 파일 하나라 백업·이동이 쉽다
      - 프로필 락(프로세스당 1개) 제약이 없다 → 병렬 실행 가능
      - 용량이 작다 (프로필은 수백 MB)

로그인은 자동화하지 않는다
------
2차 인증(OTP), CAPTCHA, One Tap 팝업은 사람이 처리해야 한다.
자동 로그인을 시도하면 그 자체가 봇 신호가 되고, 계정이 정지될 수 있다.
→ 사람이 한 번 하고, 그 결과를 재사용한다.
"""
from pathlib import Path
import json

from playwright.sync_api import sync_playwright
# ★ sync_playwright를 쓴다.
#   크롤러(steps/l1~l3)는 async인데 여기만 동기다.
#   로그인은 사람을 기다리는 단일 작업이라 동시성이 필요 없고,
#   input()을 쓰기에도 동기 쪽이 훨씬 단순하다.
#   (틱톡 login.py는 async라 input()을 run_in_executor로 넘겨야 했다)
from playwright_stealth import Stealth

from instagram.config import context_kwargs
# ★ 크롤러와 같은 context 옵션을 공유한다.
#   viewport/locale/timezone/user_agent가 로그인할 때와 크롤링할 때
#   다르면 인스타가 다른 환경으로 감지해 재인증을 요구한다.
#   → 옵션을 config.context_kwargs() 한 곳에 모아둔 이유.
#   (틱톡의 browser.persistent_launch_kwargs()와 같은 역할)

SESSION_DIR = Path("instagram/session")
SESSION_FILE = SESSION_DIR / "instagram.json"
# ⚠️ 이 파일에는 sessionid 쿠키가 들어간다. 계정 탈취가 가능한 값이라
#    .gitignore 대상이어야 한다. (확인 결과 추적되지 않고 있다)


def main():
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:

        browser = p.chromium.launch(
            # ★ 번들 Chromium이 아니라 '실제 설치된 Chrome'을 쓴다.
            #
            #   Playwright가 함께 배포하는 Chromium은 일반 Chrome과
            #   미묘하게 다른 지문을 흘린다 (빌드 플래그, 코덱 지원,
            #   navigator.userAgentData 등). 인스타는 이 차이를 본다.
            #
            #   ⚠️ 윈도우 경로가 하드코딩돼 있다.
            #     맥/리눅스에서는 이 경로가 없어 실행이 안 된다.
            #     config로 빼거나 OS별 분기가 필요한 부분.
            executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            headless=False,   # 사람이 로그인해야 하므로 창이 떠야 한다
            slow_mo=100,
            # slow_mo: 모든 동작 사이에 100ms 지연을 넣는다.
            # 여기서는 자동 조작이 거의 없어 실질 효과는 작지만,
            # 페이지 전환이 눈으로 따라갈 수 있는 속도가 된다.
        )

        context = browser.new_context(**context_kwargs())

        page = context.new_page()

        # 필요하면 잠시 주석 처리해서 비교 테스트
        #
        # ★ 이 주석이 실제 경험을 담고 있다.
        #   틱톡 L1에서 stealth를 켰더니 모든 요청이
        #   ERR_HTTP_RESPONSE_CODE_FAILURE로 실패한 적이 있다.
        #   playwright_stealth는 버전 조합에 따라 오히려 문제를 일으킨다.
        #   → 문제가 생기면 이 줄부터 끄고 비교하라는 메모.
        Stealth().apply_stealth_sync(page)

        print("=" * 60)
        print("Instagram Login")
        print("=" * 60)

        page.goto(
            "https://www.instagram.com/accounts/login/",
            wait_until="domcontentloaded",
        )

        print()
        print("브라우저에서 직접 로그인하세요.")
        print("2차 인증 및 One Tap 화면까지 모두 완료한 후")
        # ★ One Tap을 명시하는 이유:
        #   로그인 직후 "로그인 정보를 저장하시겠어요?" 팝업이 뜬다.
        #   이걸 처리하지 않고 Enter를 누르면 페이지가 전환 중인 상태라
        #   쿠키가 덜 저장될 수 있다.
        input("\n로그인이 끝났으면 Enter를 누르세요...")

        # 쿠키가 모두 저장될 시간을 조금 줌
        #
        # 인스타는 로그인 완료 후에도 백그라운드에서 추가 요청을 보내며
        # 쿠키를 갱신한다. 바로 storage_state를 뽑으면 일부가 누락된다.
        page.wait_for_timeout(5000)

        cookies = context.cookies()

        # 저장될 쿠키 이름을 출력한다.
        # 값은 찍지 않는다 — sessionid가 로그에 남으면 안 된다.
        print("\n=== 저장될 쿠키 ===")
        print([c["name"] for c in cookies])

        # ★ sessionid로 로그인 성공을 검증한다.
        #
        #   사람이 Enter를 눌렀다고 로그인이 됐다는 보장은 없다.
        #   화면(DOM 셀렉터)으로 판단하면 UI가 바뀔 때 깨지지만
        #   쿠키는 훨씬 안정적인 신호다.
        #   (틱톡 login.py도 sessionid/sessionid_ss로 같은 검증을 한다)
        has_session = any(c["name"] == "sessionid" for c in cookies)

        if has_session:
            print("\n✅ sessionid 확인됨")
        else:
            print("\n❌ sessionid가 없습니다. 로그인이 정상적으로 완료되지 않았을 가능성이 있습니다.")
            # ⚠️ 경고만 하고 저장은 진행한다.
            #    그리고 종료 코드도 남기지 않는다.
            #    (틱톡 login.py는 재확인 기회를 주고 sys.exit(0/1)로
            #     성공/실패를 구분한다 — 이쪽이 더 낫다)

        # storage_state: 쿠키 + 로컬스토리지를 JSON으로 저장.
        # 크롤러는 new_context(storage_state=...)로 이 파일을 읽어
        # 로그인된 상태로 시작한다.
        context.storage_state(path=str(SESSION_FILE))

        print()
        print("세션 저장 완료")
        print(SESSION_FILE.resolve())   # 절대경로로 찍어 어디 저장됐는지 명확히

        browser.close()
        # close()로 정상 종료한다. 다만 storage_state를 이미 파일로
        # 뽑았기 때문에, 틱톡(프로필 방식)처럼 "닫아야 flush된다"는
        # 제약은 없다.


if __name__ == "__main__":
    main()