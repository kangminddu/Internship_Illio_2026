import pandas as pd
import pymysql
import re

XLSX = "/Users/kangminsoo/Desktop/Internship_Illio_2026/SNS_정보.xlsx"

conn = pymysql.connect(
    host="127.0.0.1",
    port=3306,
    user="root",
    password="",
    database="fandom_crm",
    charset="utf8mb4"
)

with conn.cursor() as cur:
    cur.execute("SELECT seed_key FROM creators")
    already = {row[0] for row in cur.fetchall()}

conn.close()

# Sheet2 읽기
df = pd.read_excel(
    XLSX,
    sheet_name=1,
    dtype=str
)

# 컬럼 공백 제거
df.columns = df.columns.str.replace(" ", "").str.strip()

# 중복 제거
df = df.drop_duplicates(subset=["상품명2", "유튜브"])

# 유튜브 URL 있는 것만
yt = df[df["유튜브"].notna()].copy()

# seed_key 생성
yt["seed_key"] = yt["상품명2"]

# 이미 등록된 것은 제외
yt = yt[~yt["seed_key"].isin(already)]

def detect_status(url):
    url = str(url)

    if re.search(r"/channel/UC[\w-]{22}", url):
        return "resolved"

    if "/@" in url:
        return "handle_only"

    if "/c/" in url:
        return "custom_only"

    if "/user/" in url:
        return "user_legacy"

    return "unresolved"

yt["status"] = yt["유튜브"].apply(detect_status)

seed = yt[[
    "seed_key",
    "유튜브",
    "status"
]]

seed.columns = [
    "키값",
    "youtubeurl",
    "status"
]

seed.to_csv(
    "seed_sheet2.csv",
    index=False,
    encoding="utf-8-sig"
)

print(f"{len(seed)}개 생성 완료")
print(seed["status"].value_counts())
print(seed.head())