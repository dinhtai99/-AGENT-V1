"""AGENT-V1 — Post filtering, extraction, and Small Business Lead profiling (Baito Japan).

Pure functions only, no Facebook calls:
1. Normalize Vietnamese & Japanese full-width text (NFKC + accent stripping).
2. Clean Facebook DOM noise ("Facebook\\nFacebook...", auto image alt text).
3. Classify Baito/Group posts:
   - Keep Small Business Owners (< 20 people), both:
     a) Owners hiring staff (`small_business_hiring`: restaurants, izakayas, nail salons, workshops...)
     b) Individual/micro-business owners selling products/services (`small_business_selling`: food, fruit, homemade kitchen, local services...)
   - Drop Job Seekers (`job_seeker`: "em cần tìm baito") and Large Haken/Agencies (`agency_or_large`: >= 20 people, phái cử, nghiệp đoàn).
4. Step 1 Profiling: Identify WHAT they are doing business in (`business_type`,
   `business_activity`, `shop_name`, `location`/`stations` in Japan, `scale_estimate`).
5. Step 2 Deep Analysis: Identify WHAT THEY NEED (`roles_needed`, `shifts_needed`,
   `wage_or_price`, `shop_customer_segment`, `pain_points_and_needs`).
6. Aggregate posts into a deduplicated Owner/Lead list (`leads.json` & `leads.csv`).
"""

from __future__ import annotations

import csv
import json
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse


def load_json(path: Path) -> dict | list:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: dict | list) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_date(value: str | None) -> date | None:
    """Parse YYYY-MM-DD (or an ISO datetime) to a date, else None."""
    if not value:
        return None
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def in_date_range(created_at: str, date_from: str | None, date_to: str | None) -> bool:
    """True when a post's date is inside [date_from, date_to]. Missing dates pass."""
    post_date = parse_date(created_at)
    if post_date is None:
        return True
    start = parse_date(date_from)
    end = parse_date(date_to)
    if start and post_date < start:
        return False
    if end and post_date > end:
        return False
    return True


# ---------------------------------------------------------------- text normalization & cleaning

def normalize_fullwidth(text: str) -> str:
    """Convert Japanese full-width digits/letters/symbols (０８０, ￥, ー, etc.) to ASCII."""
    clean = unicodedata.normalize("NFKC", str(text or ""))
    clean = re.sub(r"(?<=\d)[ー－–—―](?=\d)", "-", clean)
    return clean


def clean_fb_noise(text: str, author_name: str = "") -> str:
    """Remove Facebook's hidden 'Facebook\\nFacebook...' spans, auto alt-text, and header noise."""
    raw = normalize_fullwidth(text or "")
    lines: list[str] = []
    seen_long: set[str] = set()
    for ln in raw.splitlines():
        s = ln.strip()
        if not s:
            continue
        # Trailing UI labels glued to the last line ("... Ẩn bớt", "... Xem thêm")
        s = re.sub(r"\s+(?:Ẩn bớt|Xem thêm|See more|See less)$", "", s).strip()
        if not s:
            continue
        low = s.lower()
        if low in (
            "facebook", "đã chia sẻ bài viết", "shared a post", "thích", "bình luận", "chia sẻ", "·",
            "theo dõi", "follow", "ẩn bớt", "xem thêm", "trả lời", "ib", "nội dung do ai tạo",
        ):
            continue
        if low.startswith((
            "có thể là hình ảnh về", "may be an image of", "có thể là hình ảnh", "không có mô tả ảnh",
            "bình luận dưới tên", "trả lời dưới tên", "comment as", "đã chia sẻ bài viết của",
        )):
            continue
        if re.match(r"^(?:khoảng\s+)?\d+\s*(?:phút|giờ|ngày|tuần|h|m|d)(?:\s+trước)?$", low):
            continue
        if re.match(r"^\+\d{1,3}$", low):
            continue
        if re.match(r"^thứ\s+[A-Za-zÀ-ỹ]+,\s+\d+\s+tháng", low):
            continue
        # Shared posts repeat the same text twice; keep the first copy only
        if len(s) >= 15:
            if low in seen_long:
                continue
            seen_long.add(low)
        lines.append(s)

    # If the first line is just the author's name, strip it from the body text
    # so names like "Tuyến" or "Tuyên" don't trigger keyword "tuyển"!
    if lines and author_name:
        if lines[0].lower() == author_name.strip().lower():
            lines = lines[1:]
    return "\n".join(lines).strip()


def norm_vn(text: str) -> str:
    """Lowercase text with full-width normalized and Vietnamese diacritics stripped."""
    clean = normalize_fullwidth(text).lower()
    decomposed = unicodedata.normalize("NFD", clean)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return stripped.replace("đ", "d")


def contains_vn(haystack: str, needle: str) -> bool:
    """Accent-insensitive and full-width-insensitive substring check."""
    if not needle:
        return True
    return norm_vn(needle) in norm_vn(haystack)


def matches_any_keyword(text: str, keyword_expr: str) -> bool:
    """Smart keyword expression matcher:
    Supports multiple keywords separated by '/' or ',' or '|' or ';' (e.g. 'nail/mi' or 'nail, mi, spa').
    For short words (<= 3 chars, e.g. 'mi'), uses word boundary matching to avoid matching 'mình', 'miễn phí'.
    For longer words, matches accent-insensitive substring.
    """
    if not keyword_expr or not keyword_expr.strip():
        return True
    norm_t = norm_vn(text)
    tokens = [norm_vn(t.strip()) for t in re.split(r"[/,|;]+", keyword_expr) if t.strip()]
    if not tokens:
        return True
    for tok in tokens:
        if len(tok) <= 3:
            pattern = r"(?:\b|_)" + re.escape(tok) + r"(?:\b|_)"
            if re.search(pattern, norm_t):
                return True
        else:
            if tok in norm_t:
                return True
    return False


# ---------------------------------------------------------------- regex patterns

JP_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+81[\s.-]?0?|\b0)(?:[7895]0[\s.-]?\d{4}[\s.-]?\d{4}|"
    r"(?:3|6|[1-9]\d{1,3})[\s.-]?\d{2,4}[\s.-]?\d{4})(?!\d)"
)
VN_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+84|0)(?:3|5|7|8|9)\d(?:[\s.-]?\d{3,4}){2}(?!\d)"
)

# Hourly wage / Monthly salary vs Product price
HOURLY_WAGE_JP_RE = re.compile(
    r"(?:時給|jikyu|lương|luong)\s*[:\-]?\s*(\d{1}(?:[.,]\d{3})|\d{3,4})\s*(?:yên|yen|円|¥|y\b)?(?:\s*/\s*(?:h|giờ|gio))?|"
    r"(\d{1}(?:[.,]\d{3})|\d{3,4})\s*(?:yên|yen|円|¥|y)\s*/\s*(?:h|giờ|gio)|"
    r"(\d{2,3}(?:[.,]\d+)?)\s*(?:man|vạn|万円)(?:\s*/\s*tháng)?",
    re.IGNORECASE,
)

ANY_PRICE_JP_RE = re.compile(
    r"(?:時給\s*)?(\d{1}(?:[.,]\d{3})|\d{3,5})\s*(?:yên|yen|円|¥|y\b)(?:\s*/\s*[A-Za-zÀ-ỹ]+)?|"
    r"(\d{2,3}(?:[.,]\d+)?)\s*(?:man|vạn|万円)",
    re.IGNORECASE,
)

PRICE_RE = re.compile(
    r"(\d+(?:[.,]\d+)?\s*(?:triệu|nghìn|VND|tr|k|đ|yên|yen|円|¥|man|万円))",
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r"\b([01]?\d|2[0-3])h[0-5]\d?\b|\b([01]?\d|2[0-3])h\b|\b([01]?\d|2[0-3]):[0-5]\d\b"
)
LEVEL_RE = re.compile(r"\b(TBY|TBK|TB\+\+|TB\+|TB-|TB|N[1-5])\b", re.IGNORECASE)

STATION_RE = re.compile(
    r"(?:ga|trạm)\s+([A-Za-zÀ-ỹ0-9\-]{2,25}(?:\s+[A-Za-zÀ-ỹ0-9\-]{2,20})?)(?=\s*(?:\(|,|;|\.|\n|$|\b(?:đi bộ|p\b|phút|tuyến|line|cách|tuyển|cần|lương|ở|tại|ạ\b|nhé|nha|từ|lịch|y/c|yc)))|"
    r"([A-Za-z0-9\-]{2,25})\s+eki\b|"
    r"([\u4e00-\u9faf\u3040-\u309f\u30a0-\u30ff]{1,12}駅)",
    re.IGNORECASE,
)

JP_AREAS = [
    # Tokyo
    "Tokyo", "Shinjuku", "Shin-Okubo", "Shinokubo", "Okubo", "Ikebukuro", "Ueno", "Akihabara",
    "Asakusa", "Kinshicho", "Kameido", "Hirai", "Koiwa", "Shin-Koiwa", "Shinkoiwa",
    "Otsuka", "Sugamo", "Komagome", "Tabata", "Nippori", "Nishi-Nippori", "Takadanobaba",
    "Nakano", "Koenji", "Asagaya", "Ogikubo", "Nishi-Ogikubo", "Kichijoji", "Mitaka",
    "Shibuya", "Harajuku", "Ebisu", "Meguro", "Gotanda", "Osaki", "Shinagawa", "Tamachi",
    "Shimbashi", "Oimachi", "Omori", "Kamata", "Roppongi", "Akabane", "Oji",
    "Kita-Senju", "Kitasenju", "Ayase", "Takenotsuka", "Nishiarai",
    "Kasai", "Nishi-Kasai", "Nishikasai", "Chofu", "Fuchu", "Kokubunji", "Kunitachi",
    "Tachikawa", "Hachioji", "Machida",
    # Saitama
    "Saitama", "Kawaguchi", "Nishi-Kawaguchi", "Nishikawaguchi", "Warabi", "Minami-Urawa",
    "Urawa", "Kita-Urawa", "Omiya", "Koshigaya", "Soka", "Kasukabe", "Toda", "Kawagoe",
    "Tokorozawa", "Asaka", "Shiki", "Wakoshi", "Narimasu",
    # Chiba
    "Chiba", "Funabashi", "Nishi-Funabashi", "Tsudanuma", "Inage", "Matsudo", "Kashiwa",
    "Ichikawa", "Motoyawata", "Gyotoku", "Minami-Gyotoku", "Myoden", "Urayasu", "Narita",
    # Kanagawa
    "Kanagawa", "Yokohama", "Kawasaki", "Tsurumi", "Higashi-Kanagawa", "Kannai",
    "Sakuragicho", "Totsuka", "Ofuna", "Sagamihara", "Atsugi", "Fujisawa", "Yokosuka",
    # Kansai (Osaka, Kyoto, Hyogo)
    "Osaka", "Namba", "Umeda", "Tennoji", "Abeno", "Shinsaibashi", "Nipponbashi",
    "Tsuruhashi", "Imazato", "Fuse", "Kyobashi", "Tenma", "Shin-Osaka", "Shin-Imamiya",
    "Higashi-Osaka", "Higashiosaka", "Sakai", "Yao", "Noda",
    "Kyoto", "Kobe", "Sannomiya", "Motomachi", "Amagasaki", "Nishinomiya", "Himeji",
    # Chubu / Aichi
    "Nagoya", "Sakae", "Kanayama", "Osu", "Fushimi", "Chikusa", "Ozone", "Hibino",
    "Aichi", "Toyohashi", "Okazaki", "Toyota", "Gifu", "Shizuoka", "Hamamatsu",
    # Kyushu / Other
    "Fukuoka", "Hakata", "Tenjin", "Ohashi", "Hiroshima", "Sendai", "Sapporo", "Hokkaido", "Okinawa",
]


