# tiktok/main.py
import argparse
import asyncio

from tiktok import config


def main():
    parser = argparse.ArgumentParser(
        prog="tiktok.main",
        description="TikTok 팬덤 CRM 크롤러 (L1/L2/L3 단계별 실행)",
    )

    parser.add_argument("--l1", action="store_true", help="L1: 채널 단위 수집")
    parser.add_argument("--l2", action="store_true", help="L2: 영상목록/상세 수집")
    parser.add_argument("--l3", action="store_true", help="L3: 댓글 수집 (Playwright)")

    parser.add_argument("--channel", help="특정 채널만 (핸들 또는 URL)")
    parser.add_argument("--video", help="특정 영상만 (영상 ID)")

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

    args = parser.parse_args()

    if not (args.l1 or args.l2 or args.l3):
        parser.print_help()
        print("\n[안내] --l1 / --l2 / --l3 중 하나 이상을 지정하세요.")
        return

    print(
        "[config] platform=%s L2_MIN_VIDEOS=%s headless=%s"
        % (
            config.PLATFORM,
            config.L2_MIN_VIDEOS,
            config.HEADLESS,
        )
    )

    print(
        "[args] channel=%s video=%s limit=%s resume=%s"
        % (
            args.channel,
            args.video,
            args.limit,
            not args.all,
        )
    )

    if args.l1:
        from tiktok.steps import l1

        asyncio.run(
            l1.run(
                channel=args.channel,
                limit=args.limit,
            )
        )

    if args.l2:
        from tiktok.steps import l2

        asyncio.run(
            l2.run(
                channel=args.channel,
                limit=args.limit,
                resume=not args.all,
            )
        )

    if args.l3:
        from tiktok.steps import l3

        asyncio.run(
            l3.run(
                channel=args.channel,
                limit=args.limit,
                resume=not args.all,
            )
        )


if __name__ == "__main__":
    main()