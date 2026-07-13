import json

with open("browse.json", encoding="utf-8") as f:
    data = json.load(f)

items = data["onResponseReceivedActions"][0]["appendContinuationItemsAction"]["continuationItems"]

meta = items[0]["richItemRenderer"]["content"]["lockupViewModel"]["metadata"]

print(meta.keys())

print(json.dumps(meta, indent=2, ensure_ascii=False)[:5000])