# ---------------------------------------------------------------- basic field extractors

def extract_phones(text: str) -> list[str]:
    """Extract all Japanese and Vietnamese phone numbers from text."""
    clean = normalize_fullwidth(text)
    found: list[str] = []
    for regex in (JP_PHONE_RE, VN_PHONE_RE):
        for m in regex.finditer(clean):
            raw = m.group(0).strip()
            digits = re.sub(r"\D", "", raw)
            if len(digits) < 9 or len(digits) > 13:
                continue
            if raw not in found:
                found.append(raw)
    return found


def extract_contact_channels(text: str) -> dict:
    """Extract phone, Line, Zalo, Viber, Messenger/Inbox contact hints."""
    clean = normalize_fullwidth(text)
    low = norm_vn(clean)
    phones = extract_phones(clean)
    channels: list[str] = []
    line_id = None
    zalo_id = None

    m_line = re.search(r"\bline\s*(?:id)?\s*[:\-]?\s*([A-Za-z0-9._\-]{3,30})", clean, re.IGNORECASE)
    if m_line and norm_vn(m_line.group(1)) not in ("hoac", "hay", "nha", "nhe", "a", "em", "minh"):
        line_id = m_line.group(1)
        channels.append(f"Line: {line_id}")
    elif re.search(r"\bline\b", low):
        channels.append("Line")

    m_zalo = re.search(r"\bzalo\s*[:\-]?\s*([A-Za-z0-9.+_\-]{6,20})", clean, re.IGNORECASE)
    if m_zalo:
        zalo_id = m_zalo.group(1)
        channels.append(f"Zalo: {zalo_id}")
    elif re.search(r"\bzalo\b", low):
        channels.append("Zalo")

    if re.search(r"\b(inbox|ib|nhan tin|messenger|cham bai)\b", low):
        channels.append("Inbox FB")
    if re.search(r"\bviber\b", low):
        channels.append("Viber")

    return {
        "phone": phones[0] if phones else None,
        "all_phones": phones,
        "line_id": line_id,
        "zalo_id": zalo_id,
        "contact_methods": channels,
    }


def extract_location_jp(text: str, group_name: str = "", group_id: str = "") -> dict:
    """Extract station (ga tàu) and region/area in Japan from post text (with group fallback)."""
    clean = normalize_fullwidth(text)
    stations: list[str] = []
    for m in STATION_RE.finditer(clean):
        st = (m.group(1) or m.group(2) or m.group(3) or "").strip(" ,.:;-()[]")
        if st and len(st) >= 2 and st.lower() not in ("tại", "ở", "gần", "cách", "đi"):
            if st not in stations:
                stations.append(st)

    areas: list[str] = []
    low_norm = norm_vn(clean)
    for area in JP_AREAS:
        area_norm = norm_vn(area)
        if re.search(rf"\b{re.escape(area_norm)}\b", low_norm):
            if area not in areas:
                areas.append(area)

    # Fallback: infer area from group_name or group_id (e.g. "nagoyabaito" -> Nagoya)
    if not areas and (group_name or group_id):
        grp_norm = norm_vn(f"{group_name} {group_id}")
        for area in JP_AREAS:
            if norm_vn(area) in grp_norm:
                areas.append(f"{area} (theo nhóm)")
                break

    explicit_loc = None
    m_loc = re.search(
        r"(?:Địa chỉ|Địa điểm|Khu vực|Quán tại|Quán ở|Tiệm ở|Tiệm tại|📌|📍)\s*:?\s*([^\n]{3,90})",
        clean,
        re.IGNORECASE,
    )
    if m_loc:
        explicit_loc = m_loc.group(1).strip()

    summary_parts: list[str] = []
    if stations:
        summary_parts.append(f"Ga {', '.join(stations[:2])}")
    if areas:
        summary_parts.append(", ".join(areas[:3]))
    if explicit_loc and not summary_parts:
        summary_parts.append(explicit_loc)

    return {
        "location": " | ".join(summary_parts) if summary_parts else explicit_loc,
        "stations": stations,
        "areas": areas,
        "raw_address": explicit_loc,
    }


def extract_wage_jp(text: str) -> str | None:
    """Extract hourly wage (¥/h), monthly salary (man), or price from post."""
    clean = normalize_fullwidth(text)
    m = HOURLY_WAGE_JP_RE.search(clean)
    if m:
        raw = m.group(0).strip()
        return re.sub(r"^(?:時給|jikyu|lương|luong)\s*[:\-]?\s*", "", raw, flags=re.IGNORECASE).strip()
    m_any = ANY_PRICE_JP_RE.search(clean)
    if m_any:
        return m_any.group(0).strip()
    m_legacy = PRICE_RE.search(clean)
    return m_legacy.group(0).strip() if m_legacy else None


# ---------------------------------------------------------------- Step 1 & Step 2 Business Analysis

BUSINESS_CATEGORIES = [
    (
        "Nhà hàng / Quán ăn Việt Nam",
        [
            "pho", "banh mi", "bun cha", "bun bo", "com tam", "quan viet",
            "nha hang viet", "do an viet", "am thuc viet", "quan an viet",
            "nem", "goi cuon", "chao long", "lau viet", "vit quay", "ga nuong",
        ],
    ),
    (
        "Quán nhậu / Izakaya / Yakiniku / Ramen",
        [
            "izakaya", "居酒屋", "quan nhau", "yakiniku", "焼肉", "quan nuong",
            "thit nuong", "ramen", "ラーメン", "sushi", "寿司", "yakitori", "焼き鳥",
            "udon", "soba", "karaoke", "bar", "pub",
        ],
    ),
    (
        "Quán Cafe / Trà sữa / Tiệm bánh",
        [
            "cafe", "ca phe", "coffee", "tra sua", "tiem banh", "banh ngot",
            "banh sinh nhat", "bakery", "che", "do uong", "an vat",
        ],
    ),
    (
        "Tiệm Nail / Mi / Spa / Làm đẹp",
        [
            "tiem nail", "lam nail", "tho nail", "noi mi", "spa", "goi dau",
            "duong sinh", "massage", "salon", "cat toc", "lam dep", "cham soc da",
        ],
    ),
    (
        "Kinh doanh Thực phẩm / Hoa quả / Tạp hóa Việt",
        [
            "thuc pham viet", "quan tap hoa", "tap hoa", "sieu thi mini",
            "do kho", "cua hang thuc pham", "ban do viet", "thit ca", "rau cu",
            "trai cay", "hoa qua", "/thung", "thung freeship", "freeship",
            "hong gion", "sau rieng", "xoai", "vai thieu", "mit", "hai san",
            "gio cha", "kim chi", "chan ga", "do an nha lam", "bep nha",
        ],
    ),
    (
        "Xưởng chế biến nhỏ / Bento / Thực phẩm",
        [
            "bento", "弁当", "com hop", "dau phu", "xuong nho",
            "xuong thuc pham", "che bien", "so che", "dong goi thuc pham", "lam banh",
        ],
    ),
    (
        "Shop bán hàng / Kho Online / Livestream",
        [
            "livestream", "live stream", "chot don", "dong hang", "shop quan ao",
            "ban hang online", "kho nho", "my pham", "dien thoai", "pack hang",
            "gom order", "hang nhat", "hang viet", "si le", "hang nha em san nhat",
            "nhan hang truoc ck sau",
        ],
    ),
    (
        "Cửa hàng Đồ cũ / Thanh lý (Recycle Shop)",
        [
            "recycle shop", "do cu", "thanh ly", "for sale", "hang bai",
            "tu lanh", "may giat", "may suoi",
        ],
    ),
    (
        "Dịch vụ Viễn thông / Wifi / Sim thẻ tại Nhật",
        [
            "cap quang", "lap mang", "wifi", "sim nhat", "sim data", "mang internet",
        ],
    ),
    (
        "Đại lý Vé máy bay / Du lịch Việt - Nhật",
        [
            "ve may bay", "japan travel", "bay thang", "vietnam airlines", "vietjet",
            "bamboo", "ana", "jal", "hanh ly",
        ],
    ),
    (
        "Bất động sản / Cho thuê nhà ở tại Nhật",
        [
            "cho thue nha", "nha 2dk", "nha 1k", "nha 1dk", "nha 3dk", "1ldk", "2ldk",
            "chi phi dau vao", "phi quan ly", "bat dong san", "tim nha",
        ],
    ),
    (
        "Dịch vụ Vận chuyển / Vệ sinh / Khách sạn nhỏ",
        [
            "chuyen nha", "van chuyen", "don phong", "khach san", "homestay",
            "ve sinh", "giao hang", "lai xe", "xe ghep", "gui do",
        ],
    ),
]

# A target description may name several kinds of businesses. These are OR
# choices; other stated criteria (area, size, wage, shift) must also match.
PROFILE_BUSINESS_GROUPS = [
    (("quan an", "nha hang", "quan pho", "am thuc viet", "f&b"),
     ("Nhà hàng / Quán ăn Việt Nam", "Quán ăn / Nhà hàng (F&B)")),
    (("izakaya", "quan nhau", "yakiniku", "quan nuong", "ramen", "sushi"),
     ("Quán nhậu / Izakaya / Yakiniku / Ramen",)),
    (("cafe", "ca phe", "tra sua", "tiem banh", "bakery"),
     ("Quán Cafe / Trà sữa / Tiệm bánh",)),
    (("nail", "spa", "noi mi", "lam dep", "salon"),
     ("Tiệm Nail / Mi / Spa / Làm đẹp", "Tiệm Nail / Mi / Spa")),
    (("thuc pham", "tap hoa", "hoa qua", "trai cay", "sieu thi mini"),
     ("Kinh doanh Thực phẩm / Hoa quả / Tạp hóa Việt",)),
    (("bento", "xuong che bien", "xuong thuc pham", "com hop"),
     ("Xưởng chế biến nhỏ / Bento / Thực phẩm",)),
    (("shop", "ban hang online", "ban hang", "livestream", "kho hang"),
     ("Shop bán hàng / Kho Online / Livestream", "Kinh doanh Online / Bán hàng cá nhân tại Nhật")),
    (("do cu", "recycle", "thanh ly"),
     ("Cửa hàng Đồ cũ / Thanh lý (Recycle Shop)",)),
    (("wifi", "sim the", "vien thong", "cap quang", "internet"),
     ("Dịch vụ Viễn thông / Wifi / Sim thẻ tại Nhật",)),
    (("ve may bay", "du lich"),
     ("Đại lý Vé máy bay / Du lịch Việt - Nhật",)),
    (("bat dong san", "cho thue nha"),
     ("Bất động sản / Cho thuê nhà ở tại Nhật",)),
    (("van chuyen", "giao hang", "khach san", "homestay", "ve sinh"),
     ("Dịch vụ Vận chuyển / Vệ sinh / Khách sạn nhỏ",)),
]

