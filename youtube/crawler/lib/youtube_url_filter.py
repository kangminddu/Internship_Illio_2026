# -*- coding: utf-8 -*-
"""
YouTube 채널 URL 검증/정규화 모듈 (seed.py에서 import해서 사용)

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
_CHANNEL_TABS = {
    "videos", "shorts", "streams", "live", "featured", "about",
    "community", "playlists", "channels", "store", "releases", "podcasts",
}

# 첫 세그먼트가 이것이면 채널이 아님 (시스템/콘텐츠 경로)
_NON_CHANNEL_FIRST_SEGMENTS = {
    "watch", "playlist", "results", "feed", "gaming", "premium",
    "account", "reporthistory", "shorts",  # /shorts/<id> 는 영상. 채널의 shorts탭은 /@x/shorts 형태
    "embed", "live", "redirect", "post", "hashtag", "source",
    "audiolibrary", "music", "movies", "trending", "t", "s", "howyoutubeworks",
    "ads", "creators", "about", "new", "upload", "logout", "signin",
    "playables", "clip", "attribution_link", "oembed", "img", "yts",
}

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
    if "@" in s and "/" not in s and not s.lower().startswith("http"):
        return None, "not_a_url (email?)"

    # 스킴 보정
    if not s.lower().startswith(("http://", "https://")):
        s = "https://" + s

    try:
        p = urlparse(s)
    except ValueError:
        return None, "unparseable"

    host = (p.hostname or "").lower()

    # youtu.be 는 영상 단축링크 -> 제외 (정책 3)
    if host == "youtu.be":
        return None, "youtu.be video link"

    # YouTube 도메인이 아니면 제외 (정책 1)
    if host not in _YT_HOSTS:
        return None, f"non-youtube domain: {host or '?'}"

    # 경로 분해 (percent-encoding 된 한글 handle 대비 unquote)
    path = unquote(p.path or "/").strip("/")
    if not path:
        return None, "youtube root (no channel path)"

    segs = path.split("/")
    first = segs[0]

    # --- 채널 유형별 판별 ---
    # 1) @handle (한글/유니코드 전부 허용)
    if first.startswith("@") and len(first) > 1:
        return f"https://www.youtube.com/{first}", None

    # 2) /channel/UC...
    if first == "channel":
        if len(segs) >= 2 and segs[1]:
            return f"https://www.youtube.com/channel/{segs[1]}", None
        return None, "channel/ without id"

    # 3) /c/이름, /user/이름
    if first in ("c", "user"):
        if len(segs) >= 2 and segs[1]:
            return f"https://www.youtube.com/{first}/{segs[1]}", None
        return None, f"{first}/ without name"

    # 4) 명백히 채널이 아닌 시스템/콘텐츠 경로
    if first.lower() in _NON_CHANNEL_FIRST_SEGMENTS:
        return None, f"non-channel path: /{first}"

    # 5) 구형 custom URL: youtube.com/이름  (예: /gamzayvaw)
    #    두 번째 세그먼트가 탭이면 잘라내고 채널 루트만 저장
    if len(segs) == 1 or (len(segs) >= 2 and segs[1].lower() in _CHANNEL_TABS):
        return f"https://www.youtube.com/{first}", None

    # 그 외 (예: /이름/뭔지모를경로) 는 보수적으로 제외
    return None, f"unrecognized path: /{path}"


# ---- 간단 자가 테스트 ----
if __name__ == "__main__":
    should_pass = [
        "https://www.youtube.com/@자용이",
        "https://www.youtube.com/@규온이kyuon",
        "https://www.youtube.com/@멸화랑",
        "https://www.youtube.com/@이히노HINO",
        "https://www.youtube.com/@필론-v5o",
        "https://youtube.com/gamzayvaw",
        "https://youtube.com/gumikoh_nari",
        "https://www.youtube.com/@하루052/videos",
        "https://www.youtube.com/c/somename",
        "https://www.youtube.com/user/olduser",
        "https://www.youtube.com/channel/UCabc123",
        "youtube.com/@no-scheme",
        "https://www.youtube.com/%EC%9E%90%EC%9A%A9",  # percent-encoded
    ]
    should_skip = [
        "https://youtu.be/xxxx",
        "https://www.youtube.com/watch?v=abc",
        "https://www.youtube.com/",
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/shorts/abc123",
        "https://www.instagram.com/foo",
        "https://www.tiktok.com/@foo",
        "https://www.twitch.tv",
        "https://goo.gl/abc",
        "https://bit.ly/abc",
        "https://c11.kr/abc",
        "someone@gmail.com",
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