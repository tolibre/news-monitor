#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
news_monitor.py  (v2 — 네이버 API 주 수집원 / 무과금 / DB 기반 다이제스트)

모드:
  python news_monitor.py check   : 실시간 수집 (5분 간격 스케줄 권장)
  python news_monitor.py digest  : 다이제스트 출력 (08:30 / 13:30 스케줄)

수집원:
  1) 네이버 뉴스 검색 API (주) — 무료, 일 25,000회 한도, 최신순 페이지네이션
  2) 구글 뉴스 RSS (보조) — 무료, 키 불필요

핵심 설계:
  - check 때 수집한 모든 기사를 SQLite에 적재
  - digest는 재검색하지 않고 DB에서 시간 구간 조회 → 실시간에 잡힌 기사는 절대 누락 없음
"""

import sys, os, re, html, sqlite3, hashlib, datetime, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
import json

# ==================== 설정 ====================
KEYWORDS = [
    "과학기술정보통신부", "과기정통부", "과기부", "우주항공청",
    "공정거래위원회", "공정위", "방송미디어통신위원회", "방미통위",
]

# 같은 기관을 가리키는 키워드를 하나의 다이제스트 섹션으로 통합.
# (키워드 → 대표 그룹명). 여기 없는 키워드는 그 자체가 그룹명이 됨.
KEYWORD_GROUPS = {
    "과학기술정보통신부": "과기정통부", "과기정통부": "과기정통부", "과기부": "과기정통부",
    "공정거래위원회": "공정위", "공정위": "공정위",
    "방송미디어통신위원회": "방미통위", "방미통위": "방미통위",
    "우주항공청": "우주항공청",
}
# 다이제스트 섹션 표시 순서 (그룹명 기준, 중복 제거)
GROUP_ORDER = []
for _k in KEYWORDS:
    _g = KEYWORD_GROUPS.get(_k, _k)
    if _g not in GROUP_ORDER:
        GROUP_ORDER.append(_g)

def display_groups(kws):
    """매칭된 키워드 집합(kws)을 표시용 그룹명 리스트로 축약.
    예: {'과학기술정보통신부','과기정통부'} → ['과기정통부']"""
    result = []
    for k in KEYWORDS:  # KEYWORDS 순서를 유지해 표시 순서 고정
        if k in kws:
            g = KEYWORD_GROUPS.get(k, k)
            if g not in result:
                result.append(g)
    return result

# ==================== 매체 화이트리스트 ====================
# 알림/다이제스트에 포함할 매체. 도메인(네이버 원문링크용) + 매체명(구글 RSS 제목 꼬리표용) 두 벌.
MEDIA_DOMAINS = {
    # 통신사
    "yna.co.kr", "newsis.com", "news1.kr",
    # 종합일간지
    "chosun.com", "joongang.co.kr", "donga.com", "hani.co.kr", "khan.co.kr",
    "hankookilbo.com", "seoul.co.kr", "kmib.co.kr", "segye.com", "munhwa.com",
    # 경제지
    "mk.co.kr", "hankyung.com", "sedaily.com", "mt.co.kr", "edaily.co.kr",
    "asiae.co.kr", "heraldcorp.com", "fnnews.com",
    # 방송·보도채널
    "kbs.co.kr", "imbc.com", "sbs.co.kr", "ytn.co.kr", "yonhapnewstv.co.kr",
    "jtbc.co.kr", "tvchosun.com", "ichannela.com", "mbn.co.kr",
    # CBS
    "nocutnews.co.kr",
    # IT·과학 전문지
    "etnews.com", "ddaily.co.kr", "zdnet.co.kr", "dt.co.kr", "inews24.com",
    "bloter.net", "it.chosun.com", "techm.kr", "dongascience.com",
    # 인터넷·기타
    "ohmynews.com", "pressian.com", "mediatoday.co.kr", "tf.co.kr", "sisain.co.kr",
    # 정책·세종 커버 보강
    "biz.chosun.com", "newspim.com", "etoday.co.kr", "ajunews.com",
    "dailian.co.kr", "kukinews.com", "asiatoday.co.kr",
    # 과학·연구 전문
    "hellodd.com", "sciencetimes.co.kr",
    # 주요 지역지
    "daejonilbo.com", "knnews.co.kr", "busan.com", "imaeil.com",
}
MEDIA_NAMES = {
    "연합뉴스", "뉴시스", "뉴스1",
    "조선일보", "중앙일보", "동아일보", "한겨레", "경향신문", "한국일보",
    "서울신문", "국민일보", "세계일보", "문화일보",
    "매일경제", "한국경제", "서울경제", "머니투데이", "이데일리",
    "아시아경제", "헤럴드경제", "파이낸셜뉴스",
    "KBS", "KBS 뉴스", "MBC", "MBC 뉴스", "SBS", "SBS 뉴스", "YTN",
    "연합뉴스TV", "JTBC", "TV조선", "채널A", "MBN",
    "노컷뉴스", "CBS노컷뉴스",
    "전자신문", "디지털데일리", "지디넷코리아", "ZDNet Korea", "디지털타임스",
    "아이뉴스24", "블로터", "IT조선", "테크M", "동아사이언스",
    "오마이뉴스", "프레시안", "미디어오늘", "더팩트", "시사IN",
    "조선비즈", "뉴스핌", "이투데이", "아주경제", "데일리안", "쿠키뉴스", "아시아투데이",
    "헬로디디", "HelloDD", "사이언스타임즈",
    "대전일보", "경남신문", "부산일보", "매일신문",
}

# 화이트리스트 밖 기사도 DB에는 저장할지 (True 권장: 나중에 매체 추가 시 과거 기사 확인 가능)
STORE_NON_WHITELIST = True

# ==================== 매체 표시명 매핑 ====================
# 도메인 → 한글 매체명. 알림/다이제스트에서 도메인 대신 매체명으로 표시할 때 사용.
DOMAIN_TO_NAME = {
    "yna.co.kr": "연합뉴스", "newsis.com": "뉴시스", "news1.kr": "뉴스1",
    "chosun.com": "조선일보", "joongang.co.kr": "중앙일보", "donga.com": "동아일보",
    "hani.co.kr": "한겨레", "khan.co.kr": "경향신문", "hankookilbo.com": "한국일보",
    "seoul.co.kr": "서울신문", "kmib.co.kr": "국민일보", "segye.com": "세계일보",
    "munhwa.com": "문화일보",
    "mk.co.kr": "매일경제", "hankyung.com": "한국경제", "sedaily.com": "서울경제",
    "mt.co.kr": "머니투데이", "edaily.co.kr": "이데일리", "asiae.co.kr": "아시아경제",
    "heraldcorp.com": "헤럴드경제", "fnnews.com": "파이낸셜뉴스",
    "kbs.co.kr": "KBS", "imbc.com": "MBC", "sbs.co.kr": "SBS", "ytn.co.kr": "YTN",
    "yonhapnewstv.co.kr": "연합뉴스TV", "jtbc.co.kr": "JTBC", "tvchosun.com": "TV조선",
    "ichannela.com": "채널A", "mbn.co.kr": "MBN",
    "nocutnews.co.kr": "노컷뉴스",
    "etnews.com": "전자신문", "ddaily.co.kr": "디지털데일리", "zdnet.co.kr": "지디넷코리아",
    "dt.co.kr": "디지털타임스", "inews24.com": "아이뉴스24", "bloter.net": "블로터",
    "it.chosun.com": "IT조선", "techm.kr": "테크M", "dongascience.com": "동아사이언스",
    "ohmynews.com": "오마이뉴스", "pressian.com": "프레시안", "mediatoday.co.kr": "미디어오늘",
    "tf.co.kr": "더팩트", "sisain.co.kr": "시사IN",
    "biz.chosun.com": "조선비즈", "newspim.com": "뉴스핌", "etoday.co.kr": "이투데이",
    "ajunews.com": "아주경제", "dailian.co.kr": "데일리안", "kukinews.com": "쿠키뉴스",
    "asiatoday.co.kr": "아시아투데이",
    "hellodd.com": "헬로디디", "sciencetimes.co.kr": "사이언스타임즈",
    "daejonilbo.com": "대전일보", "knnews.co.kr": "경남신문", "busan.com": "부산일보",
    "imaeil.com": "매일신문",
}

def media_name(source):
    """source(도메인 또는 매체명)를 표시용 한글 매체명으로 변환.
    biz.chosun.com처럼 구체적 서브도메인 매핑을 상위 도메인보다 우선 적용."""
    if not source:
        return source
    s = source.strip().lower()
    # 가장 긴(구체적인) 도메인 매칭 우선
    best = None
    for d, name in DOMAIN_TO_NAME.items():
        if s == d or s.endswith("." + d):
            if best is None or len(d) > len(best[0]):
                best = (d, name)
    if best:
        return best[1]
    return source.strip()  # 이미 매체명이거나 미등록 도메인이면 그대로

# ==================== check 선별용 핵심 매체 ====================
# check(실시간)는 선별이 목적: 아래 핵심 매체이거나, [단독]/[속보]이거나,
# 2곳 이상이 보도 중인 기사만 알림. digest는 화이트리스트 전체를 빠짐없이 포함.
CORE_MEDIA_DOMAINS = {
    # 통신사
    "yna.co.kr", "newsis.com", "news1.kr",
    # 종합일간지
    "chosun.com", "joongang.co.kr", "donga.com", "hani.co.kr", "khan.co.kr",
    "hankookilbo.com", "seoul.co.kr", "kmib.co.kr", "segye.com", "munhwa.com",
    # 방송·종편
    "kbs.co.kr", "imbc.com", "sbs.co.kr", "ytn.co.kr", "yonhapnewstv.co.kr",
    "jtbc.co.kr", "tvchosun.com", "ichannela.com", "mbn.co.kr",
    # 경제지
    "mk.co.kr", "hankyung.com", "sedaily.com", "mt.co.kr", "edaily.co.kr",
    "asiae.co.kr", "heraldcorp.com", "fnnews.com",
    # CBS
    "nocutnews.co.kr",
    # IT 핵심
    "etnews.com", "zdnet.co.kr", "ddaily.co.kr", "dongascience.com",
}
CORE_MEDIA_NAMES = {
    "연합뉴스", "뉴시스", "뉴스1",
    "조선일보", "중앙일보", "동아일보", "한겨레", "경향신문", "한국일보",
    "서울신문", "국민일보", "세계일보", "문화일보",
    "KBS", "KBS 뉴스", "MBC", "MBC 뉴스", "SBS", "SBS 뉴스", "YTN",
    "연합뉴스TV", "JTBC", "TV조선", "채널A", "MBN",
    "매일경제", "한국경제", "서울경제", "머니투데이", "이데일리",
    "아시아경제", "헤럴드경제", "파이낸셜뉴스",
    "노컷뉴스", "CBS노컷뉴스",
    "전자신문", "지디넷코리아", "ZDNet Korea", "디지털데일리", "동아사이언스",
}

def core_media(source):
    """check 알림 대상 핵심 매체인지 판별."""
    if not source:
        return False
    s = source.strip().lower()
    for d in CORE_MEDIA_DOMAINS:
        if s == d or s.endswith("." + d):
            return True
    src = source.strip()
    for n in CORE_MEDIA_NAMES:
        if src == n or src == n + " 뉴스":
            return True
    return False

def media_allowed(source):
    """source가 화이트리스트 매체인지 판별. source는 도메인(네이버) 또는 매체명(구글)."""
    if not source:
        return False
    s = source.strip().lower()
    # 도메인 매칭: news.kbs.co.kr 같은 서브도메인도 kbs.co.kr로 잡음
    for d in MEDIA_DOMAINS:
        if s == d or s.endswith("." + d):
            return True
    # 매체명 매칭 (구글 RSS)
    src = source.strip()
    for n in MEDIA_NAMES:
        if src == n or src == n + " 뉴스":
            return True
    return False

# 네이버 개발자센터(developers.naver.com) > 애플리케이션 등록 > '검색' API 선택
# 등록만 하면 무료. GitHub Actions에서는 Secrets로 주입됩니다.
NAVER_CLIENT_ID     = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")

# 텔레그램 (선택) — 비워두면 콘솔 출력
# check(실시간)용 봇
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
# digest(다이제스트)용 봇 — 비워두면 check용 봇으로 함께 전송
TELEGRAM_DIGEST_BOT_TOKEN = os.environ.get("TELEGRAM_DIGEST_BOT_TOKEN", "") or TELEGRAM_BOT_TOKEN
TELEGRAM_DIGEST_CHAT_ID   = os.environ.get("TELEGRAM_DIGEST_CHAT_ID", "") or TELEGRAM_CHAT_ID

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(BASE_DIR, "news_monitor.db")
DIGEST_DIR = os.path.join(BASE_DIR, "digests")
ALERT_DIR  = os.path.join(BASE_DIR, "alerts")

MAX_PAGES_PER_KEYWORD = 5      # 네이버: 키워드당 최대 5페이지(500건)까지 거슬러 수집
DAILY_CALL_SOFT_LIMIT = 20000  # 일일 호출 안전장치 (한도 25,000의 80%)
RETENTION_DAYS = 90            # DB 보관 기간(일). 이보다 오래된 기사는 자동 삭제. 0이면 삭제 안 함.

KST = datetime.timezone(datetime.timedelta(hours=9))

# ==================== DB ====================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS articles(
        id TEXT PRIMARY KEY,          -- 링크/제목 기반 해시 (중복 병합 키)
        title TEXT, link TEXT, source TEXT,
        pub_dt TEXT,                  -- 기사 발행시각 (ISO, KST)
        seen_dt TEXT,                 -- 수집 시각 (ISO, KST)
        keywords TEXT,                -- 매칭 키워드 (쉼표 구분)
        origin TEXT                   -- naver / google
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS api_calls(
        day TEXT PRIMARY KEY, cnt INTEGER
    )""")
    return conn