ROLE_KEYWORDS = [
    ("Bếp / Phụ bếp (Kitchen)", ["phu bep", "bep", "kitchen", "キッチン", "nau an", "so che", "nau chinh"]),
    ("Phục vụ / Chạy bàn (Hall)", ["chay ban", "phuc vu", "hall", "ホール", "oder", "order", "bung be"]),
    ("Thu ngân / Quầy (Reji)", ["thu ngan", "reji", "レジ", "quay thu ngan", "tinh tien"]),
    ("Rửa bát / Dọn dẹp (Araba)", ["rua bat", "洗い場", "don dep", "tap vu"]),
    ("Pha chế / Quầy nước", ["pha che", "barista", "quay nuoc", "tra sua"]),
    ("Thợ Nail / Nối mi / Spa", ["tho nail", "noi mi", "ktv spa", "goi dau", "massage"]),
    ("Đóng gói / Kho / Ship hàng", ["dong goi", "dong hang", "soan hang", "shipper", "giao hang", "van chuyen"]),
    ("Bán hàng / Livestream / Trực page", ["ban hang", "livestream", "truc page", "chot don", "tu van"]),
    ("Quản lý cửa hàng / Trưởng ca (Tencho)", ["quan ly", "tencho", "店長", "truong ca"]),
]


def classify_baito_post(content: str, author_name: str = "") -> dict:
    """Classify a Baito/Group post to determine if the author is a small business owner (< 20 people).

    Distinguishes:
    - `small_business_owner` (subtypes: `owner_hiring` or `owner_selling`)
    - `job_seeker`
    - `agency_or_large`
    - `unknown`
    """
    clean = clean_fb_noise(content or "", author_name=author_name)
    # Match hiring keywords on text with original Vietnamese accents too so "Tuyến" != "Tuyển"!
    clean_lower = clean.lower()
    low = norm_vn(clean)

    # 1. Check headcount mentioned (e.g. "tuyển 2 bạn", "tuyển 30 người")
    headcounts: list[int] = []
    for m in re.finditer(
        r"(?:tuyen|can|them|nhan)\s+(?:gap\s+)?(\d{1,3})\s*(?:ban|nguoi|nhan vien|nv|suat|slot|anh chi)",
        low,
    ):
        try:
            headcounts.append(int(m.group(1)))
        except ValueError:
            pass
    max_headcount = max(headcounts) if headcounts else None

    # 2. Check if it's a large agency / haken / union / factory > 20 people
    agency_markers = [
        "cong ty phai cu", "haken", "派遣", "nghiep doan", "xuat khau lao dong",
        "xkld", "tuyen cong tac vien", "tuyen ctv tuyen dung", "day chuyen nha may",
        "phi gioi thieu", "mien phi gioi thieu viec", "tong hop don baito",
    ]
    for marker in agency_markers:
        if marker in low:
            return {
                "category": "agency_or_large",
                "owner_activity": "none",
                "is_target_owner": False,
                "headcount_mentioned": max_headcount,
                "scale_estimate": "Công ty môi giới / Haken / Nghiệp đoàn (> 20 người)",
                "reason": f"Chứa dấu hiệu môi giới/công ty lớn: '{marker}'",
            }

    if max_headcount is not None and max_headcount >= 20:
        return {
            "category": "agency_or_large",
            "owner_activity": "none",
            "is_target_owner": False,
            "headcount_mentioned": max_headcount,
            "scale_estimate": f"Quy mô lớn (tuyển {max_headcount} người >= 20)",
            "reason": f"Số lượng tuyển lớn ({max_headcount} người >= 20)",
        }

    # 3. Check if it's a job seeker ("em cần tìm việc", "mình tìm baito")
    seeker_markers = [
        "em can tim baito", "minh can tim baito", "em tim baito", "minh tim baito",
        "can tim viec lam them", "muon tim baito", "muon tim viec", "em can tim viec",
        "xin tim viec", "co quan nao tuyen khong", "co anh chi nao tuyen",
        "gioi thieu giup em", "em dang that nghiep", "tim viec quanh ga",
    ]
    hiring_signals_accented = [
        "tuyển", "cần tuyển", "tuyển gấp", "tuyển phụ bếp", "tuyển chạy bàn",
        "tuyển nhân viên", "tuyển baito", "tuyển thợ", "時給", "まかない",
    ]
    hiring_signals_norm = [
        "quan minh tuyen", "quan can tuyen", "tiem minh tuyen", "nha hang can tuyen",
        "ben minh tuyen", "can tuyen", "tuyen baito", "tuyen nhan vien", "tuyen gap",
        "tuyen phu bep", "tuyen chay ban", "tuyen tho", "bao an", "ho tro tau",
        "jikyu", "yen/h", "¥/h", "phu bep", "chay ban", "hall", "kitchen",
    ]
    is_seeker_text = any(s in low for s in seeker_markers)
    has_strong_hiring = any(h in clean_lower for h in hiring_signals_accented) or any(
        h in low for h in hiring_signals_norm
    )
    if is_seeker_text and not has_strong_hiring:
        return {
            "category": "job_seeker",
            "owner_activity": "none",
            "is_target_owner": False,
            "headcount_mentioned": max_headcount,
            "scale_estimate": "Cá nhân đi tìm việc",
            "reason": "Bài đăng của người đi tìm việc làm thêm (Job Seeker)",
        }

    # 4a. Check Small Business Owner HIRING staff (< 20 people)
    owner_hiring_refs = [
        "quan minh", "tiem minh", "nha hang minh", "shop minh", "xuong minh",
        "quan anh", "quan chi", "tiem chi", "tiem anh", "quan em",
        "quan can", "tiem can", "nha hang can", "quan pho", "quan viet",
        "quan nhau", "izakaya", "tiem nail", "tiem banh", "tap hoa", "cua hang minh",
        "quan o", "quan tai", "tiem o", "tiem tai", "nha hang o", "nha hang tai",
    ]
    broad_hiring_phrases = [
        "can nguoi lam", "can ban lam", "tim nguoi lam", "tim ban lam",
        "can 1 ban", "can 2 ban", "can 3 ban", "can 4 ban", "can 5 ban",
        "tim 1 ban", "tim 2 ban", "tim 3 ban", "can gap", "can them",
        "ca sang", "ca trua", "ca chieu", "ca toi", "ca dem",
        "lương", "時給", "交通費", "まかない", "ho tro tau", "bao an",
    ]
    has_owner_hiring_ref = any(re.search(rf"\b{re.escape(r)}\b", low) for r in owner_hiring_refs)
    has_broad_hiring = any(p in low or p in clean_lower for p in broad_hiring_phrases)
    # Whole-word checks: plain substrings like "can" also hit "canh", "tim" hits "tiem"...
    has_generic_need = bool(re.search(r"\b(?:can|tim|them|nhan vien|baito)\b", low))
    has_workplace_word = bool(
        re.search(r"\b(?:quan|tiem|nha hang|bep|hall|kitchen|baito|nhan vien|lam them)\b", low)
    )
    if (
        has_strong_hiring
        or (has_owner_hiring_ref and (has_broad_hiring or has_generic_need))
        or (has_broad_hiring and has_workplace_word)
    ):
        if max_headcount:
            scale_str = f"Mô hình nhỏ (< 20 người) — Đang tuyển {max_headcount} người"
        elif has_owner_hiring_ref:
            scale_str = "Chủ quán / Tiệm kinh doanh nhỏ (< 20 người)"
        else:
            scale_str = "Cơ sở kinh doanh nhỏ (< 20 người)"
        return {
            "category": "small_business_owner",
            "owner_activity": "owner_hiring",
            "is_target_owner": True,
            "headcount_mentioned": max_headcount,
            "scale_estimate": scale_str,
            "reason": "Chủ quán/cơ sở nhỏ trực tiếp đăng tuyển nhân sự",
        }

    # 4b. Check Individual / Small Business Owner SELLING products or services directly
    selling_signals = [
        "freeship", "free ship", "ship toan nhat", "ship quanh", "len don",
        "chot don", "si le", "sỉ lẻ", "bên em", "ben em", "bên mình",
        "nhà em", "nha em", "quán em", "tiệm em", "shop em", "menu", "khách",
        "ib luôn", "ib em", "ib nha", "inbox", "/thùng", "/thung", "/kg", "/set", "/hộp", "/hop",
        "nhận chuyển nhà", "nhan chuyen nha", "xe ghép", "nhận làm", "nhan lam",
        "nhận đặt", "nhan dat", "bán", "chuyên cung cấp", "khai trương",
        "đặt lịch", "dat lich", "liên hệ", "lien he", "sẵn hàng", "san hang",
        # Shops / services advertising to the community
        "dat ban", "dang mo cua", "hang moi ve", "quan em mo", "for sale", "recycle shop",
        "lay ve", "bay thang", "chi phi dau vao", "phi quan ly", "mien phi lap dat",
        "cap quang", "dang ky nhanh",
    ]
    # Private one-off sales / buyers are not businesses
    private_markers = ("can nhuong lai", "nhuong lai ve", "em can mua", "minh can mua", "can mua cai")
    if any(m in low for m in private_markers):
        return {
            "category": "unknown",
            "owner_activity": "none",
            "is_target_owner": False,
            "headcount_mentioned": max_headcount,
            "scale_estimate": "Cá nhân mua/bán lẻ một lần",
            "reason": "Cá nhân nhượng lại / hỏi mua đồ (không phải mô hình kinh doanh)",
        }
    has_phone_or_contact = bool(extract_phones(clean)) or bool(
        re.search(r"\b(inbox|ib|line|zalo|sdt|đt)\b", low)
    )
    if any(s in clean_lower or s in low for s in selling_signals) or (
        has_phone_or_contact and any(w in low for w in ("quan", "tiem", "shop", "dich vu", "gia", "yen", "man", "円", "¥"))
    ):
        return {
            "category": "small_business_owner",
            "owner_activity": "owner_selling",
            "is_target_owner": True,
            "headcount_mentioned": None,
            "scale_estimate": "Cá nhân / Hộ kinh doanh nhỏ tại Nhật (< 5 người)",
            "reason": "Cá nhân/chủ shop nhỏ trực tiếp đăng bán hàng hoặc dịch vụ",
        }

    return {
        "category": "unknown",
        "owner_activity": "none",
        "is_target_owner": False,
        "headcount_mentioned": max_headcount,
        "scale_estimate": "Chưa rõ",
        "reason": "Không có dấu hiệu tuyển dụng hoặc kinh doanh rõ ràng",
    }


