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

# 임포트 시점에 duty_active()가 사용하므로 최상단에 둔다.
KST = datetime.timezone(datetime.timedelta(hours=9))

# ==================== 설정 ====================
# ==================== 당직 모드 ====================
# 휴일 당직 때만 추가로 볼 출입처. 아래 DUTY_DATES에 날짜를 넣으면 그날만 활성화되고
# 다음 날 자동으로 원복된다. 끄는 걸 잊어도 문제가 생기지 않게 날짜 기준으로 만들었다.
# (환경변수 DUTY_MODE=1로도 강제 활성화 가능 — 급할 때 workflow_dispatch에서 주입)
DUTY_DATES = {
    "2026-08-17": None,                              # 광복절 대체공휴일 당직
    "2026-08-21": ["산업부", "중기부", "개인정보위"],   # 임시 — 3개 부처만
}

DUTY_KEYWORDS = [
    "재정경제부", "재경부",
    "국가데이터처", "데이터처",
    "기획예산처", "예산처",
    "국토교통부", "국토부",
    "산업통상부", "산업부",
    "중소벤처기업부", "중기부",
    "개인정보보호위원회", "개인정보위",
    "농림축산식품부", "농식품부",
]

DUTY_KEYWORD_GROUPS = {
    "재정경제부": "재경부", "재경부": "재경부",
    "국가데이터처": "국가데이터처", "데이터처": "국가데이터처",
    "기획예산처": "기획예산처", "예산처": "기획예산처",
    "국토교통부": "국토부", "국토부": "국토부",
    "산업통상부": "산업부", "산업부": "산업부",
    "중소벤처기업부": "중기부", "중기부": "중기부",
    "개인정보보호위원회": "개인정보위", "개인정보위": "개인정보위",
    "농림축산식품부": "농식품부", "농식품부": "농식품부",
}

# 짧은 약칭이 다른 단어의 일부로 걸리는 오탐 방지.
# 해당 표현을 제목에서 지운 뒤에도 키워드가 남아야 진짜 매칭으로 인정한다.
# ('데이터처리'는 매우 흔해서 이게 없으면 국가데이터처 섹션이 무관한 기사로 뒤덮인다)
KEYWORD_ANTIPATTERNS = {
    "산업부":   [r"산업부문", r"산업부흥", r"산업부산물", r"산업부지", r"산업부총리"],
    "예산처":   [r"예산처리", r"예산처분"],
    "데이터처": [r"데이터처리"],
    "국토부":   [r"국토부지"],
    "재경부":   [],
    "중기부":   [r"중기부문", r"중기부담", r"중기부진", r"중기부채"],
    "농식품부": [],
    "개인정보위": [],
}

def strip_antipatterns(text, kw):
    """kw의 오탐 표현을 제거한 문자열 반환."""
    t = text
    for pat in KEYWORD_ANTIPATTERNS.get(kw, []):
        t = re.sub(pat, "", t)
    return t

DUTY_ALL_GROUPS = []
for _k in DUTY_KEYWORDS:
    _g = DUTY_KEYWORD_GROUPS[_k]
    if _g not in DUTY_ALL_GROUPS:
        DUTY_ALL_GROUPS.append(_g)
def duty_groups():
    """오늘 활성화할 당직 그룹명 목록. 비당직이면 []. 날짜는 KST 기준.
    모르는 그룹명은 경고를 낸다 — 오타로 빈 목록이 되면 그날 수집이 통째로
    조용히 사라지는데, 이게 이 시스템에서 가장 위험한 실패 양상이다."""
    env = os.environ.get("DUTY_MODE", "").strip()
    if env in ("1", "true", "True", "yes", "all", "ALL"):
        return list(DUTY_ALL_GROUPS)
    if env:
        want = {x.strip() for x in re.split(r"[,\s]+", env) if x.strip()}
        unknown = want - set(DUTY_ALL_GROUPS)
        if unknown:
            print(f"[warn] DUTY_MODE에 모르는 그룹: {', '.join(sorted(unknown))}")
        return [g for g in DUTY_ALL_GROUPS if g in want]
    today = datetime.datetime.now(KST).strftime("%Y-%m-%d")
    if today not in DUTY_DATES:
        return []
    sel = DUTY_DATES[today]
    if sel is None:
        return list(DUTY_ALL_GROUPS)
    unknown = set(sel) - set(DUTY_ALL_GROUPS)
    if unknown:
        print(f"[warn] DUTY_DATES[{today}]에 모르는 그룹: {', '.join(sorted(unknown))}")
    return [g for g in DUTY_ALL_GROUPS if g in sel]
def duty_active():
    return bool(duty_groups())

# 표시 순서는 이 리스트 순서를 그대로 따른다.
# GROUP_ORDER 생성, check의 display_groups(), digest의 rep_kw 선정이 모두
# 이 순서를 참조하므로 여기만 바꾸면 check/digest 양쪽이 함께 바뀐다.
# (2026-08-11: 공정위 → 방미통위 → 과기정통부 → 우주항공청 순으로 변경)
BASE_KEYWORDS = [
    "공정거래위원회", "공정위",
    "방송미디어통신위원회", "방미통위",
    "과학기술정보통신부", "과기정통부", "과기부",
    "우주항공청",
    "CBS",
]

# 당직일에는 추가 출입처가 기존 출입처 **뒤에** 붙는다 (평소 출입처가 항상 상단).
_DUTY_ON = duty_groups()
KEYWORDS = BASE_KEYWORDS + [k for k in DUTY_KEYWORDS
                            if DUTY_KEYWORD_GROUPS[k] in _DUTY_ON]
if _DUTY_ON:
    print(f"[duty] 당직 출입처 활성: {', '.join(_DUTY_ON)}")

# 키워드가 매칭되어도, 제목에 이 표현이 있으면 오탐으로 보고 완전히 제외.
# 예: "공정위"가 "스포츠공정위원회"에도 포함되어 오매칭되는 문제 방지.
EXCLUDE_TERMS = [
    "대한체육회", "스포츠공정위원회",
]

# ==================== CBS 키워드 전용 관문 ====================
# CBS는 다른 키워드와 성격이 다르다. 우리가 원하는 건 'CBS라는 회사에 관한 기사'인데,
# CBS는 언론사라서 검색에는 세 종류가 뒤섞여 들어온다:
#   (a) CBS 회사 사안         ← 원하는 것
#   (b) CBS가 생산한 기사      ← 구글 RSS 제목 꼬리표 ' - CBS노컷뉴스'
#   (c) CBS에서 발화된 걸 인용  ← "CBS 라디오 인터뷰에서", "김현정의 뉴스쇼" — 압도적 다수
#   (d) 미국 CBS 방송 인용
#
# 설계: 부정 필터("이런 건 빼자")로 접근하면 캡션 필터 때와 같은 실패를 반복한다.
# 한국어 인용 표현은 열린 집합이라 목록으로 다 담을 수 없고, 계속 샌다.
# 그래서 **긍정 관문**을 세운다 — 회사 사안 어휘(닫힌 집합)가 제목에 있을 때만 통과.
# 관문이 좁아 (a)도 일부 놓치지만, 사용자 지침대로 '놓침 > 넘침'을 선택한 것.
CBS_SUBJECT_TERMS = [
    "사장", "이사회", "노조", "언론노조", "파업", "임단협", "징계", "해고",
    "재허가", "재승인", "방송평가", "방송법", "과징금", "제재",
    "지분", "매각", "인수", "적자", "흑자", "매출", "임금",
    "창사", "사옥", "압수수색", "고발", "소송", "선임", "사퇴", "채용",
]

