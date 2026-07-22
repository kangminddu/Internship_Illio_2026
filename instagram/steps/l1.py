# -*- coding: utf-8 -*-
"""
l1.py  (리팩터링 + config 연동판)

설계 원칙 (프로젝트 문서 기준)
- 원본(raw)은 항상 저장: HTML은 성패 무관 항상, GraphQL JSON은 응답 있을 때만.
- 상태 판별은 GraphQL(ProfilePageContentQuery) 우선, HTML은 보조 수단.
- GraphQL 이 안 오는 것은 오류가 아니라 정상 분기 → HTML 판별로 넘어간다.
- 없는 계정에서 timeout 을 통째로 소비하지 않는다(grace 만 짧게).
- JSONL은 append-only 실행 로그. 최종 dedup은 Export가 담당.
- Resume는 확정 상태(SUCCESS/PRIVATE/NOT_FOUND)만 제외, 나머지는 재시도.

crawl_one 흐름
  (1) 페이지 방문: goto '자체' 타임아웃만 담당 (GraphQL 대기와 완전 분리)
  (2) GraphQL 유예 대기: 짧게만. 안 오면 HTML 판별로.
  (3) HTML 항상 저장
  (4) GraphQL 저장 + 파싱 (왔을 때만)
  (5) 상태 판별: GraphQL 우선, 없으면 HTML
"""

import json
import logging
import random
import re
import time
from datetime import datetime

from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PWTimeout

# ── config 연동 (실행 방식에 따라 import 경로 대응) ──
try:
    from instagram.config import (
        SESSION_FILE, OUTPUT_DIR, HTML_DIR, GRAPHQL_DIR, RESULTS_FILE,
        LOG_DIR, LOG_FILE, LOCALE, USER_AGENT, HEADLESS,
        L1_DELAY_MIN, L1_DELAY_MAX, L1_GOTO_TIMEOUT_MS, L1_RENDER_WAIT_MS,
        L1_GRAPHQL_GRACE_MS, GRAPHQL_URL_PART, PROFILE_QUERY_NAME,
        BATCH_LIMIT, STOP_ON_429,
    )
except Exception:
    from config import (
        SESSION_FILE, OUTPUT_DIR, HTML_DIR, GRAPHQL_DIR, RESULTS_FILE,
        LOG_DIR, LOG_FILE, LOCALE, USER_AGENT, HEADLESS,
        L1_DELAY_MIN, L1_DELAY_MAX, L1_GOTO_TIMEOUT_MS, L1_RENDER_WAIT_MS,
        L1_GRAPHQL_GRACE_MS, GRAPHQL_URL_PART, PROFILE_QUERY_NAME,
        BATCH_LIMIT, STOP_ON_429,
    )

try:
    from instagram.lib.graphql_parser import parse_profile
except Exception:
    from lib.graphql_parser import parse_profile                # steps/ 안에서 직접 실행


# =========================================================
# 상태 상수
# =========================================================
STATUS_SUCCESS = "SUCCESS"
STATUS_PRIVATE = "PRIVATE"
STATUS_NOT_FOUND = "NOT_FOUND"
STATUS_LOGIN_REQUIRED = "LOGIN_REQUIRED"
STATUS_CHALLENGE = "CHALLENGE"
STATUS_RATE_LIMITED = "RATE_LIMITED"
STATUS_NETWORK_ERROR = "NETWORK_ERROR"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_ERROR = "ERROR"

CONFIRMED_STATUSES = (STATUS_SUCCESS, STATUS_PRIVATE, STATUS_NOT_FOUND)

ALL_STATUSES = [
    STATUS_SUCCESS, STATUS_PRIVATE, STATUS_NOT_FOUND, STATUS_LOGIN_REQUIRED,
    STATUS_CHALLENGE, STATUS_RATE_LIMITED, STATUS_NETWORK_ERROR,
    STATUS_TIMEOUT, STATUS_ERROR,
]


# =========================================================
# 판별용 텍스트 패턴 (HTML 보조 판별 전용)
# =========================================================
NOT_FOUND_TEXTS = [
    "Sorry, this page isn",
    "isn't available",
    "페이지를 사용할 수 없습니다",
    "죄송합니다. 이 페이지를 사용할 수 없습니다",
]

PRIVATE_TEXTS = [
    "This Account is Private",
    "This account is private",
    "Account is Private",
    "비공개 계정입니다",
    "비공개 계정",
]


# =========================================================
# 로깅
# =========================================================
def setup_logging():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("l1")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


# =========================================================
# reader 연동
# =========================================================
def load_rows(limit=None):
    try:
        from instagram.reader import get_instagram_rows
    except ImportError:
        from reader import get_instagram_rows

    rows = get_instagram_rows()
    if limit:
        rows = rows[:limit]
    return rows


