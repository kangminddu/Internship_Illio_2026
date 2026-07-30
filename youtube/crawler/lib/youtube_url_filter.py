# -*- coding: utf-8 -*-
"""
youtube/crawler/lib/youtube_url_filter.py — 채널 URL 검증/정규화

왜 별도 모듈인가
------
시드 엑셀의 '유튜브' 열에는 사람이 손으로 넣은 값이 들어온다.
실제로 나온 것들:

    https://www.youtube.com/@침착맨            정상
    youtube.com/gamzayvaw                      구형 custom URL (스킴 없음)
    https://www.youtube.com/@하루052/videos    탭 경로가 붙음
    https://www.youtube.com/%EC%9E%90%EC%9A%A9 한글이 percent-encoding 됨
    https://www.instagram.com/foo              다른 플랫폼
    https://youtu.be/xxxx                      영상 링크
    someone@gmail.com                          이메일

이걸 seed.py 안에서 처리하면 조건문이 뒤엉킨다. 그리고 "왜 이 URL은
저장 안 됐지?"를 나중에 추적할 수 없다.

→ 판별 로직을 한 곳에 몰고, Skip할 때 '사유'를 함께 반환하게 했다.
   seed.py가 그 사유를 집계해서 출력한다:
       - non-youtube domain: instagram.com : 12건
       - youtu.be video link : 5건
   이게 곧 시드 엑셀의 품질 리포트가 된다.

반환 규약
------
    (정규화된 URL, None)  → 저장
    (None, 사유 문자열)   → Skip

성공/실패를 두 값으로 나눠 반환하는 이유는, 실패를 그냥 None으로만
돌려주면 "왜 실패했는지"가 사라지기 때문이다.
(이 프로젝트 전반의 원칙 — 실패도 구조화해서 전달한다)

정책:
  1) youtube.com 도메인이 아니면 저장하지 않음 (instagram/tiktok/twitch/단축URL/이메일 등 제외)
  2) YouTube 채널 URL은 최대한 저장:
       /@handle        (한글/유니코드 handle 포함)
       /c/이름
       /user/이름
       /channel/UCxxxx
       /이름            (구형 custom URL, 예: youtube.com/gamzayvaw)
     뒤에 /videos, /shorts, /streams, /featured, /about, /community,
     /playlists, /store 등 탭 경로가 붙어도 채널 루트로 정규화하여 저장
  3) 채널이 아닌 URL은 저장하지 않음:
       watch?v=..., /shorts/영상ID 단독, playlist?list=..., youtu.be/...,
       youtube.com/ (루트만), /results, /feed, /gaming 등 시스템 경로
"""
from urllib.parse import urlparse, unquote

# 채널 경로 뒤에 붙을 수 있는 "탭" — 있어도 채널로 인정하고 잘라냄
#
# 사람이 브라우저에서 URL을 복사할 때 탭 경로가 딸려온다.
# /@침착맨/videos 를 "채널이 아니다"로 버리면 멀쩡한 채널을 놓친다.
_CHANNEL_TABS = {
    "videos", "shorts", "streams", "live", "featured", "about",
    "community", "playlists", "channels", "store", "releases", "podcasts",
}

# 첫 세그먼트가 이것이면 채널이 아님 (시스템/콘텐츠 경로)
#
# 화이트리스트가 아니라 블랙리스트인 이유:
# 구형 custom URL(youtube.com/이름)은 첫 세그먼트가 '아무 문자열'이라
# 화이트리스트로는 걸러낼 수 없다. 채널이 아닌 것만 명시하고
# 나머지는 채널로 간주하는 방식을 택했다.
_NON_CHANNEL_FIRST_SEGMENTS = {
    "watch", "playlist", "results", "feed", "gaming", "premium",
    "account", "reporthistory", "shorts",  # /shorts/<id> 는 영상. 채널의 shorts탭은 /@x/shorts 형태
    "embed", "live", "redirect", "post", "hashtag", "source",
    "audiolibrary", "music", "movies", "trending", "t", "s", "howyoutubeworks",
    "ads", "creators", "about", "new", "upload", "logout", "signin",
    "playables", "clip", "attribution_link", "oembed", "img", "yts",
}
# ↑ "shorts"가 여기와 _CHANNEL_TABS 양쪽에 있다. 오타가 아니라 위치로 구분한다.
#   /shorts/abc123      → 첫 세그먼트가 shorts = 영상 링크 → 제외
#   /@침착맨/shorts     → 두 번째 세그먼트가 shorts = 채널 탭 → 채널로 인정