_AUTHOR_SHOP_TOKENS = (
    "nail", "nails", "eyelash", "eyelashes", "spa", "salon", "beauty", "hair", "barber",
    "quán", "quan", "phở", "pho", "bún", "bun", "bếp", "bep", "cơm", "com", "bánh", "banh",
    "nhà hàng", "izakaya", "ramen", "sushi", "yakiniku", "shop", "store", "mart", "market",
    "food", "foods", "travel", "tour", "mobile", "wifi", "sim", "auto", "car", "house",
    "home", "studio", "lounge", "cafe", "coffee", "bakery", "tạp hóa", "đồ cũ", "recycle",
    "kitchen", "clinic", "dental", "massage", "bistro", "grill", "bbq",
)
_AUTHOR_NOT_SHOP_TOKENS = (
    "tuyển dụng", "việc làm", "baito", "hội ", "nhóm ", "cộng đồng", "chưa rõ", "thành viên", "ẩn danh", "admin",
)
_CAPS_BRAND_STOP = {
    "VND", "F&B", "CMT", "IB", "INBOX", "SDT", "SĐT", "ZALO", "LINE", "VIP", "FULL", "PART",
    "OK", "JP", "VN", "ACE", "HANG", "HÀNG", "NHA", "NHÀ", "EM", "ANH", "CHỊ", "CHI", "CẦN",
    "CAN", "TUYỂN", "TUYEN", "SHIP", "FREE", "FREESHIP", "CK", "COD", "HOT", "NEW", "SALE",
    "KARAOKE", "MENU", "ORDER", "SET", "KG", "MAN", "YEN", "TOKYO", "OSAKA", "NAGOYA",
}


def extract_shop_name(content: str, author_name: str = "") -> str | None:
    """Best-effort extraction of the shop/restaurant/salon name from post text or FB Page author name."""
    clean = clean_fb_noise(content or "")

    # 1. Compact number/prefix + suffix shop name (e.g. '678Quán', 'MaySpa', 'VietMart')
    m_compact = re.search(
        r"\b([0-9]{1,5}[A-ZÀ-Ỹa-zà-ỹ]{0,15}|[A-ZÀ-Ỹ][A-Za-zÀ-ỹ0-9]{1,18})(Quán|Nail|Nails|Spa|Salon|Shop|Store|Mart|Kitchen|Cafe|Coffee|Travel|Eyelashes)\b",
        clean,
    )
    if m_compact:
        cand = (m_compact.group(1) + m_compact.group(2)).strip()
        if 3 <= len(cand) <= 35:
            return cand

    # 2. Brand suffix pattern (e.g. 'Linh Hiền Eyelashes', 'Sakura Nail Salon', 'Phở Hà Nội Quán')
    m_suffix = re.search(
        r"(?:^|\n|\b(?:tại|ở|ghé|đến|tiệm|quán)\s+)([A-ZÀ-Ỹ][A-Za-zÀ-ỹ0-9&'\-]{1,22}(?:\s+[A-ZÀ-Ỹ0-9][A-Za-zÀ-ỹ0-9&'\-]{1,20}){0,2}\s+(?:Nail|Nails|Eyelashes|Eyelash|Salon|Spa|Beauty|Travel|Tours|Izakaya|Ramen|Kitchen|Bakery|Cafe|Coffee|Mart|Store))\b",
        clean,
    )
    if m_suffix:
        cand = m_suffix.group(1).strip(" -–—:.,!\"'")
        first_w = cand.split()[0].lower() if cand.split() else ""
        if first_w not in {"tuyển", "tìm", "cần", "mẫu", "thợ", "nhận", "làm", "chuyên", "hội"} and 4 <= len(cand) <= 42:
            return cand

    # 3. Explicit prefix pattern ('Quán ...', 'Tiệm ...', 'Tên quán: ...')
    patterns = [
        r"(?:Quán|Tiệm|Nhà hàng|Shop|Cửa hàng|Izakaya|Tiệm nail|Quán ăn|Quán phở|Bếp|Salon)\s+(?:bên|của|nhà)?\s*([A-ZÀ-Ỹ0-9][^\n,.;:()!]{1,35})",
        r"(?:Tên quán|Tên tiệm|🏠|🏪|🍜|💅)\s*[:\-]?\s*([^\n,;]{2,40})",
    ]
    stop_words = {
        "mình", "em", "anh", "chị", "tuyển", "cần", "đang", "ở", "tại", "gần",
        "nhỏ", "mới", "việt", "nhật", "cầu", "baito", "ăn", "uống", "cũ", "này",
        "đây", "đó", "nào", "nhà", "chúc", "làm", "bên", "gấp",
    }
    for pat in patterns:
        for m in re.finditer(pat, clean):
            candidate = m.group(1).strip(" -–—:.,!\"'")
            first_word = candidate.split()[0].lower() if candidate.split() else ""
            if first_word in stop_words or len(candidate) < 2:
                continue
            candidate = re.split(
                r"\s+(?:cần tuyển|tuyển|đang cần|đang mở|ở ga|tại ga|cách ga|gần ga|chúc|khu vực|ở tại|tại)\b",
                candidate,
                flags=re.IGNORECASE,
            )[0].strip()
            if 2 <= len(candidate) <= 40 and len(candidate.split()) <= 5:
                return candidate

    # 4. ALL-CAPS brand after action cue ('Ghé JAPAN TRAVEL', 'Tại SAKURA')
    m_caps = re.search(
        r"\b(?:[Gg]hé|[Tt]ại|[Đđ]ến|[Cc]ủa|[Nn]hà hàng|[Tt]iệm|[Qq]uán)\s+([A-Z][A-Z0-9&\-]{2,}(?:\s+[A-Z0-9&\-]{2,}){0,3})\b",
        clean,
    )
    if m_caps:
        cand = m_caps.group(1).strip()
        words = cand.split()
        if any(w not in _CAPS_BRAND_STOP for w in words) and 3 <= len(cand) <= 35:
            return cand

    # 5. Author / Facebook Page name when it clearly names a business (e.g. 'Queens Nail', 'Linh Hiền Eyelashes')
    auth = (author_name or "").strip()
    if auth and 3 <= len(auth) <= 45:
        auth_low = auth.lower()
        if not any(bad in auth_low for bad in _AUTHOR_NOT_SHOP_TOKENS):
            if any(re.search(rf"(?:^|\b){re.escape(tok)}(?:\b|$)", auth_low) for tok in _AUTHOR_SHOP_TOKENS):
                # Strip trailing location tag if user wrote 'Linh Hiền Eyelashes kawaguchi'
                return auth

    return None


def analyze_business_and_needs(
    content: str,
    author_name: str = "",
    group_name: str = "",
    group_id: str = "",
) -> dict:
    """Two-stage analysis on a small business owner's Baito post:
    Stage 1: What business are they running? (`business_type`, `shop_name`, `location`, `scale_estimate`)
    Stage 2: What do they need deeply? (`roles_needed`, `shifts_needed`, `wage`, `pain_points_and_needs`)
    """
    clean = clean_fb_noise(content or "", author_name=author_name)
    low = norm_vn(clean)

    classification = classify_baito_post(clean, author_name=author_name)
    owner_activity = classification.get("owner_activity", "none")
    loc_info = extract_location_jp(clean, group_name=group_name, group_id=group_id)
    contact_info = extract_contact_channels(clean)
    wage = extract_wage_jp(clean)
    shop_name = extract_shop_name(clean, author_name=author_name)

    # Stage 1: Identify business type (Đang kinh doanh gì?)
    matched_types: list[str] = []
    for label, keywords in BUSINESS_CATEGORIES:
        if any(kw in low for kw in keywords):
            matched_types.append(label)

    if not matched_types:
        if owner_activity == "owner_selling":
            matched_types.append("Kinh doanh Online / Bán hàng cá nhân tại Nhật")
        elif any(w in low for w in ("phu bep", "chay ban", "hall", "kitchen", "bao an", "makanai", "rua bat")):
            matched_types.append("Quán ăn / Nhà hàng (F&B)")
        elif any(w in low for w in ("nail", "mi", "spa")):
            matched_types.append("Tiệm Nail / Mi / Spa")
        else:
            matched_types.append("Cơ sở kinh doanh nhỏ / Dịch vụ tại Nhật")

    business_type = matched_types[0]

    # Stage 2: Deep needs analysis (Đi sâu tìm hiểu họ cần gì?)
    roles_needed: list[str] = []
    shifts: list[str] = []
    benefits: list[str] = []
    pain_points: list[str] = []

    if owner_activity == "owner_selling":
        # Direct seller / micro-business owner selling products/services in the group
        roles_needed.append("Chủ tự vận hành / Bán hàng trực tiếp (Chưa tuyển người)")
        jp_req = "Không áp dụng (Bán hàng cho cộng đồng người Việt tại Nhật)"
        shop_customer_segment = "Cộng đồng người Việt tại Nhật Bản"
        if any(w in low for w in ("freeship", "free ship", "ship", "giao hang", "thung")):
            benefits.append("Có hỗ trợ Freeship / Giao hàng tận nơi")
            pain_points.append(
                "Đang tự đăng bài bán hàng & chốt đơn thủ công qua Inbox (cần mở rộng tệp khách hàng, tự động hóa chốt đơn/CSKH và tối ưu chi phí vận chuyển/ship hàng)"
            )
        if any(w in low for w in ("het mua", "xa kho", "xau ma", "thanh ly", "ton kho")):
            pain_points.append(
                "Áp lực đẩy hàng tồn / hàng theo mùa vụ nhanh (nhạy cảm về tốc độ tiêu thụ hàng hóa)"
            )
        if not pain_points:
            pain_points.append(
                "Cá nhân kinh doanh nhỏ đang tìm kiếm khách hàng qua hội nhóm Facebook (cần công cụ bán hàng, quản lý đơn và tiếp cận khách hàng)"
            )
    else:
        # Business owner hiring staff (`owner_hiring`)
        for role_label, role_kws in ROLE_KEYWORDS:
            if any(kw in low for kw in role_kws):
                roles_needed.append(role_label)
        if not roles_needed and classification["is_target_owner"]:
            roles_needed.append("Nhân viên làm thêm (Baito)")

        if any(w in low for w in ("ca sang", "buoi sang", "sang tu")):
            shifts.append("Ca sáng")
        if any(w in low for w in ("ca trua", "buoi trua", "10h", "11h")):
            shifts.append("Ca trưa")
        if any(w in low for w in ("ca chieu", "buoi chieu", "14h", "15h", "16h")):
            shifts.append("Ca chiều")
        if any(w in low for w in ("ca toi", "buoi toi", "17h", "18h", "19h")):
            shifts.append("Ca tối")
        if any(w in low for w in ("ca dem", "lam dem", "sau 22h", "den 24h", "den sang")):
            shifts.append("Ca đêm")
        if any(w in low for w in ("cuoi tuan", "thu 7", "chu nhat", "t7", "cn")):
            shifts.append("Cuối tuần (T7/CN)")

        jp_req = "Chưa nêu rõ"
        shop_customer_segment = "Khách Việt hoặc tổng hợp"
        if any(w in low for w in ("khong yeu cau tieng", "khong can tieng", "ko can tieng", "k can tieng")):
            jp_req = "Không yêu cầu tiếng Nhật"
            shop_customer_segment = "Chủ yếu phục vụ khách Việt (hoặc vị trí bếp/sơ chế nội bộ)"
        elif any(w in low for w in ("n2", "n1", "giao tiep tot", "tieng nhat tot")):
            jp_req = "Tiếng Nhật tốt (N2/N1 / Giao tiếp thành thạo)"
            shop_customer_segment = "Phục vụ khách bản địa Nhật Bản"
        elif any(w in low for w in ("n3", "n4", "giao tiep co ban", "biet tieng", "oder", "order")):
            jp_req = "Giao tiếp cơ bản (N4–N3)"
            shop_customer_segment = "Có khách Nhật bản địa & khách Việt"

        if any(w in low for w in ("ho tro tau", "tau xe", "kotsuhi", "交通費")):
            benefits.append("Hỗ trợ tàu xe (交通費)")
        if any(w in low for w in ("bao an", "an tai quan", "makanai", "まかない")):
            benefits.append("Bao ăn tại quán (まかない)")
        if any(w in low for w in ("visa", "tokutei", "ky su", "chuyen visa", "gia han visa")):
            benefits.append("Hỗ trợ chuyển/gia hạn Visa (Tokutei/Kỹ sư)")
        if any(w in low for w in ("tang luong", "thuong", "bonus")):
            benefits.append("Có tăng lương / thưởng")

        if any(w in low for w in ("tuyen gap", "can gap", "di lam ngay", "lam luon")):
            pain_points.append(
                "Thiếu hụt nhân sự khẩn cấp (cần người đi làm ngay, chủ quán đang chịu tải vận hành)"
            )
        if any(w in low for w in ("khai truong", "moi mo", "chi nhanh moi", "mo rong", "sap mo")):
            pain_points.append(
                "Đang mở mới / mở rộng cơ sở (cần đồng bộ nhân sự, quy trình, marketing, thiết bị)"
            )
        if any(w in low for w in ("lam lau dai", "gan bo", "on dinh", "nhan vien chinh thuc", "chuyen visa")):
            pain_points.append(
                "Khát nhân sự gắn bó lâu dài (đau đầu vì baito nghỉ/nhảy việc, sẵn sàng hỗ trợ Visa để giữ người)"
            )
        if len(roles_needed) >= 2:
            pain_points.append(f"Thiếu nhân sự ở nhiều khâu cùng lúc ({', '.join(roles_needed)})")
        if "Cuối tuần (T7/CN)" in shifts or "Ca tối" in shifts:
            pain_points.append("Quá tải vào khung giờ cao điểm (buổi tối / cuối tuần)")
        if not pain_points and classification["is_target_owner"]:
            pain_points.append("Cần bổ sung nhân sự vận hành thường xuyên cho cửa hàng")

    return {
        "is_target_owner": classification["is_target_owner"],
        "post_category": classification["category"],
        "owner_activity": owner_activity,
        "classification_reason": classification["reason"],
        "business_type": business_type,
        "all_business_types": matched_types,
        "shop_name": shop_name,
        "scale_estimate": classification["scale_estimate"],
        "headcount_mentioned": classification["headcount_mentioned"],
        "location": loc_info["location"],
        "stations": loc_info["stations"],
        "areas": loc_info["areas"],
        "wage": wage,
        "roles_needed": roles_needed,
        "shifts_needed": shifts,
        "japanese_requirement": jp_req,
        "shop_customer_segment": shop_customer_segment,
        "benefits_offered": benefits,
        "pain_points_and_needs": pain_points,
        "contact": contact_info,
    }