# =========================================================
# 저장 헬퍼
# =========================================================
def _safe_name(username):
    return re.sub(r"[^\w.-]", "_", username)


def save_html(username, html):
    HTML_DIR.mkdir(parents=True, exist_ok=True)
    path = HTML_DIR / f"{_safe_name(username)}.html"
    path.write_text(html, encoding="utf-8")
    return path


def save_graphql_json(username, data):
    GRAPHQL_DIR.mkdir(parents=True, exist_ok=True)
    path = GRAPHQL_DIR / f"{_safe_name(username)}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def append_result(result):
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")


def load_completed():
    """확정 상태(SUCCESS/PRIVATE/NOT_FOUND)의 username을 모아 Resume에서 제외."""
    completed = set()
    if not RESULTS_FILE.exists():
        return completed
    with RESULTS_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("status") in CONFIRMED_STATUSES:
                completed.add(row.get("username"))
    return completed


# =========================================================
# HTML 보조 판별 헬퍼
# =========================================================
def extract_meta(html, prop):
    for m in re.finditer(r"<meta\b[^>]*>", html, re.IGNORECASE):
        tag = m.group(0)
        if re.search(r'property\s*=\s*["\']' + re.escape(prop) + r'["\']', tag, re.IGNORECASE):
            cm = re.search(r'content\s*=\s*["\'](.*?)["\']', tag, re.IGNORECASE | re.DOTALL)
            if cm:
                return cm.group(1)
    return None


def extract_canonical(html):
    for m in re.finditer(r"<link\b[^>]*>", html, re.IGNORECASE):
        tag = m.group(0)
        if re.search(r'rel\s*=\s*["\']canonical["\']', tag, re.IGNORECASE):
            hm = re.search(r'href\s*=\s*["\'](.*?)["\']', tag, re.IGNORECASE)
            if hm:
                return hm.group(1)
    return None


def has_profile_meta(html, username):
    uname = username.lower().strip("/")
    og_url = (extract_meta(html, "og:url") or "").lower().rstrip("/")
    canonical = (extract_canonical(html) or "").lower().rstrip("/")
    for link in (og_url, canonical):
        if link.endswith("/" + uname):
            return True
    og_title = (extract_meta(html, "og:title") or "").lower()
    if f"(@{uname})" in og_title:
        return True
    return False


def looks_logged_out(html):
    if 'name="password"' in html:
        return True
    if re.search(r'action\s*=\s*["\'][^"\']*/accounts/login', html, re.IGNORECASE):
        return True
    return False


def looks_private(html):
    if re.search(r'"is_private"\s*:\s*true', html):
        return True
    return any(t in html for t in PRIVATE_TEXTS)


def looks_not_found(html):
    return any(t in html for t in NOT_FOUND_TEXTS)


def classify_by_html(final_url, http_status, html, username):
    url = (final_url or "").lower()

    if "/accounts/login" in url or "/accounts/suspended" in url:
        return STATUS_LOGIN_REQUIRED, f"[HTML] 로그인 리다이렉트 ({final_url})"
    if "/challenge" in url or "/checkpoint" in url:
        return STATUS_CHALLENGE, f"[HTML] 챌린지/체크포인트 ({final_url})"

    if http_status == 404:
        return STATUS_NOT_FOUND, "[HTML] HTTP 404"

    if not html:
        return STATUS_ERROR, "[HTML] HTML 없음"

    if has_profile_meta(html, username):
        if looks_private(html):
            return STATUS_PRIVATE, "[HTML] 프로필 메타 + 비공개 신호"
        return STATUS_SUCCESS, "[HTML] 프로필 메타 확인"

    if looks_logged_out(html):
        return STATUS_LOGIN_REQUIRED, "[HTML] 로그인 폼/월"

    if len(html) < 2000 or "</html>" not in html.lower():
        return STATUS_ERROR, "[HTML] 응답 불완전"

    if looks_not_found(html):
        return STATUS_NOT_FOUND, "[HTML] 프로필 메타 없음(구조적) + 계정없음 텍스트"
    return STATUS_NOT_FOUND, "[HTML] 프로필 메타 없음(구조적)"


# =========================================================
# 결과 레코드
# =========================================================
def _blank_result(row):
    username = row["username"]
    return {
        "key": row.get("key"),
        "username": username,
        "pk": None,
        "url": f"https://www.instagram.com/{username}/",
        "final_url": None,
        "http_status": None,
        "status": STATUS_ERROR,
        "reason": "",
        "html_path": None,
        "graphql_json_path": None,
        "user_id": None,
        "nickname": None,
        "followers": None,
        "following": None,
        "posts": None,
        "biography": None,
        "external_url": None,
        "category_name": None,
        "account_type": None,
        "is_private": None,
        "is_verified": None,
        "profile_pic_url": None,
        "ts": datetime.now().isoformat(timespec="seconds"),
    }