def call_budget_ok(conn):
    day = datetime.datetime.now(KST).strftime("%Y-%m-%d")
    row = conn.execute("SELECT cnt FROM api_calls WHERE day=?", (day,)).fetchone()
    return (row[0] if row else 0) < DAILY_CALL_SOFT_LIMIT

def count_call(conn):
    day = datetime.datetime.now(KST).strftime("%Y-%m-%d")
    conn.execute("""INSERT INTO api_calls(day,cnt) VALUES(?,1)
                    ON CONFLICT(day) DO UPDATE SET cnt=cnt+1""", (day,))

def prune_old(conn):
    """RETENTION_DAYS보다 오래된 기사 삭제 + 오래된 api_calls 정리 + VACUUM으로 파일 축소."""
    if RETENTION_DAYS <= 0:
        return 0
    now = datetime.datetime.now(KST)
    cutoff = (now - datetime.timedelta(days=RETENTION_DAYS)).isoformat()
    cur = conn.execute("DELETE FROM articles WHERE seen_dt < ?", (cutoff,))
    deleted = cur.rowcount
    day_cutoff = (now - datetime.timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
    conn.execute("DELETE FROM api_calls WHERE day < ?", (day_cutoff,))
    conn.commit()
    if deleted:
        conn.execute("VACUUM")  # 삭제 후 파일 실제 용량 회수
        conn.commit()
    return deleted

# ==================== 유틸 ====================
def clean(text):
    text = re.sub(r"<[^>]+>", "", text or "")
    return html.unescape(text).strip()

def norm_title(t):
    return re.sub(r"[\s\W]+", "", clean(t))[:60]

def group_key(title):
    """전재 기사 묶기용 제목 키. [단독][속보] 등 대괄호 태그와 ' - 매체명' 꼬리표 제거 후 정규화."""
    t = clean(title)
    t = re.sub(r"\[[^\]]*\]", "", t)          # [단독] [속보] [종합] 등 제거
    t = t.rsplit(" - ", 1)[0]                   # 구글 RSS의 ' - 매체명' 꼬리표 제거
    t = re.sub(r"\([^)]*\)$", "", t).strip()    # 끝의 (종합) (종합2보) 등 제거
    return re.sub(r"[\s\W]+", "", t)[:50]

def clean_title_display(title):
    """표시용 제목: 구글 RSS의 ' - 매체명' 꼬리표 제거 (매체명은 별도 표시하므로)."""
    t = clean(title)
    if " - " in t:
        head, tail = t.rsplit(" - ", 1)
        if len(tail) <= 15:  # 꼬리표가 매체명 길이면 제거
            t = head
    return t

def priority_mark(title):
    """[단독]/[속보] 기사에 강조 이모지 부여. 단독이 속보보다 우선.
    괄호로 감싼 태그 형태([단독],〈단독〉,【단독】,(단독) 등)만 인정 — '단독주택' 같은 오탐 방지."""
    t = title or ""
    if re.search(r"[\[〈<【(]\s*단독\s*[\]〉>】)]", t):
        return "🔥"
    if re.search(r"[\[〈<【(]\s*속보\s*[\]〉>】)]", t):
        return "⚡"
    return ""

PHOTO_TAGS = ["포토뉴스", "포토", "사진", "화보", "그래픽", "인포그래픽", "카드뉴스"]

def is_photo_article(title):
    """[포토]/[사진]/[화보]/[그래픽]/[카드뉴스] 등 사진 계열 태그가 붙은 기사인지 판별.
    [영상]은 리포트일 수 있어 제외 대상에서 뺌(살림)."""
    t = title or ""
    for tag in PHOTO_TAGS:
        if re.search(rf"[\[〈<【(]\s*{tag}\s*[\]〉>】)]", t):
            return True
    return False

def dedup_group(items):
    """items: [(title,link,source,pub_dt,...), ...] → 같은 기사 묶어서
    [(대표item, [모든 매체명]), ...] 반환. 대표는 화이트리스트 우선순위 높은 매체."""
    groups = {}
    order = []
    for it in items:
        k = group_key(it[0])
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(it)
    result = []
    for k in order:
        members = groups[k]
        # 대표 기사: pub_dt 가장 빠른 것(원 보도에 가까움) 우선
        rep = min(members, key=lambda x: x[3])
        sources = []
        for m in members:
            s = m[2]
            if s and s not in sources:
                sources.append(s)
        result.append((rep, sources))
    return result

def article_id(link, title):
    # 같은 기사가 키워드 여러 개에 걸려도 하나로 병합
    base = (link or "") if link else norm_title(title)
    # 네이버 원문링크가 있으면 그것 기준
    return hashlib.md5(base.encode("utf-8")).hexdigest()

def http_get(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def matched_keywords(title):
    return [k for k in KEYWORDS if k in title]

# ==================== 수집: 네이버 ====================
def fetch_naver(conn, keyword, known_ids):
    """최신순 페이지네이션. '이미 본 기사'가 페이지 전체를 채우면 중단."""
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET):
        return []
    new_items = []
    for page in range(MAX_PAGES_PER_KEYWORD):
        if not call_budget_ok(conn):
            print("[warn] 일일 호출 안전장치 도달 — 네이버 수집 중단")
            break
        start = page * 100 + 1
        url = ("https://openapi.naver.com/v1/search/news.json?query=" +
               urllib.parse.quote(keyword) + f"&display=100&start={start}&sort=date")
        try:
            raw = http_get(url, headers={
                "X-Naver-Client-Id": NAVER_CLIENT_ID,
                "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
            })
            count_call(conn)
            items = json.loads(raw).get("items", [])
        except Exception as e:
            print(f"[warn] naver '{keyword}' p{page+1}: {e}")
            break
        if not items:
            break
        page_all_known = True
        for it in items:
            link  = it.get("originallink") or it.get("link") or ""
            title = clean(it.get("title"))
            aid = article_id(link, title)
            if aid in known_ids:
                continue
            page_all_known = False
            known_ids.add(aid)
            try:
                pub = datetime.datetime.strptime(
                    it.get("pubDate", ""), "%a, %d %b %Y %H:%M:%S %z"
                ).astimezone(KST)
            except Exception:
                pub = datetime.datetime.now(KST)
            new_items.append(dict(id=aid, title=title, link=link,
                                  source=urllib.parse.urlparse(link).netloc,
                                  pub_dt=pub.isoformat(), origin="naver"))
        if page_all_known:
            break  # 이 페이지 전부 기수집분 → 더 거슬러 갈 필요 없음
    return new_items

# ==================== 수집: 구글 RSS (보조) ====================
def fetch_google(keyword, known_ids):
    url = ("https://news.google.com/rss/search?q=" +
           urllib.parse.quote(f'"{keyword}"') + "&hl=ko&gl=KR&ceid=KR:ko")
    out = []
    try:
        root = ET.fromstring(http_get(url))
    except Exception as e:
        print(f"[warn] google '{keyword}': {e}")
        return out
    for item in root.iter("item"):
        title = clean(item.findtext("title"))
        link  = item.findtext("link") or ""
        aid = article_id(link, title)
        if aid in known_ids:
            continue
        # 구글은 제목 뒤에 " - 매체명"이 붙음
        src = title.rsplit(" - ", 1)[1] if " - " in title else ""
        try:
            pub = datetime.datetime.strptime(
                item.findtext("pubDate", ""), "%a, %d %b %Y %H:%M:%S %Z"
            ).replace(tzinfo=datetime.timezone.utc).astimezone(KST)
        except Exception:
            pub = datetime.datetime.now(KST)
        known_ids.add(aid)
        out.append(dict(id=aid, title=title, link=link, source=src,
                        pub_dt=pub.isoformat(), origin="google"))
    return out

# ==================== 알림 ====================
TG_CHUNK = 3800  # 텔레그램 메시지당 최대 길이 (한도 4096보다 여유 있게)

def tg_escape(s):
    """텔레그램 HTML 모드에서 특수문자 이스케이프."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _send_telegram(text, token, chat_id):
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true"}).encode()
    urllib.request.urlopen(urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data), timeout=15)

def notify(text, target="check"):
    """긴 메시지는 줄(기사) 단위로 나눠 여러 개로 전송. (i/n) 표시 붙임.
    target='check'면 실시간 봇, 'digest'면 다이제스트 봇으로 전송."""
    token, chat_id = (TELEGRAM_DIGEST_BOT_TOKEN, TELEGRAM_DIGEST_CHAT_ID) \
        if target == "digest" else (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    if token and chat_id:
        try:
            if len(text) <= TG_CHUNK:
                _send_telegram(text, token, chat_id)
                return
            # 줄 단위로 청크 구성 (HTML 모드에서 각 기사가 한 줄이라 태그가 안 잘림)
            chunks, cur = [], ""
            for line in text.split("\n"):
                piece = (line if not cur else "\n" + line)
                if len(cur) + len(piece) > TG_CHUNK and cur:
                    chunks.append(cur)
                    cur = line
                else:
                    cur += piece
            if cur:
                chunks.append(cur)
            total = len(chunks)
            for i, chunk in enumerate(chunks, 1):
                _send_telegram(f"({i}/{total})\n{chunk}", token, chat_id)
            return
        except Exception as e:
            print(f"[warn] telegram: {e}")
    print(text)

# ==================== check 모드 ====================
def run_check():
    conn = db()
    now = datetime.datetime.now(KST)
    known = set(r[0] for r in conn.execute("SELECT id FROM articles"))
    collected = {}  # aid -> item(+keywords set)

    for kw in KEYWORDS:
        for it in fetch_naver(conn, kw, set(known) - set(collected)) + \
                  fetch_google(kw, set(known) - set(collected)):
            aid = it["id"]
            if aid in known and aid not in collected:
                continue
            if aid in collected:
                collected[aid]["kws"].add(kw)
                continue
            kws = set(matched_keywords(it["title"])) or {kw}
            it["kws"] = kws
            collected[aid] = it

    new_rows = []
    fresh_cutoff = (now - datetime.timedelta(hours=24)).isoformat()
    for aid, it in collected.items():
        if aid in known:
            continue
        allowed = media_allowed(it["source"])
        if allowed or STORE_NON_WHITELIST:
            conn.execute("INSERT OR IGNORE INTO articles VALUES(?,?,?,?,?,?,?,?)",
                         (aid, it["title"], it["link"], it["source"], it["pub_dt"],
                          now.isoformat(), ",".join(sorted(it["kws"])), it["origin"]))
        # 후보: 화이트리스트 매체 + 발행 24시간 이내 + 사진기사 아님
        if allowed and it["pub_dt"] >= fresh_cutoff and not is_photo_article(it["title"]):
            new_rows.append(it)
    conn.commit()

    # ===== check 선별: 핵심 매체 / 단독·속보 / 전재 확산(2곳 이상) 중 하나라도 해당해야 알림 =====
    skipped = []
    if new_rows:
        # 전재 확산 판정: 이번 배치 + 최근 24시간 DB에서 같은 제목 그룹의 매체 수 집계
        pickup_count = {}
        recent = conn.execute("SELECT title, source FROM articles WHERE seen_dt >= ?",
                              ((now - datetime.timedelta(hours=24)).isoformat(),)).fetchall()
        for title, source in recent:
            k = group_key(title)
            pickup_count.setdefault(k, set()).add(source)
        for it in new_rows:
            k = group_key(it["title"])
            pickup_count.setdefault(k, set()).add(it["source"])

        def selected(it):
            if core_media(it["source"]):
                return True
            if priority_mark(it["title"]):
                return True
            if len(pickup_count.get(group_key(it["title"]), ())) >= 2:
                return True
            return False

        skipped = [it for it in new_rows if not selected(it)]
        new_rows = [it for it in new_rows if selected(it)]

    if new_rows:
        new_rows.sort(key=lambda x: x["pub_dt"], reverse=True)
        # HTML 메시지 (제목이 클릭 가능한 링크) — 단독/속보는 맨 위로 (같은 등급 내에서는 최신순 유지)
        new_rows.sort(key=lambda x: {"🔥": 0, "⚡": 1}.get(priority_mark(x["title"]), 2))
        html_lines = [f"🆕 <b>주요 기사 {len(new_rows)}건</b> ({now.strftime('%m/%d %H:%M')})"]
        for it in new_rows[:40]:
            t = it["pub_dt"][11:16]
            kw = ",".join(display_groups(it["kws"]))
            mark = priority_mark(it["title"])
            mark_prefix = f"{mark} " if mark else ""
            html_lines.append(
                f'\n{mark_prefix}[{tg_escape(kw)}] <a href="{tg_escape(it["link"])}">{tg_escape(it["title"])}</a>'
                f'\n  {t} | {tg_escape(media_name(it["source"]))}')
        if len(new_rows) > 40:
            html_lines.append(f"\n… 외 {len(new_rows)-40}건 (다이제스트에서 전체 확인)")
        if skipped:
            html_lines.append(f"\n<i>선별 제외 {len(skipped)}건은 다이제스트에서 확인</i>")
        html_text = "\n".join(html_lines)

        # 파일 저장용 평문 (링크 원문 포함)
        plain_lines = [f"🆕 새 기사 {len(new_rows)}건 ({now.strftime('%m/%d %H:%M')})"]
        for it in new_rows[:40]:
            t = it["pub_dt"][11:16]
            plain_lines.append(f"\n[{','.join(display_groups(it['kws']))}] {it['title']}\n"
                               f"  {t} | {media_name(it['source'])}\n  {it['link']}")
        plain_text = "\n".join(plain_lines)

        quiet_hours = now.hour >= 23 or now.hour < 6
        if quiet_hours:
            print(f"[{now.strftime('%H:%M')}] 야간 시간대 — 텔레그램 알림 억제 (DB에는 저장됨, 06:00 다이제스트에서 확인 가능)")
        else:
            notify(html_text)

        os.makedirs(ALERT_DIR, exist_ok=True)
        fname = os.path.join(ALERT_DIR, f"{now.strftime('%Y-%m-%d')}.txt")
        with open(fname, "a", encoding="utf-8") as f:
            f.write(plain_text + "\n\n" + ("-" * 40) + "\n\n")
        print(f"저장: {fname}")
    else:
        if skipped:
            print(f"[{now.strftime('%H:%M')}] 주요 기사 없음 (선별 제외 {len(skipped)}건은 다이제스트로)")
        else:
            print(f"[{now.strftime('%H:%M')}] 새 기사 없음")
    conn.close()

# ==================== digest 모드 ====================
def run_digest():
    conn = db()
    now = datetime.datetime.now(KST)
    today = now.date()
    if now.hour < 7:     # 06:00 실행 → 야간 다이제스트: 전일 23:00 ~ 금일 06:00
        start = datetime.datetime.combine(today - datetime.timedelta(days=1),
                 datetime.time(23, 0), KST)
        end   = datetime.datetime.combine(today, datetime.time(6, 0), KST)
        label = "야간 다이제스트"
        # 하루 1회(야간 다이제스트 시점)만 오래된 기사 정리
        deleted = prune_old(conn)
        if deleted:
            print(f"[정리] {RETENTION_DAYS}일 초과 기사 {deleted}건 삭제")
    elif now.hour < 11:  # 08:30 실행 → 금일 06:00 ~ 08:30
        start = datetime.datetime.combine(today, datetime.time(6, 0), KST)
        end   = datetime.datetime.combine(today, datetime.time(8, 30), KST)
        label = "오전 다이제스트"
    else:                # 13:30 실행 → 금일 08:30 ~ 13:30
        start = datetime.datetime.combine(today, datetime.time(8, 30), KST)
        end   = datetime.datetime.combine(today, datetime.time(13, 30), KST)
        label = "오후 다이제스트"

    rows = conn.execute("""SELECT title,link,source,pub_dt,keywords FROM articles
                           WHERE seen_dt>=? AND seen_dt<? ORDER BY pub_dt""",
                        (start.isoformat(), end.isoformat())).fetchall()
    rows = [r for r in rows if media_allowed(r[2])]  # 화이트리스트 매체만
    fresh_cutoff = (now - datetime.timedelta(hours=24)).isoformat()
    rows = [r for r in rows if r[3] >= fresh_cutoff]  # 발행 24시간 이내만
    rows = [r for r in rows if not is_photo_article(r[0])]  # 사진기사 제외

    # 기관 그룹별 그룹핑 (공정거래위원회+공정위 → 하나의 섹션 등)
    by_group = {g: [] for g in GROUP_ORDER}
    for r in rows:
        kws = r[4].split(",") if r[4] else []
        # 대표 키워드: KEYWORDS 순서상 가장 앞선 것 → 그 그룹으로
        rep_kw = next((k for k in KEYWORDS if k in kws), (kws[0] if kws else KEYWORDS[0]))
        g = KEYWORD_GROUPS.get(rep_kw, rep_kw)
        by_group.setdefault(g, []).append(r)

    # HTML 메시지 (제목 = 클릭 가능한 링크)
    html_lines = [f"📋 <b>{tg_escape(label)}</b> | {start.strftime('%m/%d %H:%M')} ~ {end.strftime('%m/%d %H:%M')}",
                  "PLACEHOLDER_COUNT"]
    shown = 0
    seen_story = set()  # 섹션 간 같은 기사 중복 방지 (group_key 기준)
    for g in GROUP_ORDER:
        arts = by_group.get(g, [])
        if not arts:
            continue
        grouped = dedup_group(arts)
        # 앞 섹션에서 이미 나온 기사(같은 group_key)는 제외
        grouped = [(rep, srcs) for (rep, srcs) in grouped if group_key(rep[0]) not in seen_story]
        for rep, srcs in grouped:
            seen_story.add(group_key(rep[0]))
        if not grouped:
            continue
        grouped.sort(key=lambda gr: {"🔥": 0, "⚡": 1}.get(priority_mark(gr[0][0]), 2))
        shown += len(grouped)
        html_lines.append(f"\n■ <b>{tg_escape(g)}</b> ({len(grouped)}건)")
        for rep, sources in grouped:
            t, link, src, pub = clean_title_display(rep[0]), rep[1], media_name(rep[2]), rep[3]
            extra = f" 외 {len(sources)-1}곳" if len(sources) > 1 else ""
            mark = priority_mark(rep[0])
            mark_prefix = f"{mark} " if mark else ""
            html_lines.append(
                f'{mark_prefix}· [{pub[11:16]}] <a href="{tg_escape(link)}">{tg_escape(t)}</a> ({tg_escape(src)}{extra})')
    if len(rows) == 0:
        html_lines.append("\n(해당 구간 수집 기사 없음 — check 스케줄이 돌고 있었는지 확인하세요)")
    html_lines[1] = f"주요 이슈 {shown}건 (원문 {len(rows)}건)\n" + "=" * 30
    html_text = "\n".join(html_lines)

    # 파일 저장용 평문
    plain_lines = [f"📋 {label} | {start.strftime('%m/%d %H:%M')} ~ {end.strftime('%m/%d %H:%M')}",
                   "PLACEHOLDER_COUNT_P"]
    shown_p = 0
    seen_story_p = set()
    for g in GROUP_ORDER:
        arts = by_group.get(g, [])
        if not arts:
            continue
        grouped = dedup_group(arts)
        grouped = [(rep, srcs) for (rep, srcs) in grouped if group_key(rep[0]) not in seen_story_p]
        for rep, srcs in grouped:
            seen_story_p.add(group_key(rep[0]))
        if not grouped:
            continue
        grouped.sort(key=lambda gr: {"🔥": 0, "⚡": 1}.get(priority_mark(gr[0][0]), 2))
        shown_p += len(grouped)
        plain_lines.append(f"\n■ {g} ({len(grouped)}건)")
        for rep, sources in grouped:
            t, link, src, pub = clean_title_display(rep[0]), rep[1], media_name(rep[2]), rep[3]
            extra = f" 외 {len(sources)-1}곳" if len(sources) > 1 else ""
            mark = priority_mark(rep[0])
            mark_prefix = f"{mark} " if mark else ""
            plain_lines.append(f"  · {mark_prefix}[{pub[11:16]}] {t} ({src}{extra})\n    {link}")
    if len(rows) == 0:
        plain_lines.append("\n(해당 구간 수집 기사 없음)")
    plain_lines[1] = f"주요 이슈 {shown_p}건 (원문 {len(rows)}건)\n" + "=" * 40
    plain_text = "\n".join(plain_lines)

    os.makedirs(DIGEST_DIR, exist_ok=True)
    fname = os.path.join(DIGEST_DIR, f"digest_{now.strftime('%Y%m%d_%H%M')}.txt")
    with open(fname, "w", encoding="utf-8") as f:
        f.write(plain_text)

    # notify()가 4096자 초과 시 자동으로 여러 메시지로 나눠 전송함
    notify(html_text, target="digest")
    print(f"저장: {fname}")
    conn.close()

# ==================== main ====================
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode == "check":
        run_check()
    elif mode == "digest":
        run_digest()
    else:
        print("usage: python news_monitor.py [check|digest]")