def extract_fields(
    content: str,
    wanted: list[str],
    author_name: str = "",
    group_name: str = "",
    group_id: str = "",
) -> dict:
    """Extract requested fields from post text. Missing -> None, never guessed."""
    clean = clean_fb_noise(content or "", author_name=author_name)
    analysis = analyze_business_and_needs(
        clean, author_name=author_name, group_name=group_name, group_id=group_id
    )
    out: dict = {}
    for field in wanted:
        if field == "phone":
            out["phone"] = analysis["contact"]["phone"]
        elif field in ("contact", "contact_methods"):
            out[field] = ", ".join(analysis["contact"]["contact_methods"]) or analysis["contact"]["phone"]
        elif field == "line_id":
            out["line_id"] = analysis["contact"]["line_id"]
        elif field == "zalo_id":
            out["zalo_id"] = analysis["contact"]["zalo_id"]
        elif field in ("price", "wage"):
            out[field] = analysis["wage"]
        elif field == "location":
            out["location"] = analysis["location"]
        elif field == "station":
            out["station"] = ", ".join(analysis["stations"]) if analysis["stations"] else None
        elif field == "business_type":
            out["business_type"] = analysis["business_type"]
        elif field == "shop_name":
            out["shop_name"] = analysis["shop_name"]
        elif field == "scale":
            out["scale"] = analysis["scale_estimate"]
        elif field in ("roles", "roles_needed"):
            out[field] = analysis["roles_needed"] or None
        elif field in ("needs", "pain_points"):
            out[field] = analysis["pain_points_and_needs"] or None
        elif field == "time":
            m = TIME_RE.search(clean)
            out["time"] = m.group(0) if m else (", ".join(analysis["shifts_needed"]) or None)
        elif field == "level":
            out["level"] = sorted(set(LEVEL_RE.findall(clean))) or None
        else:
            m = re.search(rf"{re.escape(field)}\s*:\s*([^\n]{{1,120}})", clean, re.IGNORECASE)
            out[field] = m.group(1).strip() if m else None
    return out


def parse_price_vnd(text: str | None) -> int | None:
    """Convert an extracted price/wage like '65k', '1.5 triệu', '100.000đ', '1200¥' to numeric value."""
    if not text:
        return None
    clean = normalize_fullwidth(str(text))
    m = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(triệu|nghìn|VND|tr|k|đ|yên|yen|円|¥|y|man|vạn|万円)?",
        clean,
        re.IGNORECASE,
    )
    if not m:
        return None
    raw_num = m.group(1)
    unit = (m.group(2) or "").lower()
    if re.match(r"^\d{1,3}(?:\.\d{3})+$", raw_num):
        num_str = raw_num.replace(".", "")
    elif re.match(r"^\d{1,3}(?:,\d{3})+$", raw_num):
        num_str = raw_num.replace(",", "")
    else:
        num_str = raw_num.replace(",", ".")
    try:
        value = float(num_str)
    except ValueError:
        return None
    if unit in ("triệu", "tr"):
        value *= 1_000_000
    elif unit in ("nghìn", "k"):
        value *= 1_000
    elif unit in ("man", "vạn", "万円"):
        value *= 10_000
    return int(value)


def parse_customer_profile(profile: str) -> dict:
    """Pull matchable criteria out of a free-text customer description."""
    text = normalize_fullwidth(profile or "").strip()
    if not text:
        return {}
    out: dict = {}
    low = norm_vn(text)

    # Parentheses after a broad target usually list examples, not an exclusive
    # set. "Chủ quán ăn ..." remains a sector filter; "chủ KD nhỏ (... )" does not.
    sector_text = low
    if "(" in low and any(phrase in low.split("(", 1)[0] for phrase in (
        "ca nhan kinh doanh nho", "chu mo hinh kinh doanh nho",
    )) and not re.search(r"\bchi\s+", low.split("(", 1)[1]):
        sector_text = low.split("(", 1)[0]
    business_types = {
        label
        for aliases, labels in PROFILE_BUSINESS_GROUPS
        if any(alias in sector_text for alias in aliases)
        for label in labels
    }
    if business_types:
        out["business_types"] = sorted(business_types)

    if any(
        kw in low
        for kw in (
            "kinh doanh nho", "chu quan", "chu mo hinh", "duoi 20 nguoi",
            "< 20 nguoi", "chu tiem", "chu cua hang", "baito",
        )
    ):
        out["require_small_business_owner"] = True
        m_scale = re.search(r"(?:duoi|toi da|khong qua|<)\s*(\d+)\s*nguoi", low)
        out["max_headcount"] = int(m_scale.group(1)) if m_scale else 20

    levels = sorted(set(LEVEL_RE.findall(text)))
    if levels:
        out["levels"] = levels

    if not out.get("require_small_business_owner"):
        has_nu = bool(re.search(r"\bnu\b", low))
        has_nam = bool(re.search(r"\bnam\b", low))
        if has_nu and not has_nam:
            out["gender"] = "nữ"
        elif has_nam and not has_nu:
            out["gender"] = "nam"

    wanted_areas = [a for a in JP_AREAS if re.search(rf"\b{re.escape(norm_vn(a))}\b", low)]
    if wanted_areas:
        out["locations"] = wanted_areas

    m = re.search(
        r"từ\s*(\d+(?:[.,]\d+)?\s*(?:triệu|nghìn|VND|tr|k|đ|yên|yen|¥))\s*đến\s*(\d+(?:[.,]\d+)?\s*(?:triệu|nghìn|VND|tr|k|đ|yên|yen|¥))",
        text,
        re.IGNORECASE,
    )
    if m:
        out["price_min"] = parse_price_vnd(m.group(1))
        out["price_max"] = parse_price_vnd(m.group(2))
    else:
        m = re.search(
            r"(?:dưới|không quá|tối đa)\s*(\d+(?:[.,]\d+)?\s*(?:triệu|nghìn|VND|tr|k|đ|yên|yen|¥))",
            text,
            re.IGNORECASE,
        )
        if m:
            out["price_max"] = parse_price_vnd(m.group(1))
        m = re.search(
            r"(?:trên|từ)\s*(\d+(?:[.,]\d+)?\s*(?:triệu|nghìn|VND|tr|k|đ|yên|yen|¥))",
            text,
            re.IGNORECASE,
        )
        if m:
            out["price_min"] = parse_price_vnd(m.group(1))

    low_time = re.sub(r"toi\s*(da|thieu)", "", low)
    if "buoi sang" in low_time or re.search(r"\bsang\b", low_time):
        out["time_of_day"] = (6, 11)
    elif "buoi chieu" in low_time or re.search(r"\bchieu\b", low_time):
        out["time_of_day"] = (12, 17)
    elif "buoi toi" in low_time or re.search(r"\btoi\b", low_time):
        out["time_of_day"] = (17, 23)
    elif re.search(r"\btrua\b", low_time):
        out["time_of_day"] = (11, 13)
    return out