def _join_reason(*parts):
    return " | ".join(p for p in parts if p)


# =========================================================
# GraphQL 캡처 헬퍼
# =========================================================
def _is_profile_graphql(response):
    """PolarisProfilePageContentQuery 프로필 GraphQL 응답인지 (요청 헤더 기준)."""
    try:
        req = response.request
        if req.method != "POST":
            return False
        if GRAPHQL_URL_PART not in response.url:
            return False
        return req.headers.get("x-fb-friendly-name") == PROFILE_QUERY_NAME
    except Exception:
        return False


def _read_graphql_json(response):
    """GraphQL body -> dict. 실패해도 절대 예외를 던지지 않고 None."""
    if response is None:
        return None
    try:
        return response.json()
    except Exception:
        try:
            txt = response.text()
            return json.loads(txt) if txt else None
        except Exception:
            return None


def _looks_terminal(http_status, final_url):
    """더 기다릴 필요 없이 즉시 결론나는 상태 -> grace 를 통째로 건너뛴다."""
    if http_status in (404, 410):
        return True
    u = (final_url or "").lower()
    if "/accounts/login" in u or "/accounts/suspended" in u:
        return True
    if "/challenge" in u or "/checkpoint" in u:
        return True
    return False


# =========================================================
# 단일 계정 크롤링
# =========================================================
def crawl_one(page, row):
    username = row["username"]
    result = _blank_result(row)
    url = result["url"]
    notes = []

    # --- GraphQL 캡처 리스너: goto '이전'에 등록해야 goto 중 도착분을 놓치지 않는다 ---
    captured = {"response": None}

    def _on_response(response):
        if captured["response"] is not None:
            return
        if _is_profile_graphql(response):
            captured["response"] = response

    page.on("response", _on_response)

    goto_ok = False
    goto_error = None

    try:
        # ---- (1) 네비게이션: goto '자체' 타임아웃만 담당 (GraphQL 대기와 완전 분리) ----
        try:
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=L1_GOTO_TIMEOUT_MS,
            )
            goto_ok = True
            result["http_status"] = response.status if response else None
        except PWTimeout:
            goto_error = "timeout"
            notes.append("goto 타임아웃")
        except Exception as e:
            goto_error = "network"
            notes.append(f"goto 예외: {e!r}")

        try:
            result["final_url"] = page.url
        except Exception:
            pass

        # ---- (2) GraphQL 유예 대기: '짧게만'. 안 오면 그대로 HTML 판별로 넘어간다 ----
        #      없는 계정(404 등)은 _looks_terminal 로 grace 자체를 건너뛴다.
        if (
            goto_ok
            and captured["response"] is None
            and not _looks_terminal(result["http_status"], result["final_url"])
        ):
            deadline = time.monotonic() + (L1_GRAPHQL_GRACE_MS / 1000.0)
            # wait_for_timeout 이 이벤트 루프를 pump 하므로 그 사이 리스너가 응답을 잡는다.
            while captured["response"] is None and time.monotonic() < deadline:
                page.wait_for_timeout(100)

        # HTML 로 판별할 예정이면(=GraphQL 못 잡음) SPA 렌더 잠깐 안정화
        if goto_ok and captured["response"] is None:
            try:
                page.wait_for_timeout(L1_RENDER_WAIT_MS)
            except Exception:
                pass

    finally:
        # 리스너 반드시 제거. 같은 page 를 루프에서 재사용하므로 안 지우면
        # 계정마다 리스너가 누적되어 후반부로 갈수록 느려지고 오탐이 생긴다.
        page.remove_listener("response", _on_response)

    # ---- (3) HTML 항상 저장 (성패 무관) ----
    html = None
    try:
        html = page.content()
        try:
            result["final_url"] = page.url  # 리다이렉트 후 최종 URL 갱신
        except Exception:
            pass
        result["html_path"] = str(save_html(username, html))
    except Exception as e:
        notes.append(f"html 확보 실패: {e!r}")

    # ---- (4) GraphQL 저장 + 파싱 (왔을 때만) ----
    graphql = _read_graphql_json(captured["response"])
    profile = None
    if graphql is not None:
        try:
            result["graphql_json_path"] = str(save_graphql_json(username, graphql))
        except Exception as e:
            notes.append(f"GraphQL 저장 실패: {e!r}")
        try:
            profile = parse_profile(graphql)   # user=null 이면 None 반환
        except Exception as e:
            notes.append(f"GraphQL 파싱 예외: {e!r}")
        if profile:
            result.update(profile)

    # ---- (5) 상태 판별: GraphQL 우선, 없으면 HTML ----
    if profile:
        if result.get("is_private"):
            status, reason = STATUS_PRIVATE, "GraphQL + private"
        else:
            status, reason = STATUS_SUCCESS, "GraphQL + profile"
    else:
        # GraphQL 이 없는 것은 '오류가 아니라 정상 분기'.
        # 단, HTML 조차 못 건졌고 goto 도 실패했을 때만 진짜 오류로 남긴다.
        if not html:
            if goto_error == "timeout":
                status, reason = STATUS_TIMEOUT, "goto 타임아웃 + HTML 없음"
            elif goto_error == "network":
                status, reason = STATUS_NETWORK_ERROR, "goto 네트워크 예외 + HTML 없음"
            else:
                status, reason = STATUS_ERROR, "HTML 없음"
        else:
            status, reason = classify_by_html(
                result["final_url"],
                result["http_status"],
                html,
                username,
            )
            if goto_error:  # goto 는 실패했지만 HTML 은 건진 경우 흔적만 남김
                notes.append(f"goto_error={goto_error} (HTML 확보됨, HTML 판별)")

    result["status"] = status
    result["reason"] = _join_reason(reason, *notes)
    return result


