#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
retitle.py — 네이버 API가 40~50자에서 잘라 보낸 제목(끝이 ASCII '...')을
             원문 페이지의 og:title로 복구한다.

배경 (앞 세션 실측, 재조사 금지)
- 09/17 저녁 다이제스트 305건 중 30건(9.8%)이 잘린 제목. 전부 origin=naver.
- 30건 모두 DB 안에 온전한 대체본 없음 — 원문을 읽는 수밖에 없다.
- 링크는 언론사 직링크(mt.co.kr, fnnews.com 등)라 og:title 접근이 쉽다.
- 표본 2건 확인 완료(2/2 성공).

동작
- 기본은 드라이런: DB를 읽어 대상만 골라 "전 → 후"를 출력하고 끝낸다.
- APPLY=1 환경변수를 줘야 실제로 news_monitor.db에 덮어쓴다.
- 복구에 성공한 기사는 제목이 더 이상 잘림 상태가 아니게 되므로,
  다음 실행에서 is_truncated_title() 필터를 자연히 통과 못 해 재요청되지 않는다.
  (별도 "처리됨" 플래그/컬럼 없이 이 성질만으로 재요청 방지 요건을 만족한다.)
- 실패(네트워크 오류, og:title 없음, 접두 불일치 등)는 기존 제목을 그대로 둔다.
  다음 실행에서 다시 시도된다 — 이건 "복구 성공작 재요청 금지"와는 별개 요건이라
  의도된 동작이다.
- news_monitor.py는 건드리지 않는다. db(), is_truncated_title(), clean_title_display(),
  clean(), strip_media_tail()을 그대로 가져다 써서 판정 로직을 이원화하지 않는다.

제약 (중요)
- 이 작업환경(Claude)은 언론사 사이트에 접속할 수 없다(프록시 403). 그래서 네트워크
  구간은 Claude가 실측 검증을 끝낼 수 없다 — 드라이런 출력을 사람이 GitHub Actions에서
  1회 수동 실행해 눈으로 확인하는 것으로 메운다. 이 스크립트가 어떤 예외 상황에서도
  예외를 밖으로 던지지 않고 "실패 → 기존 제목 유지"로 흡수하도록 설계된 이유다.
