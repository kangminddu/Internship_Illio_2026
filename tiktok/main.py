# tiktok/main.py
import os
import sys
import argparse
import asyncio

from tiktok import config

os.environ["PYTHONUTF8"] = "1"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
# 파이프라인 단계 순서
PIPELINE = ["l1", "l2", "l3"]


def run_stage(stage, channel=None, limit=None, resume=True):
    """단계 이름 하나를 받아 해당 모듈을 실행."""

    print(f"\n{'=' * 40}")
    print(f"▶ {stage.upper()}")
    print(f"{'=' * 40}")

    if stage == "l1":
        from tiktok.steps import l1

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


def main():

    parser = argparse.ArgumentParser(
        prog="tiktok.main",
        description="TikTok Creator Fan CRM Pipeline",
    )

    parser.add_argument(
        "--channel",
        help="특정 채널만 (핸들 또는 URL)",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=config.BATCH_LIMIT,
        help="처리할 최대 대상 수 (테스트용)",
    )

    parser.add_argument(
        "--all",
        action="store_true",
        help="이미 성공한 채널도 포함하여 전체 재수집",
    )

    parser.add_argument(
        "--from",
        dest="from_stage",
        help="이 단계부터 끝까지 실행 (l1/l2/l3)",
    )

    parser.add_argument("--l1", action="store_true")
    parser.add_argument("--l2", action="store_true")
    parser.add_argument("--l3", action="store_true")

    args = parser.parse_args()

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
    #   1) --from l2  -> l2부터 끝까지
    #   2) --l1 --l3 -> 선택한 단계만
    #   3) 아무 옵션 없으면 전체
    # -----------------------------
    if args.from_stage:

        if args.from_stage not in PIPELINE:
            print(
                f"⚠️ 알 수 없는 단계: {args.from_stage} "
                f"(가능: {', '.join(PIPELINE)})"
            )
            return

        start = PIPELINE.index(args.from_stage)
        stages = PIPELINE[start:]

    else:

        flags = {
            "l1": args.l1,
            "l2": args.l2,
            "l3": args.l3,
        }

        selected = [stage for stage in PIPELINE if flags[stage]]
        stages = selected if selected else PIPELINE

    # -----------------------------
    # 실행
    # -----------------------------
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