# =========================================================
# 요약
# =========================================================
def summarize(results, log):
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    log.info("=" * 44)
    log.info("결과 요약 (총 %d개)", len(results))
    for status in ALL_STATUSES:
        if counts.get(status):
            log.info("  %-16s %d", status, counts[status])

    for status in ALL_STATUSES:
        if status == STATUS_SUCCESS:
            continue
        names = [r["username"] for r in results if r["status"] == status]
        if names:
            shown = ", ".join(names[:30]) + (" ..." if len(names) > 30 else "")
            log.info("  - %s: %s", status, shown)
    log.info("=" * 44)

# =========================================================
# 메인
# =========================================================
def main(limit=BATCH_LIMIT, headless=HEADLESS, resume=True):
    log = setup_logging()

    if not resume and RESULTS_FILE.exists():
        RESULTS_FILE.unlink()
        log.info("resume=False: 기존 결과 파일 삭제")

    if not SESSION_FILE.exists():
        log.error("세션 파일이 없습니다: %s (login.py를 먼저 실행하세요)", SESSION_FILE)
        return

    rows = load_rows(limit)
    log.info("전체 대상 : %d개", len(rows))

    completed = load_completed()
    if completed:
        log.info("확정 상태(제외) : %d개", len(completed))
    rows = [row for row in rows if row["username"] not in completed]
    log.info("신규 수집 대상 : %d개", len(rows))
    if not rows:
        log.info("처리할 계정 없음 (다 끝남).")
        return

    log.info("딜레이 %.1f~%.1f초 | 차단 연속 %d회 중단",
             L1_DELAY_MIN, L1_DELAY_MAX, STOP_ON_429)

    # 차단으로 간주할 상태 (세션 보호용)
    BLOCK_STATUSES = (STATUS_CHALLENGE, STATUS_LOGIN_REQUIRED, STATUS_RATE_LIMITED)

    results = []
    block_streak = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            storage_state=str(SESSION_FILE),
            locale=LOCALE,
            user_agent=USER_AGENT,
        )
        page = context.new_page()

        for i, row in enumerate(rows, 1):
            r = crawl_one(page, row)
            results.append(r)
            append_result(r)

            log.info(
                "[%d/%d] @%s -> %s (http=%s) :: %s",
                i,
                len(rows),
                r["username"],
                r["status"],
                r["http_status"],
                r["reason"],
            )

            # ── 차단 신호 연속 감지 -> 세션 보호를 위해 중단 ──
            if r["status"] in BLOCK_STATUSES:
                block_streak += 1
                log.warning("차단 신호 연속 %d회 (%s @%s)",
                            block_streak, r["status"], r["username"])
                if block_streak >= STOP_ON_429:
                    log.error(
                        "차단 %d회 연속 -> 세션 보호를 위해 중단 "
                        "(딜레이를 늘리거나 세션을 재발급하세요)",
                        STOP_ON_429,
                    )
                    break
            else:
                block_streak = 0  # 정상 결과가 나오면 연속 카운트 리셋

            time.sleep(random.uniform(L1_DELAY_MIN, L1_DELAY_MAX))

        context.close()
        browser.close()

    summarize(results, log)


if __name__ == "__main__":
    main(limit=BATCH_LIMIT, headless=HEADLESS, resume=True)