_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}


def normalize_youtube_channel_url(raw: str):
    """
    반환:
      (정규화된 채널 URL, None)  -> 저장
      (None, 사유 문자열)        -> Skip
    """
    if not raw or not isinstance(raw, str):
        return None, "empty"

    s = raw.strip()

    # 이메일 주소 등 URL이 아닌 것
    # ('@'가 있는데 '/'가 없고 스킴도 없으면 이메일로 본다.
    #  아래에서 스킴을 붙이면 "https://someone@gmail.com"이 되어
    #  urlparse가 host를 gmail.com으로 잡아버리므로 미리 걸러야 한다)
    if "@" in s and "/" not in s and not s.lower().startswith("http"):
        return None, "not_a_url (email?)"

    # 스킴 보정
    # 엑셀에는 "youtube.com/gamzayvaw"처럼 스킴 없이 적힌 값이 흔하다.
    # urlparse는 스킴이 없으면 전체를 path로 인식해 hostname이 None이 된다.
    if not s.lower().startswith(("http://", "https://")):
        s = "https://" + s

    try:
        p = urlparse(s)
    except ValueError:
        return None, "unparseable"

    host = (p.hostname or "").lower()

    # youtu.be 는 영상 단축링크 -> 제외 (정책 3)
    # 도메인은 유튜브지만 채널이 아니라 영상이다. 별도로 먼저 걸러낸다.
    if host == "youtu.be":
        return None, "youtu.be video link"

    # YouTube 도메인이 아니면 제외 (정책 1)
    # 시드 엑셀의 '유튜브' 열에 인스타/틱톡/트위치 주소가 들어오는 경우가 있다.
    if host not in _YT_HOSTS:
        return None, f"non-youtube domain: {host or '?'}"

    # 경로 분해 (percent-encoding 된 한글 handle 대비 unquote)
    #
    # 한글 핸들을 브라우저에서 복사하면 %EC%9E%90%EC%9A%A9 형태가 된다.
    # unquote 없이 처리하면 "%EC%9E%90..."을 채널명으로 인식해
    # 나중에 요청할 때 이중 인코딩되어 404가 난다.
    path = unquote(p.path or "/").strip("/")
    if not path:
        return None, "youtube root (no channel path)"

    segs = path.split("/")
    first = segs[0]

    # --- 채널 유형별 판별 ---
    # 순서가 중요하다. 구체적인 형태(@, channel, c, user)를 먼저 확정하고,
    # 명백한 비채널을 걸러낸 뒤, 남은 것을 구형 custom URL로 본다.

    # 1) @handle (한글/유니코드 전부 허용)
    # 정규식으로 문자 종류를 제한하지 않는다. 유튜브 핸들에는
    # 한글, 일본어, 이모지 계열까지 들어갈 수 있어 화이트리스트가 무의미하다.
    if first.startswith("@") and len(first) > 1:
        return f"https://www.youtube.com/{first}", None

    # 2) /channel/UC...
    # 가장 확실한 형태. UC ID가 곧 유튜브의 영구 식별자다.
    if first == "channel":
        if len(segs) >= 2 and segs[1]:
            return f"https://www.youtube.com/channel/{segs[1]}", None
        return None, "channel/ without id"

    # 3) /c/이름, /user/이름  (구형 커스텀 / 레거시 계정)
    if first in ("c", "user"):
        if len(segs) >= 2 and segs[1]:
            return f"https://www.youtube.com/{first}/{segs[1]}", None
        return None, f"{first}/ without name"

    # 4) 명백히 채널이 아닌 시스템/콘텐츠 경로
    # 이 검사가 5)보다 먼저 와야 한다. 순서가 반대면
    # /watch, /playlist 같은 것도 "구형 custom URL"로 인정되어 저장된다.
    if first.lower() in _NON_CHANNEL_FIRST_SEGMENTS:
        return None, f"non-channel path: /{first}"

    # 5) 구형 custom URL: youtube.com/이름  (예: /gamzayvaw)
    #    두 번째 세그먼트가 탭이면 잘라내고 채널 루트만 저장
    #
    #    /gamzayvaw          → 세그먼트 1개 → 채널
    #    /gamzayvaw/videos   → 두 번째가 탭 → 탭을 버리고 채널 루트
    if len(segs) == 1 or (len(segs) >= 2 and segs[1].lower() in _CHANNEL_TABS):
        return f"https://www.youtube.com/{first}", None

    # 그 외 (예: /이름/뭔지모를경로) 는 보수적으로 제외
    #
    # 판별 불가한 것을 억지로 저장하면 L1이 404를 받고, 그 채널은
    # deleted로 마킹되어 영구 제외된다. 애초에 안 넣는 편이 낫다.
    # (Skip은 seed 로그에 남아 나중에 사람이 확인할 수 있다)
    return None, f"unrecognized path: /{path}"


