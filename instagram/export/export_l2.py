import os
import warnings
warnings.filterwarnings("ignore", category=UserWarning)

import pandas as pd
import pymysql

from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

from config import DB, EXPORT_DIR


sql = """
SELECT

ROW_NUMBER() OVER(
ORDER BY
cr.nickname,
ct.published_at DESC
) AS '번호',

cr.nickname AS '크리에이터',

COALESCE(cr.agency,'-') AS '소속',

DATE_FORMAT(
ct.published_at,
'%Y-%m-%d'
) AS '게시일',

DATEDIFF(
CURDATE(),
ct.published_at
) AS '업로드경과일',

CASE
    WHEN ct.content_type='feed_image' THEN '피드'
    WHEN ct.content_type='carousel' THEN '캐러셀'
    WHEN ct.content_type='reels' THEN '릴스'
    ELSE ct.content_type
END AS '콘텐츠유형',

cs.view_count AS '조회수',

cs.like_count AS '좋아요',

cs.comment_count AS '댓글수',

CASE
    WHEN ct.is_paid_promotion=1 THEN 'O'
    ELSE 'X'
END AS '유료광고',

ct.caption_text AS '캡션',

CONCAT(
'https://www.instagram.com/p/',
ct.external_id,
'/'
) AS '포스트URL'

FROM creators cr

JOIN channels ch
ON cr.creator_id = ch.creator_id

JOIN contents ct
ON ch.channel_id = ct.channel_id

LEFT JOIN (

SELECT cs1.*

FROM content_snapshots cs1

JOIN (

SELECT
content_id,
MAX(captured_at) latest

FROM content_snapshots

GROUP BY content_id

) t

ON cs1.content_id=t.content_id
AND cs1.captured_at=t.latest

) cs

ON ct.content_id=cs.content_id

WHERE ch.platform='instagram'

ORDER BY
cr.nickname,
ct.published_at DESC;
"""


def sanitize_formula(val):
    """엑셀이 수식으로 인식하는 것 방지"""
    if isinstance(val, str) and val and val[0] in ("=", "+", "-", "@"):
        return "'" + val
    return val


# -------------------------------------------------
# DB → DataFrame
# -------------------------------------------------

conn = pymysql.connect(**DB)
df = pd.read_sql(sql, conn)
conn.close()

for col in ["크리에이터", "소속", "캡션"]:
    if col in df.columns:
        df[col] = df[col].fillna("").apply(sanitize_formula)


filename = os.path.join(EXPORT_DIR, "L2_인스타그램_게시물.xlsx")
df.to_excel(filename, index=False, sheet_name="L2")


# ==================================================
# Excel Styling
# ==================================================

wb = load_workbook(filename)
ws = wb["L2"]

header_fill = PatternFill(fill_type="solid", fgColor="1F4E78")
header_font = Font(bold=True, color="FFFFFF")

center = Alignment(horizontal="center", vertical="center")

thin = Side(border_style="thin", color="D9D9D9")
border = Border(
    left=thin,
    right=thin,
    top=thin,
    bottom=thin
)

# -------------------------
# Header
# -------------------------

for cell in ws[1]:
    cell.fill = header_fill
    cell.font = header_font
    cell.alignment = center
    cell.border = border

# -------------------------
# Body
# -------------------------

for row in ws.iter_rows(min_row=2):
    for cell in row:
        cell.border = border

        if cell.column in [1, 4, 5, 6, 7, 8, 9, 10]:
            cell.alignment = center

# -------------------------
# Number Format
# -------------------------

for col in ["G", "H", "I"]:
    for cell in ws[col][1:]:
        cell.number_format = "#,##0"

# -------------------------
# Width
# -------------------------

widths = {
    "A": 8,
    "B": 20,
    "C": 16,
    "D": 14,
    "E": 14,
    "F": 12,
    "G": 12,
    "H": 12,
    "I": 12,
    "J": 10,
    "K": 70,
    "L": 45,
}

for col, width in widths.items():
    ws.column_dimensions[col].width = width

ws.freeze_panes = "A2"
ws.auto_filter.ref = ws.dimensions

wb.save(filename)

print(f"완료 : {filename}")
print(f"게시물 {len(df)}개")