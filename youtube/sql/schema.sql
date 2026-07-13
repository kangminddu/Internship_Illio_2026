-- =============================================================================
-- 크리에이터 팬덤 CRM — 팬 관계 분석 스키마 (MySQL 8 / MariaDB 10.5+ 호환)
--
-- 호환 메모:
--   - 전 테이블 collation을 utf8mb4_unicode_ci 로 명시 → MySQL/MariaDB 기본값
--     차이(0900_ai_ci vs general_ci)로 인한 'Illegal mix of collations' 방어.
--   - JSON 네이티브 타입 미사용 → MariaDB(JSON=LONGTEXT 별칭)에서도 동일 동작.
--   - CURRENT_TIMESTAMP 다중 컬럼 사용 → MariaDB 10.2+, MySQL 5.6+ 필요.
--
-- 데이터 레이어(가이드라인 §6):
--   L1(채널)=channels/channel_snapshots · L2(콘텐츠)=contents/content_snapshots
--   L3(댓글)=comments · 파생=channel_metrics
--
-- 실행 순서 = FK 의존성 순서. 그대로 위에서 아래로 실행 가능.
-- =============================================================================

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 1;

-- =============================================================================
-- 그룹 1 — 셀럽 · 채널 (공통 항목 + L1)
-- =============================================================================