# ---- 간단 자가 테스트 ----
#
# 별도 테스트 프레임워크 없이 파일 자체를 실행해 검증한다.
#   python -m youtube.crawler.lib.youtube_url_filter
#
# 여기 나열된 케이스는 전부 '실제 시드 엑셀에서 나온 것'이다.
# 새 엑셀에서 예상 못 한 형태가 나오면 여기 추가하고 돌려서
# 기존 케이스가 깨지지 않는지 확인한다. (회귀 테스트 역할)
if __name__ == "__main__":
    should_pass = [
        "https://www.youtube.com/@자용이",                  # 한글 핸들
        "https://www.youtube.com/@규온이kyuon",             # 한글+영문 혼합
        "https://www.youtube.com/@멸화랑",
        "https://www.youtube.com/@이히노HINO",
        "https://www.youtube.com/@필론-v5o",                # 하이픈 포함
        "https://youtube.com/gamzayvaw",                    # 구형 custom, www 없음
        "https://youtube.com/gumikoh_nari",
        "https://www.youtube.com/@하루052/videos",          # 탭 경로 붙음
        "https://www.youtube.com/c/somename",
        "https://www.youtube.com/user/olduser",
        "https://www.youtube.com/channel/UCabc123",
        "youtube.com/@no-scheme",                           # 스킴 없음
        "https://www.youtube.com/%EC%9E%90%EC%9A%A9",  # percent-encoded
    ]
    should_skip = [
        "https://youtu.be/xxxx",                            # 영상 단축링크
        "https://www.youtube.com/watch?v=abc",              # 영상
        "https://www.youtube.com/",                         # 루트만
        "https://www.youtube.com/playlist?list=PL123",      # 재생목록
        "https://www.youtube.com/shorts/abc123",            # 쇼츠 영상 (채널 탭 아님)
        "https://www.instagram.com/foo",                    # 다른 플랫폼
        "https://www.tiktok.com/@foo",
        "https://www.twitch.tv",
        "https://goo.gl/abc",                               # 단축 URL
        "https://bit.ly/abc",
        "https://c11.kr/abc",
        "someone@gmail.com",                                # 이메일
        "",
    ]
    ok = True
    for u in should_pass:
        norm, why = normalize_youtube_channel_url(u)
        if not norm:
            print(f"[FAIL-pass] {u!r} -> skip ({why})"); ok = False
        else:
            print(f"[pass] {u!r} -> {norm}")
    for u in should_skip:
        norm, why = normalize_youtube_channel_url(u)
        if norm:
            print(f"[FAIL-skip] {u!r} -> saved as {norm}"); ok = False
        else:
            print(f"[skip] {u!r} ({why})")
    print("ALL OK" if ok else "SOME FAILURES")