# Creator Fandom CRM

Creator Fandom CRM은 YouTube, Instagram, TikTok 크리에이터의 데이터를 자동으로 수집하여
팬덤 분석에 필요한 데이터베이스와 CSV 데이터를 생성하는 Python 기반 크롤링 시스템입니다.

본 프로젝트는 플랫폼별 크롤러를 독립적으로 관리하면서도 하나의 프로젝트에서 통합 실행할 수 있도록 설계되었습니다.

---

# Features

- YouTube 크롤링
  - Channel (L1)
  - Content (L2)
  - Comment (L3)

- Instagram 크롤링
  - Profile (L1)
  - Feed / Reels (L2)
  - Comment (L3)

- TikTok 크롤링
  - Profile (L1)
  - Video (L2)
  - Comment (L3)

- Metric 계산

- CSV Export

---

# Project Structure

```
Internship_Illio_2026/
│
├── README.md
├── requirements.txt
├── main.py
│
├── youtube/
├── instagram/
├── tiktok/
│
├── db/
└── output/
```

---

# Requirements

- Python 3.11 이상
- MariaDB 10.5 이상 (또는 MySQL 8.0 이상)
- Google Chrome (Instagram / TikTok 로그인 사용 시)

### Python Package 설치

```bash
pip install -r requirements.txt
```

---

# requirements.txt

예시

```text
beautifulsoup4>=4.13
lxml>=6.0
openpyxl>=3.1
pandas>=2.2
playwright>=1.54
PyMySQL>=1.1
requests>=2.32
tqdm>=4.67
```

Playwright 브라우저 설치

```bash
playwright install
```

---

# Database

프로젝트는 MariaDB(MySQL)를 사용합니다.

데이터베이스 생성 후 스키마를 적용해야 합니다.

```
fandom_crm
```

주요 테이블

| Table | Description |
|--------|-------------|
| creators | 크리에이터 |
| channels | 플랫폼 채널 |
| contents | 게시물 |
| comments | 댓글 |
| channel_metrics | 채널 지표 |
| content_snapshots | 조회수 스냅샷 |
| crawl_logs | 수집 로그 |

자세한 구조는 `db/README.md`를 참고하세요.

---

# Input Excel

프로그램은 Excel(.xlsx) 파일을 입력으로 사용합니다.

현재 버전에서는

```
Sheet1
```

만 읽습니다.

필수 컬럼은 아래와 같습니다.

| Column | Description |
|---------|-------------|
| creator_name | 크리에이터 이름 |
| youtube | YouTube URL 또는 @handle |
| instagram | Instagram Username |
| tiktok | TikTok Username |

---

# Quick Start

1. Python Package 설치

```bash
pip install -r requirements.txt
```

2. Playwright 설치

```bash
playwright install
```

3. DB 생성

4. config.py 작성

5. 프로그램 실행

```bash
python main.py
```

---

# Documentation

각 플랫폼의 자세한 설명은 아래 문서를 참고하세요.

- youtube/README.md
- instagram/README.md
- tiktok/README.md
- db/README.md