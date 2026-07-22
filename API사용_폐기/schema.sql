-- ============================================================
--  크리에이터 팬덤 분석 시스템 — PostgreSQL 스키마 (YouTube 1차)
--  구조: 3레이어 로우 데이터(L1 채널 / L2 콘텐츠 / L3 댓글) + 파생 지표
--  설계 원칙
--   1) 사람(creators)과 채널(channels)을 분리 → 멀티플랫폼을 한 사람으로 묶음
--   2) 시계열 보존: 채널 통계·파생 지표는 '덮어쓰기'가 아니라 스냅샷 누적 (Moat)
--   3) 로우 데이터 원본 보관: raw(JSONB)에 API 원본 저장 → 파생 지표 재계산 가능
--   4) 플랫폼 고유 지표는 extra(JSONB)로 수용 → TikTok·IG 확장 시 스키마 변경 최소화
-- ============================================================

-- 플랫폼 종류 (확장 대비)
CREATE TYPE platform_t AS ENUM
    ('youtube','tiktok','instagram','chzzk','soop','twitch');

-- ------------------------------------------------------------
-- 사람(크리에이터) — 채널 위의 상위 엔티티
-- 고세구가 SOOP·YouTube·TikTok을 다 해도 여기선 1명
-- ------------------------------------------------------------
CREATE TABLE creators (
    id          BIGSERIAL PRIMARY KEY,
    name        VARCHAR(100) NOT NULL,                 -- 활동명/본명(내부 관리용)
    birthday    DATE,                                  -- 생일 (버추얼의 가상 생일 포함)
    is_virtual  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 기념일 — 크리에이터당 여러 개 (데뷔일, 계약 기념일, 채널 개설일, 첫 방송일 등)
CREATE TABLE creator_anniversaries (
    id          BIGSERIAL PRIMARY KEY,
    creator_id  BIGINT NOT NULL REFERENCES creators(id) ON DELETE CASCADE,
    type        VARCHAR(40) NOT NULL,                  -- debut | contract | channel_open | first_broadcast ...
    date        DATE NOT NULL,
    label       VARCHAR(100),
    UNIQUE (creator_id, type)
);

-- ------------------------------------------------------------
-- L1: 채널 정체성 (정적 정보)
-- 한 크리에이터가 플랫폼별로 여러 채널을 가짐
-- ------------------------------------------------------------
CREATE TABLE channels (
    id                  BIGSERIAL PRIMARY KEY,
    creator_id          BIGINT NOT NULL REFERENCES creators(id) ON DELETE CASCADE,
    platform            platform_t NOT NULL,
    channel_url         TEXT NOT NULL UNIQUE,          -- 모든 데이터의 기준값(가이드라인상 PK 성격)
    platform_channel_id VARCHAR(128),                  -- YouTube channelId(UC...) 등
    nickname            VARCHAR(120),                  -- 닉네임/채널명
    category            VARCHAR(60),                   -- 콘텐츠 카테고리
    tags                TEXT[],                        -- 해시태그/키워드
    bio                 TEXT,                          -- 프로필 소개글
    channel_opened_at   TIMESTAMPTZ,                   -- 채널 개설일 (YT: snippet.publishedAt)
    is_primary          BOOLEAN NOT NULL DEFAULT FALSE,-- 주력/서브 플랫폼 구분
    status              VARCHAR(20) NOT NULL DEFAULT 'active', -- active | inactive | archived
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (platform, platform_channel_id)
);

-- L1 통계 스냅샷: 주 1회 수집마다 1행 추가(덮어쓰지 않음) → 시계열 추이 확보
CREATE TABLE channel_stats (
    id               BIGSERIAL PRIMARY KEY,
    channel_id       BIGINT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    collected_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    follower_count   BIGINT,                           -- 구독자 수(YT) / 팔로워 수(TT,IG) 통칭
    total_view_count BIGINT,                           -- 채널 누적 조회수 (YT)
    video_count      INTEGER,                          -- 총 공개 영상 수
    -- 플랫폼 고유 지표 (스트리밍: avg_viewers/avg_chat_count/revenue/avg_stream_hours,
    --                    TikTok: total_likes/following_count, IG: account_type/external_link ...)
    extra            JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw              JSONB,                            -- API 원본 응답 통째 보관
    UNIQUE (channel_id, collected_at)
);

-- ------------------------------------------------------------
-- L2: 콘텐츠(영상/포스트)
-- 영상 정체성 + 최신 통계 (이력이 필요하면 별도 video_stats 스냅샷으로 분리)
-- ------------------------------------------------------------
CREATE TABLE videos (
    id                BIGSERIAL PRIMARY KEY,
    channel_id        BIGINT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    platform_video_id VARCHAR(128) NOT NULL,           -- 영상 고유 ID
    published_at      TIMESTAMPTZ,                     -- 게시일
    title             TEXT,
    duration_seconds  INTEGER,                         -- 영상 길이 (ISO8601 → 초 변환 저장)
    category_id       VARCHAR(40),                     -- YT categoryId
    content_type      VARCHAR(20),                     -- IG: reels/feed/carousel (YT는 NULL)
    view_count        BIGINT,                          -- 최신 통계(주 1회 갱신 시 덮어씀)
    like_count        BIGINT,
    comment_count     BIGINT,
    comments_disabled BOOLEAN NOT NULL DEFAULT FALSE,  -- 댓글 비활성 영상 플래그(ER 분모 제외용)
    raw               JSONB,
    first_collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (channel_id, platform_video_id)
);

-- ------------------------------------------------------------
-- L3: 댓글  ★ 핵심 ★
-- author_id를 영상 가로질러 묶어 '중복률 / 고정 댓글러' 산출
-- ------------------------------------------------------------
CREATE TABLE comments (
    id                  BIGSERIAL PRIMARY KEY,
    video_id            BIGINT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    platform_comment_id VARCHAR(128) NOT NULL,
    author_id           VARCHAR(128),                  -- YT authorChannelId.value (객체에서 추출)
    author_name         VARCHAR(200),
    text                TEXT,
    text_length         INTEGER,                       -- grapheme 기준 글자 수(수집 시 계산 저장)
    like_count          INTEGER,
    is_reply            BOOLEAN NOT NULL DEFAULT FALSE, -- 답글 여부(포함/제외 정책 대응)
    published_at        TIMESTAMPTZ,
    collected_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw                 JSONB,
    UNIQUE (video_id, platform_comment_id)
);

-- ------------------------------------------------------------
-- 파생 지표 스냅샷 (로우와 분리, 산출 시마다 1행 추가 → 팬덤 추이 추적)
-- ------------------------------------------------------------
CREATE TABLE channel_metrics (
    id                       BIGSERIAL PRIMARY KEY,
    channel_id               BIGINT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    computed_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    sample_video_count       INTEGER,                  -- 산출에 사용한 영상 수
    sample_period_start      DATE,
    sample_period_end        DATE,
    view_to_subscriber_ratio NUMERIC(10,4),            -- 구독자 대비 평균 조회수 비율
    engagement_rate          NUMERIC(10,4),            -- 공개 참여율(ER)
    upload_frequency         NUMERIC(10,4),            -- 업로드 빈도(주당)
    commenter_overlap_rate   NUMERIC(10,4),            -- 댓글 작성자 중복률
    core_commenter_count     INTEGER,                  -- 고정 댓글러 수
    avg_comment_length       NUMERIC(10,2),            -- 평균 댓글 길이
    loyalty_score            NUMERIC(12,4),            -- 통합 충성도 점수
    UNIQUE (channel_id, computed_at)
);

-- ------------------------------------------------------------
-- 인덱스 (join / 집계 핵심 경로)
-- ------------------------------------------------------------
CREATE INDEX idx_channels_creator      ON channels(creator_id);
CREATE INDEX idx_channel_stats_channel ON channel_stats(channel_id, collected_at DESC);
CREATE INDEX idx_videos_channel        ON videos(channel_id);
CREATE INDEX idx_comments_video        ON comments(video_id);
CREATE INDEX idx_comments_author       ON comments(author_id);   -- 중복률/고정 댓글러의 핵심
CREATE INDEX idx_metrics_channel       ON channel_metrics(channel_id, computed_at DESC);
