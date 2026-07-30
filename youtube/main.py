"""
youtube/main.py — 파이프라인 오케스트레이터

이 파일은 크롤링을 하지 않는다. 8개 단계를 정해진 순서로
별도 프로세스에 띄우는 런처(launcher)일 뿐이다.

왜 import해서 함수를 호출하지 않고 subprocess를 쓰는가:
  - 단계마다 성격이 완전히 다르다. L1/L2는 requests, L3는 Playwright로
    브라우저를 띄운다. L3가 메모리를 물고 죽어도 나머지 단계는
    영향받지 않아야 한다.
  - 프로세스가 끝나면 메모리가 통째로 회수된다. 8시간짜리 작업을
    연달아 돌릴 때 누수가 쌓이지 않는다.
  - 각 크롤러를 단독으로도 실행할 수 있다
    (python -m youtube.crawler.crawler_l2a).

실행 예:
  python -m youtube.main                 # 8단계 전부
  python -m youtube.main --l1 --l2a      # 지정한 단계만
  python -m youtube.main --from l2a      # l2a부터 끝까지
  python -m youtube.main --file seed.xlsx  # 시드 적재 후 전체 실행
"""
import subprocess
import sys
import argparse


# ─────────────────────────────────────────────────────────
# 단계 정의
# ─────────────────────────────────────────────────────────

# 파이프라인 단계 순서 (한 곳에서 관리)
#
# 순서에 의미가 있다:
#   l1    채널 기본정보 (구독자, UC ID, 생존 여부)
#   email 치지직 이메일 보강 — L1이 저장한 description을 재사용
#   l2a   영상/쇼츠 목록 + 활동성 '잠정' 판정
#   l2b   영상 개별 페이지 (정확한 게시일, 좋아요, 댓글수)
#   l3    댓글 → 팬
#   backfill  활동성 '확정' 판정
#   metric    파생지표 계산
#   export    엑셀 출력
#
# backfill이 metric 앞에 있는 이유:
#   쇼츠는 목록 페이지에 날짜가 없어 L2a 시점엔 published_at이 NULL이다.
#   L2b가 그 값을 채운 뒤 backfill이 재판정해야 활동성이 확정되고,
#   metric은 그 확정값(active/low_active)으로 대상을 고른다.
#   순서가 바뀌면 쇼츠 중심 채널이 지표에서 누락된다.
PIPELINE = ["l1", "email", "l2a", "l2b", "l3", "backfill", "metric", "export"]

# 각 단계가 실제로 실행하는 모듈 매핑
#
# 튜플의 두 번째 값 = stop_on_fail
#   True  → 이 단계가 실패하면 파이프라인 전체 중단
#   False → 실패해도 다음 단계로 진행
#
# 수집 단계(l1~l3, backfill)는 True다. L2a가 실패하면 영상 목록이 없어
# L2b·L3가 할 일이 없기 때문. 반대로 metric/export는 이미 수집된
# 데이터를 가공하는 단계라, 하나 실패해도 나머지는 의미가 있어 False.
#
# metric과 export는 모듈이 여러 개다. 순서대로 실행된다:
#   calc_metrics가 channel_metrics 행을 만들고
#   calc_l3_metrics가 그 행에 댓글 지표를 UPDATE로 얹는다.
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


# ─────────────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────────────

def run(module, stop_on_fail=True, extra_args=None):
    """모듈 하나를 별도 프로세스로 실행.

    sys.executable을 쓰는 이유: 현재 실행 중인 파이썬(= venv 안의 python)을
    그대로 사용해야 한다. 그냥 "python"이라고 하면 시스템 파이썬이 잡혀
    패키지를 못 찾는다.

    capture_output을 쓰지 않으므로 자식 프로세스의 출력이
    부모 stdout으로 그대로 흘러나온다. 진행 상황을 실시간으로 볼 수 있다.
    """
    print(f"\n{'='*40}\n▶ {module}\n{'='*40}")

    cmd = [sys.executable, "-m", module]
    if extra_args:
        cmd.extend(extra_args)

    result = subprocess.run(cmd)   # 이 단계가 끝날 때까지 블로킹

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
        # --channel은 L3에서만 의미가 있다.
        # (특정 채널의 댓글만 다시 수집하는 디버깅용)
        if stage == "l3" and channel is not None:
            run(module, stop_on_fail=stop_on_fail,
                extra_args=["--channel", str(channel)])
        else:
            run(module, stop_on_fail=stop_on_fail)


def main(
    seed_file=None,
    channel=None,
    l1=False,
    email=False,
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
    # Reset (미구현)
    # -----------------------------
    # 어느 테이블을 어디까지 비울지 결정이 안 돼서 보류했다.
    # 현재는 SQL로 직접 처리한다.
    if reset:
        print("RESET 기능은 아직 구현 예정입니다.")
        return

    # -----------------------------
    # Seed — 지정되면 항상 먼저
    # -----------------------------
    # 엑셀 → creators/channels 적재. 채널 목록이 있어야 L1이 대상을 고르므로
    # 단계 선택과 무관하게 맨 앞에서 실행한다.
    if seed_file:
        run("youtube.crawler.seed", extra_args=["--file", seed_file])

    # -----------------------------
    # 실행할 단계 목록 결정 (우선순위 순)
    #
    #   1) --from l2a  → l2a부터 끝까지 (l2a,l2b,l3,backfill,metric,export)
    #      중간에 끊긴 작업을 이어서 돌릴 때 쓴다.
    #
    #   2) 개별 플래그 (--l1 --l2a) → 지정한 것들만
    #      단, 사용자가 쓴 순서가 아니라 PIPELINE 순서대로 실행된다.
    #      --l2a --l1 이라고 써도 l1 → l2a 순으로 간다.
    #
    #   3) 아무것도 없으면 → 8단계 전부
    #
    #   ※ --from이 있으면 개별 플래그는 무시된다 (경고 없이).
    # -----------------------------
    if from_stage:
        if from_stage not in PIPELINE:
            print(f"⚠️  알 수 없는 단계: {from_stage} (가능: {', '.join(PIPELINE)})")
            sys.exit(1)
        start = PIPELINE.index(from_stage)
        stages = PIPELINE[start:]
    else:
        flags = {"l1": l1, "email": email, "l2a": l2a, "l2b": l2b, "l3": l3,
                 "backfill": backfill, "metric": metric, "export": export}
        # PIPELINE을 순회하며 켜진 것만 고른다 → 순서가 자동으로 보장된다
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

    # 각 단계를 개별 실행하는 스위치.
    # 아무것도 안 주면 전체 실행이 기본값이다.
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