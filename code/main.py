import subprocess, sys

def run(module, stop_on_fail=True):
    print(f"\n{'='*40}\n▶ {module}\n{'='*40}")
    result = subprocess.run([sys.executable, "-m", module])
    if result.returncode != 0:
        print(f"⚠️  {module} 실패 (exit {result.returncode})")
        if stop_on_fail:
            print("→ 중단합니다.")
            sys.exit(1)
        else:
            print("→ 건너뛰고 계속합니다.")
    return result.returncode

def main():
    # 크롤링: 실패하면 멈춤 (뒷단계가 앞 데이터에 의존하니까)
    run("crawler.crawler_l1_parallel")
    run("crawler.crawler_l2a")
    run("crawler.crawler_l2")
    run("crawler.crawler_l3")
    # 계산: 실패해도 export는 봐야 하니 계속
    run("metrics.calc_metrics",    stop_on_fail=False)
    run("metrics.calc_l3_metrics", stop_on_fail=False)
    # export: 각각 독립이라 하나 실패해도 나머지 계속
    run("export.export_l1",     stop_on_fail=False)
    run("export.export_l2",     stop_on_fail=False)
    run("export.export_l3",     stop_on_fail=False)
    run("export.export_metric", stop_on_fail=False)
    print("\n✅ 전체 완료")

if __name__ == "__main__":
    main()