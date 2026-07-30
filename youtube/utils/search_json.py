# utils/search_json.py
"""
API 응답 JSON의 특정 경로를 눈으로 확인하는 1회성 탐색 스크립트.

★ 파이프라인의 일부가 아니다.
   extract_session.py로 받은 응답을 browse.json으로 저장해두고,
   "이 경로에 정확히 뭐가 들어있나"를 확인하려고 만든 것.

왜 필요했나
------
유튜브 응답 JSON은 중첩이 7~8단계라 그냥 print하면 수만 줄이 나온다.
파서를 짜려면 특정 지점만 골라서 봐야 했다.

이 파일로 확인한 것:
  metadataRows 안에 조회수와 게시일이 어떤 키로, 어떤 순서로 들어있는지
  → youtube_parser.parse_l2_videos의 파싱 경로가 여기서 나왔다.

경로를 하드코딩한 이유
------
조사용이라 일부러 그렇게 했다. 경로가 틀리면 KeyError가 나는데,
그게 곧 "구조가 내 예상과 다르다"는 신호다. 조사 단계에서는
조용히 None을 반환하는 것보다 터지는 게 낫다.

반면 실제 파서(youtube_parser.find_first)는 정반대로 만들었다.
경로를 모르고도 키 이름으로 찾도록 — 유튜브가 구조를 바꿔도 견디게.
같은 JSON을 다루는데 목적에 따라 설계가 정반대가 되는 사례다.

사용법:
  1) extract_session.py 실행 → 응답을 browse.json으로 저장
  2) 이 파일의 경로를 바꿔가며 실행해 구조 확인
"""
import json

with open("browse.json", encoding="utf-8") as f:
    data = json.load(f)

# 스크롤로 추가된 항목들의 경로.
# onResponseReceivedActions[0] → appendContinuationItemsAction
# = "기존 목록 뒤에 이만큼 덧붙여라"는 지시.
# 첫 로드가 아니라 '추가 로드' 응답이라 이 경로를 탄다.
items = data["onResponseReceivedActions"][0]["appendContinuationItemsAction"]["continuationItems"]

# 첫 항목의 메타데이터만 꺼낸다.