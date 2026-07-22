# code/test_tiktok_pipeline.py

import os
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("ENSEMBLE_TOKEN")
ROOT = "https://ensembledata.com/apis"

VIDEO_URL = "https://www.tiktok.com/@daviddobrik/video/7401663653528882463"


# =====================================================
# POST 정보
# =====================================================
def get_post(url):
    endpoint = "/tt/post/info"

    params = {
        "url": url,
        "token": TOKEN,
        "new_version": False,
        "download_video": False
    }

    res = requests.get(ROOT + endpoint, params=params)

    print("POST STATUS:", res.status_code)

    if res.status_code != 200:
        print(res.text)
        raise Exception("Post API Error")

    return res.json()["data"][0]


# =====================================================
# 댓글
# =====================================================
def get_comments(aweme_id, cursor=0):
    endpoint = "/tt/post/comments"

    params = {
        "aweme_id": aweme_id,
        "cursor": cursor,
        "token": TOKEN
    }

    res = requests.get(ROOT + endpoint, params=params)

    print("COMMENT STATUS:", res.status_code)

    if res.status_code != 200:
        print(res.text)
        raise Exception("Comment API Error")

    return res.json()


# =====================================================
# 시간 변환
# =====================================================
def ts(timestamp):
    if not timestamp:
        return None

    return datetime.fromtimestamp(timestamp)


# =====================================================
# 영상 가져오기
# =====================================================
post = get_post(VIDEO_URL)

stats = post["statistics"]
video = post["video"]
author = post["author"]


# =====================================================
# 해시태그
# =====================================================
hashtags = []

for extra in post.get("text_extra", []):

    hashtag = extra.get("hashtag_name")

    if hashtag:
        hashtags.append(hashtag)


# =====================================================
# L1 채널 단위
# =====================================================
creator = {
    "followers": author.get("follower_count"),
    "following": author.get("following_count"),
    "total_likes": author.get("total_favorited"),
    "bio": author.get("signature")
}

print("\n===== L1 CREATOR =====")

for k, v in creator.items():
    print(f"{k}: {v}")


# =====================================================
# L2 영상 단위
# =====================================================
content = {
    "video_id": post["aweme_id"],
    "created_at": ts(post["create_time"]),
    "play_count": stats["play_count"],
    "like_count": stats["digg_count"],
    "comment_count": stats["comment_count"],
    "duration_sec": video["duration"] / 1000,
    "hashtags": hashtags
}

print("\n===== L2 CONTENT =====")

for k, v in content.items():
    print(f"{k}: {v}")


# =====================================================
# L3 댓글 단위
# =====================================================
comments_response = get_comments(post["aweme_id"])

comments = (
    comments_response
    .get("data", {})
    .get("comments", [])
)

print("\n===== L3 COMMENTS =====")
print("댓글 개수:", len(comments))

comment_rows = []

for c in comments:

    user = c.get("user", {})

    row = {
        "comment_user_id": user.get("uid"),
        "comment_nickname": user.get("nickname"),
        "comment_text": c.get("text"),
        "created_at": ts(c.get("create_time")),
        "like_count": c.get("digg_count")
    }

    comment_rows.append(row)


for c in comment_rows[:10]:

    print("----------------------")
    print("작성자 ID:", c["comment_user_id"])
    print("닉네임:", c["comment_nickname"])
    print("댓글:", c["comment_text"])
    print("작성일:", c["created_at"])
    print("좋아요:", c["like_count"])