def match_customer_profile(post: dict, profile: str) -> tuple[bool, dict]:
    """Check one post against a free-text customer profile."""
    criteria = parse_customer_profile(profile)
    if not criteria:
        return True, {"matched": ["no_criteria"], "contradicted": [], "unknown": []}

    raw_content = post.get("content") or ""
    content = norm_vn(raw_content)
    extracted = post.get("extracted") or {}
    matched: list[str] = []
    contradicted: list[str] = []
    unknown: list[str] = []

    if criteria.get("require_small_business_owner"):
        analysis = post.get("business_analysis") or analyze_business_and_needs(
            raw_content,
            author_name=post.get("author_name") or "",
            group_name=post.get("group_name") or "",
            group_id=post.get("group_id") or "",
        )
        post["business_analysis"] = analysis
        max_hc = criteria.get("max_headcount", 20)
        hc = analysis.get("headcount_mentioned")
        if hc is not None and hc >= max_hc:
            contradicted.append("scale_under_20")
        elif analysis.get("is_target_owner"):
            matched.append("small_business_owner")
            if analysis.get("business_type"):
                matched.append(f"business:{analysis['business_type']}")
        else:
            contradicted.append(f"not_owner:{analysis.get('post_category', 'unknown')}")

    if "business_types" in criteria:
        analysis = post.get("business_analysis") or analyze_business_and_needs(
            raw_content,
            author_name=post.get("author_name") or "",
            group_name=post.get("group_name") or "",
            group_id=post.get("group_id") or "",
        )
        post["business_analysis"] = analysis
        have = set(analysis.get("all_business_types") or [analysis.get("business_type")])
        if have & set(criteria["business_types"]):
            matched.append("business_type")
        else:
            contradicted.append("business_type")

    if "levels" in criteria:
        have = {str(level).upper() for level in (extracted.get("level") or [])}
        if not have:
            unknown.append("level")
        elif {str(level).upper() for level in criteria["levels"]} & have:
            matched.append("level")
        else:
            contradicted.append("level")

    if "gender" in criteria and not criteria.get("require_small_business_owner"):
        want = norm_vn(criteria["gender"])
        other = "nam" if want == "nu" else "nu"
        if re.search(rf"\b{re.escape(want)}\b", content):
            matched.append("gender")
        elif re.search(rf"\b{other}\b", content):
            contradicted.append("gender")
        else:
            unknown.append("gender")

    if "locations" in criteria:
        analysis = post.get("business_analysis") or analyze_business_and_needs(
            raw_content,
            author_name=post.get("author_name") or "",
            group_name=post.get("group_name") or "",
            group_id=post.get("group_id") or "",
        )
        post["business_analysis"] = analysis
        areas = analysis.get("areas") or []
        explicit_areas = [area for area in areas if "(theo nhóm)" not in area]
        evidence = norm_vn(" ".join(explicit_areas or areas))
        if any(re.search(rf"\b{re.escape(norm_vn(loc))}\b", evidence) for loc in criteria["locations"]):
            matched.append("location")
        elif explicit_areas:
            contradicted.append("location")
        else:
            unknown.append("location")

    price = parse_price_vnd(extracted.get("price") or extracted.get("wage"))
    if "price_max" in criteria and criteria["price_max"] is not None:
        if price is None:
            unknown.append("price_max")
        elif price <= criteria["price_max"]:
            matched.append("price_max")
        else:
            contradicted.append("price_max")
    if "price_min" in criteria and criteria["price_min"] is not None:
        if price is None:
            unknown.append("price_min")
        elif price >= criteria["price_min"]:
            matched.append("price_min")
        else:
            contradicted.append("price_min")

    if "time_of_day" in criteria:
        hour = None
        t = extracted.get("time") or ""
        m = re.search(r"(\d{1,2})h", str(t))
        if m:
            hour = int(m.group(1))
        else:
            m = re.search(r"(\d{1,2}):\d{2}", str(t))
            if m:
                hour = int(m.group(1))
        start, end = criteria["time_of_day"]
        if hour is not None:
            if start <= hour < end or (end == 23 and hour == 23):
                matched.append("time_of_day")
            else:
                contradicted.append("time_of_day")
        else:
            words = re.sub(r"toi\s*(da|thieu)", "", content)
            word_ranges = {"sang": (6, 11), "trua": (11, 13), "chieu": (12, 17), "toi": (17, 23)}
            found = [rng for word, rng in word_ranges.items() if re.search(rf"\b{word}\b", words)]
            if not found:
                unknown.append("time_of_day")
            elif any(rng == (start, end) for rng in found):
                matched.append("time_of_day")
            else:
                contradicted.append("time_of_day")

    details = {"matched": matched, "contradicted": contradicted, "unknown": unknown}
    kept = not contradicted and not unknown and bool(matched)
    return kept, details


def post_matches_filters(post: dict, task: dict) -> bool:
    """Apply task filters and enrich post with `business_analysis` & `profile_match`."""
    raw_content = post.get("content") or ""
    content = raw_content.lower()
    extracted = post.get("extracted") or {}

    analysis = post.get("business_analysis") or analyze_business_and_needs(
        raw_content,
        author_name=post.get("author_name") or "",
        group_name=post.get("group_name") or "",
        group_id=post.get("group_id") or "",
    )
    post["business_analysis"] = analysis

    if task.get("only_small_business", False):
        if not analysis["is_target_owner"]:
            return False

    levels = task.get("levels")
    if levels:
        wanted = {str(level).upper() for level in levels}
        have = {str(level).upper() for level in (extracted.get("level") or [])}
        if not (wanted & have):
            return False

    min_price = task.get("min_price")
    max_price = task.get("max_price")
    if min_price is not None or max_price is not None:
        price = parse_price_vnd(extracted.get("price") or extracted.get("wage"))
        if price is None:
            return False
        if min_price is not None and price < int(min_price):
            return False
        if max_price is not None and price > int(max_price):
            return False

    location_keyword = (task.get("location_keyword") or "").strip()
    if location_keyword:
        location = str(extracted.get("location") or "")
        if not contains_vn(content, location_keyword) and not contains_vn(location, location_keyword):
            return False

    for word in task.get("exclude_keywords") or []:
        if str(word).strip() and contains_vn(content, str(word).strip()):
            return False

    profile = (task.get("customer_profile") or "").strip()
    if profile:
        matched, details = match_customer_profile(post, profile)
        post["profile_match"] = details
        if not matched:
            return False
    return True


# ---------------------------------------------------------------- Lead Aggregation (List & Deep Needs)

def _lead_key(post: dict) -> str:
    """Unique key to deduplicate/group posts by the same business owner."""
    author_url = (post.get("author_url") or "").strip()
    if author_url and "anonymous" not in author_url.lower() and "/stories/" not in author_url.lower():
        return f"fb:{author_url}"
    analysis = post.get("business_analysis") or {}
    phone = (analysis.get("contact") or {}).get("phone") or (post.get("extracted") or {}).get("phone")
    if phone:
        digits = re.sub(r"\D", "", str(phone))
        if digits:
            return f"phone:{digits}"
    author_name = (post.get("author_name") or "").strip()
    if author_name and norm_vn(author_name) not in ("", "chua ro ten fb", "thanh vien an danh", "anonymous participant"):
        return f"name:{norm_vn(author_name)[:50]}"
    # Fallback: group repeated posts with identical opening headline (e.g. same shop posting multiple albums)
    clean_text = clean_fb_noise(post.get("content") or "")
    first_line = next((ln.strip() for ln in clean_text.splitlines() if len(ln.strip()) >= 10), "")
    if first_line:
        return f"headline:{norm_vn(first_line)[:45]}"
    return f"post:{post.get('post_id') or post.get('post_url') or id(post)}"


def build_leads_list(posts: list[dict], existing_leads: list[dict] | None = None) -> list[dict]:
    """Aggregate matching posts into a structured list of Small Business Owners (Leads)."""
    by_key: dict[str, dict] = {}
    post_id_to_key: dict[str, str] = {}
    for lead in existing_leads or []:
        k = lead.get("lead_key")
        if k:
            by_key[k] = lead
            for sp in lead.get("source_posts") or []:
                pid = sp.get("post_id")
                if pid:
                    post_id_to_key[str(pid)] = k

    for post in posts:
        clean_content = clean_fb_noise(post.get("content") or "", author_name=post.get("author_name") or "")
        analysis = post.get("business_analysis") or analyze_business_and_needs(
            clean_content,
            author_name=post.get("author_name") or "",
            group_name=post.get("group_name") or "",
            group_id=post.get("group_id") or "",
        )
        contact = analysis.get("contact") or {}
        pid = str(post.get("post_id") or "")
        key = post_id_to_key.get(pid) or _lead_key(post)
        if pid:
            post_id_to_key[pid] = key

        if key not in by_key:
            by_key[key] = {
                "lead_key": key,
                "personal_info": {
                    "author_name": post.get("author_name") or "Chưa rõ tên FB",
                    "author_url": post.get("author_url") or "",
                    "group_member_url": post.get("group_member_url") or "",
                    "phone": contact.get("phone"),
                    "all_phones": contact.get("all_phones") or [],
                    "line_id": contact.get("line_id"),
                    "zalo_id": contact.get("zalo_id"),
                    "contact_methods": contact.get("contact_methods") or [],
                },
                "step1_business_model": {
                    "business_type": analysis.get("business_type"),
                    "owner_activity": analysis.get("owner_activity"),
                    "all_business_types": analysis.get("all_business_types") or [],
                    "shop_name": analysis.get("shop_name"),
                    "scale_estimate": analysis.get("scale_estimate"),
                    "location": analysis.get("location"),
                    "stations": analysis.get("stations") or [],
                    "areas": analysis.get("areas") or [],
                },
                "step2_deep_needs": {
                    "roles_needed": analysis.get("roles_needed") or [],
                    "shifts_needed": analysis.get("shifts_needed") or [],
                    "wage_offered": analysis.get("wage"),
                    "japanese_requirement": analysis.get("japanese_requirement"),
                    "shop_customer_segment": analysis.get("shop_customer_segment"),
                    "benefits_offered": analysis.get("benefits_offered") or [],
                    "pain_points_and_needs": analysis.get("pain_points_and_needs") or [],
                },
                "source_posts": [],
            }

        lead = by_key[key]
        p_info = lead["personal_info"]
        b_model = lead["step1_business_model"]
        d_needs = lead["step2_deep_needs"]

        if post.get("author_name") and p_info["author_name"] == "Chưa rõ tên FB":
            p_info["author_name"] = post["author_name"]
        if post.get("author_url") and not p_info["author_url"]:
            p_info["author_url"] = post["author_url"]
        if post.get("group_member_url") and not p_info["group_member_url"]:
            p_info["group_member_url"] = post["group_member_url"]
        if contact.get("phone") and not p_info["phone"]:
            p_info["phone"] = contact["phone"]
        for ph in contact.get("all_phones") or []:
            if ph not in p_info["all_phones"]:
                p_info["all_phones"].append(ph)
        if contact.get("line_id") and not p_info["line_id"]:
            p_info["line_id"] = contact["line_id"]
        if contact.get("zalo_id") and not p_info["zalo_id"]:
            p_info["zalo_id"] = contact["zalo_id"]
        for cm in contact.get("contact_methods") or []:
            if cm not in p_info["contact_methods"]:
                p_info["contact_methods"].append(cm)

        generic_types = {
            "Cơ sở kinh doanh nhỏ / Dịch vụ tại Nhật",
            "Quán ăn / Nhà hàng (F&B)",
            "Kinh doanh Online / Bán hàng cá nhân tại Nhật",
        }
        if analysis.get("business_type") and (
            not b_model.get("business_type") or b_model["business_type"] in generic_types
        ):
            b_model["business_type"] = analysis["business_type"]
        if analysis.get("shop_name") and not b_model["shop_name"]:
            b_model["shop_name"] = analysis["shop_name"]
        if analysis.get("location") and not b_model["location"]:
            b_model["location"] = analysis["location"]
        for st in analysis.get("stations") or []:
            if st not in b_model["stations"]:
                b_model["stations"].append(st)
        for ar in analysis.get("areas") or []:
            if ar not in b_model["areas"]:
                b_model["areas"].append(ar)
        for bt in analysis.get("all_business_types") or []:
            if bt not in b_model["all_business_types"]:
                b_model["all_business_types"].append(bt)

        if analysis.get("wage"):
            d_needs["wage_offered"] = analysis["wage"]
        for r in analysis.get("roles_needed") or []:
            if r not in d_needs["roles_needed"]:
                d_needs["roles_needed"].append(r)
        for sh in analysis.get("shifts_needed") or []:
            if sh not in d_needs["shifts_needed"]:
                d_needs["shifts_needed"].append(sh)
        for bf in analysis.get("benefits_offered") or []:
            if bf not in d_needs["benefits_offered"]:
                d_needs["benefits_offered"].append(bf)
        for pp in analysis.get("pain_points_and_needs") or []:
            if pp not in d_needs["pain_points_and_needs"]:
                d_needs["pain_points_and_needs"].append(pp)

        post_url = post.get("post_url") or ""
        existing_urls = {sp.get("post_url") for sp in lead["source_posts"]}
        if post_url not in existing_urls:
            lead["source_posts"].append({
                "post_id": post.get("post_id") or "",
                "post_url": post_url,
                "group_id": post.get("group_id") or "",
                "group_name": post.get("group_name") or "",
                "created_at": post.get("created_at") or "",
                "content_snippet": clean_content[:300],
            })
        else:
            for sp in lead["source_posts"]:
                if sp.get("post_url") == post_url:
                    sp["content_snippet"] = clean_content[:300]

    return list(by_key.values())


