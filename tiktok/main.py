# tiktok/main.py
"""
틱톡 파이프라인 오케스트레이터.

유튜브 main.py와 하는 일은 같지만 실행 방식이 다르다.

    유튜브 : subprocess.run([sys.executable, "-m", module])  → 별도 프로세스
    틱톡   : from tiktok.steps import l1; asyncio.run(l1.run())  → 직접 호출

왜 다른가
------
유튜브는 8단계이고 requests와 Playwright가 섞여 있다.
L3가 브라우저를 띄우다 메모리를 물고 죽어도 나머지 단계는 살아야 하므로
프로세스 격리가 필요했다.

틱톡은 3단계뿐이고 L1/L2/L3가 전부 async + Playwright다.
단계마다 프로세스를 새로 띄우면 브라우저 기동 비용만 늘고 이점이 없다.
직접 import해서 asyncio.run으로 부르는 편이 단순하다.

단, 대가가 있다. 한 단계가 예외로 죽으면 파이프라인 전체가 중단된다.
유튜브의 stop_on_fail 같은 단계별 실패 정책이 없다.
(3단계 모두 앞 단계에 의존하므로 실무상 큰 문제는 아니다 —
 L1 없이 L2를, L2 없이 L3를 돌릴 수 없다)
"""
import os
import sys
import argparse
import asyncio

from tiktok import config

# ── 인코딩 강제 ──
#
# 윈도우 서버(한국어 로케일)에서 실행할 때 필요하다.
# 크롤러가 이모지(🚨 ✅ 😴)와 한글을 출력하는데,
# 기본 인코딩이 cp949라 UnicodeEncodeError로 죽는다.
# 특히 로그를 파일로 리다이렉트할 때 확실히 터진다.
#
# chcp 65001만으로는 부족하다. 그건 콘솔 코드페이지고,
# 파이썬이 파일에 쓸 때 쓰는 인코딩은 별개이기 때문.
# → 코드에서 직접 강제한다. (유튜브 main.py에는 이 처리가 없어서
#    실행 전에 set PYTHONIOENCODING=utf-8을 쳐야 한다)
os.environ["PYTHONUTF8"] = "1"

if hasattr(sys.stdout, "reconfigure"):
    # reconfigure는 파이썬 3.7+. hasattr로 방어.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 파이프라인 단계 순서
#   l1  채널 기본정보 (팔로워, 총영상수, bio, sec_uid)  — 비로그인
#   l2  영상 목록 + 활동성 판정                          — 로그인 필요
#   l3  댓글 → 팬                                        — 로그인 필요
#
# 유튜브와 달리 backfill/metric/export가 여기 없다.
# 지표 계산과 엑셀 출력은 별도 모듈로 직접 실행한다:
#   python -m tiktok.metrics.calc_metric
#   python -m tiktok.export.export_metric
# (활동성 판정을 L2가 직접 하므로 backfill 같은 재판정 단계가 불필요하다.
#  틱톡은 영상 목록 API가 createTime을 유닉스 타임스탬프로 정확히 주기 때문에
#  유튜브처럼 '쇼츠 게시일이 나중에 채워지는' 문제가 없다)
PIPELINE = ["l1", "l2", "l3"]


def run_stage(stage, channel=None, limit=None, resume=True):
    """단계 이름 하나를 받아 해당 모듈을 실행."""

    print(f"\n{'=' * 40}")
    print(f"▶ {stage.upper()}")
    print(f"{'=' * 40}")

    # import를 함수 안에서 하는 이유:
    # 모듈 최상위에서 세 개를 다 import하면, --l1만 돌릴 때도
    # l2/l3가 로드되면서 playwright와 관련 의존성이 전부 올라온다.
    # 필요한 단계만 로드하는 게 기동이 빠르고, 한 모듈에 import 에러가
    # 있어도 다른 단계는 돌아간다.
    if stage == "l1":
        from tiktok.steps import l1

        # ⚠️ l1에는 resume을 넘기지 않는다.
        #   l1의 resume은 crawl_logs가 아니라 'channel_name IS NULL'이
        #   기준이라 --all로 끌 수 있는 구조가 아니다.
        #   (l2/l3는 crawl_logs 기반이라 resume 인자를 받는다)
        asyncio.run(
            l1.run(
                channel=channel,
                limit=limit,
            )
        )

    elif stage == "l2":
        from tiktok.steps import l2

        asyncio.run(
            l2.run(
                channel=channel,
                limit=limit,
                resume=resume,
            )
        )

    elif stage == "l3":
        from tiktok.steps import l3

        asyncio.run(
            l3.run(
                channel=channel,
                limit=limit,
                resume=resume,
            )
        )
    # 단계 이름이 셋 다 아니면 조용히 아무것도 안 한다.
    # (PIPELINE에서만 값이 오므로 실무상 도달하지 않지만,
    #  유튜브의 STAGE_MODULES 딕셔너리 방식과 달리 if/elif라
    #  단계를 추가할 때 여기도 고쳐야 한다)


