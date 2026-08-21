#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill.py — 임의 시간 구간 소급 수집 (일회성)

목적:
  duty 모드가 꺼져 있던 구간의 특정 부처 기사를 뒤늦게 확인하기 위한 도구.
  check가 그 키워드로 수집을 안 했으면 DB에 아무것도 없으므로, digest 시간 구간을
  아무리 조정해도 나오지 않는다. 네이버 API로 새로 긁어오는 수밖에 없다.

설계 원칙 (중요):
  - **DB를 건드리지 않는다.** articles/excluded_log 모두 쓰지 않고 읽지도 않는다.
    news_monitor.db를 수정하면 git push 경합(인수인계 4항 '바이너리 DB 충돌')이
    재발할 수 있고, 이 스크립트는 일회성이라 DB에 남길 이유가 없다.
    → check/digest와 동시에 돌려도 안전하다.
  - 필터 함수는 news_monitor에서 import한다. 배포본이 어떤 버전이든 그쪽 로직을
    그대로 따라가므로 이 파일이 필터 규칙을 이중 관리하지 않는다.

사용:
  python backfill.py                        # 어제 20:00 ~ 지금, 기본 3개 부처
  python backfill.py "2026-08-20 20:00"
  python backfill.py "2026-08-20 20:00" "2026-08-21 09:30"
  python backfill.py --groups 국토부,농식품부 "2026-08-20 20:00"

환경변수: NAVER_CLIENT_ID / NAVER_CLIENT_SECRET,
         TELEGRAM_DIGEST_BOT_TOKEN / TELEGRAM_DIGEST_CHAT_ID
         (없으면 콘솔 출력으로 폴백 — notify()가 알아서 처리)
