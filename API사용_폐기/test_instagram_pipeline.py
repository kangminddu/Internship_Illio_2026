import os
import re
import requests

from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("ENSEMBLE_TOKEN")
ROOT = "https://ensembledata.com/apis"

USERNAME = "daviddobrik"


# =====================================================
# 시간 변환
# =====================================================
def ts(timestamp):

    if not timestamp:
        return None

    return datetime.fromtimestamp(timestamp)


# =====================================================
# 해시태그
# =====================================================
def extract_hashtags(text):

    if not text:
        return []

    return re.findall(r"#(\w+)", text)


# =====================================================
# User Detailed Info
# =====================================================
def get_user(username):

    endpoint = "/instagram/user/detailed-info"

    params = {
        "username": username,
        "token": TOKEN
    }

    res = requests.get(ROOT + endpoint, params=params)

    print("USER STATUS:", res.status_code)

    if res.status_code != 200:
        print(res.text)
        raise Exception("User API Error")

    return res.json()["data"]


# =====================================================
# User Posts
# =====================================================
def get_posts(user_id):

    endpoint = "/instagram/user/posts"

    params = {
        "user_id": user_id,
        "depth": 1,
        "chunk_size": 10,
        "start_cursor": "",
        "alternative_method": False,
        "token": TOKEN
    }

    res = requests.get(ROOT + endpoint, params=params)

    print("POST STATUS:", res.status_code)

    if res.status_code != 200:
        print(res.text)
        raise Exception("Post API Error")

    return res.json()["data"]["posts"]


# =====================================================
# Post Comments
# =====================================================
def get_comments(media_id):

    endpoint = "/instagram/post/comments"

    params = {
        "media_id": media_id,
        "cursor": "",
        "sorting": "popular",
        "token": TOKEN
    }

    res = requests.get(ROOT + endpoint, params=params)

    print("COMMENT STATUS:", res.status_code)

    if res.status_code != 200:
        print(res.text)
        raise Exception("Comment API Error")

    return res.json()["data"]["comments"]


# =====================================================
# L1 CREATOR
# =====================================================
user = get_user(USERNAME)

creator = {
    "followers":
        user["edge_followed_by"]["count"],

    "following":
        user["edge_follow"]["count"],

    "post_count":
        user["edge_owner_to_timeline_media"]["count"],

    "bio":
        user["biography"]
}

print("\n===== L1 CREATOR =====")

for k, v in creator.items():
    print(f"{k}: {v}")


# =====================================================
# L2 CONTENT
# =====================================================
posts = get_posts(user["id"])

post = posts[0]["node"]

caption = ""

caption_edges = post["edge_media_to_caption"]["edges"]

if caption_edges:
    caption = caption_edges[0]["node"]["text"]

content = {
    "post_id":
        post["id"],

    "created_at":
        ts(post["taken_at_timestamp"]),

    "like_count":
        post["edge_media_preview_like"]["count"],

    "comment_count":
        post["edge_media_to_comment"]["count"],

    "hashtags":
        extract_hashtags(caption)
}

print("\n===== L2 CONTENT =====")

for k, v in content.items():
    print(f"{k}: {v}")


# =====================================================
# L3 COMMENTS
# =====================================================
comments = get_comments(post["id"])

print("\n===== L3 COMMENTS =====")
print("댓글 개수:", len(comments))

for c in comments[:10]:

    node = c["node"]
    user = node["user"]

    print("----------------------")
    print("작성자 ID:", user["id"])
    print("닉네임:", user["username"])
    print("댓글:", node["text"])
    print("작성일:", ts(node["created_at"]))
    print("좋아요:", node.get("comment_like_count"))