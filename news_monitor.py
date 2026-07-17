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
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(BASE_DIR, "news_monitor.db")
DIGEST_DIR = os.path.join(BASE_DIR, "digests")
ALERT_DIR  = os.path.join(BASE_DIR, "alerts")

MAX_PAGES_PER_KEYWORD = 5      # 네이버: 키워드당 최대 5페이지(500건)까지 거슬러 수집
DAILY_CALL_SOFT_LIMIT = 20000  # 일일 호출 안전장치 (한도 25,000의 80%)

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

# ==================== 유틸 ====================
def clean(text):
    text = re.sub(r"<[^>]+>", "", text or "")
    return html.unescape(text).strip()

def norm_title(t):
    return re.sub(r"[\s\W]+", "", clean(t))[:60]

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

def _send_telegram(text):
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID, "text": text,
        "disable_web_page_preview": "true"}).encode()
    urllib.request.urlopen(urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data=data), timeout=15)

def notify(text):
    """긴 메시지는 기사 단위로 나눠 여러 개로 전송. (i/n) 표시 붙임."""
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            if len(text) <= TG_CHUNK:
                _send_telegram(text)
                return
            # 줄들을 블록으로 묶기: 4칸 들여쓰기 줄(URL 등)은 앞 줄에 붙여서 한 덩어리로
            blocks, cur_block = [], ""
            for line in text.split("\n"):
                if line.startswith("    ") and cur_block:
                    cur_block += "\n" + line
                else:
                    if cur_block:
                        blocks.append(cur_block)
                    cur_block = line
            if cur_block:
                blocks.append(cur_block)
            # 블록 단위로 청크 구성
            chunks, cur = [], ""
            for block in blocks:
                piece = (block if not cur else "\n" + block)
                if len(cur) + len(piece) > TG_CHUNK and cur:
                    chunks.append(cur)
                    cur = block
                else:
                    cur += piece
            if cur:
                chunks.append(cur)
            total = len(chunks)
            for i, chunk in enumerate(chunks, 1):
                _send_telegram(f"({i}/{total})\n{chunk}")
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
        # 알림 대상: 화이트리스트 매체 + 발행 24시간 이내 기사만
        if allowed and it["pub_dt"] >= fresh_cutoff:
            new_rows.append(it)
    conn.commit()

    if new_rows:
        new_rows.sort(key=lambda x: x["pub_dt"], reverse=True)
        lines = [f"🆕 새 기사 {len(new_rows)}건 ({now.strftime('%m/%d %H:%M')})"]
        for it in new_rows[:30]:
            t = it["pub_dt"][11:16]
            lines.append(f"\n[{','.join(sorted(it['kws']))}] {it['title']}\n"
                         f"  {t} | {it['source']}\n  {it['link']}")
        if len(new_rows) > 30:
            lines.append(f"\n… 외 {len(new_rows)-30}건 (다이제스트에서 전체 확인)")
        text = "\n".join(lines)

        quiet_hours = now.hour >= 23 or now.hour < 6
        if quiet_hours:
            print(f"[{now.strftime('%H:%M')}] 야간 시간대 — 텔레그램 알림 억제 (DB에는 저장됨, 06:00 다이제스트에서 확인 가능)")
        else:
            notify(text)

        os.makedirs(ALERT_DIR, exist_ok=True)
        fname = os.path.join(ALERT_DIR, f"{now.strftime('%Y-%m-%d')}.txt")
        with open(fname, "a", encoding="utf-8") as f:
            f.write(text + "\n\n" + ("-" * 40) + "\n\n")
        print(f"저장: {fname}")
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

    # 키워드별 그룹핑 (한 기사가 여러 키워드면 각 섹션에 표기하되 대표 섹션 1회)
    by_kw = {k: [] for k in KEYWORDS}
    for r in rows:
        first_kw = r[4].split(",")[0] if r[4] else KEYWORDS[0]
        # 대표 키워드: KEYWORDS 순서상 가장 앞선 것
        kws = r[4].split(",") if r[4] else []
        rep = next((k for k in KEYWORDS if k in kws), first_kw)
        by_kw.setdefault(rep, []).append(r)

    lines = [f"📋 {label} | {start.strftime('%m/%d %H:%M')} ~ {end.strftime('%m/%d %H:%M')}",
             f"총 {len(rows)}건\n" + "=" * 40]
    for k in KEYWORDS:
        arts = by_kw.get(k, [])
        if not arts:
            continue
        lines.append(f"\n■ {k} ({len(arts)}건)")
        for t, link, src, pub, kws in arts:
            lines.append(f"  · [{pub[11:16]}] {t} ({src})\n    {link}")
    if len(rows) == 0:
        lines.append("\n(해당 구간 수집 기사 없음 — check 스케줄이 돌고 있었는지 확인하세요)")

    text = "\n".join(lines)
    os.makedirs(DIGEST_DIR, exist_ok=True)
    fname = os.path.join(DIGEST_DIR, f"digest_{now.strftime('%Y%m%d_%H%M')}.txt")
    with open(fname, "w", encoding="utf-8") as f:
        f.write(text)

    # notify()가 4096자 초과 시 자동으로 여러 메시지로 나눠 전송함
    notify(text)
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
