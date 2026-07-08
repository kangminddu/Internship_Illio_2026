import pandas as pd
import re

XLSX = "/Users/kangminsoo/Desktop/Internship_Illio_2026/SNS_정보.xlsx"

df = pd.read_excel(XLSX, dtype=str).drop_duplicates()

yt = df[df["youtubeurl"].notna()].copy()
yt = yt.drop_duplicates(subset=["키값"])

def classify(u):
    u = str(u).strip()

    if re.search(r"/channel/UC[\w-]{22}", u):
        return "resolved"

    if "/@" in u:
        return "handle_only"

    if "/c/" in u:
        return "custom_only"

    if "/user/" in u:
        return "user_legacy"

    if "goo.gl" in u:
        return "unresolved"

    if "youtube.com" not in u and "youtu.be" not in u:
        return "unresolved"

    return "unresolved"

yt["status"] = yt["youtubeurl"].apply(classify)

# resolved 제외
target = yt[
    yt["status"].isin(["handle_only", "custom_only", "user_legacy"])
][["키값", "youtubeurl", "status"]]

print(target["status"].value_counts())
print(f"\n총 {len(target)}개")

target.to_csv(
    "seed_handle_etc.csv",
    index=False,
    encoding="utf-8-sig"
)

print("\nseed_handle_etc.csv 저장 완료")