"""

import sys, os, re, json, datetime, urllib.parse
import xml.etree.ElementTree as ET

from news_monitor import (
    KST, clean, clean_title_display, priority_mark,
    media_allowed, media_name, short_media_name,
    is_photo_article, is_junk_title, is_truncated_title,
    dedup_group, cluster_by_topic, http_get, article_id,
    notify, tg_escape, strip_antipatterns,
    DUTY_KEYWORDS, DUTY_KEYWORD_GROUPS,
    NAVER_CLIENT_ID, NAVER_CLIENT_SECRET,
)

# ==================== 이번 소급 대상 ====================
# 기본값. --groups 인자로 덮어쓸 수 있다.
DEFAULT_GROUPS = ["산업부", "중기부", "개인정보위"]

def keywords_for(groups):
    """그룹명 목록 → 검색 키워드 목록. 매핑은 news_monitor의 DUTY_KEYWORD_GROUPS를
    그대로 쓴다 — 이 파일이 부처 약칭을 따로 관리하면 배포본과 어긋난다."""
    unknown = [g for g in groups if g not in set(DUTY_KEYWORD_GROUPS.values())]
    if unknown:
        raise SystemExit(f"모르는 그룹: {', '.join(unknown)}  "
                         f"(가능: {', '.join(dict.fromkeys(DUTY_KEYWORD_GROUPS.values()))})")
    out = {}
    for g in groups:
        out[g] = [k for k in DUTY_KEYWORDS if DUTY_KEYWORD_GROUPS[k] == g]
    return out


def kw_match(title, kws):
    """news_monitor.run_check()와 **동일한** 판정을 한다.
    네이버는 본문까지 검색하므로 제목에 부처명이 없는 기사가 대다수인데,
    check는 그걸 살린다(검색어를 그대로 신뢰). backfill이 제목 매칭만 인정하면
    같은 구간인데도 check보다 훨씬 적게 잡혀 '소급'이 성립하지 않는다.
      - 제목에 키워드가 있고 오탐 표현이 아님        → 매칭
      - 제목에 어떤 키워드도 없음(본문 매칭)          → 매칭 (check와 동일)
      - 제목에 있지만 오탐 표현뿐('산업부문' 등)      → 탈락
    """
    t = clean(title or "")
    t = t.rsplit(" - ", 1)[0].strip()
    for kw in kws:
        if kw in strip_antipatterns(t, kw):
            return True
    if not any(kw in t for kw in kws):
        return True          # 제목엔 없고 본문에만 — check가 살리는 경로
    return False


MAX_PAGES = 10          # 네이버 start 한도(1000) 기준 최대치. 구간이 길어 필요함.
GOOGLE_ENABLED = True   # 보조. 구글 RSS는 구간 지정이 안 되므로 받은 뒤 걸러냄.


def fetch_naver_range(keyword, since, until, seen):
    """최신순 페이지네이션. pub_dt가 since 이전으로 내려가면 즉시 중단."""
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET):
        print("[warn] 네이버 API 키 없음 — 네이버 수집 건너뜀")
        return []
    out = []
    for page in range(MAX_PAGES):
        start = page * 100 + 1
        url = ("https://openapi.naver.com/v1/search/news.json?query=" +
               urllib.parse.quote(keyword) + f"&display=100&start={start}&sort=date")
        try:
            raw = http_get(url, headers={
                "X-Naver-Client-Id": NAVER_CLIENT_ID,
                "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
            })
            items = json.loads(raw).get("items", [])
        except Exception as e:
            print(f"[warn] naver '{keyword}' p{page+1}: {e}")
            break
        if not items:
            break
        oldest = None
        for it in items:
            link = it.get("originallink") or it.get("link") or ""
            title = clean(it.get("title"))
            try:
                pub = datetime.datetime.strptime(
                    it.get("pubDate", ""), "%a, %d %b %Y %H:%M:%S %z").astimezone(KST)
            except Exception:
                continue
            oldest = pub if oldest is None else min(oldest, pub)
            if not (since <= pub <= until):
                continue
            aid = article_id(link, title)
            if aid in seen:
                continue
            seen.add(aid)
            out.append(dict(title=title, link=link,
                            source=urllib.parse.urlparse(link).netloc,
                            pub_dt=pub.isoformat()))
        # 이 페이지의 가장 오래된 기사가 구간 시작보다 앞이면 더 거슬러 갈 필요 없음
        if oldest is not None and oldest < since:
            break
    else:
        print(f"[warn] '{keyword}': {MAX_PAGES}페이지를 다 썼는데도 구간 시작에 못 닿음. "
              f"이 키워드는 구간 앞부분이 누락됐을 수 있음.")
    return out


def fetch_google_range(keyword, since, until, seen):
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
        link = item.findtext("link") or ""
        try:
            pub = datetime.datetime.strptime(
                item.findtext("pubDate", ""), "%a, %d %b %Y %H:%M:%S %Z"
            ).replace(tzinfo=datetime.timezone.utc).astimezone(KST)
        except Exception:
            continue
        if not (since <= pub <= until):
            continue
        aid = article_id(link, title)
        if aid in seen:
            continue
        seen.add(aid)
        src = title.rsplit(" - ", 1)[1] if " - " in title else ""
        out.append(dict(title=title, link=link, source=src, pub_dt=pub.isoformat()))
    return out


def collect(since, until, group_map):
    """그룹 → ([기사], [(제외기사, 사유)])."""
    seen = set()
    by_group, excluded, stats = {}, {}, {}
    for g, kws in group_map.items():
        raw = []
        for kw in kws:
            raw += fetch_naver_range(kw, since, until, seen)
            if GOOGLE_ENABLED:
                raw += fetch_google_range(kw, since, until, seen)
        n_raw = len(raw)
        raw = [it for it in raw if kw_match(it["title"], kws)]
        n_kw = len(raw)
        raw = [it for it in raw if media_allowed(it["source"])]
        n_media = len(raw)
        # digest와 같은 기준: 누락 방지가 목적이므로 strict=True
        keep, drop = [], []
        for it in raw:
            if is_photo_article(it["title"], it["link"], strict=True):
                drop.append((it, "사진"))
            elif is_junk_title(it["title"]):
                drop.append((it, "무의미"))
            else:
                keep.append(it)
        by_group[g] = sorted(keep, key=lambda x: x["pub_dt"])
        excluded[g] = drop
        stats[g] = (n_raw, n_kw, n_media, len(keep))
    return by_group, excluded, stats


def render(by_group, excluded, stats, group_map, since, until, as_html=True):
    esc = tg_escape if as_html else (lambda s: s)
    head = ("📌 <b>소급 수집</b> | " if as_html else "📌 소급 수집 | ") + \
           f"{since.strftime('%m/%d %H:%M')} ~ {until.strftime('%m/%d %H:%M')}"
    lines = [head, "PLACEHOLDER"]
    total = 0
    shown_titles = set()
    for g in group_map:
        arts = by_group.get(g, [])
        if not arts:
            continue
        tuples = [(a["title"], a["link"], a["source"], a["pub_dt"]) for a in arts]
        grouped = dedup_group(tuples)
        clusters = cluster_by_topic(grouped, lambda gs: gs[0][0])

        def clu_rank(clu):
            best = min({"🔥": 0, "⚡": 1}.get(priority_mark(gs[0][0]), 2) for gs in clu)
            return (best, -len(clu))
        clusters.sort(key=clu_rank)

        sec_pos = len(lines)
        lines.append(None)
        sec = 0
        for clu in clusters:
            clu.sort(key=lambda gs: gs[0][3])
            started = False
            for rep, sources in clu:
                t = clean_title_display(rep[0])
                if is_truncated_title(t):
                    alt = next((clean_title_display(g2[0][0]) for g2 in clu
                                if not is_truncated_title(clean_title_display(g2[0][0]))), None)
                    if alt:
                        t = alt
                tkey = re.sub(r"[\s\W]+", "", t)
                if tkey in shown_titles:
                    continue
                shown_titles.add(tkey)
                sec += 1
                total += 1
                if sec > 1 and not started:
                    lines.append("")
                started = True
                mark = priority_mark(rep[0])
                mp = f"{mark} " if mark else ""
                src = short_media_name(media_name(rep[2]))
                hhmm = rep[3][11:16]
                lines.append(f"{mp}{esc(t)}({esc(src)} {hhmm})")
                if rep[1]:
                    lines.append(f'<a href="{esc(rep[1])}">🔗 원문</a>' if as_html else rep[1])
        if sec == 0:
            del lines[sec_pos]
        else:
            lines[sec_pos] = (f"\n■ <b>{esc(g)}</b> ({sec}건)" if as_html
                              else f"\n■ {g} ({sec}건)")

    if total == 0:
        lines.append("\n(구간 내 해당 기사 없음)")
    # 필터로 걸러낸 기사의 실체 — 숫자만으로는 과필터인지 알 수 없다.
    drops = [(g, it, tag) for g in group_map for (it, tag) in excluded.get(g, [])]
    if drops:
        txt = f"제외 {len(drops)}건 (사진/무의미제목)"
        lines.append(f"\n<i>{esc(txt)}</i>" if as_html else f"\n({txt})")
        for g, it, tag in drops:
            ttl = clean_title_display(it["title"])
            src = short_media_name(media_name(it["source"]))
            if as_html:
                lines.append(f'<i>[{tag}] {esc(ttl)}({esc(src)})</i> '
                             f'<a href="{esc(it["link"])}">🔗</a>')
            else:
                lines.append(f"[{tag}] {ttl}({src})\n{it['link']}")

    # 단계별 카운터 — 0건이 나왔을 때 어느 단계에서 죽었는지 바로 보이게
    diag = ["", "[단계별] 원수집 → 오탐제외 → 화이트리스트 → 필터통과"]
    for g, (a, b, c, d) in stats.items():
        diag.append(f"  {g}: {a} → {b} → {c} → {d}")
    lines.append("\n" + ("<i>" + esc("\n".join(diag)) + "</i>" if as_html
                         else "\n".join(diag)))
    lines[1] = f"총 {total}건\n" + ("=" * (30 if as_html else 40))
    return "\n".join(lines)


def parse_dt(s):
    s = s.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%m-%d %H:%M", "%H:%M"):
        try:
            d = datetime.datetime.strptime(s, fmt)
            now = datetime.datetime.now(KST)
            if fmt == "%H:%M":
                d = d.replace(year=now.year, month=now.month, day=now.day)
            elif fmt == "%m-%d %H:%M":
                d = d.replace(year=now.year)
            return d.replace(tzinfo=KST)
        except ValueError:
            continue
    raise SystemExit(f"시각 형식을 못 읽음: {s}  (예: \"2026-08-20 20:00\")")


if __name__ == "__main__":
    now = datetime.datetime.now(KST)
    argv = sys.argv[1:]
    groups = list(DEFAULT_GROUPS)
    if "--groups" in argv:
        i = argv.index("--groups")
        groups = [x for x in re.split(r"[,\s]+", argv[i + 1]) if x]
        del argv[i:i + 2]
    group_map = keywords_for(groups)
    if argv:
        since = parse_dt(argv[0])
    else:
        since = datetime.datetime.combine(
            now.date() - datetime.timedelta(days=1), datetime.time(20, 0), KST)
    until = parse_dt(argv[1]) if len(argv) > 1 else now
    if since >= until:
        raise SystemExit("시작 시각이 종료 시각보다 늦습니다.")
    print(f"구간: {since} ~ {until} / 대상: {', '.join(groups)}")
    by_group, excluded, stats = collect(since, until, group_map)
    notify(render(by_group, excluded, stats, group_map, since, until, as_html=True),
           target="digest")
    print(render(by_group, excluded, stats, group_map, since, until, as_html=False))