# 위 관문을 통과해도 이게 있으면 (b)(c)(d)로 보고 제외.
# 관문과 달리 이쪽은 열린 집합이라 완벽할 수 없다 — 어디까지나 보조 장치.
CBS_CONTEXT_EXCLUDE = [
    "라디오", "표준FM", "음악FM", "뉴스쇼", "김현정", "박재홍", "한판승부",
    "시사자키", "출연", "인터뷰", "노컷", "미국", "CBS방송", "CBS뉴스",
    "CBS News", "인터뷰서",
]

# 키워드당 네이버 수집 페이지 상한(기본 MAX_PAGES_PER_KEYWORD).
# CBS는 관문을 못 넘는 후보가 대부분이라 DB에 남지 않고, 그래서 fetch_naver의
# '이 페이지 전부 기수집분' 조기중단이 영영 걸리지 않는다. 매 실행 5페이지를
# 다 긁는 걸 막기 위해 1페이지(최신 100건)로 제한. 30분마다 도니 충분하다.
KEYWORD_MAX_PAGES = {
    "CBS": 1,
}

# 같은 기관을 가리키는 키워드를 하나의 다이제스트 섹션으로 통합.
# (키워드 → 대표 그룹명). 여기 없는 키워드는 그 자체가 그룹명이 됨.
KEYWORD_GROUPS = {
    "과학기술정보통신부": "과기정통부", "과기정통부": "과기정통부", "과기부": "과기정통부",
    "공정거래위원회": "공정위", "공정위": "공정위",
    "방송미디어통신위원회": "방미통위", "방미통위": "방미통위",
    "우주항공청": "우주항공청",
    "CBS": "CBS",
}
# 당직 키워드 매핑은 활성 여부와 무관하게 항상 합쳐둔다.
# 당직 다음 날 06:00 다이제스트가 전날 13:30~06:00 구간(=당직 시간대)을 커버하는데,
# 그때 duty_active()는 이미 False다. 매핑이 없으면 그 기사들의 그룹을 못 찾는다.
KEYWORD_GROUPS.update(DUTY_KEYWORD_GROUPS)
# 다이제스트 섹션 표시 순서 (그룹명 기준, 중복 제거)
GROUP_ORDER = []
for _k in KEYWORDS:
    _g = KEYWORD_GROUPS.get(_k, _k)
    if _g not in GROUP_ORDER:
        GROUP_ORDER.append(_g)

# 당직 다음 날처럼 duty_active()가 꺼진 상태에서 당직 시간대 기사를 렌더할 때,
# GROUP_ORDER에 없는 그룹이 데이터에만 존재할 수 있다. 그대로 두면 렌더 루프가
# GROUP_ORDER만 돌기 때문에 그 기사들이 **조용히 사라진다**.
# 데이터에 실제로 있는 그룹을 뒤에 덧붙여 누락을 막는다.
def effective_group_order(present):
    order = [g for g in GROUP_ORDER]
    for g in present:
        if g not in order:
            order.append(g)
    return order

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
    # 경제지 (서울경제·이데일리·아시아경제는 제외 — check 알림량 축소, digest에는 계속 포함)
    "mk.co.kr", "hankyung.com", "mt.co.kr", "heraldcorp.com", "fnnews.com",
    # IT 핵심 (전자신문·지디넷·디지털데일리는 제외 — 위와 동일 이유)
    # (현재 비어있음)
    # CBS — 노컷뉴스는 제외 (check 알림량 축소, digest에는 계속 포함)
}
CORE_MEDIA_NAMES = {
    "연합뉴스", "뉴시스", "뉴스1",
    "조선일보", "중앙일보", "동아일보", "한겨레", "경향신문", "한국일보",
    "서울신문", "국민일보", "세계일보", "문화일보",
    "KBS", "KBS 뉴스", "MBC", "MBC 뉴스", "SBS", "SBS 뉴스", "YTN",
    "연합뉴스TV", "JTBC", "TV조선", "채널A", "MBN",
    # 서울경제·이데일리·아시아경제·전자신문·지디넷·디지털데일리·동아사이언스·노컷뉴스는
    # check 알림량 축소를 위해 제외 (digest에는 계속 포함됨 — media_allowed()는 별도 유지)
    "매일경제", "한국경제", "머니투데이", "헤럴드경제", "파이낸셜뉴스",
}

def short_media_name(name):
    """보고용 축약 매체명: 끝의 '일보'/'신문'을 떼어냄.
    부산일보→부산, 조선일보→조선, 서울신문→서울, 경향신문→경향 등.
    떼고 나서 2글자 미만이 되면 원래 이름을 유지한다.
    (매일경제·서울경제 등은 접미가 다르므로 영향 없음)"""
    n = (name or "").strip()
    m = re.sub(r"(일보|신문)$", "", n)
    return m if len(m) >= 2 else n

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