SOCIAL_WEB_DOMAINS = (
    "facebook.com",
    "fb.com",
    "m.facebook.com",
    "instagram.com",
    "tiktok.com",
    "line.me",
    "lin.ee",
    "hotpepper.jp",
    "tabelog.com",
    "gnavi.co.jp",
    "retty.me",
    "epark.jp",
    "minimo.jp",
    "minimodel.jp",
    "linktr.ee",
    "lit.link",
    "youtube.com",
    "youtu.be",
    "threads.net",
    "x.com",
    "twitter.com",
    "ameblo.jp",
    "note.com",
)


def classify_website_url(url: str | None) -> str:
    """Classify a Google Maps website link into NO_WEB, SOCIAL_ONLY, or HAS_WEB."""
    raw = (url or "").strip()
    if not raw:
        return "NO_WEB"
    try:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        host = (parsed.netloc or "").lower().lstrip("www.")
    except Exception:
        host = raw.lower()
    if not host:
        return "NO_WEB"
    if any(host == d or host.endswith(f".{d}") for d in SOCIAL_WEB_DOMAINS):
        return "SOCIAL_ONLY"
    return "HAS_WEB"


def extract_address_and_social_clues(text: str) -> dict:
    """Extract Japanese address snippets, postal codes, and external links from post text."""
    s = text or ""
    postal_codes = re.findall(r"〒?\s*\d{3}-\d{4}", s)
    # Match lines containing Japanese prefectures/wards or Vietnamese address cues
    address_lines: list[str] = []
    for ln in s.splitlines():
        clean_ln = ln.strip()
        if not clean_ln or len(clean_ln) > 140:
            continue
        if re.search(
            r"(?:〒\s*\d{3}-\d{4}|(?:東京|大阪|名古屋|愛知|埼玉|千葉|神奈川|福岡|兵庫|京都)[都府県]|\d+-\d+-\d+|địa chỉ|số nhà|tòa\s|lầu\s|tầng\s|cổng\s|lối ra\s)",
            clean_ln,
            re.I,
        ):
            if clean_ln not in address_lines:
                address_lines.append(clean_ln)
                if len(address_lines) >= 4:
                    break

    urls = re.findall(r"https?://[^\s<>\"')]+", s)
    social_links: list[str] = []
    external_webs: list[str] = []
    for u in urls:
        u_clean = u.rstrip(".,;:!)")
        if "facebook.com/groups/" in u_clean:
            continue
        kind = classify_website_url(u_clean)
        if kind == "SOCIAL_ONLY":
            if u_clean not in social_links:
                social_links.append(u_clean)
        elif kind == "HAS_WEB":
            if u_clean not in external_webs:
                external_webs.append(u_clean)

    return {
        "postal_codes": list(dict.fromkeys(p.strip() for p in postal_codes)),
        "address_clues": address_lines,
        "social_links": social_links[:5],
        "external_websites": external_webs[:3],
    }


def analyze_lead_behavior(source_posts: list[dict], lead: dict | None = None) -> dict:
    """Analyze posting patterns, golden hours, shop/address clues across all posts of one customer."""
    posts = source_posts or []
    lead = lead or {}
    b_model = lead.get("step1_business_model") or {}
    p_info = lead.get("personal_info") or {}

    hour_buckets = {
        "Sáng (6h-11h)": 0,
        "Trưa/Chiều (11h-17h)": 0,
        "Tối (17h-21h)": 0,
        "Đêm khuya (21h-5h)": 0,
    }
    style_counts = {
        "Tuyển nhân sự / Baito": 0,
        "Bán hàng / Khuyến mãi": 0,
        "Thanh lý / Sang nhượng": 0,
        "Hỏi đáp / Tìm nguồn": 0,
    }

    shop_names: list[str] = []
    if b_model.get("shop_name"):
        shop_names.append(str(b_model["shop_name"]))
    stations: list[str] = list(b_model.get("stations") or [])
    areas: list[str] = list(b_model.get("areas") or [])
    phones: list[str] = list(p_info.get("all_phones") or ([p_info["phone"]] if p_info.get("phone") else []))
    address_clues: list[str] = []
    social_links: list[str] = []
    external_webs: list[str] = []
    dates: list[str] = []

    for sp in posts:
        text = sp.get("content") or sp.get("content_snippet") or ""
        norm_t = norm_vn(text)
        created = (sp.get("created_at") or "").strip()
        if created:
            dates.append(created[:10])
            m_hr = re.search(r"[T\s](\d{1,2}):\d{2}", created)
            if m_hr:
                hr = int(m_hr.group(1))
                if 6 <= hr < 11:
                    hour_buckets["Sáng (6h-11h)"] += 1
                elif 11 <= hr < 17:
                    hour_buckets["Trưa/Chiều (11h-17h)"] += 1
                elif 17 <= hr < 21:
                    hour_buckets["Tối (17h-21h)"] += 1
                else:
                    hour_buckets["Đêm khuya (21h-5h)"] += 1

        if any(k in norm_t for k in ("tuyen", "can tim nhan vien", "tho nail", "phu bep", "chay ban", "baito", "luong")):
            style_counts["Tuyển nhân sự / Baito"] += 1
        if any(k in norm_t for k in ("khuyen mai", "giam gia", "uu dai", "khai truong", "order", "dat lich", "menu", "chuyen ship", "ban")):
            style_counts["Bán hàng / Khuyến mãi"] += 1
        if any(k in norm_t for k in ("thanh ly", "sang nhuong", "sang quan", "chuyen nhuong")):
            style_counts["Thanh lý / Sang nhượng"] += 1
        if any(k in norm_t for k in ("hoi kinh nghiem", "xin review", "co ai biet", "tim nguon", "nha cung cap")):
            style_counts["Hỏi đáp / Tìm nguồn"] += 1

        analysis = analyze_business_and_needs(text, author_name=p_info.get("author_name") or "")
        contact_info = analysis.get("contact") or {}
        for ph in contact_info.get("all_phones") or ([contact_info["phone"]] if contact_info.get("phone") else []):
            if ph and ph not in phones:
                phones.append(ph)
        if analysis.get("shop_name") and analysis["shop_name"] not in shop_names:
            shop_names.append(analysis["shop_name"])
        for st in analysis.get("stations") or []:
            if st not in stations:
                stations.append(st)
        for ar in analysis.get("areas") or []:
            if ar not in areas:
                areas.append(ar)

        clues = extract_address_and_social_clues(text)
        for addr in clues["address_clues"]:
            if addr not in address_clues:
                address_clues.append(addr)
        for sl in clues["social_links"]:
            if sl not in social_links:
                social_links.append(sl)
        for ew in clues["external_websites"]:
            if ew not in external_webs:
                external_webs.append(ew)

    dominant_hours = [k for k, v in sorted(hour_buckets.items(), key=lambda kv: kv[1], reverse=True) if v > 0]
    dominant_styles = [f"{k} ({v} bài)" for k, v in sorted(style_counts.items(), key=lambda kv: kv[1], reverse=True) if v > 0]

    total = len(posts)
    if total >= 5:
        pace_label = f"Đăng rất tích cực ({total} bài)"
    elif total >= 2:
        pace_label = f"Hoạt động thường xuyên ({total} bài)"
    else:
        pace_label = "Mới ghi nhận 1 bài"

    valid_dates = sorted(d for d in dates if len(d) >= 10)
    date_span = f"{valid_dates[0]} → {valid_dates[-1]}" if len(valid_dates) >= 2 else (valid_dates[0] if valid_dates else "")

    return {
        "total_posts": total,
        "pace_label": pace_label,
        "date_span": date_span,
        "golden_hour": dominant_hours[0] if dominant_hours else "Chưa đủ dữ liệu giờ đăng",
        "styles": dominant_styles,
        "primary_style": dominant_styles[0] if dominant_styles else "Chưa phân loại rõ",
        "shop_names": shop_names[:3],
        "phones": phones[:4],
        "stations": stations[:5],
        "areas": areas[:5],
        "address_clues": address_clues[:4],
        "social_links": social_links[:5],
        "external_websites": external_webs[:3],
    }