"""
import os
import re
import sys
import html
import time
import urllib.request
import urllib.error

from news_monitor import db, is_truncated_title, clean_title_display, clean

APPLY = os.environ.get("APPLY") == "1"

TIMEOUT = 12            # 초. 개별 기사 페이지 하나 못 받아온다고 전체를 막으면 안 됨.
SLEEP_BETWEEN = 0.3     # 초. 언론사 서버에 짧은 시간 안에 몰아치지 않기 위한 최소 예의.
MAX_RETRY = 1           # 일시적 오류 1회만 재시도(과도한 재시도는 오히려 민폐).

# 한국 언론사 사이트에서 실제로 마주치는 인코딩. Content-Type/meta에 charset이
# 없거나 못 읽은 페이지에 한해 이 순서로 시도한다.
FALLBACK_ENCODINGS = ("utf-8", "cp949", "euc-kr")

# meta charset="..." 또는 content="text/html; charset=..." 양쪽 표기 다 잡는다.
_CHARSET_RE = re.compile(rb'charset\s*=\s*["\']?\s*([a-zA-Z0-9_-]+)', re.I)

# <meta ...> 태그를 통째로 한 덩이씩 뽑는다 — '.'을 DOTALL로 여러 태그에 걸쳐
# 매칭시키면(content="..." 값 안에 다음 태그의 속성까지 먹혀드는 사고가 남) 안 되므로
# 태그 하나 단위로 자른 뒤, 그 안에서만 속성을 파싱한다.
_META_TAG_RE = re.compile(r'<meta\b[^>]*>', re.I)
_ATTR_RE = re.compile(r'''([a-zA-Z][\w:-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')''')


def _parse_meta_attrs(tag):
    attrs = {}
    for m in _ATTR_RE.finditer(tag):
        name = m.group(1).lower()
        value = m.group(2) if m.group(2) is not None else m.group(3)
        attrs[name] = value
    return attrs


def find_og_title(text):
    """og:title (property 또는 name 어느 쪽으로 와도) 값을 찾는다.
    속성 순서(content가 먼저 오든 property가 먼저 오든)는 상관없다."""
    for tag in _META_TAG_RE.findall(text):
        attrs = _parse_meta_attrs(tag)
        key = attrs.get("property") or attrs.get("name")
        if key and key.strip().lower() == "og:title":
            return attrs.get("content")
    return None

_ENCODING_ALIASES = {
    "ks_c_5601-1987": "euc-kr",
    "ksc5601": "euc-kr",
    "korean": "euc-kr",
    "x-windows-949": "cp949",
    "ms949": "cp949",
}


def _norm_encoding(name):
    name = (name or "").strip().lower()
    return _ENCODING_ALIASES.get(name, name)


def decode_html(raw_bytes, header_charset=None):
    """응답 바이트를 최대한 맞는 인코딩으로 디코드한다.
    순서: HTTP 헤더 charset → 본문 <meta charset> → 흔한 한국어 인코딩 순차 시도 →
    (그래도 실패하면) utf-8 + errors=replace로 절대 죽지 않게."""
    candidates = []
    if header_charset:
        candidates.append(_norm_encoding(header_charset))
    m = _CHARSET_RE.search(raw_bytes[:4096])
    if m:
        candidates.append(_norm_encoding(m.group(1).decode("ascii", "ignore")))
    for enc in FALLBACK_ENCODINGS:
        if enc not in candidates:
            candidates.append(enc)

    for enc in candidates:
        if not enc:
            continue
        try:
            return raw_bytes.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def fetch_og_title(url):
    """원문 페이지에서 og:title을 읽어온다.
    반환: (og_title_or_None, 실패사유_or_None)"""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        },
    )
    raw = None
    header_charset = None
    last_err = None
    for attempt in range(MAX_RETRY + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
                header_charset = r.headers.get_content_charset()
            break
        except Exception as e:  # 네트워크/HTTP 오류 전부 — 실패로 흡수하고 다음 기사로
            last_err = e
            if attempt < MAX_RETRY:
                time.sleep(0.5)
            continue
    if raw is None:
        return None, f"fetch 실패: {last_err}"

    text = decode_html(raw, header_charset)
    content = find_og_title(text)
    if content is None:
        return None, "og:title 없음"
    content = html.unescape(content).strip()
    if not content:
        return None, "og:title 비어있음"
    return content, None


def recover_title(raw_title, link):
    """(새_제목_or_None, 실패사유_or_None) 반환.
    실패 시 새_제목은 반드시 None — 호출부가 "기존 제목 유지"로 처리한다."""
    if not link:
        return None, "링크 없음"

    og_title, err = fetch_og_title(link)
    if og_title is None:
        return None, err

    if is_truncated_title(og_title):
        return None, "og:title도 잘려 있음"

    new_clean = clean_title_display(og_title)
    if not new_clean:
        return None, "정제 후 빈 제목"

    # 엉뚱한 기사(리다이렉트/캐시/공용 og:title) 방어: 복구본이 원 제목의
    # 앞부분으로 시작해야 한다. 말줄임표(ASCII '...')만 잘림 표시이므로 그것만 뗀다
    # — 한글 문장부호 말줄임표(…)는 원 제목 내용일 수 있어 건드리지 않는다.
    orig_clean = clean(raw_title).strip()
    orig_prefix = orig_clean[:-3].rstrip() if orig_clean.endswith("...") else orig_clean

    # 공백 차이(줄바꿈/이중공백 등)는 무시하고 문자 나열만 비교.
    if not new_clean.replace(" ", "").startswith(orig_prefix.replace(" ", "")):
        return None, (f"접두 불일치: 원본 '{orig_prefix[:25]}...' vs "
                       f"복구본 '{new_clean[:25]}...'")

    return new_clean, None


def main():
    conn = db()
    rows = conn.execute("SELECT id, title, link, source FROM articles").fetchall()
    targets = [r for r in rows if is_truncated_title(r[1])]

    print(f"[retitle] 전체 {len(rows)}건 중 잘린 제목 {len(targets)}건 대상 "
          f"({'APPLY=1, 실제 기록' if APPLY else 'DRY-RUN, DB 미기록'})")

    ok = fail = 0
    for aid, title, link, source in targets:
        new_title, err = recover_title(title, link)
        before = clean_title_display(title)
        if new_title is None:
            fail += 1
            print(f"[실패] ({source}) {err}")
            print(f"   전: {before}")
        else:
            ok += 1
            print(f"[성공] ({source})")
            print(f"   전: {before}")
            print(f"   후: {new_title}")
            if APPLY:
                conn.execute("UPDATE articles SET title=? WHERE id=?", (new_title, aid))
        time.sleep(SLEEP_BETWEEN)

    if APPLY:
        conn.commit()

    tail = "" if APPLY else " — 드라이런이므로 DB는 그대로. APPLY=1로 다시 돌리면 기록됨."
    print(f"[retitle] 완료: 성공 {ok} / 실패 {fail} / 대상 {len(targets)}{tail}")


if __name__ == "__main__":
    main()