-- 1. creators : 플랫폼 무관 "사람(셀럽)" 마스터
CREATE TABLE creators (
    creator_id      BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    seed_key        VARCHAR(50)     NULL     COMMENT '시드 파일 키값(G_35 등), 초기 적재 추적용',
    nickname        VARCHAR(100)    NOT NULL COMMENT '대표 활동명',
    category        VARCHAR(50)     NULL     COMMENT '주력 콘텐츠 카테고리',
    birthday        DATE            NULL     COMMENT '버추얼 가상 생일 포함',
    debut_date      DATE            NULL     COMMENT '데뷔/첫 방송일 등 대표 기념일',
    memo            TEXT            NULL,
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (creator_id),
    UNIQUE KEY uq_creators_seed (seed_key),
    KEY idx_creators_nickname (nickname)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 2. channels : 셀럽 × 플랫폼. channel_url = 가이드라인의 Primary Key 기준값
CREATE TABLE channels (
    channel_id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    creator_id             BIGINT UNSIGNED NOT NULL,
    platform               VARCHAR(20)     NOT NULL COMMENT 'youtube/tiktok/instagram/chzzk/soop...',
    channel_url_raw        VARCHAR(768)    NOT NULL COMMENT '엑셀 원본 URL, 무손실 보관',
    channel_url_normalized VARCHAR(768)    NULL     COMMENT '스킴/www/쿼리스트링 제거 정규화',
    channel_id_status      ENUM('resolved','handle_only','custom_only','user_legacy','unresolved')
                           NOT NULL DEFAULT 'unresolved'
                           COMMENT 'UC확보=resolved / @핸들만 / c커스텀 / user레거시 / goo.gl·외부·깨짐=unresolved',
    external_channel_id    VARCHAR(128)    NULL     COMMENT '플랫폼 자체 ID (YT의 UC...)',
    channel_name           VARCHAR(150)    NULL     COMMENT '플랫폼별 채널명',
    is_primary             TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '주력=1 / 서브=0',
    account_type           VARCHAR(20)     NULL     COMMENT 'IG 일반/크리에이터/비즈니스',
    bio                    TEXT            NULL     COMMENT '프로필 소개글',
    external_link          VARCHAR(512)    NULL     COMMENT 'IG 외부링크 등 연결 채널',
    channel_opened_at      DATETIME        NULL     COMMENT '채널 개설일',
    created_at             DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at             DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (channel_id),
    UNIQUE KEY uq_channels_raw (channel_url_raw),
    KEY idx_channels_creator (creator_id),
    KEY idx_channels_platform (platform),
    KEY idx_channels_idstatus (channel_id_status),
    KEY idx_channels_extid (external_channel_id),
    CONSTRAINT fk_channels_creator FOREIGN KEY (creator_id)
        REFERENCES creators (creator_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 3. channel_snapshots : L1 로우 데이터 시계열 (주 1회 append)
CREATE TABLE channel_snapshots (
    snapshot_id        BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    channel_id         BIGINT UNSIGNED NOT NULL,
    captured_at        DATETIME        NOT NULL,
    follower_count     BIGINT UNSIGNED NULL COMMENT 'YT 구독자 = IG/TT 팔로워 통합',
    following_count    BIGINT UNSIGNED NULL COMMENT 'TT/IG 팔로잉 수',
    total_view_count   BIGINT UNSIGNED NULL COMMENT 'YT 채널 누적 조회수',
    total_video_count  INT UNSIGNED    NULL COMMENT '총 영상/게시물 수',
    total_like_count   BIGINT UNSIGNED NULL COMMENT 'TT 누적 좋아요',
    PRIMARY KEY (snapshot_id),
    UNIQUE KEY uq_snap_channel_time (channel_id, captured_at),
    CONSTRAINT fk_snap_channel FOREIGN KEY (channel_id)
        REFERENCES channels (channel_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 13. channel_url_aliases : 한 채널의 여러 URL/핸들을 원본 안 건드리고 이어붙임
CREATE TABLE channel_url_aliases (
    alias_id     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    channel_id   BIGINT UNSIGNED NOT NULL,
    alias_type   ENUM('handle','custom','user','shortlink','channel_id','external') NOT NULL,
    alias_value  VARCHAR(768)    NOT NULL COMMENT '@handle, /c/name, UC..., goo.gl 등',
    is_confirmed TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '크롤링으로 동일 채널 확정=1',
    created_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (alias_id),
    UNIQUE KEY uq_alias (alias_type, alias_value),
    KEY idx_alias_channel (channel_id),
    CONSTRAINT fk_alias_channel FOREIGN KEY (channel_id)
        REFERENCES channels (channel_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =============================================================================
-- 그룹 2 — 콘텐츠 (L2)
-- =============================================================================

-- 4. contents : 영상/포스트의 불변 속성 (플랫폼 3사 통합)
CREATE TABLE contents (
    content_id      BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    channel_id      BIGINT UNSIGNED NOT NULL,
    external_id     VARCHAR(128)    NOT NULL COMMENT '플랫폼 영상/포스트 고유 ID',
    content_type    ENUM('video','shorts','reels','feed_image','carousel','tiktok') NOT NULL,
    published_at    DATETIME        NULL     COMMENT '게시일',
    duration_sec    INT UNSIGNED    NULL     COMMENT '영상 재생 시간(초). 이미지=NULL',
    category_id     VARCHAR(50)     NULL     COMMENT 'YT categoryId 등',
    caption_text    TEXT            NULL     COMMENT '제목/캡션/설명글',
    collected_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (content_id),
    UNIQUE KEY uq_contents_ext (channel_id, external_id),
    KEY idx_contents_channel (channel_id),
    KEY idx_contents_published (published_at),
    CONSTRAINT fk_contents_channel FOREIGN KEY (channel_id)
        REFERENCES channels (channel_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 5. content_snapshots : 조회수·좋아요·댓글수 시계열 (주 1회 update = append)
CREATE TABLE content_snapshots (
    snapshot_id    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    content_id     BIGINT UNSIGNED NOT NULL,
    captured_at    DATETIME        NOT NULL,
    view_count     BIGINT UNSIGNED NULL,
    like_count     BIGINT UNSIGNED NULL,
    comment_count  BIGINT UNSIGNED NULL,
    PRIMARY KEY (snapshot_id),
    UNIQUE KEY uq_csnap (content_id, captured_at),
    CONSTRAINT fk_csnap_content FOREIGN KEY (content_id)
        REFERENCES contents (content_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 6a. hashtags : 태그 정규화 마스터
CREATE TABLE hashtags (
    hashtag_id  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    tag_text    VARCHAR(200)    NOT NULL,
    PRIMARY KEY (hashtag_id),
    UNIQUE KEY uq_tag (tag_text)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 6b. content_hashtags : 콘텐츠 ↔ 태그 N:M
CREATE TABLE content_hashtags (
    content_id  BIGINT UNSIGNED NOT NULL,
    hashtag_id  BIGINT UNSIGNED NOT NULL,
    PRIMARY KEY (content_id, hashtag_id),
    KEY idx_ch_tag (hashtag_id),
    CONSTRAINT fk_ch_content FOREIGN KEY (content_id) REFERENCES contents (content_id) ON DELETE CASCADE,
    CONSTRAINT fk_ch_tag     FOREIGN KEY (hashtag_id) REFERENCES hashtags (hashtag_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =============================================================================
-- 그룹 3 — 팬 (L3, 제품의 심장)
-- =============================================================================

-- 7. fans : 팬 계정 마스터. 크로스-채널 엮기의 중심
CREATE TABLE fans (
    fan_id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    platform            VARCHAR(20)     NOT NULL,
    external_author_id  VARCHAR(128)    NOT NULL COMMENT 'YT는 UC... 채널ID',
    person_id           BIGINT UNSIGNED NULL     COMMENT '동일인 병합용 훅(지금은 미사용)',
    existence_status    ENUM('normal','deleted','private','suspended','unknown')
                        NOT NULL DEFAULT 'normal' COMMENT '존재 축',
    activity_status     ENUM('active','dormant','unknown')
                        NOT NULL DEFAULT 'unknown' COMMENT '활동 축',
    collection_status   ENUM('collecting','blocked','excluded','pending')
                        NOT NULL DEFAULT 'collecting' COMMENT '수집 축',
    first_seen_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at        DATETIME        NULL,
    created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (fan_id),
    UNIQUE KEY uq_fans_ext (platform, external_author_id),
    KEY idx_fans_person (person_id),
    KEY idx_fans_existence (existence_status),
    KEY idx_fans_collection (collection_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 8. fan_status_history : 상태 축별 변경 이력 (감사 로그)
CREATE TABLE fan_status_history (
    history_id   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    fan_id       BIGINT UNSIGNED NOT NULL,
    status_axis  ENUM('existence','activity','collection') NOT NULL COMMENT '어느 축이',
    old_value    VARCHAR(20)     NULL,
    new_value    VARCHAR(20)     NOT NULL,
    reason       VARCHAR(255)    NULL,
    changed_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (history_id),
    KEY idx_fsh_fan (fan_id, changed_at),
    CONSTRAINT fk_fsh_fan FOREIGN KEY (fan_id) REFERENCES fans (fan_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 9. comments : contents(셀럽) ↔ fans(팬) 를 잇는 다리
--    주의: fan_id 에는 CASCADE 미적용 (팬 삭제돼도 댓글=엮기 원천 보존)
CREATE TABLE comments (
    comment_id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    content_id           BIGINT UNSIGNED NOT NULL,
    fan_id               BIGINT UNSIGNED NOT NULL,
    external_comment_id  VARCHAR(128)    NOT NULL COMMENT '플랫폼 댓글 고유 ID(중복 방지)',
    parent_comment_id    BIGINT UNSIGNED NULL     COMMENT '대댓글이면 부모 comment_id',
    author_display_name  VARCHAR(150)    NULL     COMMENT '작성 시점 닉네임(변경 이력 자연 보존)',
    comment_text         TEXT            NULL,
    like_count           INT UNSIGNED    NULL,
    published_at         DATETIME        NULL,
    collected_at         DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (comment_id),
    UNIQUE KEY uq_comment_ext (content_id, external_comment_id),
    KEY idx_comments_fan (fan_id),
    KEY idx_comments_content (content_id),
    KEY idx_comments_fan_content (fan_id, content_id),
    KEY idx_comments_parent (parent_comment_id),
    CONSTRAINT fk_comments_content FOREIGN KEY (content_id) REFERENCES contents (content_id) ON DELETE CASCADE,
    CONSTRAINT fk_comments_fan     FOREIGN KEY (fan_id)     REFERENCES fans (fan_id),
    CONSTRAINT fk_comments_parent  FOREIGN KEY (parent_comment_id) REFERENCES comments (comment_id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 10. comment_sentiments : 감정분석 결과 분리 (모델 버전별 보존 → 재분석 비교)
CREATE TABLE comment_sentiments (
    sentiment_id   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    comment_id     BIGINT UNSIGNED NOT NULL,
    model_version  VARCHAR(50)     NOT NULL COMMENT '분석 모델/버전',
    label          ENUM('positive','negative','neutral','mixed') NOT NULL,
    score          DECIMAL(5,4)    NULL     COMMENT '신뢰도/극성 점수',
    analyzed_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (sentiment_id),
    UNIQUE KEY uq_sentiment (comment_id, model_version),
    CONSTRAINT fk_sent_comment FOREIGN KEY (comment_id) REFERENCES comments (comment_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =============================================================================
-- 그룹 4 — 파생 · 외부 (파생 지표 + §4 Advocacy)
-- =============================================================================

-- 11. channel_metrics : 파생 지표 산출 결과 캐시 (산출일자별 append, 로우에서 재계산 가능)
CREATE TABLE channel_metrics (
    metric_id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    channel_id                 BIGINT UNSIGNED NOT NULL,
    calculated_at              DATETIME        NOT NULL COMMENT '산출 시점',
    sample_period_start        DATE            NULL COMMENT '산출에 쓴 수집 기간 시작',
    sample_period_end          DATE            NULL,
    sample_content_count       INT UNSIGNED    NULL COMMENT '산출에 쓴 콘텐츠 표본 수',
    aggregation_method         ENUM('mean_trimmed','median','mean') NULL COMMENT 'YT=mean_trimmed / TT=median',
    -- 로우 데이터 가공 산출 파생 지표 (가이드라인 §3)
    view_per_follower_ratio    DECIMAL(10,4)   NULL COMMENT '구독자/팔로워 대비 평균 조회수 비율',
    engagement_rate           DECIMAL(10,4)   NULL COMMENT '공개 참여율(ER)',
    like_view_ratio            DECIMAL(10,4)   NULL COMMENT '조회수 대비 좋아요 비율',
    comment_view_ratio         DECIMAL(10,4)   NULL COMMENT '조회수 대비 댓글 비율',
    upload_frequency_weekly    DECIMAL(10,4)   NULL COMMENT '업로드 빈도(주당)',
    commenter_overlap_rate     DECIMAL(10,4)   NULL COMMENT '댓글 작성자 중복률(코어 팬덤 깊이)',
    regular_commenter_count    INT UNSIGNED    NULL COMMENT '고정 댓글러 수(실질 코어 팬덤 모수)',
    avg_comment_length         DECIMAL(10,4)   NULL COMMENT '평균 댓글 길이(몰입도)',
    loyalty_score              DECIMAL(12,4)   NULL COMMENT '통합 충성도 점수',
    PRIMARY KEY (metric_id),
    UNIQUE KEY uq_metric_channel_time (channel_id, calculated_at),
    KEY idx_metric_loyalty (loyalty_score),
    CONSTRAINT fk_metric_channel FOREIGN KEY (channel_id)
        REFERENCES channels (channel_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 12. advocacy_snapshots : 팬덤 자발성 및 외부 확산 지표 시계열 (§4)
CREATE TABLE advocacy_snapshots (
    snapshot_id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    channel_id           BIGINT UNSIGNED NOT NULL COMMENT '주력 채널 기준(또는 creator 대표 채널)',
    captured_at          DATETIME        NOT NULL,
    source_type          ENUM('discord','fancafe','twitter_x','dcinside','other') NOT NULL,
    member_count         BIGINT UNSIGNED NULL COMMENT '디스코드 멤버/팬카페 가입자 수',
    mention_count        BIGINT UNSIGNED NULL COMMENT '닉네임 검색/언급 빈도',
    note                 VARCHAR(255)    NULL,
    PRIMARY KEY (snapshot_id),
    UNIQUE KEY uq_advocacy (channel_id, source_type, captured_at),
    CONSTRAINT fk_advocacy_channel FOREIGN KEY (channel_id)
        REFERENCES channels (channel_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;