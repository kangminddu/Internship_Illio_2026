import subprocess
import sys
import argparse


# 파이프라인 단계 순서 정의 (한 곳에서 관리)
# backfill은 반드시 metric 앞 — activity 재판정 후에 metric이 대상을 고르므로.
PIPELINE = ["l1", "email", "l2a", "l2b", "l3", "backfill", "metric", "export"]

# 각 단계가 실제로 실행하는 모듈 매핑
STAGE_MODULES = {
    "l1":       [("youtube.crawler.crawler_l1_parallel", True)],
    "email":    [("youtube.crawler.chzzk_email", False)], 
    "l2a":      [("youtube.crawler.crawler_l2a", True)],
    "l2b":      [("youtube.crawler.crawler_l2", True)],
    "l3":       [("youtube.crawler.crawler_l3", True)],
    "backfill": [("youtube.backfill_activity", True)],        
    "metric":   [("youtube.metrics.calc_metrics", False),
                 ("youtube.metrics.calc_l3_metrics", False)],
    "export":   [("youtube.export.export_l1", False),
                 ("youtube.export.export_l2", False),
                 ("youtube.export.export_l3", False),
                 ("youtube.export.export_metric", False)],
}


def run(module, stop_on_fail=True, extra_args=None):
    print(f"\n{'='*40}\n▶ {module}\n{'='*40}")

    cmd = [sys.executable, "-m", module]
    if extra_args:
        cmd.extend(extra_args)

    result = subprocess.run(cmd)

    if result.returncode != 0:
        print(f"⚠️  {module} 실패 (exit {result.returncode})")
        if stop_on_fail:
            print("→ 중단합니다.")
            sys.exit(1)
        else:
            print("→ 건너뛰고 계속합니다.")

    return result.returncode


def run_stage(stage, channel=None):
    """단계 이름 하나를 받아 해당 모듈들을 실행."""
    for module, stop_on_fail in STAGE_MODULES[stage]:
        # L3만 --channel 전달
        if stage == "l3" and channel is not None:
            run(module, stop_on_fail=stop_on_fail,
                extra_args=["--channel", str(channel)])
        else:
            run(module, stop_on_fail=stop_on_fail)


def main(
    seed_file=None,
    channel=None,
    l1=False,
    email = False,
    l2a=False,
    l2b=False,
    l3=False,
    backfill=False,
    metric=False,
    export=False,
    reset=False,
    from_stage=None,
):

    # -----------------------------
    # Reset (추후 구현)
    # -----------------------------
    if reset:
        print("RESET 기능은 아직 구현 예정입니다.")
        return

    # -----------------------------
    # Seed (지정되면 항상 먼저)
    # -----------------------------
    if seed_file:
        run("youtube.crawler.seed", extra_args=["--file", seed_file])

    # -----------------------------
    # 실행할 단계 목록 결정
    #   1) --from x  → x부터 끝까지
    #   2) 개별 플래그 → 지정한 것들만 (PIPELINE 순)
    #   3) 아무것도 없으면 → 전체
    # -----------------------------
    if from_stage:
        if from_stage not in PIPELINE:
            print(f"⚠️  알 수 없는 단계: {from_stage} (가능: {', '.join(PIPELINE)})")
            sys.exit(1)
        start = PIPELINE.index(from_stage)
        stages = PIPELINE[start:]
    else:
        flags = {"l1": l1, "email": email,"l2a": l2a, "l2b": l2b, "l3": l3,
                 "backfill": backfill, "metric": metric, "export": export}
        selected = [s for s in PIPELINE if flags[s]]
        stages = selected if selected else PIPELINE

    # -----------------------------
    # 실행
    # -----------------------------
    for stage in stages:
        run_stage(stage, channel=channel)

    print("\n✅ 완료")


if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description="YouTube Creator Fan CRM Pipeline"
    )

    parser.add_argument("--file", help="Seed Excel (.xlsx)")
    parser.add_argument("--channel", type=int,
                        help="Run L3 for one channel only")
    parser.add_argument("--from", dest="from_stage",
                        help="이 단계부터 끝까지 실행 (l1/email/l2a/l2b/l3/backfill/metric/export)")

    parser.add_argument("--l1", action="store_true")
    parser.add_argument("--email", action="store_true")
    parser.add_argument("--l2a", action="store_true")
    parser.add_argument("--l2b", action="store_true")
    parser.add_argument("--l3", action="store_true")
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--metric", action="store_true")
    parser.add_argument("--export", action="store_true")
    parser.add_argument("--reset", action="store_true")

    args = parser.parse_args()

    main(
        seed_file=args.file,
        channel=args.channel,
        l1=args.l1,
        email=args.email,
        l2a=args.l2a,
        l2b=args.l2b,
        l3=args.l3,
        backfill=args.backfill,
        metric=args.metric,
        export=args.export,
        reset=args.reset,
        from_stage=args.from_stage,
    )