def score_lead_web_priority(
    lead: dict,
    behavior: dict | None = None,
    maps_info: dict | None = None,
) -> dict:
    """Compute a 1-10 priority score indicating how much this shop needs a Website / Landing Page."""
    p_info = lead.get("personal_info") or {}
    b_model = lead.get("step1_business_model") or {}
    d_needs = lead.get("step2_deep_needs") or {}
    behavior = behavior or analyze_lead_behavior(lead.get("source_posts") or [], lead)
    maps = maps_info if isinstance(maps_info, dict) else (lead.get("maps_info") or {})

    reasons: list[str] = []
    score = 0.0

    # 1. Contactability (max 3 pts)
    has_direct_contact = bool(
        p_info.get("phone")
        or p_info.get("all_phones")
        or p_info.get("line_id")
        or p_info.get("zalo_id")
        or behavior.get("phones")
    )
    if has_direct_contact:
        score += 2.0
        reasons.append("Có SĐT / Line / Zalo liên hệ ngay (+2đ)")
    if p_info.get("author_url"):
        score += 1.0
        reasons.append("Có Facebook cá nhân nhắn tin trực tiếp (+1đ)")

    # 2. Industry Fit & Activity (max 2 pts)
    btype = norm_vn(b_model.get("business_type") or "")
    high_fit_sector = any(
        k in btype
        for k in ("nail", "spa", "toc", "nha hang", "quan an", "izakaya", "cafe", "thuc pham", "shop", "kinh doanh online", "do cu", "vien thong", "ve may bay", "bat dong san")
    )
    if high_fit_sector:
        score += 1.0
        reasons.append(f"Ngành '{b_model.get('business_type') or 'KD nhỏ'}' chuyển đổi Web/Landing Page cao (+1đ)")

    is_active_or_expanding = (
        behavior.get("total_posts", 0) >= 2
        or bool(d_needs.get("roles_needed"))
        or bool(behavior.get("shop_names") or b_model.get("shop_name"))
    )
    if is_active_or_expanding:
        score += 1.0
        reasons.append("Đang hoạt động tích cực / tuyển nhân sự (+1đ)")

    # 3. Google Maps & Website Gap (max 5 pts)
    maps_status = (maps.get("status") or "UNCHECKED").upper() if maps else "UNCHECKED"
    web_status = (maps.get("website_status") or "UNCHECKED").upper() if maps else "UNCHECKED"
    is_preliminary = maps_status == "UNCHECKED"

    if not is_preliminary:
        if maps_status == "FOUND":
            reviews = int(maps.get("review_count") or 0)
            rating = maps.get("rating")
            if reviews >= 10:
                score += 2.0
                reasons.append(f"Quán sống khỏe trên GG Maps ({rating or '—'}⭐ · {reviews} đánh giá) (+2đ)")
            else:
                score += 1.0
                reasons.append(f"Đã xác minh mặt bằng thật trên GG Maps (+1đ)")

            if web_status == "NO_WEB":
                score += 3.0
                reasons.append("🎯 Google Maps CHƯA gắn Website/Landing Page (+3đ)")
            elif web_status == "SOCIAL_ONLY":
                bonus = 3.0 if high_fit_sector else 2.0
                score += bonus
                reasons.append(f"📱 Maps chỉ gắn link MXH/Nền tảng khác, chưa có Web riêng (+{int(bonus)}đ)")
            elif web_status == "HAS_WEB_WEAK":
                score += 2.0
                reasons.append("⚠️ Website hiện tại lỗi / thiếu HTTPS hoặc chưa chuẩn Mobile (+2đ)")
            elif web_status == "HAS_WEB":
                score += 0.5
                reasons.append("🌐 Đã có Website riêng trên Maps (chỉ chào Landing Page phụ) (+0.5đ)")
        elif maps_status == "NOT_FOUND":
            score += 2.0
            reasons.append("📍 Chưa thấy quán trên GG Maps (Home salon/Mới mở — cần combo Maps + Web) (+2đ)")
        else:
            # CHECK_ERROR or SKIPPED
            is_preliminary = True
            reasons.append("⏳ Chưa tra được Google Maps (bạn có thể thử lại)")
    else:
        # Preliminary estimate before Google Maps check (capped at 7 so verified 8-10 stand out)
        if behavior.get("shop_names") or b_model.get("shop_name"):
            score += 1.0
            reasons.append("Đã nhận diện được tên quán/thương hiệu (+1đ sơ bộ)")
        if behavior.get("stations") or b_model.get("location"):
            score += 1.0
            reasons.append("Đã rõ khu vực/ga tàu tại Nhật (+1đ sơ bộ)")
        if behavior.get("external_websites"):
            score += 0.0
            reasons.append("Đã thấy dẫn link web ngoài trong bài FB")
        else:
            score += 0.5
        score = min(score, 7.0)
        reasons.append("⏳ Điểm sơ bộ (Hãy bấm 'Check GG Maps' để chốt điểm 1–10 chính thức)")

    final_score = max(1, min(10, int(round(score))))
    if final_score >= 9:
        tier, tier_label = "S", "🔥 Chốt ngay (9–10đ)"
    elif final_score >= 7:
        tier, tier_label = "A", "⭐ Tiềm năng cao (7–8đ)"
    elif final_score >= 5:
        tier, tier_label = "B", "📌 Trung bình (5–6đ)"
    else:
        tier, tier_label = "C", "⚪ Thấp (1–4đ)"

    # Build personalized Web / Landing Page pitch
    author_name = p_info.get("author_name") or ""
    greeting_name = f" {author_name}" if author_name and "chưa rõ" not in author_name.lower() else ""
    shop_ref = (maps.get("place_name") if maps_status == "FOUND" else "") or (behavior.get("shop_names") or [None])[0] or (b_model.get("shop_name") or "")
    area_ref = (behavior.get("stations") or [None])[0] or (behavior.get("areas") or [None])[0] or (b_model.get("location") or "")

    shop_phrase = f"tiệm/quán {shop_ref}" if shop_ref else "cửa hàng mình"
    area_phrase = f" ở khu vực {area_ref}" if area_ref else ""

    if maps_status == "FOUND" and web_status == "NO_WEB":
        web_opener = (
            f"Chào anh/chị{greeting_name}, em thấy {shop_phrase}{area_phrase} trên Google Maps đang có đánh giá rất tốt"
            f"{' (' + str(maps.get('rating')) + '⭐)' if maps.get('rating') else ''}, "
            "nhưng mục Website trên Maps hiện đang để trống. Nhiều khách tìm quán trên Google muốn xem nhanh bảng giá / menu hoặc đặt lịch ngay mà chưa thấy link. "
            "Bên em chuyên làm Landing Page đặt lịch & bảng giá tối ưu cho tiệm người Việt tại Nhật (chỉ 1–2 ngày xong). Anh/chị có muốn xem thử vài mẫu bên em đã làm không ạ?"
        )
    elif maps_status == "FOUND" and web_status == "SOCIAL_ONLY":
        web_opener = (
            f"Chào anh/chị{greeting_name}, em thấy {shop_phrase}{area_phrase} trên Google Maps hiện đang dẫn về link mạng xã hội/nền tảng ngoài. "
            "Nếu gắn thêm 1 trang Landing Page riêng (có bảng giá, ảnh nổi bật và nút đặt lịch/Line 1 chạm), khách tìm từ Google Maps sẽ chốt lịch nhanh hơn hẳn và đỡ mất phí trung gian. "
            "Em gửi anh/chị xem thử mẫu giao diện phù hợp ngành mình nhé?"
        )
    elif maps_status == "NOT_FOUND":
        web_opener = (
            f"Chào anh/chị{greeting_name}, em thấy bài đăng của {shop_phrase}{area_phrase} trong hội nhóm tại Nhật. "
            "Hiện bên em đang hỗ trợ trọn gói đưa quán lên Google Maps chuẩn SEO + Landing Page bảng giá/đặt lịch giúp khách quanh ga tự tìm đến đều đặn hàng tuần. "
            "Anh/chị có tiện trao đổi 5 phút để em tư vấn thử không ạ?"
        )
    else:
        web_opener = (
            f"Chào anh/chị{greeting_name}, em thấy {shop_phrase}{area_phrase} hoạt động rất đều trong các hội nhóm tại Nhật"
            f"{' (nhất là khung ' + behavior['golden_hour'].lower() + ')' if behavior.get('golden_hour') and 'Chưa đủ' not in behavior['golden_hour'] else ''}. "
            "Bên em chuyên làm Landing Page giới thiệu quán, bảng giá/menu và nút đặt lịch tự động qua Line cho chủ kinh doanh Việt tại Nhật. "
            "Anh/chị có quan tâm tham khảo vài mẫu thực tế bên em không ạ?"
        )

    return {
        "score": final_score,
        "tier": tier,
        "tier_label": tier_label,
        "is_preliminary": is_preliminary,
        "reasons": reasons,
        "web_opener": web_opener,
        "maps_status": maps_status,
        "website_status": web_status,
    }


def save_leads_csv(path: Path, leads: list[dict]) -> None:
    """Export the aggregated leads list to an Excel-friendly UTF-8-SIG CSV file."""
    headers = [
        "Tên FB Chủ Quán / Cá Nhân KD",
        "Link FB Cá Nhân",
        "Link Hồ Sơ Trong Nhóm",
        "Số Điện Thoại",
        "Line / Zalo / Liên Hệ",
        "Đang Kinh Doanh Gì (Ngành Nghề)",
        "Tên Quán / Cửa Hàng",
        "Quy Mô Ước Tính",
        "Ga Tàu / Khu Vực (Nhật Bản)",
        "Vị Trí Đang Tuyển / Hình Thức VH",
        "Ca Làm Việc Thiếu",
        "Mức Lương / Giá Sản Phẩm",
        "Tệp Khách Hàng Mục Tiêu",
        "Đi Sâu: Họ Đang Cần Gì / Nỗi Đau Vận Hành",
        "Chế Độ / Dịch Vụ Kèm Theo",
        "Link Bài Đăng Gốc",
        "Nội Dung Bài Đăng (Đã Làm Sạch)",
        "Mô tả khách hàng (tùy chỉnh)",
        "Trạng thái chăm sóc",
        "Ghi chú chăm sóc",
        "Điểm Web (1-10)",
        "Trạng thái Web (Google Maps)",
        "Website trên Maps",
        "Link Google Maps",
        "Thói quen đăng bài",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for lead in leads:
            p = lead.get("personal_info") or {}
            b = lead.get("step1_business_model") or {}
            n = lead.get("step2_deep_needs") or {}
            posts = lead.get("source_posts") or []
            first_post = posts[0] if posts else {}
            beh = lead.get("behavior_analysis") or analyze_lead_behavior(posts, lead)
            maps = lead.get("maps_info") or {}
            wp = lead.get("web_priority") or score_lead_web_priority(lead, beh, maps)
            writer.writerow([
                p.get("author_name") or "",
                p.get("author_url") or "",
                p.get("group_member_url") or "",
                ", ".join(p.get("all_phones") or []) or (p.get("phone") or ""),
                ", ".join(p.get("contact_methods") or []),
                b.get("business_type") or "",
                b.get("shop_name") or "",
                b.get("scale_estimate") or "",
                b.get("location") or ", ".join((b.get("stations") or []) + (b.get("areas") or [])),
                ", ".join(n.get("roles_needed") or []),
                ", ".join(n.get("shifts_needed") or []),
                n.get("wage_offered") or "",
                n.get("shop_customer_segment") or "",
                " | ".join(n.get("pain_points_and_needs") or []),
                ", ".join(n.get("benefits_offered") or []),
                ", ".join(sp.get("post_url", "") for sp in posts if sp.get("post_url")),
                (first_post.get("content_snippet") or "").replace("\n", " "),
                (lead.get("crm") or {}).get("description") or "",
                (lead.get("crm") or {}).get("status") or "new",
                (lead.get("crm") or {}).get("note") or "",
                f"{wp.get('score', 0)}/10 ({'Sơ bộ' if wp.get('is_preliminary') else 'Chính thức'})",
                maps.get("website_status") or "UNCHECKED",
                maps.get("website") or "",
                maps.get("maps_url") or "",
                f"{beh.get('pace_label', '')} | {beh.get('primary_style', '')}",
            ])
