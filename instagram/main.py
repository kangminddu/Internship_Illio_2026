"""
instagram/main.py — 파이프라인 오케스트레이터

유튜브 main.py와 거의 같은 구조다. subprocess로 단계를 띄운다.
(틱톡만 asyncio.run으로 직접 호출 — 3단계뿐이라 프로세스 격리 이점이 작아서)

세 플랫폼 런처 비교
------
    유튜브 : 8단계 (seed/l1/email/l2a/l2b/l3/backfill/metric/export), subprocess
    인스타 : 5단계 (l1/l2/l3/metric/export), subprocess
    틱톡   : 3단계 (l1/l2/l3), 직접 호출 + 지표/엑셀은 별도 실행

인스타에 backfill이 없는 이유:
  활동성 판정이 calc_metric.py 안에 들어가 있다.
  (유튜브는 L2a 잠정 → backfill 확정, 틱톡은 L2에서 확정)
  → 세 플랫폼의 판정 시점이 전부 다르다. 리뷰 안건.
"""
import subprocess
import sys
import argparse


# ------------------------------------------------
# Pipeline 순서
# ------------------------------------------------
# l1     프로필 정보 (팔로워, bio, 게시물 수)
# l2     게시물 목록 + 릴스 (좋아요/댓글수 포함)
# l3     댓글 → 팬
# metric 파생지표 (+ 활동성 판정이 여기 들어가 있음)
# export 엑셀
#
# 유튜브의 seed가 여기 없다. --file로 지정할 때만 실행된다(아래 main 참고).
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
# 튜플의 두 번째 값 = stop_on_fail
#   True  → 실패 시 파이프라인 전체 중단
#   False → 실패해도 다음 단계 진행
#
# 수집 단계(l1~l3)는 True다. L1이 실패하면 L2가 볼 계정이 없다.
# metric/export는 이미 수집된 데이터를 가공하는 단계라
# 하나 실패해도 나머지는 의미가 있어 False.
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

    # 두 모듈이 순서대로 실행된다.
    # calc_metric이 channel_metrics 행을 만들고,
    # calc_l3_metric이 그 행에 댓글 지표를 UPDATE로 얹는다.
    # (세 플랫폼 모두 같은 구조)
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
    """모듈 하나를 별도 프로세스로 실행.

    sys.executable을 쓰는 이유: 현재 실행 중인 파이썬(venv 안의 python)을
    그대로 써야 한다. "python"이라고 하면 시스템 파이썬이 잡혀
    패키지를 못 찾는다.

    capture_output을 안 쓰므로 자식 프로세스 출력이 그대로 흘러나온다.
    L1이 4~8시간 걸리는데 진행 상황을 실시간으로 봐야 한다.
    """
    print(f"\n{'=' * 40}")
    print(f"▶ {module}")
    print(f"{'=' * 40}")

    cmd = [sys.executable, "-m", module]

    if extra_args:
        cmd.extend(extra_args)

    result = subprocess.run(cmd)   # 이 단계가 끝날 때까지 블로킹

    if result.returncode != 0:
        print(f"⚠️ {module} 실패 (exit {result.returncode})")

        if stop_on_fail:
            print("→ 중단합니다.")
            sys.exit(1)
        else:
            print("→ 건너뛰고 계속합니다.")

    return result.returncode
    # ⚠️ 이 반환값을 run_stage가 버린다.
    #    stop_on_fail=False인 단계가 실패해도 최종적으로
    #    "✅ 완료"가 출력되고 종료 코드는 0이다.
    #    (유튜브 main.py도 같은 문제)


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
    # 어느 테이블을 어디까지 비울지 결정이 안 돼서 보류.
    # 현재는 SQL로 직접 처리한다. (유튜브 main.py도 동일)
    if reset:
        print("RESET 기능은 아직 구현 예정입니다.")
        return

    # -----------------------------
    # Seed (지정되면 항상 먼저)
    # -----------------------------
    # 계정 목록이 있어야 L1이 대상을 고르므로,
    # 단계 선택과 무관하게 맨 앞에서 실행한다.
    #
    # ⚠️ 모듈 경로가 "load_seed"다. 다른 단계는 전부
    #    "instagram.steps.xxx" 형태인데 여기만 최상위 모듈을 가리킨다.
    #    실제 파일 위치와 맞는지 확인이 필요한 부분.
    #    (유튜브는 "youtube.crawler.seed"로 명확하다)
    if seed_file:
        run("load_seed", extra_args=["--file", seed_file])

    # -----------------------------
    # 실행할 단계 목록 결정
    # -----------------------------
    # 우선순위:
    #   1) --from l2  → l2부터 끝까지 (l2, l3, metric, export)
    #   2) --l1 --l3  → 지정한 것만. 단, PIPELINE 순서대로 실행된다
    #                   (--l3 --l1로 써도 l1 → l3 순)
    #   3) 아무것도 없으면 전체
    #
    # --from이 있으면 개별 플래그는 무시된다(경고 없이).
    if from_stage:

        if from_stage not in PIPELINE:
            print(
                f"⚠️ 알 수 없는 단계: {from_stage} "
                f"(가능: {', '.join(PIPELINE)})"
            )
            sys.exit(1)
            # ↑ 종료 코드 1을 남긴다. 틱톡 main.py는 return이라
            #   스크립트 자동화에서 실패를 감지할 수 없다. 이쪽이 낫다.

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

        # PIPELINE을 순회하며 켜진 것만 고른다.
        # → 사용자가 쓴 순서가 아니라 정의된 순서대로 실행된다.
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
        dest="from_stage",   # 'from'은 파이썬 예약어라 dest로 바꾼다
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
    # ⚠️ 단계를 추가하려면 6곳을 고쳐야 한다:
    #    PIPELINE, STAGE_MODULES, main() 파라미터, flags 딕셔너리,
    #    argparse, main() 호출부.
    #    STAGE_MODULES.keys()를 단일 소스로 삼고 argparse를 루프로
    #    생성하면 한 곳으로 줄일 수 있다. (유튜브도 같은 문제)