def main():

    parser = argparse.ArgumentParser(
        prog="tiktok.main",
        description="TikTok Creator Fan CRM Pipeline",
    )

    parser.add_argument(
        "--channel",
        help="특정 채널만 (핸들 또는 URL)",
        # 유튜브의 --channel은 type=int(channel_id)이고 L3에만 전달되는데,
        # 여기는 핸들 문자열이고 세 단계 모두에 전달된다.
        # 단일 채널 테스트/재수집용.
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=config.BATCH_LIMIT,   # config 기본값을 CLI로 덮어쓸 수 있다
        help="처리할 최대 대상 수 (테스트용)",
    )

    parser.add_argument(
        "--all",
        action="store_true",
        help="이미 성공한 채널도 포함하여 전체 재수집",
        # resume의 반대. 아래에서 resume=not args.all로 뒤집는다.
        # 유튜브에는 없는 옵션 — 유튜브는 attempted_at 기준 7일 갱신 주기로
        # 재수집을 제어하고, 틱톡은 전체 재수집을 명시적 플래그로 처리한다.
    )

    parser.add_argument(
        "--from",
        dest="from_stage",   # 'from'은 파이썬 예약어라 dest로 이름을 바꾼다
        help="이 단계부터 끝까지 실행 (l1/l2/l3)",
    )

    parser.add_argument("--l1", action="store_true")
    parser.add_argument("--l2", action="store_true")
    parser.add_argument("--l3", action="store_true")

    args = parser.parse_args()

    # 실행 조건을 먼저 출력한다.
    # 몇 시간짜리 작업을 걸어두고 나중에 로그를 보면
    # "어떤 설정으로 돌린 거지?"를 알 수 없다.
    # HEADLESS 값이 특히 중요하다 — L2/L3는 CAPTCHA 수동 해결이
    # 전제라 False여야 하고, L1은 무인이라 True가 낫다.
    print(
        f"[config] "
        f"platform={config.PLATFORM} "
        f"L2_MIN_VIDEOS={config.L2_MIN_VIDEOS} "
        f"HEADLESS={config.HEADLESS}"
    )

    print(
        f"[args] "
        f"channel={args.channel} "
        f"limit={args.limit} "
        f"resume={not args.all}"
    )

    # -----------------------------
    # 실행할 단계 결정
    #   1) --from l2  -> l2부터 끝까지 (l2, l3)
    #   2) --l1 --l3 -> 선택한 단계만
    #   3) 아무 옵션 없으면 전체
    #
    #   우선순위가 있다. --from이 있으면 개별 플래그는 무시된다(경고 없이).
    # -----------------------------
    if args.from_stage:

        if args.from_stage not in PIPELINE:
            print(
                f"⚠️ 알 수 없는 단계: {args.from_stage} "
                f"(가능: {', '.join(PIPELINE)})"
            )
            return
            # ⚠️ 유튜브는 sys.exit(1)로 종료 코드를 남기는데
            #    여기는 return이라 종료 코드가 0이다.
            #    스크립트로 자동화할 때 실패를 감지할 수 없다.

        start = PIPELINE.index(args.from_stage)
        stages = PIPELINE[start:]

    else:

        flags = {
            "l1": args.l1,
            "l2": args.l2,
            "l3": args.l3,
        }

        # PIPELINE을 순회하며 켜진 것만 고른다.
        # → 사용자가 --l3 --l1 순서로 써도 l1 → l3 순으로 실행된다.
        #   순서가 자동으로 보장되는 구조.
        selected = [stage for stage in PIPELINE if flags[stage]]
        stages = selected if selected else PIPELINE

    # -----------------------------
    # 실행
    # -----------------------------
    # 순차 실행. 한 단계가 끝나야 다음이 시작된다.
    # (asyncio.run이 블로킹이므로 자연스럽게 직렬화된다)
    for stage in stages:
        run_stage(
            stage,
            channel=args.channel,
            limit=args.limit,
            resume=not args.all,
        )

    print("\n✅ 완료")


if __name__ == "__main__":
    main()