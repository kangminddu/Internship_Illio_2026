import subprocess
import sys
import argparse


# ------------------------------------------------
# Pipeline 순서
# ------------------------------------------------
PIPELINE = [
    "l1",
    "l2",
    "l3",
    "metric",
    "export",
]


# ------------------------------------------------
# 단계별 실행 모듈
# ------------------------------------------------
STAGE_MODULES = {
    "l1": [
        ("instagram.steps.l1", True),
    ],

    "l2": [
        ("instagram.steps.l2", True),
    ],

    "l3": [
        ("instagram.steps.l3", True),
    ],

    "metric": [
        ("instagram.metric.calc_metric", False),
        ("instagram.metric.calc_l3_metric", False),
    ],

    "export": [
        ("instagram.export.export_l1", False),
        ("instagram.export.export_l2", False),
        ("instagram.export.export_l3", False),
        ("instagram.export.export_metric", False),
    ],
}


# ------------------------------------------------
# Module Runner
# ------------------------------------------------
def run(module, stop_on_fail=True, extra_args=None):
    print(f"\n{'=' * 40}")
    print(f"▶ {module}")
    print(f"{'=' * 40}")

    cmd = [sys.executable, "-m", module]

    if extra_args:
        cmd.extend(extra_args)

    result = subprocess.run(cmd)

    if result.returncode != 0:
        print(f"⚠️ {module} 실패 (exit {result.returncode})")

        if stop_on_fail:
            print("→ 중단합니다.")
            sys.exit(1)
        else:
            print("→ 건너뛰고 계속합니다.")

    return result.returncode


# ------------------------------------------------
# Stage Runner
# ------------------------------------------------
def run_stage(stage):
    """단계 이름 하나를 받아 해당 모듈들을 실행."""
    for module, stop_on_fail in STAGE_MODULES[stage]:
        run(module, stop_on_fail=stop_on_fail)


# ------------------------------------------------
# Main
# ------------------------------------------------
def main(
    seed_file=None,
    l1=False,
    l2=False,
    l3=False,
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
        run("load_seed", extra_args=["--file", seed_file])

    # -----------------------------
    # 실행할 단계 목록 결정
    # -----------------------------
    if from_stage:

        if from_stage not in PIPELINE:
            print(
                f"⚠️ 알 수 없는 단계: {from_stage} "
                f"(가능: {', '.join(PIPELINE)})"
            )
            sys.exit(1)

        start = PIPELINE.index(from_stage)
        stages = PIPELINE[start:]

    else:

        flags = {
            "l1": l1,
            "l2": l2,
            "l3": l3,
            "metric": metric,
            "export": export,
        }

        selected = [
            s
            for s in PIPELINE
            if flags[s]
        ]

        stages = selected if selected else PIPELINE

    # -----------------------------
    # 실행
    # -----------------------------
    for stage in stages:
        run_stage(stage)

    print("\n✅ Instagram Pipeline 완료")


# ------------------------------------------------
# CLI
# ------------------------------------------------
if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description="Instagram Creator Fan CRM Pipeline"
    )

    parser.add_argument(
        "--file",
        help="Seed Excel (.xlsx)",
    )

    parser.add_argument(
        "--from",
        dest="from_stage",
        help="이 단계부터 끝까지 실행 (l1/l2/l3/metric/export)",
    )

    parser.add_argument("--l1", action="store_true")
    parser.add_argument("--l2", action="store_true")
    parser.add_argument("--l3", action="store_true")
    parser.add_argument("--metric", action="store_true")
    parser.add_argument("--export", action="store_true")
    parser.add_argument("--reset", action="store_true")

    args = parser.parse_args()

    main(
        seed_file=args.file,
        l1=args.l1,
        l2=args.l2,
        l3=args.l3,
        metric=args.metric,
        export=args.export,
        reset=args.reset,
        from_stage=args.from_stage,
    )