# KST는 파일 상단(설정부 앞)으로 옮김 — duty_active()가 임포트 시점에 쓰기 때문

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
    # digest에서 strict 필터(사진/무의미제목)로 제외된 기사 기록.
    # "(제외: 사진 N건)" 숫자만으로는 실제로 뭘 걸렀는지 알 수 없어서,
    # 눈으로 확인 가능하게 별도 로그를 남긴다. news_monitor.db 자체가
    # 매 실행 git 커밋되므로 이 테이블도 자동으로 저장소에 남는다.
    conn.execute("""CREATE TABLE IF NOT EXISTS excluded_log(
        id TEXT PRIMARY KEY,          -- article_id (같은 기사 중복 기록 방지)
        title TEXT, link TEXT, source TEXT,
        reason TEXT,                  -- photo / junk
        run_dt TEXT                   -- 이 기사가 제외된 digest 실행 시각
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

# 주제 클러스터링에서 무시할 흔한 단어 (변별력 없음)
TOPIC_STOPWORDS = {
    "대통령", "장관", "부총리", "위원장", "의원", "정부", "국회", "오늘", "내일",
    "관련", "위해", "밝혀", "밝혔다", "예정", "추진", "발표", "개최", "참석",
    "기자", "뉴스", "종합", "속보", "단독", "영상", "포토",
}

def topic_tokens(title):
    """제목에서 주제 판별용 핵심 토큰(2글자 이상 한글/영문/숫자 덩어리) 추출."""
    t = clean(title)
    t = re.sub(r"\[[^\]]*\]", "", t)
    t = t.rsplit(" - ", 1)[0]
    # 한글 2자 이상, 영문 3자 이상, 숫자 포함 토큰
    raw = re.findall(r"[가-힣]{2,}|[A-Za-z]{3,}|[0-9]+[가-힣A-Za-z%]*", t)
    toks = set()
    for w in raw:
        if w in TOPIC_STOPWORDS:
            continue
        # 한글 조사/어미 제거 — 겹조사('알뜰폰에도' 등)를 위해 더 없어질 때까지 반복.
        # 한 번만 돌리면 '알뜰폰에도'가 '알뜰폰에'로 남아 '알뜰폰'과 다른 토큰이 돼
        # 같은 사안이 서로 다른 주제로 쪼개진다.
        w2 = w
        prev = None
        while prev != w2:
            prev = w2
            w2 = re.sub(r"(으로|에서|에게|까지|부터|이라|라며|라고|한다|했다|된다|됐다)$", "", w2)
            if len(w2) > 2:
                w2 = re.sub(r"(은|는|이|가|을|를|의|에|도|와|과|만|들)$", "", w2)
        if len(w2) >= 2 and w2 not in TOPIC_STOPWORDS:
            toks.add(w2)
    return toks

def cluster_by_topic(items, title_getter, min_overlap=2, min_ratio=0.3):
    """items를 제목 토큰 유사도로 주제별 묶음. 
    두 기사의 공통 토큰이 min_overlap개 이상이고, 작은 쪽 집합의 min_ratio 이상 겹치면 같은 주제.
    min_ratio 기본값 0.3 — 0.5에서는 같은 사안(예: '알뜰폰 2.0' 27건)이 매체별 제목 차이만으로
    14조각까지 쪼개졌음. 0.3으로 낮춰도 서로 다른 사안이 잘못 합쳐지는 사례는 확인되지 않음.
    반환: [[item, item, ...], ...] (묶음 리스트, 각 묶음은 원래 순서 유지)"""
    n = len(items)
    tokens = [topic_tokens(title_getter(it)) for it in items]
    parent = list(range(n))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    for i in range(n):
        for j in range(i + 1, n):
            if not tokens[i] or not tokens[j]:
                continue
            common = tokens[i] & tokens[j]
            smaller = min(len(tokens[i]), len(tokens[j]))
            if len(common) >= min_overlap and len(common) / smaller >= min_ratio:
                union(i, j)
    clusters = {}
    for i in range(n):
        r = find(i)
        clusters.setdefault(r, []).append(items[i])
    # 원래 등장 순서(첫 멤버 인덱스) 기준 정렬
    return [clusters[k] for k in sorted(clusters.keys())]

def clean_title_display(title):
    """표시용 제목: 구글 RSS의 ' - 매체명' 꼬리표 제거 (매체명은 별도 표시하므로)."""
    t = clean(title)
    if " - " in t:
        head, tail = t.rsplit(" - ", 1)
        if len(tail) <= 15:  # 꼬리표가 매체명 길이면 제거
            t = head
    return t

def is_truncated_title(title):
    """네이버/구글 API가 제목을 중간에서 잘라 마침표 3개(...)로 보내는 경우 판별.
    한글 문장부호 말줄임표(…, 유니코드 U+2026)는 정상 제목에도 쓰이므로 대상이 아니고,
    ASCII 마침표 3개로 끝나는 것만 트렁케이션으로 봄 — 실제 digest 데이터로 확인한 패턴."""
    t = clean_title_display(title or "").strip()
    return t.endswith("...")

# ==================== 중요도 점수 ====================
# 출입처 섹션 구조는 그대로 두고, **섹션 안에서** 클러스터(주제 묶음)를 중요도 순으로 정렬한다.
# 새로 수집하는 데이터 없이 이미 가진 신호만 조합하므로 회귀 위험이 작다.
#
# 신호 3가지:
#   - 단독/속보 태그        : 기자 판단이 이미 들어간 가장 강한 신호
#   - 전재 매체 수(확산도)   : 여러 매체가 받아쓴 사안 = 파급력이 큰 사안
#   - 핵심매체 보도 여부     : 통신사·종합일간지·주요 방송이 다뤘는지
#
# 가중치는 여기서 조정한다. 값을 바꾸면 check/digest 양쪽에 함께 반영된다.
# 주의: 규칙 기반 점수라 오정렬은 반드시 생긴다. 조용히 중요한 단독 기사가
# 대형 전재 사안에 밀릴 수 있다. 단독을 항상 최상단에 두려면 EXCLUSIVE를 크게(예: 99) 잡으면
# 사실상 사전식 정렬이 된다.
# 2026-08-13: 사용자 결정 — 단독을 절대 우선으로. exclusive/breaking을 크게 잡아
# 사실상 사전식 정렬로 만든다(단독 > 속보 > 나머지, 그 안에서 확산도·핵심매체 순).
IMPORTANCE_WEIGHTS = {
    "exclusive":  99.0,  # [단독] — 항상 섹션 최상단
    "breaking":   50.0,  # [속보] — 단독 다음
    "per_source": 0.5,   # 전재 매체 1곳당
    "max_spread": 3.0,   # 전재 점수 상한 (한 사안이 매체 수만으로 독주하지 않게)
    "core_media": 1.0,   # 핵심매체가 보도했으면 가산
}

def importance_score(titles, n_sources=1, sources=()):
    """클러스터의 중요도 점수. 높을수록 위로.
    titles  : 클러스터에 속한 기사 제목들 (단독/속보 판정용)
    n_sources: 이 사안을 보도한 매체 수 (전재 확산도)
    sources : 매체 목록 (핵심매체 포함 여부 판정용)"""
    W = IMPORTANCE_WEIGHTS
    score = 0.0
    marks = {priority_mark(t) for t in titles}
    if "🔥" in marks:
        score += W["exclusive"]
    elif "⚡" in marks:
        score += W["breaking"]
    score += min((max(n_sources, 1) - 1) * W["per_source"], W["max_spread"])
    if any(core_media(s) for s in sources):
        score += W["core_media"]
    return score

def priority_mark(title):
    """[단독]/[속보] 기사에 강조 이모지 부여. 단독이 속보보다 우선.
    괄호로 감싼 태그 형태([단독],〈단독〉,【단독】,(단독) 등)만 인정 — '단독주택' 같은 오탐 방지."""
    t = title or ""
    if re.search(r"[\[〈<【(]\s*단독\s*[\]〉>】)]", t):
        return "🔥"
    if re.search(r"[\[〈<【(]\s*속보\s*[\]〉>】)]", t):
        return "⚡"
    return ""

PHOTO_TAGS = ["포토뉴스", "포토", "사진", "화보", "그래픽", "인포그래픽", "카드뉴스",
              "그래픽뉴스", "영상뉴스", "픽", "pic", "PIC", "photo", "포토와이드"]

# 사진 캡션 특유의 관형형 서술어 — "발언하는 OOO", "OOO 만난 OOO" 처럼
# 장면을 묘사하며 인물/사물을 수식하는 형태.
CAPTION_VERBS = (
    r"(하는|되는|시키는|나누는|나선|앉은|만난|듣는|잡은|맞잡은|악수하는|"
    r"참석하는|입장하는|퇴장하는|주재하는|발언하는|모두발언하는|answering|"
    r"인사하는|기념촬영하는|촬영하는|바라보는|웃는|미소짓는|밝히는|답하는|"
    r"질의하는|경청하는|생각에|자리한|기다리는|이동하는|들어서는|나서는|"
    r"둘러보는|살펴보는|시연하는|체험하는|관람하는|서명하는|전달하는|받는)"
)

# 캡션이 행사·장면명으로 끝나는 패턴 (서술 없이 명사 종결)
CAPTION_TAIL = (
    r"(기념촬영|간담회|회의|행사|시상식|기자회견|포럼|세미나|협약식|출범식|"
    r"개회식|폐회식|현판식|기공식|준공식|간담|면담|접견|오찬|만찬|리셉션|"
    r"세리머니|퍼포먼스|전경|모습|장면|현장)$"
)

# 기사다운 서술형 종결 (이게 있으면 캡션 아님)
ARTICLE_ENDINGS = (
    r"(한다|된다|했다|됐다|난다|본다|한다는|밝혀|밝혔다|나서|나섰다|출범|추진|"
    r"개정|선정|돌입|투입|막는다|만든다|알린다|성공|완료|확대|강화|착수|검토|"
    r"제시|공개|발표|도입|구축|육성|지원|참여|지정|수상|채택|승인|의결|합의|"
    r"결정|무산|철회|반발|비판|우려|논란|전망|계획|예고|촉구|요구|주문|당부|"
    r"경고|해명|반박|부인|시인|사과|중단|재개|연기|취소|폐지|신설|개편|"
    r"인상|인하|급등|급락|증가|감소|둔화|회복|악화|개선|기록|돌파|넘어|"
    r"이어|맞손|맞손잡고|손잡고|나온다|온다|간다|연다|열린다|없다|있다|"
    r"아니다|같다|보인다|드러나|드러났다|확인|점검|조사|수사|기소|구속|"
    r"판결|선고|무죄|유죄|배상|보상|환수|과징금|제재|처분|고발|소송|"
    # 연구·성과·산업 계열 (사진 캡션에는 거의 안 쓰이는 명사형 종결)
    r"규명|입증|개발|발견|관측|측정|분석|실증|시연|출시|공급|수출|수주|계약|"
    r"체결|이전|상용화|양산|가동|준공|착공|설립|유치|모집|공모|접수|개최|"
    r"운영|시행|적용|보급|전환|혁신|성장|진출|확산|정착|안착|달성|경신|"
    r"우승|선발|위촉|임명|해임|사임|사퇴|퇴임|취임|승진|영입|합병|인수|"
    r"매각|분할|상장|공시|실적|매출|영업익|순익|적자|흑자|손실|투자|"
    r"성료|폐막|개막|마무리|종료|시작|출시|공표|고시|발간|출간|공표)$"
)

# 제목 자체가 무의미한 쓰레기 (매체명만 있거나 방송 자막 등)
JUNK_TITLE_PATTERNS = [
    r"^[가-힣A-Za-z0-9]{1,6}$",                  # '뉴스핌' 처럼 단어 하나
    r"자막방송",                                   # 방송 자막 아카이브
    r"^▒.*▒$",                                   # ▒종합 경제정보 미디어 - 이데일리IR▒
    r"뉴스퀘어|뉴스룸|뉴스데스크|8뉴스|뉴스9|뉴스투데이",  # 방송 프로그램 통짜
    r"^\d{4}년\s*\d{1,2}월\s*\d{1,2}일",          # '2026년 7월 22일 ...' 로 시작
]

def is_junk_title(title):
    """제목만으로 정보가치가 없는 항목(매체명 단독, 방송 자막 등)."""
    t = clean(title or "")
    t = t.rsplit(" - ", 1)[0].strip()   # 구글 RSS 매체 꼬리표 제거
    if not t:
        return True
    for pat in JUNK_TITLE_PATTERNS:
        if re.search(pat, t):
            return True
    return False

def is_caption_like(title, strict=False):
    """제목이 '기사 제목'이 아니라 '사진 캡션'처럼 생겼는지 판별.
    아래 3가지 신호 중 2개 이상이면 캡션으로 봄:
      (1) 관형형 서술어로 인물/사물을 수식  (발언하는 OOO, OOO 만난 OOO)
      (2) 따옴표/말줄임표 등 기사 제목 특유의 부호가 전혀 없음
      (3) 서술형 종결어미가 없음 (…막는다, …출범 같은 끝맺음 부재)
    추가로 행사명 명사 종결(간담회/회의/기념촬영)도 (1) 대신 인정.
    실제 digest 데이터로 검증: 사진 26건 전부 2개 이상, 진짜 기사는 최대 1개."""
    t = clean(title or "")
    t = t.rsplit(" - ", 1)[0].strip()
    t = re.sub(r"\[[^\]]*\]", "", t).strip()   # 태그는 별도 로직이 처리
    if not t:
        return False

    # 발언 인용(따옴표) 또는 말줄임표는 기사 제목의 확실한 표식.
    # 사진 캡션은 발언을 인용하지 않으므로, 이게 있으면 캡션 아님으로 확정.
    # 예: 배경훈 "통신사는 AI 인프라·플랫폼 기업"…과기정통부·통신3사 CEO 간담회
    if re.search(r"[\"'“”‘’]", t) and re.search(r"[…]|\.\.\.", t):
        return False

    # (1) 캡션형 관형 서술어 또는 행사명 종결 — **필수 조건**.
    # 이전에는 (2)+(3)만으로도 캡션 판정이 성립했는데, (2)와 (3)은 모두 '부정형'이라
    # 열린 집합이다: ARTICLE_ENDINGS(191개)에 없는 종결어를 쓰고 부호를 안 쓴
    # 평범한 정책기사가 전부 걸린다. 한국어 종결형은 목록으로 다 담을 수 없으므로
    # 이 경로는 데이터와 무관하게 계속 오탐을 낳는다.
    # (1)은 화이트리스트(닫힌 집합)라 오탐을 만들지 않으므로 이것을 관문으로 세운다.
    s1 = bool(re.search(CAPTION_VERBS + r"(\s|$)", t)) or bool(re.search(CAPTION_TAIL, t))
    if not s1:
        return False

    # (2) 기사 제목다운 문장부호 부재
    s2 = not re.search(r"[\"'“”‘’…·%]|\.\.\.", t)
    # (3) 서술형 종결어미 부재
    s3 = not re.search(ARTICLE_ENDINGS, t)

    # strict=True(digest용): 세 신호가 모두 켜져야 캡션으로 확정.
    #   digest의 목표는 '빠뜨리지 않는 것'이므로 확신이 있을 때만 버린다.
    # strict=False(check용): (1)에 더해 (2)나 (3) 중 하나면 캡션.
    #   check의 목표는 '선별'이므로 노이즈를 좀 더 적극적으로 걷어낸다.
    return (s2 and s3) if strict else (s2 or s3)

def is_photo_article(title, link="", strict=False):
    """사진/그래픽 기사 판별. 세 갈래로 확인:
      A. [포토]/[사진]/[헤럴드pic] 등 대괄호 태그
      B. URL 패턴 (뉴스1 /photos/, 연합 PYH, 더팩트 photomovie 등)
      C. 제목이 사진 캡션처럼 생긴 경우 (is_caption_like)
    [영상]은 리포트일 수 있어 태그 기준에서는 계속 제외(살림)."""
    t = title or ""

    # A. 대괄호/괄호 태그 — 'pic'은 헤럴드pic 처럼 접미로 붙는 경우도 잡음
    for tag in PHOTO_TAGS:
        if re.search(rf"[\[〈<【(][^\]〉>】)]*{re.escape(tag)}[^\[〈<【(]*[\]〉>】)]", t, re.I):
            return True

    # B. URL 패턴
    l = (link or "").lower()
    if "news1.kr/photos/" in l:                      # 뉴스1 사진 전용
        return True
    if re.search(r"yna\.co\.kr/view/pyh", l):        # 연합뉴스 사진 ID(PYH)
        return True
    if "/photomovie/" in l or "/photo/" in l:        # 더팩트 등 사진 섹션
        return True
    if re.search(r"newsis\.com/view/NISI", l, re.I): # 뉴시스 사진 ID(NISI) — 기사는 NISX
        return True

    # C. 캡션형 제목 (A/B는 태그·URL 기반 고신뢰 신호라 strict와 무관하게 적용)
    if is_caption_like(t, strict=strict):
        return True

    return False

# ==================== 대표 기사 선정 우선순위 ====================
# 보도자료 등 기관 소스로 여러 매체가 같은 내용을 쓴 묶음에서는 통신사 기사를 대표로 세운다.
# 원문에 가장 가깝고 링크가 안정적이며, 제목이 사안을 건조하게 요약하는 경향이 있어서다.
# 순서상 앞일수록 우선. 여기 없는 매체는 모두 동일하게 후순위.
WIRE_ORDER = ["연합뉴스", "뉴시스", "뉴스1"]

def wire_rank(source):
    """통신사 우선순위. 낮을수록 우선. 통신사가 아니면 len(WIRE_ORDER)."""
    n = media_name(source)
    try:
        return WIRE_ORDER.index(n)
    except ValueError:
        return len(WIRE_ORDER)

def article_rank(title, source, pub_dt):
    """묶음 안에서 대표/표시 순서를 정하는 정렬 키.
    단독·속보 > 통신사(연합 > 뉴시스 > 뉴스1) > 먼저 보도된 것.
    단독이 통신사보다 앞서는 건 사용자 지침('단독 기사가 아니라면 통신사 우선')에 따른 것."""
    pr = {"🔥": 0, "⚡": 1}.get(priority_mark(title), 2)
    return (pr, wire_rank(source), pub_dt)

# 통신사보다 이만큼(분) 앞서 발행됐으면 '원 보도'로 보고 대표 자리를 지켜준다.
# 보도자료 전재는 대개 같은 시간대에 일제히 풀리므로, 뚜렷한 시차가 있으면
# 그 매체가 먼저 취재해 쓴 것으로 본다. ([단독] 태그가 없는 특종을 구제하는 장치)
SCOOP_LEAD_MINUTES = 60

def _pr_tier(title):
    return {"🔥": 0, "⚡": 1}.get(priority_mark(title), 2)

def _parse_dt(s):
    try:
        return datetime.datetime.fromisoformat(s)
    except Exception:
        return None

def pick_representative(members, get_title, get_source, get_pub):
    """묶음의 대표 기사 선정.
    기본: 단독·속보 > 통신사(연합 > 뉴시스 > 뉴스1) > 발행순.
    예외: 통신사가 대표로 뽑혔더라도, 비통신사가 '가장 먼저 쓴 통신사'보다
          SCOOP_LEAD_MINUTES 이상 앞서 발행했으면 그쪽을 대표로 유지한다."""
    base = min(members, key=lambda x: article_rank(get_title(x), get_source(x), get_pub(x)))
    if wire_rank(get_source(base)) >= len(WIRE_ORDER):
        return base                      # 대표가 이미 비통신사 — 예외 규칙 불필요

    # 비교 기준은 '가장 먼저 쓴 통신사' 시각. 대표(연합)의 시각을 쓰면
    # 뉴시스가 더 먼저 쓴 경우에 시차가 부풀려져 오판한다.
    wire_dts = [_parse_dt(get_pub(x)) for x in members
                if wire_rank(get_source(x)) < len(WIRE_ORDER)]
    wire_dts = [d for d in wire_dts if d]
    if not wire_dts:
        return base
    earliest_wire = min(wire_dts)

    base_pr = _pr_tier(get_title(base))
    best, best_dt = None, None
    for mm in members:
        if wire_rank(get_source(mm)) < len(WIRE_ORDER):
            continue                     # 통신사끼리는 이 규칙의 대상이 아님
        if _pr_tier(get_title(mm)) > base_pr:
            continue                     # 단독·속보 등급이 더 낮으면 승격시키지 않음
        dt = _parse_dt(get_pub(mm))
        if dt is None:
            continue
        if (earliest_wire - dt).total_seconds() >= SCOOP_LEAD_MINUTES * 60:
            if best_dt is None or dt < best_dt:
                best, best_dt = mm, dt
    return best or base

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
        # 대표 기사: 단독·속보 > 통신사(연합 우선) > 발행순.
        # 단, 비통신사가 통신사보다 1시간 이상 먼저 썼으면 원 보도로 보고 대표 유지.
        rep = pick_representative(members, lambda x: x[0], lambda x: x[2], lambda x: x[3])
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

def is_excluded(title):
    """제목에 EXCLUDE_TERMS 중 하나라도 있으면 True — 키워드 매칭됐어도 오탐으로 제외."""
    t = title or ""
    return any(term in t for term in EXCLUDE_TERMS)

def cbs_gate_reason(title):
    """cbs_subject의 판정 근거를 단계별로 반환 — 관문 어디서 막혔는지 계측용.
    반환: 'no_cbs' | 'context' | 'no_subject' | 'pass'"""
    t = clean(title or "")
    t = t.rsplit(" - ", 1)[0].strip()
    if not re.search(r"(?<![A-Za-z])CBS(?![A-Za-z])", t):
        return "no_cbs"          # 제목에 CBS 없음 (네이버는 본문까지 검색하므로 대다수가 여기)
    if any(x in t for x in CBS_CONTEXT_EXCLUDE):
        return "context"         # 인용/출처/미국 CBS 표지에 걸림
    if not any(x in t for x in CBS_SUBJECT_TERMS):
        return "no_subject"      # 회사 사안 어휘 없음 → 관문 어휘가 좁다는 신호
    return "pass"

def cbs_subject(title):
    """제목이 'CBS라는 회사를 다룬 기사'인지 판별 (CBS 키워드 전용 관문)."""
    return cbs_gate_reason(title) == "pass"

# 키워드 → 추가 판정 함수. 여기 등록된 키워드는 제목이 함수를 통과해야만 매칭 인정.
# 전역 EXCLUDE_TERMS와 달리 해당 키워드에만 적용되므로, 다른 출입처 기사에는
# 영향을 주지 않는다. (예: '라디오'를 전역 제외어로 넣으면 과기정통부 주파수
# 기사까지 통째로 날아간다 — 그래서 키워드별로 분리했다.)
KEYWORD_GATES = {
    "CBS": cbs_subject,
}

def matched_keywords(title):
    """제목에서 매칭된 키워드 목록. 게이트가 있는 키워드는 게이트 통과 시에만 인정.
    구글 RSS의 ' - 매체명' 꼬리표는 떼고 본다 — 안 그러면 CBS노컷뉴스가 만든 기사가
    전부 'CBS' 키워드에 걸린다. (기존 4개 출입처 키워드는 매체명에 포함될 일이 없어
    이 변경의 영향을 받지 않는다.)"""
    if is_excluded(title):
        return []
    t = clean(title or "")
    t = t.rsplit(" - ", 1)[0].strip()
    out = []
    for k in KEYWORDS:
        if k not in strip_antipatterns(t, k):
            continue          # 오탐 표현('산업부문','데이터처리' 등)뿐이면 매칭 아님
        gate = KEYWORD_GATES.get(k)
        if gate and not gate(title):
            continue
        out.append(k)
    return out

# ==================== 수집: 네이버 ====================
def fetch_naver(conn, keyword, known_ids):
    """최신순 페이지네이션. '이미 본 기사'가 페이지 전체를 채우면 중단."""
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET):
        return []
    new_items = []
    max_pages = KEYWORD_MAX_PAGES.get(keyword, MAX_PAGES_PER_KEYWORD)
    for page in range(max_pages):
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

def _send_telegram(text, token, chat_id, parse_mode="HTML"):
    payload = {
        "chat_id": chat_id, "text": text,
        "disable_web_page_preview": "true"}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    data = urllib.parse.urlencode(payload).encode()
    urllib.request.urlopen(urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data), timeout=15)

def notify(text, target="check", parse_mode="HTML"):
    """긴 메시지는 줄(기사) 단위로 나눠 여러 개로 전송. (i/n) 표시 붙임.
    target='check'면 실시간 봇, 'digest'면 다이제스트 봇으로 전송.
    parse_mode=None이면 순수 텍스트(복사용 보고양식)로 전송."""
    token, chat_id = (TELEGRAM_DIGEST_BOT_TOKEN, TELEGRAM_DIGEST_CHAT_ID) \
        if target == "digest" else (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    if token and chat_id:
        try:
            if len(text) <= TG_CHUNK:
                _send_telegram(text, token, chat_id, parse_mode)
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
                _send_telegram(f"({i}/{total})\n{chunk}", token, chat_id, parse_mode)
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
    # 게이트 키워드(CBS) 관측용 카운터. 관문이 너무 좁은지 로그로 감시한다.
    cbs_candidates = cbs_passed = 0
    cbs_stage = {}      # 관문 단계별 탈락 집계
    cbs_samples = []    # 제목에 CBS가 있었는데 탈락한 것들 (관문 조정용 표본)

    for kw in KEYWORDS:
        for it in fetch_naver(conn, kw, set(known) - set(collected)) + \
                  fetch_google(kw, set(known) - set(collected)):
            aid = it["id"]
            if is_excluded(it["title"]):
                continue  # 오탐 제외어 포함 시 아예 수집하지 않음
            # 게이트가 걸린 키워드(CBS)는 제목이 관문을 통과할 때만 인정한다.
            # 네이버 검색은 본문까지 훑기 때문에 제목에 CBS가 없는 기사도 잔뜩 돌려주고,
            # 아래 폴백(or {kw})이 그걸 전부 CBS 섹션으로 밀어넣어 버린다.
            gate = KEYWORD_GATES.get(kw)
            gate_ok = True
            if gate:
                cbs_candidates += 1
                reason = cbs_gate_reason(it["title"])
                cbs_stage[reason] = cbs_stage.get(reason, 0) + 1
                gate_ok = (reason == "pass")
                if gate_ok:
                    cbs_passed += 1
                elif reason in ("context", "no_subject"):
                    # 제목에 CBS가 있는데도 걸러진 것 — 관문 조정 판단에 필요한 표본.
                    # no_cbs(본문만 매칭)는 양이 많고 정보가치가 없어 남기지 않는다.
                    cbs_samples.append(f"[{reason}] {it['title'][:60]}")
            if aid in known and aid not in collected:
                continue
            if aid in collected:
                if gate_ok:
                    collected[aid]["kws"].add(kw)
                continue
            kws = set(matched_keywords(it["title"]))
            if not kws:
                if gate:
                    continue  # 게이트 키워드는 폴백 금지 — 관문을 못 넘으면 수집 안 함
                # 제목에 키워드가 있었는데도 매칭이 안 됐다면 오탐 표현에 걸린 것
                # ('한국산업부문' 등). 폴백으로 되살리면 오탐이 그대로 들어온다.
                if kw in clean(it["title"]):
                    continue
                kws = {kw}   # 제목엔 없고 본문에만 있는 경우 — 검색어를 그대로 사용
            it["kws"] = kws
            collected[aid] = it

    new_rows = []
    if cbs_candidates:
        st = cbs_stage
        print(f"[CBS] 검색 {cbs_candidates}건 → 제목에CBS없음 {st.get('no_cbs',0)} / "
              f"인용·출처차단 {st.get('context',0)} / 주제어없음 {st.get('no_subject',0)} / "
              f"통과 {cbs_passed}건")
        for s in cbs_samples[:15]:
            print(f"  [CBS탈락] {s}")
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
        if (allowed and it["pub_dt"] >= fresh_cutoff
                and not is_photo_article(it["title"], it["link"])
                and not is_junk_title(it["title"])):
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
        # 출입처(그룹)별 → 주제 클러스터 → 기사(확인시점순) 구조
        by_g = {g: [] for g in GROUP_ORDER}
        for it in new_rows:
            gs = display_groups(it["kws"])
            g = gs[0] if gs else GROUP_ORDER[0]
            by_g.setdefault(g, []).append(it)

        def pick_rep(clu):
            """주제 클러스터의 대표 기사 1건 선정.
            단독/속보 > 통신사(연합 우선) > 발행순 + 원 보도(1시간 이상 선행) 예외.
            그 위에 '제목이 잘리지 않은 것'을 먼저 거른다 — check는 digest와 달리
            잘린 제목을 대체할 방법이 없어 그대로 알림에 노출되기 때문."""
            whole = [a for a in clu if not is_truncated_title(a["title"])] or clu
            return pick_representative(whole, lambda a: a["title"],
                                       lambda a: a["source"], lambda a: a["pub_dt"])

        def render_check(as_html):
            esc = tg_escape if as_html else (lambda s: s)
            body = []
            shown = 0
            for g in effective_group_order(by_g.keys()):
                arts = by_g.get(g, [])
                if not arts:
                    continue
                clusters = cluster_by_topic(arts, lambda it: it["title"])
                def clu_rank(clu):
                    # 섹션 안에서 중요도 순 정렬. 확산도는 이번 배치 크기가 아니라
                    # pickup_count(최근 24시간 같은 제목 그룹의 매체 수)를 쓴다 —
                    # 이번 실행에 1건만 새로 들어왔어도 이미 널리 퍼진 사안일 수 있기 때문.
                    srcs = set()
                    for a in clu:
                        srcs |= pickup_count.get(group_key(a["title"]), set())
                        srcs.add(a["source"])
                    sc = importance_score([a["title"] for a in clu],
                                          n_sources=len(srcs), sources=srcs)
                    # 점수 동률이면 먼저 보도된 것(원 보도에 가까움) 우선
                    return (-sc, min(a["pub_dt"] for a in clu))
                clusters.sort(key=clu_rank)
                body.append(f"\n■ <b>{esc(g)}</b>" if as_html else f"\n■ {g}")
                for clu in clusters:
                    # 같은 주제는 대표 1건만 알림. 나머지는 '(관련 N건)'으로만 표시하고
                    # 전체 목록은 다이제스트에서 확인 (check=선별, digest=누락없음 원칙 유지).
                    it = pick_rep(clu)
                    shown += 1
                    t = it["pub_dt"][11:16]
                    mark = priority_mark(it["title"])
                    mp = f"{mark} " if mark else ""
                    src = media_name(it["source"])
                    rel = f" (관련 {len(clu) - 1}건)" if len(clu) > 1 else ""
                    if as_html:
                        body.append(
                            f'{mp}· <a href="{esc(it["link"])}">{esc(it["title"])}</a>'
                            f' [{t}|{esc(src)}]{rel}')
                    else:
                        body.append(f"{mp}· {it['title']} [{t}|{src}]{rel}\n    {it['link']}")
            head = (f"🆕 <b>주요 기사 {shown}건</b> " if as_html
                    else f"🆕 주요 기사 {shown}건 ") + f"({now.strftime('%m/%d %H:%M')})"
            lines = [head] + body
            if skipped:
                note = f"\n<i>선별 제외 {len(skipped)}건은 다이제스트에서 확인</i>" if as_html \
                       else f"\n(선별 제외 {len(skipped)}건은 다이제스트에서 확인)"
                lines.append(note)
            return "\n".join(lines)

        html_text = render_check(as_html=True)
        plain_text = render_check(as_html=False)

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
    is_saturday = now.weekday() == 5  # 월=0 ... 토=5, 일=6
    if now.hour < 7:     # 06:00 실행 → 야간 다이제스트: 전일 13:30 ~ 금일 06:00
        # 전일 오후 digest(13:30) 이후 수집분을 모두 포함한다.
        # 예전에는 '전일 23:00 ~'이라 13:30~23:00에 수집된 기사가 어느 digest에도
        # 들어가지 않고 사라졌고, check가 06시부터 도는 탓에 야간 구간은 늘 비어 있었다.
        start = datetime.datetime.combine(today - datetime.timedelta(days=1),
                 datetime.time(13, 30), KST)
        end   = datetime.datetime.combine(today, datetime.time(6, 0), KST)
        label = "야간 다이제스트"
        # 하루 1회(야간 다이제스트 시점)만 오래된 기사 정리 — 토요일 발송 스킵과 무관하게 계속 실행
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

    if is_saturday:
        # 토요일은 다이제스트를 발송하지 않음. DB 정리(위 prune_old)는 이미 실행됐으므로
        # 그대로 두고 발송/저장 단계만 건너뛴다. 재수집(re-query)조차 하지 않아 API 호출도 없음.
        print(f"[{now.strftime('%m/%d %H:%M')}] 토요일 — {label} 발송 생략")
        conn.close()
        return

    rows = conn.execute("""SELECT title,link,source,pub_dt,keywords FROM articles
                           WHERE seen_dt>=? AND seen_dt<? ORDER BY pub_dt""",
                        (start.isoformat(), end.isoformat())).fetchall()
    rows = [r for r in rows if media_allowed(r[2])]  # 화이트리스트 매체만
    fresh_cutoff = (now - datetime.timedelta(hours=24)).isoformat()
    rows = [r for r in rows if r[3] >= fresh_cutoff]  # 발행 24시간 이내만
    # digest는 "빠뜨리지 않는 것"이 목표 → strict=True (확신할 때만 제외)
    # 제외된 기사는 실체를 excluded_log 테이블에 남긴다. "(제외: N건)" 숫자만으로는
    # 뭘 걸렀는지 알 수 없어 필터가 실제로 맞는 판단을 했는지 검증할 방법이 없기 때문.
    # news_monitor.db 자체가 매 실행 git 커밋되므로 이 로그도 자동으로 저장소에 남는다.
    photo_rows = [r for r in rows if is_photo_article(r[0], r[1], strict=True)]
    rows = [r for r in rows if r not in photo_rows]
    junk_rows = [r for r in rows if is_junk_title(r[0])]
    rows = [r for r in rows if r not in junk_rows]
    photo_excluded, junk_excluded = len(photo_rows), len(junk_rows)

    if photo_rows or junk_rows:
        for r, reason in [(r, "photo") for r in photo_rows] + [(r, "junk") for r in junk_rows]:
            title, link, source = r[0], r[1], r[2]
            aid = article_id(link, title)
            conn.execute("""INSERT OR REPLACE INTO excluded_log VALUES(?,?,?,?,?,?)""",
                         (aid, title, link, source, reason, now.isoformat()))
        conn.commit()

    # 기관 그룹별 그룹핑 (공정거래위원회+공정위 → 하나의 섹션 등)
    by_group = {g: [] for g in GROUP_ORDER}
    for r in rows:
        kws = r[4].split(",") if r[4] else []
        # 대표 키워드: KEYWORDS 순서상 가장 앞선 것 → 그 그룹으로
        rep_kw = next((k for k in KEYWORDS if k in kws), (kws[0] if kws else KEYWORDS[0]))
        g = KEYWORD_GROUPS.get(rep_kw, rep_kw)
        by_group.setdefault(g, []).append(r)

    # HTML/평문 공통 렌더러
    def render_digest(as_html):
        esc = tg_escape if as_html else (lambda s: s)
        head = (f"📋 <b>{esc(label)}</b> | " if as_html else f"📋 {label} | ") + \
               f"{start.strftime('%m/%d %H:%M')} ~ {end.strftime('%m/%d %H:%M')}"
        lines = [head, "PLACEHOLDER"]
        shown_local = 0
        seen = set()
        shown_titles = set()   # 최종 표시 제목 기준 중복 방지
        for g in effective_group_order(by_group.keys()):
            arts = by_group.get(g, [])
            if not arts:
                continue
            grouped = dedup_group(arts)  # 전재(동일기사) 묶기 → [(대표item, [매체]), ...]
            grouped = [(rep, srcs) for (rep, srcs) in grouped if group_key(rep[0]) not in seen]
            for rep, srcs in grouped:
                seen.add(group_key(rep[0]))
            if not grouped:
                continue
            # 주제 클러스터링: grouped 항목들을 대표 제목 기준으로 묶음
            clusters = cluster_by_topic(grouped, lambda gs: gs[0][0])
            # 클러스터 정렬: 섹션 안에서 중요도 순 (단독·속보 > 전재 확산 > 핵심매체)
            def clu_rank(clu):
                titles = [gs[0][0] for gs in clu]
                srcs = set()
                for rep, sources in clu:
                    srcs |= set(sources)
                sc = importance_score(titles, n_sources=len(srcs), sources=srcs)
                # 점수 동률이면 먼저 보도된 것 우선
                return (-sc, min(gs[0][3] for gs in clu))
            clusters.sort(key=clu_rank)
            sec_pos = len(lines)      # 섹션 헤더 자리를 잡아두고, 건수는 렌더 후 채운다
            lines.append(None)
            sec_count = 0
            for clu in clusters:
                # 클러스터 내부: 단독·속보 > 통신사(연합 우선) > 발행순
                clu.sort(key=lambda gs: article_rank(gs[0][0], gs[0][2], gs[0][3]))
                cluster_started = False
                for rep, sources in clu:
                    t = clean_title_display(rep[0])
                    if is_truncated_title(t):
                        # 잘린 제목은 같은 주제 안의 온전한 제목으로 대체
                        alt = next((clean_title_display(g2[0][0]) for g2 in clu
                                    if not is_truncated_title(clean_title_display(g2[0][0]))), None)
                        if alt:
                            t = alt
                    # 대체 결과 앞 줄과 완전히 같아졌으면(같은 기사의 전재) 한 줄만 남긴다
                    tkey = re.sub(r"[\s\W]+", "", t)
                    if tkey in shown_titles:
                        continue
                    shown_titles.add(tkey)
                    shown_local += 1
                    sec_count += 1
                    # 같은 주제 묶음은 붙여서, 다른 주제 사이에는 빈 줄을 넣어 구분한다.
                    # (제목 줄에 ▶/┗ 같은 기호를 붙이면 복사할 때 제목이 오염되므로
                    #  기호 대신 여백으로 묶음을 표현)
                    if sec_count > 1 and not cluster_started:
                        lines.append("")
                    cluster_started = True
                    src = short_media_name(media_name(rep[2]))
                    link = rep[1]
                    mark = priority_mark(rep[0])
                    mark_prefix = f"{mark} " if mark else ""
                    # 제목은 일반 텍스트(복사하면 제목 줄만 깨끗하게 나옴).
                    # 링크는 아랫줄에 별도로 두되, 화면에는 짧은 텍스트로만 보이게 앵커를 씌운다
                    # (구글 RSS URL이 300자에 달해 그대로 노출하면 화면을 다 잡아먹기 때문).
                    lines.append(f"{mark_prefix}{esc(t)}({esc(src)})")
                    if link:
                        lines.append(f'<a href="{esc(link)}">🔗 원문</a>' if as_html else link)
            # 섹션 헤더 확정: 중복 제거 후 실제 표시된 건수를 사용
            if sec_count == 0:
                del lines[sec_pos]        # 전부 중복이라 빈 섹션이면 헤더도 제거
            else:
                lines[sec_pos] = (f"\n■ <b>{esc(g)}</b> ({sec_count}건)" if as_html
                                  else f"\n■ {g} ({sec_count}건)")
        if len(rows) == 0:
            lines.append("\n(해당 구간 수집 기사 없음 — check 스케줄이 돌고 있었는지 확인하세요)")
        # 필터로 걸러낸 건수 + 실제 목록 표기 — 과필터 감시용.
        # 목록을 여기 바로 싣는 이유: excluded 모드는 DB가 git으로 다음 실행까지
        # 전달돼야 동작하는데, push가 조용히 실패하면 목록이 사라진다(실제로 겪음).
        # digest 시점엔 이미 데이터가 손에 있으므로 왕복 없이 바로 붙인다.
        if photo_rows or junk_rows:
            parts = []
            if photo_excluded:
                parts.append(f"사진 {photo_excluded}건")
            if junk_excluded:
                parts.append(f"무의미제목 {junk_excluded}건")
            txt = "제외: " + ", ".join(parts)
            lines.append(f"\n<i>{esc(txt)}</i>" if as_html else f"\n({txt})")
            for r, tag in [(r, "사진") for r in photo_rows] + [(r, "무의미") for r in junk_rows]:
                ttl = clean_title_display(r[0])
                src = short_media_name(media_name(r[2]))
                if as_html:
                    lines.append(f'<i>[{tag}] {esc(ttl)}({esc(src)})</i> '
                                 f'<a href="{esc(r[1])}">🔗</a>')
                else:
                    lines.append(f"[{tag}] {ttl}({src})\n{r[1]}")
        sep = "=" * (30 if as_html else 40)
        lines[1] = f"주요 이슈 {shown_local}건 (원문 {len(rows)}건)\n" + sep
        return "\n".join(lines)

    html_text = render_digest(as_html=True)
    plain_text = render_digest(as_html=False)

    os.makedirs(DIGEST_DIR, exist_ok=True)
    fname = os.path.join(DIGEST_DIR, f"digest_{now.strftime('%Y%m%d_%H%M')}.txt")
    with open(fname, "w", encoding="utf-8") as f:
        f.write(plain_text)

    # notify()가 4096자 초과 시 자동으로 여러 메시지로 나눠 전송함
    # (본문 자체가 '제목(매체)' 보고양식이므로 별도 보고양식 메시지는 보내지 않음)
    notify(html_text, target="digest")
    print(f"저장: {fname}")
    conn.close()

# ==================== excluded 모드 (제외 기사 조회) ====================
def run_excluded(days=1):
    """최근 N일간 digest strict 필터(사진/무의미제목)로 제외된 기사 목록을 텔레그램(digest 봇)으로 전송.
    "(제외: 사진 N건)" 숫자 뒤에 실제로 뭐가 걸렸는지 확인하고 싶을 때 수동 실행.
    cron-job.org에 별도 잡을 안 만들었다면, GitHub Actions 화면에서
    workflow_dispatch의 mode 입력에 'excluded'를 넣어 수동 실행하면 된다."""
    conn = db()
    now = datetime.datetime.now(KST)
    cutoff = (now - datetime.timedelta(days=days)).isoformat()
    rows = conn.execute("""SELECT title,link,source,reason,run_dt FROM excluded_log
                           WHERE run_dt>=? ORDER BY run_dt DESC""", (cutoff,)).fetchall()
    # 진단용: 테이블 전체 상태. "제외된 게 없다"와 "DB가 전달 안 됐다"를 구분하기 위함.
    # 이 둘을 구분 못 해서 실제로 오진한 적 있음(digest는 제외 1건이라 했는데 조회는 0건).
    total = conn.execute("SELECT COUNT(*) FROM excluded_log").fetchone()[0]
    last = conn.execute("SELECT MAX(run_dt) FROM excluded_log").fetchone()[0]
    n_art = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    conn.close()

    diag = (f"\n\n[진단] 로그 총 {total}건 / 마지막 기록 {last or '없음'} / "
            f"articles {n_art}건\n"
            f"digest는 제외를 알렸는데 여기가 0건이면, DB가 git으로 전달되지 않은 것"
            f"(Actions의 DB 커밋 단계 push 실패)입니다.")

    if not rows:
        notify(f"최근 {days}일간 제외된 기사 없음{diag}", target="digest", parse_mode=None)
        print(f"제외 기사 없음{diag}")
        return

    reason_label = {"photo": "사진", "junk": "무의미제목"}
    lines = [f"🔍 최근 {days}일 제외 기사 {len(rows)}건 (사진/무의미제목 strict 필터)", "=" * 30]
    for title, link, source, reason, run_dt in rows:
        lines.append(f"[{reason_label.get(reason, reason)}] {title} ({media_name(source)})")
        lines.append(f"  {link}")
    text = "\n".join(lines) + diag
    notify(text, target="digest", parse_mode=None)  # 평문 — 링크 그대로 노출해도 무방(내부 확인용)
    # notify()가 토큰 없을 때 이미 print()로 폴백하므로 여기서 별도 출력 안 함

# ==================== main ====================
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode == "check":
        run_check()
    elif mode == "digest":
        run_digest()
    elif mode == "excluded":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        run_excluded(days)
    else:
        print("usage: python news_monitor.py [check|digest|excluded [days]]")
