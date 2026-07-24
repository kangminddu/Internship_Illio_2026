-- ============================================================
-- 기존 channels 테이블의 불량 데이터 정리
-- 실행 전 반드시 백업: mysqldump fandom_crm channels > channels_backup.sql
-- ============================================================

-- 0) 삭제 대상 미리 확인 (먼저 이걸로 눈으로 검토!)
SELECT channel_id, channel_url_raw, channel_url_normalized
FROM channels
WHERE platform = 'youtube'
  AND (
        -- youtube.com 도메인이 아님 (instagram/tiktok/twitch/단축URL 등)
        channel_url_normalized NOT LIKE '%youtube.com%'
        -- youtube.com이어도 채널이 아닌 URL (watch/playlist/루트 등)
        OR channel_url_normalized REGEXP 'youtube\\.com/?$'
        OR channel_url_normalized LIKE '%youtube.com/watch%'
        OR channel_url_normalized LIKE '%youtube.com/playlist%'
        OR channel_url_normalized LIKE '%youtu.be%'
      );

-- 1) 위 결과가 맞으면: 해당 채널의 crawl_logs 먼저 삭제 (FK 대비)
DELETE cl FROM crawl_logs cl
JOIN channels c ON c.channel_id = cl.channel_id
WHERE c.platform = 'youtube'
  AND (
        c.channel_url_normalized NOT LIKE '%youtube.com%'
        OR c.channel_url_normalized REGEXP 'youtube\\.com/?$'
        OR c.channel_url_normalized LIKE '%youtube.com/watch%'
        OR c.channel_url_normalized LIKE '%youtube.com/playlist%'
        OR c.channel_url_normalized LIKE '%youtu.be%'
      );

-- 2) 스냅샷도 있으면 삭제
DELETE cs FROM channel_snapshots cs
JOIN channels c ON c.channel_id = cs.channel_id
WHERE c.platform = 'youtube'
  AND (
        c.channel_url_normalized NOT LIKE '%youtube.com%'
        OR c.channel_url_normalized REGEXP 'youtube\\.com/?$'
        OR c.channel_url_normalized LIKE '%youtube.com/watch%'
        OR c.channel_url_normalized LIKE '%youtube.com/playlist%'
        OR c.channel_url_normalized LIKE '%youtu.be%'
      );

-- 3) channels 본체 삭제
DELETE FROM channels
WHERE platform = 'youtube'
  AND (
        channel_url_normalized NOT LIKE '%youtube.com%'
        OR channel_url_normalized REGEXP 'youtube\\.com/?$'
        OR channel_url_normalized LIKE '%youtube.com/watch%'
        OR channel_url_normalized LIKE '%youtube.com/playlist%'
        OR channel_url_normalized LIKE '%youtu.be%'
      );

-- 4) (선택) 과거 403 실패 로그 정리 — 불량 URL 때문에 실패로 기록된 채널들이
--    resume 로직에서 "이미 처리됨"으로 skip되지 않도록, 정상 채널의 403 로그 삭제
DELETE FROM crawl_logs
WHERE layer = 'L1'
  AND status = 'failed'
  AND http_status IN (403, 429);

-- 5) 결과 확인
SELECT COUNT(*) AS remaining_youtube_channels
FROM channels WHERE platform = 'youtube';

SELECT COUNT(*) AS non_youtube_leftover
FROM channels
WHERE platform = 'youtube'
  AND channel_url_normalized NOT LIKE '%youtube.com%';
-- → 0이어야 정상