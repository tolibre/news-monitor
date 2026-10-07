# -*- coding: utf-8 -*-
"""
render_page.py — 다이제스트 HTML 페이지 렌더러 (D 세션, 2026-09-19)

run_digest()가 이미 만든 섹션/클러스터 데이터를 받아 시안(사내 보고 양식 포함,
https://claude.ai/artifact/2eaSQR1Awokxq1CQf55cUR)과 동일한 구조의 HTML 한 장을
생성한다. news_monitor.py 쪽 로직(클러스터링·정렬·매체 축약 등)은 이 파일에서
재구현하지 않고 news_monitor.py에서 그대로 import해 재사용한다 — 판정 로직 이원화 방지
(retitle.py에서 이미 쓴 방식과 동일한 원칙).

이 파일은 news_monitor.py를 전혀 수정하지 않고 옆에서 동작한다. news_monitor.py 쪽의
유일한 변경은 PAGE_MODE 스위치이며, render_digest() 안에서 이미 계산된 그룹별 클러스터
리스트를 이 모듈의 build_groups_data()에 넘기기만 한다(계산 로직 재실행 없음 —
digest 출력과 페이지가 서로 다른 클러스터링 결과를 낼 위험 자체를 없앤다).
"""

import datetime
import html as _html
import json
import os
import re

# ==================== news_monitor.py에서 그대로 가져오는 것들 ====================
from news_monitor import (
    KST,
    clean_title_display,
    is_truncated_title,
    short_media_name,
    media_name,
    priority_mark,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PAGE_DIR = os.path.join(BASE_DIR, "docs")          # GitHub Pages 소스 디렉터리
PAGE_PATH = os.path.join(PAGE_DIR, "index.html")

# ---------- 아카이브 (0-17, 2026-09-20) ----------
# docs/index.html은 매 digest마다 덮어쓰이므로, 같은 HTML을 docs/archive/에 타임스탬프
# 이름으로 한 장 더 남기고 목록 페이지(docs/archive/index.html)를 매번 새로 굽는다.
# 목록은 별도 상태파일 없이 archive/ 안의 파일들을 "매번 전수 스캔"해서 만든다 —
# Actions 러너는 매 실행 새로 체크아웃하므로 커밋된 파일 외엔 아무 상태도 남지 않고,
# 목록과 실제 파일이 어긋날 여지 자체를 없애기 위함(자가치유).
ARCHIVE_DIRNAME = "archive"
ARCHIVE_DIR = os.path.join(PAGE_DIR, ARCHIVE_DIRNAME)
ARCHIVE_KEEP_DAYS = 30             # 이보다 오래된 아카이브 페이지는 삭제. 0이면 삭제 안 함.

SCOOP = "🔥"
FLASH = "⚡"


def _fmt_time(pub_dt_iso):
    """pub_dt(ISO 문자열) → 'HH:MM' 표시. 파싱 실패 시 빈 문자열."""
    try:
        dt = datetime.datetime.fromisoformat(pub_dt_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt.astimezone(KST).strftime("%H:%M")
    except Exception:
        return ""


def _resolve_title(rep_title, clu_titles):
    """digest 본문과 동일한 잘린 제목 대체 로직 — clean_title_display 후 잘려 있으면
    같은 클러스터 안의 온전한 제목으로 바꾼다. render_digest()의 해당 블록과 동일하게
    유지할 것(로직을 두 곳에 따로 두면 화면과 페이지가 어긋난다)."""
    t = clean_title_display(rep_title)
    if is_truncated_title(t):
        alt = next(
            (clean_title_display(t2) for t2 in clu_titles
             if not is_truncated_title(clean_title_display(t2))),
            None,
        )
        if alt:
            t = alt
    return t


def build_groups_data(group_sections):
    """group_sections: [(group_name, sorted_clusters), ...] 형태의 리스트.
    sorted_clusters: render_digest()에서 clu_rank로 정렬을 마친 clusters 리스트 그대로
    (각 clu는 article_rank로 이미 정렬돼 있는 [(rep, sources), ...] 리스트).

    반환값은 시안 아티팩트의 data JSON과 동일한 구조:
      {"groups": [{"name":.., "clusters": [[{t,s,l,m,p,n}, ...], ...], "n": N}, ...]}
    """
    groups_out = []
    for gname, clusters in group_sections:
        clu_out = []
        gcount = 0
        for clu in clusters:
            clu_titles = [rep[0] for rep, _ in clu]
            items = []
            seen_tkeys = set()
            for rep, sources in clu:
                title, link, source, pub_dt = rep[0], rep[1], rep[2], rep[3]
                t = _resolve_title(title, clu_titles)
                # render_digest()와 동일한 완전 중복 억제(같은 사안 대체로 앞줄과
                # 완전히 같아진 경우) — 페이지에도 같은 줄이 두 번 나오지 않게 한다.
                import re as _re
                tkey = _re.sub(r"[\s\W]+", "", t)
                if tkey in seen_tkeys:
                    continue
                seen_tkeys.add(tkey)
                items.append({
                    "t": t,
                    "s": short_media_name(media_name(source)),
                    "l": link or "",
                    "m": priority_mark(title),
                    "p": _fmt_time(pub_dt),
                    "n": len(sources) if sources else 1,
                })
            if not items:
                continue
            clu_out.append(items)
            gcount += len(items)
        if gcount == 0:
            continue
        groups_out.append({"name": gname, "clusters": clu_out, "n": gcount})
    return groups_out


def render_html(label, start, end, raw_count, groups, nav=""):
    """groups: build_groups_data()의 반환값. 시안 HTML을 그대로 옮기고
    <script id="data"> 안의 JSON만 교체한다. CSS/구조/JS는 시안과 동일하게 유지 —
    D 세션은 렌더링(데이터 주입)만 한다.

    nav: 머리말에 끼울 링크 줄 HTML(없으면 빈 줄 — 0-17 이전과 동일한 모양).
    같은 본문을 index.html용/아카이브용으로 두 번 렌더할 때 이 값만 달라진다."""
    data = {
        "label": label,
        "start": start,
        "end": end,
        "raw": raw_count,
        "groups": groups,
    }
    data_json = json.dumps(data, ensure_ascii=False)
    # JSON을 <script type="application/json"> 안에 넣을 때 "</script" 이스케이프 필수
    data_json_safe = data_json.replace("</", "<\\/")
    label_esc = _html.escape(label)

    return (_TEMPLATE
            .replace("__LABEL__", label_esc)
            .replace("__NAV__", nav or "")
            .replace("__DATA_JSON__", data_json_safe))


def save_page(html_text, out_path=PAGE_PATH):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_text)
    return out_path


# ==================== 아카이브 (0-17, 2026-09-20) ====================
# 머리말 링크 줄. index.html은 아카이브 목록으로, 아카이브 페이지는 목록·최신 둘 다로.
# (아카이브 페이지는 docs/archive/ 안에 있으므로 "./"=목록, "../"=최신)
_NAV_INDEX = '    <div class="navline"><a href="%s/">지난 다이제스트 \u203a</a></div>' % ARCHIVE_DIRNAME
_NAV_ARCHIVED = ('    <div class="navline"><a href="./">\u2039 지난 다이제스트</a>'
                 '<a href="../">\u21bb 최신 다이제스트</a></div>')

_STAMP_RE = re.compile(r"^(\d{8})_(\d{4})\.html$")
_DATA_RE = re.compile(r'<script id="data" type="application/json">(.*?)</script>', re.S)
_WEEKDAY = "월화수목금토일"


def archive_stamp(when):
    """아카이브 파일명 stamp — digest 구간의 끝 시각 기준. 예: 20260920_1730"""
    return when.strftime("%Y%m%d_%H%M")


def archive_path(when):
    return os.path.join(ARCHIVE_DIR, archive_stamp(when) + ".html")


def _read_entry(fname):
    """아카이브 페이지 한 장에서 목록에 쓸 정보를 뽑는다. 못 읽으면 None(=목록에서 조용히 제외).
    페이지 안에 이미 박혀 있는 data JSON을 그대로 읽으므로 별도 메타파일이 필요 없다."""
    m = _STAMP_RE.match(fname)
    if not m:
        return None
    try:
        with open(os.path.join(ARCHIVE_DIR, fname), encoding="utf-8") as f:
            text = f.read()
        dm = _DATA_RE.search(text)
        if not dm:
            return None
        # render_html()에서 "</" -> "<\/" 로 이스케이프한 것을 되돌린다.
        data = json.loads(dm.group(1).replace("<\\/", "</"))
    except Exception:
        return None

    total = scoop = flash = 0
    for g in data.get("groups", []):
        for clu in g.get("clusters", []):
            for it in clu:
                total += 1
                if it.get("m") == SCOOP:
                    scoop += 1
                elif it.get("m") == FLASH:
                    flash += 1

    d, t = m.group(1), m.group(2)
    try:
        wd = _WEEKDAY[datetime.date(int(d[:4]), int(d[4:6]), int(d[6:])).weekday()]
    except ValueError:
        wd = ""
    return {
        "file": fname,
        "daykey": d,
        "dayname": "%s/%s (%s)" % (d[4:6], d[6:], wd),
        "label": data.get("label", ""),
        "start": data.get("start", ""),
        "end": data.get("end", ""),
        "raw": data.get("raw", 0),
        "total": total,
        "scoop": scoop,
        "flash": flash,
        "groups": [(g.get("name", ""), g.get("n", 0)) for g in data.get("groups", [])],
    }


def scan_archive():
    """archive/ 전수 스캔 → 최신순 엔트리 리스트. 목록 페이지는 항상 이걸로 새로 굽는다."""
    if not os.path.isdir(ARCHIVE_DIR):
        return []
    entries = []
    for fname in os.listdir(ARCHIVE_DIR):
        e = _read_entry(fname)
        if e:
            entries.append(e)
    entries.sort(key=lambda e: e["file"], reverse=True)
    return entries


def prune_archive(now=None):
    """ARCHIVE_KEEP_DAYS보다 오래된 아카이브 페이지 삭제. 삭제 건수 반환.
    (DB의 prune_old와 같은 취지 — 저장소가 무한정 커지지 않게)"""
    if ARCHIVE_KEEP_DAYS <= 0 or not os.path.isdir(ARCHIVE_DIR):
        return 0
    now = now or datetime.datetime.now(KST)
    cutoff = (now - datetime.timedelta(days=ARCHIVE_KEEP_DAYS)).strftime("%Y%m%d")
    removed = 0
    for fname in os.listdir(ARCHIVE_DIR):
        m = _STAMP_RE.match(fname)
        if m and m.group(1) < cutoff:
            try:
                os.remove(os.path.join(ARCHIVE_DIR, fname))
                removed += 1
            except OSError:
                pass
    return removed


def render_archive_index(entries):
    """아카이브 목록 페이지 HTML. 날짜별로 묶고 최신 날짜가 위."""
    esc = _html.escape
    blocks = []
    cur = None
    cards = []

    def flush():
        if cur and cards:
            blocks.append('<section><h2>%s</h2>%s</section>' % (esc(cur), "".join(cards)))

    for e in entries:
        if e["dayname"] != cur:
            flush()
            cur = e["dayname"]
            cards = []
        marks = []
        if e["scoop"]:
            marks.append('<span class="t-scoop">단독 %d</span>' % e["scoop"])
        if e["flash"]:
            marks.append('<span class="t-flash">속보 %d</span>' % e["flash"])
        beats = " · ".join("%s %d" % (esc(n), c) for n, c in e["groups"] if c)
        cards.append(
            '<a class="card" href="%s">'
            '<div class="ttl">%s <span class="win mono">%s ~ %s</span></div>'
            '<div class="sub"><b class="mono">%d</b>건%s<span class="dim">원문 %d</span></div>'
            '%s</a>' % (
                esc(e["file"]), esc(e["label"]), esc(e["start"]), esc(e["end"]),
                e["total"],
                ("".join("<span>%s</span>" % m for m in marks)),
                e["raw"],
                ('<div class="beats">%s</div>' % beats) if beats else "",
            )
        )
    flush()

    body = "".join(blocks) or '<p class="empty">아직 보관된 다이제스트가 없습니다.</p>'
    return (_ARCHIVE_TEMPLATE
            .replace("__COUNT__", str(len(entries)))
            .replace("__KEEP__", str(ARCHIVE_KEEP_DAYS))
            .replace("__ROWS__", body))


def save_archive_index(entries=None):
    entries = scan_archive() if entries is None else entries
    return save_page(render_archive_index(entries),
                     out_path=os.path.join(ARCHIVE_DIR, "index.html"))


def adopt_existing_index(now=None):
    """docs/index.html이 이미 있는데 아카이브에 같은 판본이 없으면, 덮어쓰기 전에
    아카이브로 옮겨 담는다. 0-17 이전에 만들어져 아카이브가 없는 페이지(지금 공개돼
    있는 그 한 장)를 다음 실행 때 자동으로 살려내기 위한 것이고, 이후에도
    "아카이브가 없는 index.html은 잃지 않는다"는 안전망으로 계속 남는다.

    stamp의 연도는 data JSON에 없으므로(구간 표기가 MM/DD HH:MM뿐) 실행 시각의
    연도를 쓰되, 그러면 미래가 되는 경우(연말·연초 경계)만 한 해 뺀다.
    반환: 옮겨 담은 파일 경로 또는 None."""
    if not os.path.isfile(PAGE_PATH):
        return None
    try:
        with open(PAGE_PATH, encoding="utf-8") as f:
            text = f.read()
        dm = _DATA_RE.search(text)
        if not dm:
            return None
        data = json.loads(dm.group(1).replace("<\\/", "</"))
        end = data.get("end", "")
        mmdd, hhmm = end.split()
        mm, dd = mmdd.split("/")
        hh, mi = hhmm.split(":")
    except Exception:
        return None

    now = now or datetime.datetime.now(KST)
    year = now.year
    try:
        when = datetime.datetime(year, int(mm), int(dd), int(hh), int(mi), tzinfo=KST)
        if when > now + datetime.timedelta(days=1):
            when = when.replace(year=year - 1)
    except ValueError:
        return None

    out = archive_path(when)
    if os.path.exists(out):
        return None                     # 이미 보관돼 있음 — 아무 것도 하지 않는다

    save_page(render_html(data.get("label", ""), data.get("start", ""), end,
                          data.get("raw", 0), data.get("groups", []),
                          nav=_NAV_ARCHIVED),
              out_path=out)
    return out


def publish(label, start_dt, end_dt, raw_count, groups, now=None):
    """0-17 이후 페이지 저장의 단일 진입점. news_monitor.py는 이것만 부르면 된다.

      ① docs/index.html            — 최신 다이제스트(지금까지와 동일한 주소)
      ② docs/archive/<stamp>.html  — 같은 내용의 보존본(덮어쓰이지 않음)
      ③ docs/archive/index.html    — 전수 스캔으로 매번 새로 굽는 목록
      ④ 오래된 보존본 정리

    반환값은 ①의 경로 — 기존 save_page() 반환값과 같은 의미라, 호출부의
    "페이지가 실제로 생성됐는가" 판정(both 모드 링크)을 그대로 쓸 수 있다.
    start_dt/end_dt는 datetime 권장(문자열이면 아카이브 없이 ①만 쓴다)."""
    has_dt = hasattr(end_dt, "strftime")
    s = start_dt.strftime("%m/%d %H:%M") if hasattr(start_dt, "strftime") else str(start_dt)
    e = end_dt.strftime("%m/%d %H:%M") if has_dt else str(end_dt)

    # 덮어쓰기 전에 — 아카이브가 없는 기존 index.html이 있으면 먼저 건져 둔다.
    adopted = adopt_existing_index(now or (end_dt if has_dt else None))
    if adopted:
        print(f"[archive] 기존 페이지 보관: {os.path.basename(adopted)}")

    page_path = save_page(render_html(label, s, e, raw_count, groups, nav=_NAV_INDEX))
    if not has_dt:
        return page_path

    save_page(render_html(label, s, e, raw_count, groups, nav=_NAV_ARCHIVED),
              out_path=archive_path(end_dt))
    prune_archive(now or end_dt)
    save_archive_index()
    return page_path


def summary_card_text(label, start, end, groups, page_url=""):
    """PAGE_MODE=summary일 때 텔레그램에 보낼 요약 카드 1개.
    기관별 건수 + 단독·속보 + 페이지 링크. HTML parse_mode 기준."""
    from news_monitor import tg_escape

    total = sum(g["n"] for g in groups)
    scoop = flash = 0
    for g in groups:
        for clu in g["clusters"]:
            for it in clu:
                if it["m"] == SCOOP:
                    scoop += 1
                elif it["m"] == FLASH:
                    flash += 1

    lines = [
        f"📋 <b>{tg_escape(label)}</b> | {start} ~ {end}",
        f"주요 이슈 {total}건 · 단독 {scoop} · 속보 {flash}",
        "",
    ]
    for g in groups:
        if g["n"]:
            lines.append(f"■ {tg_escape(g['name'])} {g['n']}건")
    if page_url:
        lines.append("")
        lines.append(f'<a href="{tg_escape(page_url)}">🔗 전체 보기</a>')
    return "\n".join(lines)


# ==================== HTML 템플릿 (시안 그대로, data만 치환) ====================
_TEMPLATE = r"""<!doctype html><html><head><meta charset=utf8><meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover"><style>:root{color-scheme:light;box-sizing:border-box;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}html{scroll-padding-top:env(safe-area-inset-top,0px)}body{margin:0;padding:0;font:14px -apple-system,BlinkMacSystemFont,sans-serif;background:#faf9f5;color:#141413}img{max-width:100%}[hidden]:not([hidden=until-found i]){display:none!important}</style></head><body>
<title>__LABEL__</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+KR:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
:root{
  --ground:#eceff1; --surface:#ffffff; --surface-2:#f5f7f8;
  --ink:#131a20; --ink-2:#3d4a54; --muted:#6b7b86;
  --line:#d2dade; --line-soft:#e3e9ec;
  --accent:#1c5f88; --accent-soft:#e2edf4;
  --scoop:#b8430e; --scoop-soft:#fbe9df;
  --flash:#a81f1f; --flash-soft:#fae4e4;
  --pick:#0f6f52; --pick-soft:#e3f2ec;
  --shadow:0 2px 10px rgba(19,26,32,.10);
  --veil:rgba(19,26,32,.45);
}
@media (prefers-color-scheme: dark){ :root:not([data-theme="light"]){
  --ground:#0e1418; --surface:#151d23; --surface-2:#1b242b;
  --ink:#e7eef2; --ink-2:#bccad3; --muted:#8497a3;
  --line:#26333b; --line-soft:#1f2a31;
  --accent:#63aedd; --accent-soft:#16303f;
  --scoop:#f28c52; --scoop-soft:#3a2115;
  --flash:#ef7070; --flash-soft:#3a1a1a;
  --pick:#57c39a; --pick-soft:#15332a;
  --shadow:0 2px 12px rgba(0,0,0,.5);
  --veil:rgba(0,0,0,.6);
}}
:root[data-theme="dark"]{
  --ground:#0e1418; --surface:#151d23; --surface-2:#1b242b;
  --ink:#e7eef2; --ink-2:#bccad3; --muted:#8497a3;
  --line:#26333b; --line-soft:#1f2a31;
  --accent:#63aedd; --accent-soft:#16303f;
  --scoop:#f28c52; --scoop-soft:#3a2115;
  --flash:#ef7070; --flash-soft:#3a1a1a;
  --pick:#57c39a; --pick-soft:#15332a;
  --shadow:0 2px 12px rgba(0,0,0,.5);
  --veil:rgba(0,0,0,.6);
}
*{box-sizing:border-box}
[hidden]{display:none !important}
body{
  margin:0; background:var(--ground); color:var(--ink);
  font-family:'IBM Plex Sans KR',system-ui,-apple-system,'Apple SD Gothic Neo','Malgun Gothic',sans-serif;
  font-size:15px; line-height:1.5; -webkit-text-size-adjust:100%;
}
.mono{font-family:'IBM Plex Mono','SFMono-Regular',Menlo,monospace;font-variant-numeric:tabular-nums}

header{
  position:sticky; top:env(safe-area-inset-top,0px); z-index:20;
  background:var(--surface); border-bottom:1px solid var(--line);
}
.wrap{max-width:1000px; margin:0 auto; padding-left:16px; padding-right:16px}
.masthead{padding-block:14px 10px; display:flex; flex-wrap:wrap; align-items:baseline; gap:8px 14px}
h1{margin:0; font-size:19px; font-weight:700; letter-spacing:-.01em}
.window{font-size:12.5px; color:var(--muted); letter-spacing:.02em}
.navline{display:flex; flex-wrap:wrap; gap:8px; align-items:center; padding-block:10px 0}
.navline a{
  font-size:12px; font-weight:500; text-decoration:none;
  color:var(--accent); background:var(--accent-soft);
  border:1px solid transparent; border-radius:999px; padding:4px 11px;
}
.navline a:hover{border-color:var(--accent)}
.tallies{display:flex; flex-wrap:wrap; gap:6px 16px; margin-left:auto; font-size:12.5px; color:var(--ink-2)}
.tallies b{font-weight:600; font-size:14px}
.t-scoop b{color:var(--scoop)} .t-flash b{color:var(--flash)}

.beats{display:flex; gap:6px; overflow-x:auto; padding-block:0 10px; scrollbar-width:thin}
.beat{
  flex:0 0 auto; display:flex; align-items:baseline; gap:6px;
  border:1px solid var(--line); background:var(--surface-2); color:var(--ink-2);
  border-radius:999px; padding:5px 11px; font-size:13px; font-weight:500;
  cursor:pointer; white-space:nowrap;
}
.beat:hover{border-color:var(--accent); color:var(--ink)}
.beat .c{font-size:11.5px; color:var(--muted)}
.beat[aria-pressed="true"]{background:var(--accent); border-color:var(--accent); color:#fff}
.beat[aria-pressed="true"] .c{color:rgba(255,255,255,.8)}

.controls{display:flex; flex-wrap:wrap; gap:8px; padding-block:0 12px; align-items:center}
.seg{display:flex; border:1px solid var(--line); border-radius:7px; overflow:hidden}
.seg button{
  border:0; background:var(--surface); color:var(--ink-2);
  font:inherit; font-size:12.5px; padding:6px 12px; cursor:pointer;
  border-right:1px solid var(--line);
}
.seg button:last-child{border-right:0}
.seg button[aria-pressed="true"]{background:var(--accent-soft); color:var(--accent); font-weight:600}
#q{
  flex:1 1 180px; min-width:0; border:1px solid var(--line); border-radius:7px;
  background:var(--surface); color:var(--ink); font:inherit; font-size:13px; padding:6px 10px;
}
#q::placeholder{color:var(--muted)}
#q:focus,.seg button:focus-visible,.beat:focus-visible,.more:focus-visible,
.bar button:focus-visible,.panel button:focus-visible{outline:2px solid var(--accent); outline-offset:1px}

main{padding-block:0 96px}
section{margin-top:26px; scroll-margin-top:170px}
.sec-head{
  display:flex; align-items:baseline; gap:9px;
  padding-bottom:7px; border-bottom:2px solid var(--ink); margin-bottom:2px;
}
.sec-head h2{margin:0; font-size:15.5px; font-weight:700; letter-spacing:-.005em}
.sec-head .n{font-size:12px; color:var(--muted)}
.topic{
  background:var(--surface); border-bottom:1px solid var(--line-soft);
  padding:9px 12px 9px 11px;
}
.topic.pinned{border-left:3px solid var(--scoop); padding-left:8px}
.topic.pinned.flash{border-left-color:var(--flash)}
.row{display:flex; gap:8px; align-items:baseline; padding:2px 4px; border-radius:5px}
.row.on{background:var(--pick-soft)}
.pick{
  flex:0 0 auto; width:15px; height:15px; margin:0; cursor:pointer;
  accent-color:var(--pick); transform:translateY(2px);
}
.tag{
  flex:0 0 auto; font-size:10.5px; font-weight:600; letter-spacing:.04em;
  padding:1.5px 6px; border-radius:4px; transform:translateY(-1px);
}
.tag.scoop{background:var(--scoop-soft); color:var(--scoop)}
.tag.flash{background:var(--flash-soft); color:var(--flash)}
a.title{
  color:var(--ink); text-decoration:none; font-size:14.5px; line-height:1.45;
  text-underline-offset:3px;
}
a.title:hover{text-decoration:underline; text-decoration-color:var(--accent)}
.meta{
  flex:0 0 auto; margin-left:auto; display:flex; gap:8px; align-items:baseline;
  font-size:11.5px; color:var(--muted); white-space:nowrap;
}
.more{
  margin-top:5px; margin-left:23px; border:0; background:none; padding:2px 0; cursor:pointer;
  font:inherit; font-size:11.5px; color:var(--accent); font-weight:500;
}
.more:hover{text-decoration:underline}
.dupes{margin-top:5px; margin-left:23px; padding-left:11px; border-left:1px solid var(--line); display:grid; gap:3px}
.dupes[hidden]{display:none}
.dupes a.title{font-size:13px; color:var(--ink-2)}
.empty{padding:40px 0; text-align:center; color:var(--muted); font-size:13.5px}

/* ---------- selection bar ---------- */
.bar{
  position:fixed; left:0; right:0; bottom:0; z-index:30;
  background:var(--surface); border-top:1px solid var(--line);
  box-shadow:var(--shadow);
  padding:10px 16px calc(10px + env(safe-area-inset-bottom,0px));
}
.bar-in{max-width:1000px; margin:0 auto; display:flex; gap:10px; align-items:center; flex-wrap:wrap}
.bar .count{font-size:13.5px; font-weight:600}
.bar .count b{color:var(--pick); font-size:16px}
.bar .spacer{margin-left:auto}
.btn{
  border:1px solid var(--line); background:var(--surface-2); color:var(--ink-2);
  font:inherit; font-size:13px; font-weight:500; padding:7px 14px; border-radius:7px; cursor:pointer;
}
.btn:hover{border-color:var(--accent); color:var(--ink)}
.btn.primary{background:var(--pick); border-color:var(--pick); color:#fff}
.btn.primary:hover{filter:brightness(1.08); color:#fff}

/* ---------- report panel ---------- */
.veil{
  position:fixed; inset:0; z-index:40; background:var(--veil);
  display:flex; align-items:flex-end; justify-content:center; padding:16px;
}
@media (min-width:640px){ .veil{align-items:center} }
.panel{
  background:var(--surface); border-radius:12px; width:100%; max-width:660px;
  max-height:86vh; display:flex; flex-direction:column; box-shadow:var(--shadow);
  border:1px solid var(--line);
}
.panel-head{
  display:flex; align-items:baseline; gap:10px; padding:14px 16px 10px;
  border-bottom:1px solid var(--line-soft);
}
.panel-head h3{margin:0; font-size:15px; font-weight:700}
.panel-head .sub{font-size:12px; color:var(--muted)}
.panel-head .x{margin-left:auto; border:0; background:none; cursor:pointer; color:var(--muted); font-size:20px; line-height:1; padding:0 2px}
#report{
  flex:1 1 auto; min-height:220px; margin:12px 16px; padding:12px;
  border:1px solid var(--line); border-radius:8px; resize:vertical;
  background:var(--surface-2); color:var(--ink);
  font-family:'IBM Plex Sans KR',system-ui,sans-serif; font-size:13.5px; line-height:1.8;
}
.panel-foot{display:flex; gap:10px; align-items:center; padding:0 16px 14px; flex-wrap:wrap}
.hint{font-size:11.5px; color:var(--muted)}
.hint.ok{color:var(--pick); font-weight:600}

@media (max-width:560px){
  .tallies{margin-left:0; width:100%}
  .meta{margin-left:0; width:100%; padding-left:23px}
  .row{flex-wrap:wrap}
  section{scroll-margin-top:200px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>

<header>
  <div class="wrap">
__NAV__
    <div class="masthead">
      <h1 id="label">__LABEL__</h1>
      <span class="window mono" id="window"></span>
      <div class="tallies">
        <span>표시 <b class="mono" id="n-shown">0</b>건</span>
        <span>주제 <b class="mono" id="n-topic">0</b>개</span>
        <span class="t-scoop">단독 <b class="mono" id="n-scoop">0</b></span>
        <span class="t-flash">속보 <b class="mono" id="n-flash">0</b></span>
        <span>원문 <b class="mono" id="n-raw">0</b>건</span>
      </div>
    </div>
    <div class="beats" id="beats"></div>
    <div class="controls">
      <div class="seg" role="group" aria-label="보기 범위">
        <button data-f="all" aria-pressed="true">전체</button>
        <button data-f="mark" aria-pressed="false">단독·속보</button>
        <button data-f="spread" aria-pressed="false">전재 2건+</button>
        <button data-f="picked" aria-pressed="false">선택분</button>
      </div>
      <input id="q" type="search" placeholder="제목·매체 검색 (예: 담합, 연합뉴스)" autocomplete="off">
    </div>
  </div>
</header>

<main class="wrap" id="main"></main>

<div class="bar" id="bar" hidden>
  <div class="bar-in">
    <span class="count"><b class="mono" id="n-pick">0</b>건 선택</span>
    <span class="hint">체크한 기사만 보고 양식으로 묶습니다</span>
    <span class="spacer"></span>
    <button class="btn" type="button" id="clear">선택 해제</button>
    <button class="btn primary" type="button" id="make">보고 양식 만들기</button>
  </div>
</div>

<div class="veil" id="veil" hidden>
  <div class="panel" role="dialog" aria-modal="true" aria-labelledby="ptitle">
    <div class="panel-head">
      <h3 id="ptitle">&lt;모니터&gt;</h3>
      <span class="sub" id="psub"></span>
      <button class="x" type="button" id="close" aria-label="닫기">&times;</button>
    </div>
    <textarea id="report" spellcheck="false"></textarea>
    <div class="panel-foot">
      <button class="btn primary" type="button" id="copy">복사</button>
      <button class="btn" type="button" id="selectall">전체 선택</button>
      <span class="hint" id="copyhint">단독 먼저, 그다음 출입처 순서(방미통위 → 공정위 → 과기정통부 → 우주청 → 2진). 직접 고쳐도 됩니다.</span>
    </div>
  </div>
</div>

<script id="data" type="application/json">__DATA_JSON__</script>
<script>
(function(){
  var D = JSON.parse(document.getElementById('data').textContent);
  var main = document.getElementById('main');
  var state = {beat:null, filter:'all', q:''};
  var picked = new Set();
  var REPORT_ORDER = ['방미통위','공정위','과기정통부','우주항공청'];
  var SCOOP = '🔥', FLASH = '⚡';

  // stable ids + flat index
  var ALL = {};
  D.groups.forEach(function(g, gi){
    g.clusters.forEach(function(c, ci){
      c.forEach(function(it, ii){
        it.id = gi+'-'+ci+'-'+ii;
        it.g = g.name;
        it.ord = gi*100000 + ci*100 + ii;
        ALL[it.id] = it;
      });
    });
  });

  document.getElementById('label').textContent = D.label;
  document.getElementById('window').textContent = D.start + ' — ' + D.end;
  document.getElementById('n-raw').textContent = D.raw.toLocaleString();

  var scoop=0, flash=0, topics=0;
  D.groups.forEach(function(g){
    g.clusters.forEach(function(c){
      topics++;
      c.forEach(function(it){ if(it.m===SCOOP) scoop++; else if(it.m===FLASH) flash++; });
    });
  });
  document.getElementById('n-topic').textContent = topics;
  document.getElementById('n-scoop').textContent = scoop;
  document.getElementById('n-flash').textContent = flash;

  var beats = document.getElementById('beats');
  D.groups.forEach(function(g){
    var b = document.createElement('button');
    b.className='beat'; b.type='button'; b.setAttribute('aria-pressed','false');
    b.innerHTML = '<span>'+g.name+'</span><span class="c mono">'+g.n+'</span>';
    b.addEventListener('click', function(){
      state.beat = (state.beat===g.name) ? null : g.name;
      [].forEach.call(beats.children, function(el,i){
        el.setAttribute('aria-pressed', String(D.groups[i].name===state.beat));
      });
      render();
    });
    beats.appendChild(b);
  });

  [].forEach.call(document.querySelectorAll('.seg button'), function(btn){
    btn.addEventListener('click', function(){
      state.filter = btn.dataset.f;
      [].forEach.call(document.querySelectorAll('.seg button'), function(b){
        b.setAttribute('aria-pressed', String(b===btn));
      });
      render();
    });
  });

  var q = document.getElementById('q'), timer;
  q.addEventListener('input', function(){
    clearTimeout(timer);
    timer = setTimeout(function(){ state.q = q.value.trim().toLowerCase(); render(); }, 120);
  });

  function keep(it){
    if(state.filter==='mark' && !it.m) return false;
    if(state.filter==='spread' && it.n < 2) return false;
    if(state.filter==='picked' && !picked.has(it.id)) return false;
    if(state.q && (it.t+' '+it.s).toLowerCase().indexOf(state.q) === -1) return false;
    return true;
  }
  function esc(s){ return String(s).replace(/[&<>"]/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }

  function itemRow(it){
    var tag = it.m===SCOOP ? '<span class="tag scoop">단독</span>'
            : it.m===FLASH ? '<span class="tag flash">속보</span>' : '';
    return '<div class="row'+(picked.has(it.id)?' on':'')+'" data-id="'+it.id+'">' +
      '<input class="pick" type="checkbox" id="p'+it.id+'" '+(picked.has(it.id)?'checked':'')+
      ' aria-label="보고에 포함">' + tag +
      '<a class="title" href="'+esc(it.l)+'" target="_blank" rel="noopener">'+esc(it.t)+'</a>' +
      '<span class="meta"><span>'+esc(it.s)+'</span><span class="mono">'+esc(it.p)+'</span>' +
      (it.n>1 ? '<span class="mono">전재'+it.n+'</span>' : '') + '</span></div>';
  }

  // 큰 묶음(30건 이상)은 접어도 몇 건인지 보이게 하고 기본 펼침으로 둔다.
  // (C 세션(0-14) 결과 — 접기 자체는 안전하나 53건짜리 묶음이 여전히 나오므로
  //  대표 1건 뒤에 대량이 숨는 화면을 피하기 위한 D 세션 자체 판단)
  var AUTO_EXPAND = 30;

  function render(){
    var html = '', shown = 0;
    D.groups.forEach(function(g){
      if(state.beat && g.name !== state.beat) return;
      var blocks = '', gcount = 0;
      g.clusters.forEach(function(c, ci){
        var items = c.filter(keep);
        if(!items.length) return;
        gcount += items.length; shown += items.length;
        var lead = items[0], rest = items.slice(1);
        var cls = 'topic' + (lead.m ? ' pinned' : '') + (lead.m===FLASH ? ' flash' : '');
        var body = itemRow(lead);
        if(rest.length){
          var id = 'c'+g.name.replace(/\s/g,'')+ci;
          var bigCluster = items.length >= AUTO_EXPAND;
          body += '<button class="more" type="button" aria-expanded="'+(bigCluster?'true':'false')+'" aria-controls="'+id+'">'+
                  (bigCluster ? '접기 (' : '+') + rest.length + '건 같은 사안' + (bigCluster ? ')' : '') + '</button>'+
                  '<div class="dupes" id="'+id+'"'+(bigCluster?'':' hidden')+'>'+rest.map(itemRow).join('')+'</div>';
        }
        blocks += '<div class="'+cls+'">'+body+'</div>';
      });
      if(!gcount) return;
      html += '<section id="sec-'+g.name+'"><div class="sec-head"><h2>'+g.name+'</h2>'+
              '<span class="n mono">'+gcount+'건</span></div>'+blocks+'</section>';
    });
    main.innerHTML = html || '<p class="empty">조건에 맞는 기사가 없습니다.</p>';
    document.getElementById('n-shown').textContent = shown;

    [].forEach.call(main.querySelectorAll('.more'), function(btn){
      btn.addEventListener('click', function(){
        var box = document.getElementById(btn.getAttribute('aria-controls'));
        var open = box.hidden;
        box.hidden = !open;
        btn.setAttribute('aria-expanded', String(open));
        btn.textContent = open ? '접기 ('+box.children.length+'건 같은 사안)' : '+'+box.children.length+'건 같은 사안';
      });
    });
  }

  // one delegated handler for checkboxes
  main.addEventListener('change', function(e){
    var cb = e.target;
    if(!cb.classList || !cb.classList.contains('pick')) return;
    var row = cb.closest('.row'), id = row.getAttribute('data-id');
    if(cb.checked){ picked.add(id); row.classList.add('on'); }
    else { picked.delete(id); row.classList.remove('on'); }
    syncBar();
  });

  function syncBar(){
    var n = picked.size;
    document.getElementById('n-pick').textContent = n;
    document.getElementById('bar').hidden = (n === 0);
  }

  function buildReport(){
    // 속보도 고를 수 있게 둔다 — 판정이 제목 말머리 기반이라 보고감이 섞인다.
    // 쓸지 말지는 고르는 단계에서 정한다.
    var items = [];
    picked.forEach(function(id){ if(ALL[id]) items.push(ALL[id]); });
    var rank = function(name){
      var i = REPORT_ORDER.indexOf(name);
      return i === -1 ? REPORT_ORDER.length : i;
    };
    items.sort(function(a,b){
      // 1) 단독 먼저  2) 출입처순  3) 중요도순
      var sa = (a.m === SCOOP) ? 0 : 1, sb = (b.m === SCOOP) ? 0 : 1;
      if(sa !== sb) return sa - sb;
      var ra = rank(a.g), rb = rank(b.g);
      if(ra !== rb) return ra - rb;
      return a.ord - b.ord;
    });
    var lines = ['<모니터>'];
    items.forEach(function(it){ lines.push(it.t + '(' + it.s + ')'); });
    return {text: lines.join('\n'), n: items.length};
  }

  var veil = document.getElementById('veil');
  var report = document.getElementById('report');
  var hint = document.getElementById('copyhint');
  var hintDefault = hint.textContent;

  function openPanel(){
    var r = buildReport();
    report.value = r.text;
    document.getElementById('psub').textContent = r.n + '건 · 단독 상단 · 출입처순';
    hint.textContent = hintDefault; hint.classList.remove('ok');
    veil.hidden = false;
    report.focus();
  }
  function closePanel(){ veil.hidden = true; }

  document.getElementById('make').addEventListener('click', openPanel);
  document.getElementById('close').addEventListener('click', closePanel);
  veil.addEventListener('click', function(e){ if(e.target === veil) closePanel(); });
  document.addEventListener('keydown', function(e){ if(e.key === 'Escape' && !veil.hidden) closePanel(); });

  document.getElementById('clear').addEventListener('click', function(){
    picked.clear(); syncBar(); render();
  });
  document.getElementById('selectall').addEventListener('click', function(){
    report.focus(); report.select();
  });
  document.getElementById('copy').addEventListener('click', function(){
    var done = function(){ hint.textContent = '복사됐습니다.'; hint.classList.add('ok'); };
    var fail = function(){
      report.focus(); report.select();
      hint.textContent = '자동 복사가 막혔습니다 — 전체 선택했으니 ⌘C로 복사하세요.';
      hint.classList.remove('ok');
    };
    try{
      if(navigator.clipboard && navigator.clipboard.writeText){
        navigator.clipboard.writeText(report.value).then(done, fail);
      } else { fail(); }
    }catch(err){ fail(); }
  });

  render(); syncBar();
})();
</script>

</body></html>"""

# ==================== 아카이브 목록 템플릿 (0-17) ====================
# 본문 페이지와 같은 색 토큰·서체를 쓰되, 필터/선택 같은 동작은 없는 정적 목록이다.
_ARCHIVE_TEMPLATE = r"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>지난 다이제스트</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+KR:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
:root{
  --ground:#eceff1; --surface:#ffffff; --surface-2:#f5f7f8;
  --ink:#131a20; --ink-2:#3d4a54; --muted:#6b7b86;
  --line:#d2dade; --line-soft:#e3e9ec;
  --accent:#1c5f88; --accent-soft:#e2edf4;
  --scoop:#b8430e; --flash:#a81f1f;
  --shadow:0 2px 10px rgba(19,26,32,.10);
}
@media (prefers-color-scheme: dark){ :root:not([data-theme="light"]){
  --ground:#0e1418; --surface:#151d23; --surface-2:#1b242b;
  --ink:#e7eef2; --ink-2:#bccad3; --muted:#8497a3;
  --line:#26333b; --line-soft:#1f2a31;
  --accent:#63aedd; --accent-soft:#16303f;
  --scoop:#f28c52; --flash:#ef7070;
  --shadow:0 2px 12px rgba(0,0,0,.5);
}}
*{box-sizing:border-box}
body{
  margin:0; background:var(--ground); color:var(--ink);
  font-family:'IBM Plex Sans KR',system-ui,-apple-system,'Apple SD Gothic Neo','Malgun Gothic',sans-serif;
  font-size:15px; line-height:1.5; -webkit-text-size-adjust:100%;
  padding-bottom:env(safe-area-inset-bottom,0px);
}
.mono{font-family:'IBM Plex Mono','SFMono-Regular',Menlo,monospace;font-variant-numeric:tabular-nums}
header{
  position:sticky; top:env(safe-area-inset-top,0px); z-index:20;
  background:var(--surface); border-bottom:1px solid var(--line);
}
.wrap{max-width:1000px; margin:0 auto; padding:0 16px}
.navline{display:flex; flex-wrap:wrap; gap:8px; align-items:center; padding-block:10px 0}
.navline a{
  font-size:12px; font-weight:500; text-decoration:none;
  color:var(--accent); background:var(--accent-soft);
  border:1px solid transparent; border-radius:999px; padding:4px 11px;
}
.navline a:hover{border-color:var(--accent)}
.masthead{padding-block:12px 14px; display:flex; flex-wrap:wrap; align-items:baseline; gap:6px 14px}
h1{margin:0; font-size:19px; font-weight:700; letter-spacing:-.01em}
.note{font-size:12.5px; color:var(--muted)}
main{padding-block:16px 40px}
section{margin-bottom:22px}
h2{
  margin:0 0 8px; font-size:12.5px; font-weight:600; color:var(--muted);
  letter-spacing:.04em; padding-bottom:6px; border-bottom:1px solid var(--line-soft);
}
.card{
  display:block; text-decoration:none; color:inherit;
  background:var(--surface); border:1px solid var(--line); border-radius:10px;
  padding:12px 14px; margin-bottom:8px;
}
.card:hover{border-color:var(--accent); box-shadow:var(--shadow)}
.ttl{font-size:15px; font-weight:600; display:flex; flex-wrap:wrap; align-items:baseline; gap:8px}
.win{font-size:12px; font-weight:400; color:var(--muted); letter-spacing:.02em}
.sub{margin-top:3px; font-size:12.5px; color:var(--ink-2); display:flex; flex-wrap:wrap; gap:4px 12px; align-items:baseline}
.sub b{font-size:15px; font-weight:600; margin-right:1px}
.sub .dim{color:var(--muted)}
.t-scoop{color:var(--scoop); font-weight:600}
.t-flash{color:var(--flash); font-weight:600}
.beats{margin-top:5px; font-size:12px; color:var(--muted)}
.empty{padding:48px 0; text-align:center; color:var(--muted); font-size:13.5px}
</style></head><body>

<header><div class="wrap">
  <div class="navline"><a href="../">&#8634; 최신 다이제스트</a></div>
  <div class="masthead">
    <h1>지난 다이제스트</h1>
    <span class="note">__COUNT__개 보관 &middot; 최근 __KEEP__일치</span>
  </div>
</div></header>

<main class="wrap">
__ROWS__
</main>

</body></html>"""


if __name__ == "__main__":
    # 수동 스모크 테스트용 — 실제 배선은 news_monitor.py의 PAGE_MODE 분기에서 호출된다.
    demo = build_groups_data([("테스트그룹", [])])
    out = render_html("테스트 다이제스트", "09/17 13:30", "09/17 17:30", 0, demo)
    path = save_page(out)
    print(f"페이지 저장: {path}")


# ==================== 라이브 보고 페이지 (0-20, 2026-09-21) ====================
# docs/live/index.html은 '껍데기'(고정 HTML)이고 데이터는 docs/live/data.json을
# 열 때마다 no-store로 받아온다. 그래서 북마크 한 번이면 언제 열어도 최신 수집분이
# 보이고, 껍데기는 내용이 안 바뀌어 git에도 변경으로 잡히지 않는다(data.json만 커밋).
# 어느 보고를 보여줄지는 브라우저가 '연 시각'으로 고른다(news_monitor.report_timeline
# 이 [이전, 현재, 다음] 보고를 다 넣어 주므로 야간에 수집이 멈춰 있어도 맞게 뜬다).
LIVE_DIR = os.path.join(PAGE_DIR, "live")


def publish_live(data):
    """data: news_monitor.build_live_data()의 반환값. 반환: data.json 경로."""
    os.makedirs(LIVE_DIR, exist_ok=True)
    data_path = os.path.join(LIVE_DIR, "data.json")
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    shell_path = os.path.join(LIVE_DIR, "index.html")
    # 껍데기는 내용이 같으면 다시 쓰지 않는다(mtime만 바뀌는 불필요한 쓰기 방지 —
    # git은 내용 기준이라 어차피 변경으로 안 잡히지만 명시적으로).
    old = None
    if os.path.exists(shell_path):
        with open(shell_path, encoding="utf-8") as f:
            old = f.read()
    if old != _LIVE_TEMPLATE:
        with open(shell_path, "w", encoding="utf-8") as f:
            f.write(_LIVE_TEMPLATE)
    return data_path


_LIVE_TEMPLATE = r"""<!doctype html>
<html lang="ko"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>뉴스레이다</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><circle cx='16' cy='16' r='15' fill='%2308224B'/><circle cx='16' cy='16' r='10' fill='none' stroke='%23fff' stroke-opacity='.45' stroke-width='1.5'/><circle cx='16' cy='16' r='5' fill='none' stroke='%23fff' stroke-opacity='.45' stroke-width='1.5'/><path d='M16 16 L16 3 A13 13 0 0 1 27.3 9.5 Z' fill='%23fff' fill-opacity='.8'/><circle cx='22' cy='11' r='2' fill='%23fff'/></svg>">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css">
<style>
/* 사내 UI 디자인 시스템 v1.0 토큰(tokens.css) — 0-25. 다크 값은 같은 역할로 이 페이지에서 유도한 것. */
:root{
  color-scheme:light;
  --navy-900:#08224B; --blue-700:#0F4C9E; --blue-600:#1565D8; --blue-500:#3B86EE; --blue-100:#D9E7FA; --blue-50:#F0F6FE;
  --ink-900:#14171A; --ink-700:#3C4450; --ink-500:#6C7684; --ink-400:#9AA3AF;
  --line-300:#D3D9E0; --line-200:#E8ECF1; --surf-100:#F4F6F9; --surf-0:#FFFFFF;
  --red-600:#C8262C; --red-50:#FCEAEA; --red-line:#E9B6B8; --scoop-bg:#C8262C;
  --green-600:#1B7F4C; --amber-600:#B26A00; --amber-50:#FDF3E1;
  /* 역할 */
  --link:var(--blue-600); --primary:var(--blue-600); --primary-hover:var(--blue-700); --on-primary:#FFFFFF;
  --appbar:var(--navy-900); --appbar-ink:#FFFFFF; --appbar-ink-2:#C3D0E4; --appbar-line:#3A5683;
  --rep-bg:#EEF1F5; --dis-bg:#EEF1F5; --dis-ink:#9AA3AF; --dis-line:#E8ECF1;
  --new-bg:var(--navy-900); --new-ink:#FFFFFF; --on-org:#FFFFFF;
  --sh-dropdown:0 2px 6px rgba(16,24,40,.10); --sh-modal:0 12px 32px rgba(16,24,40,.18); --veil:rgba(16,24,40,.45);
  /* 조직 식별색 (규격 9) */
  --o-ftc:#123F66; --o-ftc-bg:#E4EBF2;
  --o-msit:#1F6B45; --o-msit-bg:#E6F1EA;
  --o-kcc:#6E3480; --o-kcc-bg:#F1E8F4;
  --o-kasa:#0B6B6B; --o-kasa-bg:#E2F0F0;
  --o-etc:#5A6068; --o-etc-bg:#ECEFF2;
  --o-me:#08224B; --o-me-bg:#E3E8F0;
  --font:"Pretendard Variable","Pretendard",-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Malgun Gothic","Segoe UI",sans-serif;
}
@media (prefers-color-scheme: dark){ :root:not([data-theme="light"]){
  color-scheme:dark;
  --navy-900:#0B1A30; --blue-700:#8DB8F5; --blue-600:#7FB0F5; --blue-500:#5E9BF2; --blue-100:#1A2B45; --blue-50:#141E2C;
  --ink-900:#E6EAEF; --ink-700:#C2CAD3; --ink-500:#8D97A3; --ink-400:#5E6873;
  --line-300:#2F3944; --line-200:#252D36; --surf-100:#0F141A; --surf-0:#171D24;
  --red-600:#EF6B6F; --red-50:#3A1A1C; --red-line:#6B2E31; --scoop-bg:#C8262C;
  --green-600:#4FBF86; --amber-600:#F0B55A; --amber-50:#33270F;
  --link:#7FB0F5; --primary:#2F6FD6; --primary-hover:#3D7DE6; --on-primary:#FFFFFF;
  --appbar:#0B1A30; --appbar-ink:#E6EAEF; --appbar-ink-2:#9FB0C8; --appbar-line:#2A3D5C;
  --rep-bg:#1E252E; --dis-bg:#1E252E; --dis-ink:#5E6873; --dis-line:#252D36;
  --new-bg:#C9D6EA; --new-ink:#08224B; --on-org:#0F141A;
  --sh-dropdown:0 2px 6px rgba(0,0,0,.45); --sh-modal:0 12px 32px rgba(0,0,0,.6); --veil:rgba(0,0,0,.6);
  --o-ftc:#8FB4DB; --o-ftc-bg:#16243A;
  --o-msit:#7CC9A0; --o-msit-bg:#122A1F;
  --o-kcc:#C99BD8; --o-kcc-bg:#2A1A31;
  --o-kasa:#6FCACA; --o-kasa-bg:#0F2A2A;
  --o-etc:#A7AEB6; --o-etc-bg:#22272D;
  --o-me:#A9B8D6; --o-me-bg:#1B2333;
}}
:root[data-theme="dark"]{
  color-scheme:dark;
  --navy-900:#0B1A30; --blue-700:#8DB8F5; --blue-600:#7FB0F5; --blue-500:#5E9BF2; --blue-100:#1A2B45; --blue-50:#141E2C;
  --ink-900:#E6EAEF; --ink-700:#C2CAD3; --ink-500:#8D97A3; --ink-400:#5E6873;
  --line-300:#2F3944; --line-200:#252D36; --surf-100:#0F141A; --surf-0:#171D24;
  --red-600:#EF6B6F; --red-50:#3A1A1C; --red-line:#6B2E31; --scoop-bg:#C8262C;
  --green-600:#4FBF86; --amber-600:#F0B55A; --amber-50:#33270F;
  --link:#7FB0F5; --primary:#2F6FD6; --primary-hover:#3D7DE6; --on-primary:#FFFFFF;
  --appbar:#0B1A30; --appbar-ink:#E6EAEF; --appbar-ink-2:#9FB0C8; --appbar-line:#2A3D5C;
  --rep-bg:#1E252E; --dis-bg:#1E252E; --dis-ink:#5E6873; --dis-line:#252D36;
  --new-bg:#C9D6EA; --new-ink:#08224B; --on-org:#0F141A;
  --sh-dropdown:0 2px 6px rgba(0,0,0,.45); --sh-modal:0 12px 32px rgba(0,0,0,.6); --veil:rgba(0,0,0,.6);
  --o-ftc:#8FB4DB; --o-ftc-bg:#16243A;
  --o-msit:#7CC9A0; --o-msit-bg:#122A1F;
  --o-kcc:#C99BD8; --o-kcc-bg:#2A1A31;
  --o-kasa:#6FCACA; --o-kasa-bg:#0F2A2A;
  --o-etc:#A7AEB6; --o-etc-bg:#22272D;
  --o-me:#A9B8D6; --o-me-bg:#1B2333;
}
/* 출입처 → 식별색 (섹션 머리·출입처 칩). 목록에 없는 그룹(CBS·당직 부처)은 2진 회색 */
section, .beat{--og:var(--o-etc); --ob:var(--o-etc-bg)}
[data-g="공정위"], [data-b="공정위"]{--og:var(--o-ftc); --ob:var(--o-ftc-bg)}
[data-g="과기정통부"], [data-b="과기정통부"]{--og:var(--o-msit); --ob:var(--o-msit-bg)}
[data-g="방미통위"], [data-b="방미통위"]{--og:var(--o-kcc); --ob:var(--o-kcc-bg)}
[data-g="우주항공청"], [data-b="우주항공청"]{--og:var(--o-kasa); --ob:var(--o-kasa-bg)}
[data-g="김광일 기자"], [data-b="김광일 기자"]{--og:var(--o-me); --ob:var(--o-me-bg)}

*{box-sizing:border-box}
[hidden]{display:none !important}
html{scroll-padding-top:env(safe-area-inset-top,0px)}
body{
  margin:0; background:var(--surf-100); color:var(--ink-900);
  font-family:var(--font); font-size:13px; line-height:1.5; -webkit-text-size-adjust:100%;
  -webkit-font-smoothing:antialiased;
  padding-bottom:env(safe-area-inset-bottom,0px);
}
.mono{font-variant-numeric:tabular-nums}
.wrap{max-width:1080px; margin:0 auto; padding-left:16px; padding-right:16px}
:focus-visible{outline:2px solid var(--blue-500); outline-offset:2px}

/* 앱 헤더(네이비 띠) + 필터 바(blue-50) */
header{position:sticky; top:0; z-index:20; background:var(--blue-50); border-bottom:1px solid var(--line-300)}
.appbar{background:var(--appbar); color:var(--appbar-ink); padding-top:env(safe-area-inset-top,0px)}
.top{display:flex; flex-wrap:wrap; align-items:center; gap:4px 12px; padding-block:8px}
h1{margin:0; font-size:16px; line-height:1.4; font-weight:700; letter-spacing:-.02em; color:var(--appbar-ink)}
.brand{display:flex; align-items:center; gap:8px; color:var(--appbar-ink); font-weight:700; font-size:13px; letter-spacing:-.01em; white-space:nowrap; text-decoration:none; padding-right:12px; border-right:1px solid var(--appbar-line)}
.brand svg{width:20px; height:20px; flex:0 0 auto; color:#3B86EE}
.fresh{font-size:12px; color:var(--appbar-ink-2); display:flex; align-items:center; gap:8px}
.fresh button, .reports button{
  height:28px; border:1px solid var(--appbar-line); background:transparent; color:var(--appbar-ink);
  font:inherit; font-size:12px; border-radius:2px; padding:0 8px; cursor:pointer;
  transition:background-color 120ms ease-out, border-color 120ms ease-out;
}
.fresh button:hover, .reports button:hover{border-color:var(--appbar-ink-2)}
.reports{margin-left:auto; display:flex; gap:4px}
.reports button[aria-pressed="true"]{background:var(--appbar-ink); border-color:var(--appbar-ink); color:var(--appbar); font-weight:600}
.filters{padding-top:8px}
.warn{margin:0 0 8px; padding:8px 12px; border-radius:2px; border:1px solid var(--amber-600); background:var(--amber-50); color:var(--ink-900); font-size:12px}
.warn::before{content:'확인 필요'; font-weight:700; color:var(--amber-600); margin-right:8px}

.tabs{display:flex; gap:4px; overflow-x:auto; padding-block:0 8px; scrollbar-width:thin}
.tab{
  flex:0 0 auto; text-align:left; cursor:pointer;
  border:1px solid var(--line-300); background:var(--surf-0); color:var(--ink-700);
  border-radius:2px; padding:4px 12px; font:inherit; line-height:1.35;
  transition:background-color 120ms ease-out, border-color 120ms ease-out;
}
.tab .l{display:block; font-size:13px; font-weight:600; white-space:nowrap}
.tab .s{display:flex; gap:8px; font-size:11px; color:var(--ink-500); white-space:nowrap}
.tab .st-live{color:var(--green-600); font-weight:600}
.tab .st-live::before{content:'● '; font-size:8px; vertical-align:1px}
.tab .pk{color:var(--blue-700); font-weight:700}
.tab .nwt{color:var(--blue-700); font-weight:700}
.tab:hover{border-color:var(--blue-500); background:var(--blue-50)}
.tab[aria-pressed="true"]{background:var(--blue-100); border-color:var(--blue-500); color:var(--blue-700)}
.tab.future{color:var(--ink-400)}
.tab.future .s{color:var(--ink-400)}
.tab.future[aria-pressed="true"]{color:var(--blue-700)}
.tab.tog{border-style:dashed; background:transparent; color:var(--ink-500)}
.tab.tog .l{font-weight:500}
.tab.over{border-style:dashed}

.beats{display:flex; gap:4px; overflow-x:auto; padding-block:0 8px; scrollbar-width:thin}
.beat{flex:0 0 auto; display:flex; align-items:baseline; gap:4px; height:28px; align-items:center; border:1px solid transparent; background:var(--ob); color:var(--og); border-radius:999px; padding:0 12px; font:inherit; font-size:12px; font-weight:600; cursor:pointer; white-space:nowrap}
.beat .c{font-size:11px; font-weight:500; font-variant-numeric:tabular-nums}
.beat:hover{border-color:var(--og)}
.beat[aria-pressed="true"]{background:var(--og); border-color:var(--og); color:var(--on-org)}
.controls{display:flex; flex-wrap:wrap; gap:8px; padding-block:0 8px; align-items:center}
.seg{display:flex; border:1px solid var(--line-300); border-radius:2px; overflow:hidden; background:var(--surf-0)}
.seg button{height:30px; border:0; background:var(--surf-0); color:var(--ink-700); font:inherit; font-size:13px; padding:0 12px; cursor:pointer; border-right:1px solid var(--line-300)}
.seg button:last-child{border-right:0}
.seg button:hover{background:var(--blue-50)}
.seg button[aria-pressed="true"]{background:var(--blue-100); color:var(--blue-700); font-weight:600; box-shadow:inset 0 0 0 1px var(--blue-500)}
#q{flex:1 1 160px; min-width:0; height:32px; border:1px solid var(--line-300); border-radius:2px; background:var(--surf-0); color:var(--ink-900); font:inherit; font-size:13px; padding:0 12px}
#q::placeholder{color:var(--ink-500)}
#q:focus{outline:none; border-color:var(--blue-600); box-shadow:0 0 0 2px var(--blue-100)}

main{padding-block:4px 112px}
.note{margin:12px 0 0; font-size:12px; color:var(--ink-500)}
.note.over{padding:8px 12px; border:1px dashed var(--line-300); border-radius:2px; background:var(--surf-0); color:var(--ink-700)}
.seghead{margin:32px 0 0; display:flex; align-items:baseline; gap:12px; font-size:13px; color:var(--ink-500)}
.seghead b{font-size:16px; color:var(--ink-900)}
.seghead:first-child{margin-top:16px}
section{margin-top:16px}
.sec-head{display:flex; align-items:baseline; gap:8px; padding:0 0 4px; border-bottom:2px solid var(--og)}
.sec-head h2{margin:0; font-size:16px; line-height:1.4; font-weight:700; letter-spacing:-.02em; color:var(--og)}
.sec-head .n{font-size:12px; color:var(--ink-500)}
.sec-head .nwc{color:var(--blue-700); font-weight:700}
.topic{background:var(--surf-0); border-bottom:1px solid var(--line-200); padding:8px 12px}
.topic.pinned{box-shadow:inset 3px 0 0 var(--red-600)}
.row{display:flex; gap:8px; align-items:baseline; padding:2px 4px; border-radius:2px}
.row:hover{background:var(--blue-50)}
.row.rep{background:var(--rep-bg)}
.row.on{background:var(--blue-100); box-shadow:inset 2px 0 0 var(--blue-700)}
.sg{flex:0 0 auto; font-size:13px; color:var(--ink-500); line-height:1; transform:translateY(1px)}
.sg.live{color:var(--green-600); font-weight:700}
.badge, .nw, .tag, .rp{
  flex:0 0 auto; display:inline-flex; align-items:center; height:18px; padding:0 6px;
  border-radius:999px; font-size:10.5px; font-weight:700; line-height:1; white-space:nowrap; transform:translateY(-1px);
}
.nw{background:var(--new-bg); color:var(--new-ink)}
.tag.scoop{background:var(--scoop-bg); color:#FFFFFF}
.tag.flash{background:var(--red-50); color:var(--red-600); border:1px solid var(--red-line)}
.rp{background:var(--surf-0); color:var(--ink-700); border:1px solid var(--line-300); font-weight:600}
.seenbar{margin:12px 0 0; padding:8px 12px; border:1px solid var(--line-300); border-radius:2px; background:var(--surf-0); display:flex; flex-wrap:wrap; gap:8px 12px; align-items:center; font-size:13px; color:var(--ink-700)}
.seenbar b{color:var(--ink-900)}
.seenbar .nwc{color:var(--blue-700); font-weight:700}
.seenbar .sp{margin-left:auto; display:flex; gap:8px}
.seenbar .btn{height:28px; padding:0 12px; font-size:12px}
.pick{flex:0 0 auto; width:16px; height:16px; margin:0; cursor:pointer; accent-color:var(--blue-600); transform:translateY(3px)}
a.title{color:var(--ink-900); text-decoration:none; font-size:14px; line-height:1.5; text-underline-offset:3px; text-wrap:pretty}
a.title:hover{color:var(--link); text-decoration:underline}
a.title:visited{color:var(--ink-900)}
.meta{flex:0 0 auto; margin-left:auto; display:flex; gap:8px; align-items:baseline; font-size:12px; color:var(--ink-500); white-space:nowrap; font-variant-numeric:tabular-nums}
.more{margin-top:4px; margin-left:24px; border:0; background:none; padding:2px 0; cursor:pointer; font:inherit; font-size:12px; color:var(--link); font-weight:600}
.more:hover{text-decoration:underline}
.dupes{margin-top:4px; margin-left:24px; padding-left:12px; border-left:1px solid var(--line-300); display:grid; gap:4px}
.dupes a.title{font-size:13px; color:var(--ink-700)}
.empty{padding:36px 0; text-align:center; color:var(--ink-500); font-size:13px}

.bar{position:fixed; left:0; right:0; bottom:0; z-index:30; background:var(--surf-0); border-top:1px solid var(--line-300); box-shadow:var(--sh-dropdown); padding:8px 16px calc(8px + env(safe-area-inset-bottom,0px))}
.bar-in{max-width:1080px; margin:0 auto; display:flex; gap:8px; align-items:center; flex-wrap:wrap}
.bar .count{font-size:13px; color:var(--ink-700)}
.bar .count b{color:var(--blue-700); font-size:16px}
.bar .spacer{margin-left:auto}
.btn{
  height:32px; display:inline-flex; align-items:center; gap:4px;
  border:1px solid var(--line-300); background:var(--surf-0); color:var(--ink-700);
  font:inherit; font-size:13px; font-weight:500; padding:0 12px; border-radius:2px; cursor:pointer; white-space:nowrap;
  transition:background-color 120ms ease-out, border-color 120ms ease-out;
}
.btn:hover{border-color:var(--blue-500); background:var(--blue-50)}
.btn.sec{border-color:var(--blue-600); color:var(--blue-700); font-weight:600}
.btn.primary{background:var(--primary); border-color:var(--primary); color:var(--on-primary); font-weight:600}
.btn.primary:hover{background:var(--primary-hover); border-color:var(--primary-hover)}
.btn:disabled, .btn:disabled:hover{background:var(--dis-bg); border-color:var(--dis-line); color:var(--dis-ink); cursor:default}

.veil{position:fixed; inset:0; z-index:40; background:var(--veil); display:flex; align-items:flex-end; justify-content:center; padding:16px}
@media (min-width:640px){ .veil{align-items:center} }
.panel{background:var(--surf-0); border-radius:6px; width:100%; max-width:660px; max-height:86vh; display:flex; flex-direction:column; box-shadow:var(--sh-modal); border:1px solid var(--line-300)}
.panel-head{display:flex; align-items:baseline; gap:12px; padding:12px 16px; border-bottom:1px solid var(--line-200)}
.panel-head h3{margin:0; font-size:16px; font-weight:700}
.panel-head .sub{font-size:12px; color:var(--ink-500)}
.panel-head .x{margin-left:auto; border:0; background:none; cursor:pointer; color:var(--ink-500); font-size:20px; line-height:1; width:32px; height:32px; border-radius:2px}
.panel-head .x:hover{background:var(--blue-50); color:var(--ink-900)}
#report{flex:1 1 auto; min-height:220px; margin:12px 16px; padding:12px; border:1px solid var(--line-300); border-radius:2px; resize:vertical; background:var(--surf-0); color:var(--ink-900); font-family:var(--font); font-size:14px; line-height:1.7}
#report:focus{outline:none; border-color:var(--blue-600); box-shadow:0 0 0 2px var(--blue-100)}
.panel-foot{display:flex; gap:12px; align-items:center; padding:0 16px 16px; flex-wrap:wrap}
.hint{font-size:12px; color:var(--ink-500)}
.hint.ok{color:var(--green-600); font-weight:700}
/* 기기 연동(0-26) — 머리 버튼 상태 + 설정 창 */
.fresh button.sync.ok{border-color:var(--appbar-ink-2)}
.fresh button.sync.ok::before{content:''; display:inline-block; width:6px; height:6px; border-radius:50%; background:#4FBF86; margin-right:6px; vertical-align:1px}
.fresh button.sync.err{border-color:#F0B55A; color:#F0B55A}
.spanel{max-width:520px}
.sbody{padding:12px 16px 16px; font-size:13px; color:var(--ink-700); overflow:auto}
.sbody p{margin:0 0 12px}
.sbody b{color:var(--ink-900)}
.sbody ol{margin:0 0 12px; padding-left:20px; display:grid; gap:4px}
.sbody code{font-family:inherit; font-weight:600; color:var(--ink-900); background:var(--surf-100); border:1px solid var(--line-200); border-radius:2px; padding:0 4px}
.sbody a{color:var(--link)}
.sbody .row2{display:flex; gap:8px; flex-wrap:wrap; align-items:center}
.sbody input{flex:1 1 220px; min-width:0; height:32px; border:1px solid var(--line-300); border-radius:2px; background:var(--surf-0); color:var(--ink-900); font:inherit; font-size:13px; padding:0 12px}
.sbody input:focus{outline:none; border-color:var(--blue-600); box-shadow:0 0 0 2px var(--blue-100)}
.sbody .stat{padding:8px 12px; border:1px solid var(--line-300); border-radius:2px; background:var(--surf-100); margin-bottom:12px}
.sbody .stat.err{border-color:var(--amber-600); background:var(--amber-50); color:var(--ink-900)}
.sbody .hint{display:block; margin-top:8px}
.foot{margin-top:40px; font-size:12px; color:var(--ink-500)}
.foot a{color:var(--link)}

.sm{display:none}
@media (max-width:560px){
  .lg{display:none} .sm{display:inline}
  .brand{padding-right:8px} h1{font-size:15px}
  .bar-in{flex-wrap:nowrap; gap:8px}
  .bar .count{font-size:12px; white-space:nowrap}
  .btn{padding:0 8px; font-size:12px}
  .reports{margin-left:0; width:100%}
  .meta{margin-left:0; width:100%; padding-left:24px}
  .row{flex-wrap:wrap}
}
/* 폴드 커버(≤400px): 출입처 칩 → 색 있는 텍스트 + 가운뎃점, 체크박스 20px (규격 8) */
@media (max-width:400px){
  .beats{gap:0}
  .beat{background:none; border:0; border-radius:0; padding:0 4px; height:32px; color:var(--og)}
  .beat + .beat::before{content:'·'; color:var(--ink-400); margin-right:8px; font-weight:400}
  .beat[aria-pressed="true"]{background:none; color:var(--og); text-decoration:underline; text-underline-offset:4px; text-decoration-thickness:2px}
  .pick{width:20px; height:20px; transform:translateY(4px)}
  .meta{padding-left:28px}
  .more, .dupes{margin-left:28px}
}
</style>
</head><body>
<header>
  <div class="appbar">
    <div class="wrap top">
      <a class="brand" href="./" title="뉴스레이다 — 처음 화면으로"><svg viewBox="0 0 32 32" aria-hidden="true"><circle cx="16" cy="16" r="15" fill="currentColor"/><circle cx="16" cy="16" r="10" fill="none" stroke="#fff" stroke-opacity=".45" stroke-width="1.5"/><circle cx="16" cy="16" r="5" fill="none" stroke="#fff" stroke-opacity=".45" stroke-width="1.5"/><path d="M16 16 L16 3 A13 13 0 0 1 27.3 9.5 Z" fill="#fff" fill-opacity=".8"/><circle cx="22" cy="11" r="2" fill="#fff"/></svg>뉴스레이다</a>
      <h1 id="title">보고 준비</h1>
      <span class="fresh"><span id="fresh">불러오는 중…</span><button type="button" id="reload">새로고침</button><button type="button" id="sync" class="sync off" title="체크·‘여기까지 확인’을 다른 기기와 맞추기">기기 연동</button></span>
      <div class="reports" id="reports"></div>
    </div>
  </div>
  <div class="wrap filters">
    <p class="warn" id="warn" hidden></p>
    <div class="tabs" id="tabs"></div>
    <div class="beats" id="beats"></div>
    <div class="controls">
      <div class="seg" role="group" aria-label="보기 범위">
        <button data-f="all" aria-pressed="true">전체</button>
        <button data-f="mark" aria-pressed="false">단독·속보</button>
        <button data-f="spread" aria-pressed="false">전재 2건+</button>
        <button data-f="picked" aria-pressed="false">선택분</button>
        <button data-f="new" aria-pressed="false" id="f-new" hidden>새 기사</button>
      </div>
      <input id="q" type="search" placeholder="제목·매체 검색" autocomplete="off">
    </div>
  </div>
</header>

<main class="wrap" id="main"><p class="empty">불러오는 중…</p></main>

<div class="bar" id="bar">
  <div class="bar-in">
    <span class="count">선택 <span id="segpart"><span class="lg">이 구간 </span><span class="sm">구간 </span><b class="mono" id="n-seg">0</b> · </span><span class="lg">보고 </span>전체 <b class="mono" id="n-all">0</b></span>
    <span class="spacer"></span>
    <button class="btn sec" type="button" id="bar-seen" hidden>여기까지 확인</button>
    <button class="btn" type="button" id="make-seg"><span class="lg">이 </span>구간 양식</button>
    <button class="btn primary" type="button" id="make-all"><span id="all-pre" class="lg">보고 </span>전체 양식</button>
  </div>
</div>

<div class="veil" id="veil" hidden>
  <div class="panel" role="dialog" aria-modal="true" aria-labelledby="ptitle">
    <div class="panel-head">
      <h3 id="ptitle">&lt;모니터&gt;</h3>
      <span class="sub" id="psub"></span>
      <button class="x" type="button" id="close" aria-label="닫기">&times;</button>
    </div>
    <textarea id="report" spellcheck="false"></textarea>
    <div class="panel-foot">
      <button class="btn primary" type="button" id="copy">복사</button>
      <button class="btn" type="button" id="unpick" hidden>이 구간 선택 해제</button>
      <span class="hint" id="copyhint">단독 먼저, 그다음 출입처 순서(방미통위 → 공정위 → 과기정통부 → 우주청 → 2진). 본인 기사는 &lt;기처리&gt;로 따로 모읍니다. 직접 고쳐도 됩니다.</span>
    </div>
  </div>
</div>

<div class="veil" id="sveil" hidden>
  <div class="panel spanel" role="dialog" aria-modal="true" aria-labelledby="stitle">
    <div class="panel-head">
      <h3 id="stitle">기기 연동</h3>
      <span class="sub" id="ssub"></span>
      <button class="x" type="button" id="sclose" aria-label="닫기">&times;</button>
    </div>
    <div class="sbody" id="sbody"></div>
  </div>
</div>

<script>
(function(){
  var REPORT_ORDER = ['방미통위','공정위','과기정통부','우주항공청'];
  var SCOOP = '🔥', FLASH = '⚡';
  var WD = '일월화수목금토';
  var STORE = 'nm-live-picks-v1';
  var TABSTORE = 'nm-live-tab-v1';   // 마지막으로 본 탭(새로고침해도 그 탭으로)
  var TAB_KEEP_MS = 3*3600e3;        // 3시간 넘게 지난 기억은 무시(다음 날 아침엔 새 구간으로)
  var AUTO_EXPAND = 30;

  var D = null;               // data.json
  var ui = {rep:null, tab:null, beat:null, filter:'all', q:''};
  var repManual = false;      // 사용자가 보고를 직접 골랐으면 자동 전환하지 않는다
  var tabManual = false;
  var lastFetch = 0;
  var ITEMS = {};             // uid -> item (현재 보고 + 넘어감 구간)
  var MERGED = false;         // '전체' 탭을 출입처·기사묶음으로 합쳐 그리는 중(0-22)
  // 구간 탭 접기(0-24b) — 기본은 접힘('전체'·'→ 다음 보고'만). '구간별 보기'로 펼치면 이 브라우저에 기억.
  var SEGSTORE = 'nm-live-segview-v1';
  var segOpen = false;
  try{ segOpen = localStorage.getItem(SEGSTORE) === '1'; }catch(e){}
  function tabShown(t){ return segOpen || t.all || t.over; }

  // ---------- '여기까지 확인' 표시 (0-24, '전체' 탭 전용. 기기 연동을 켜면 다른 기기와 맞춘다 — 0-26) ----------
  // {보고id: {at: 확인 시점의 마지막 수집 시각, prev: 그 전 확인 시각(되돌리기용), t: 누른 시각}}
  // 되돌려서 기록이 없어진 보고는 {at:null, prev:null, t} — 지운 사실도 다른 기기에 전해야 해서 남긴다.
  // 페이지를 여는 것만으로는 기록하지 않는다 — 폰으로 잠깐 열어 봐도 '본 것'이 되지 않게.
  var SEENSTORE = 'nm-live-seen-v1';
  var seenMarks = {};
  try{ seenMarks = JSON.parse(localStorage.getItem(SEENSTORE) || '{}') || {}; }catch(e){ seenMarks = {}; }
  function saveSeen(){ seenMarks = pruneRid(seenMarks, SEEN_KEEP_DAYS); storeLocal(); syncSoon(); }
  function seenAt(rid){ var m = seenMarks[rid]; return m && m.at ? T(m.at) : null; }
  function isNew(it){ var a = seenAt(it.rid); return a != null && !!it.q && !it.d && T(it.q) > a; }
  function newCount(r){
    var a = seenAt(r.id), n = 0; if(a == null) return 0;
    r.segments.forEach(function(s){ s.groups.forEach(function(g){ g.clusters.forEach(function(c){
      c.forEach(function(it){ if(it.q && !it.d && T(it.q) > a) n++; }); }); }); });
    return n;
  }

  // ---------- 선택 저장 (기기 연동을 켜면 다른 기기와 맞춘다 — 0-26) ----------
  var picks = {};             // {reportId: {segId: {key:1}}} — 그리기·보고 양식이 쓰는 모양(0-20부터 같음)
  try{ picks = JSON.parse(localStorage.getItem(STORE) || '{}') || {}; }catch(e){ picks = {}; }
  // 병합용 기록(0-26): {reportId: {segId: {key: [1|0, 바꾼 시각 ms]}}}. 해제도 0으로 남긴다 —
  // 지운 걸 다른 기기에 전하려면 '지웠다'는 기록이 있어야 해서. picks는 여기서 v=1만 뽑은 것.
  var PTSTORE = 'nm-live-pickt-v1';
  var pickt = {};
  try{ pickt = JSON.parse(localStorage.getItem(PTSTORE) || '{}') || {}; }catch(e){ pickt = {}; }
  (function reconcile(){
    // 기록과 picks가 어긋나면(연동 전 체크, 또는 되돌린 옛 판에서 고친 것) picks가 최근 사실이다.
    var now = Date.now(), seen = {};
    Object.keys(picks).forEach(function(r){ Object.keys(picks[r] || {}).forEach(function(s){ Object.keys(picks[r][s] || {}).forEach(function(k){
      seen[r+'\n'+s+'\n'+k] = 1;
      var e = pickt[r] && pickt[r][s] && pickt[r][s][k];
      if(!e || !e[0]) ptSet(r, s, k, [1, e ? now : 0]);
    }); }); });
    Object.keys(pickt).forEach(function(r){ Object.keys(pickt[r] || {}).forEach(function(s){ Object.keys(pickt[r][s] || {}).forEach(function(k){
      var e = pickt[r][s][k];
      if(e && e[0] && !seen[r+'\n'+s+'\n'+k]) pickt[r][s][k] = [0, now];
    }); }); });
  })();
  function ptSet(r, s, k, e){ pickt[r] = pickt[r] || {}; pickt[r][s] = pickt[r][s] || {}; pickt[r][s][k] = e; }
  function rebuildPicks(){
    picks = {};
    Object.keys(pickt).forEach(function(r){ Object.keys(pickt[r] || {}).forEach(function(s){ Object.keys(pickt[r][s] || {}).forEach(function(k){
      var e = pickt[r][s][k]; if(Array.isArray(e) && e[0]) pset(r, s)[k] = 1;
    }); }); });
  }
  function setPick(rid, sid, key, on){
    ptSet(rid, sid, key, [on ? 1 : 0, Date.now()]);
    var o = pset(rid, sid); if(on) o[key] = 1; else delete o[key];
  }
  // 보관 기한이 지난 보고 기록은 버린다(id 앞 8자리 = 보고일).
  // 체크(picks·pickt)는 60일 — 중요도 학습(항목 37)의 원료라서. 해제 기록 [0, 시각]도 같은 기한이어야
  // 다른 기기의 옛 체크가 되살아나지 않는다. '여기까지 확인'(seenMarks)은 지난 보고에선 쓸 데가 없어 14일.
  var PICK_KEEP_DAYS = 60, SEEN_KEEP_DAYS = 14;
  function pruneRid(o, days){
    var c = ymd(new Date(Date.now() - days*864e5));
    Object.keys(o).forEach(function(k){ if(k.slice(0,8) < c) delete o[k]; });
    return o;
  }
  function storeLocal(){
    try{
      localStorage.setItem(STORE, JSON.stringify(picks));
      localStorage.setItem(PTSTORE, JSON.stringify(pickt));
      localStorage.setItem(SEENSTORE, JSON.stringify(seenMarks));
    }catch(e){}
  }
  function savePicks(){ pruneRid(pickt, PICK_KEEP_DAYS); pruneRid(picks, PICK_KEEP_DAYS); storeLocal(); syncSoon(); }
  function pset(rid, sid){ picks[rid] = picks[rid] || {}; picks[rid][sid] = picks[rid][sid] || {}; return picks[rid][sid]; }
  function isPicked(rid, sid, key){ return !!(picks[rid] && picks[rid][sid] && picks[rid][sid][key]); }

  function ymd(d){ return d.getFullYear()+('0'+(d.getMonth()+1)).slice(-2)+('0'+d.getDate()).slice(-2); }
  function hm(d){ return ('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2); }
  function esc(s){ return String(s).replace(/[&<>"]/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  function T(s){ return new Date(s).getTime(); }

  // ---------- 기기 연동 (0-26) ----------
  // 체크(pickt)와 '여기까지 확인'(seenMarks)을 내 GitHub 비공개 gist의 파일 하나에 맞춰 둔다.
  // 토큰은 기기마다 한 번 넣고 이 브라우저의 localStorage에만 둔다 — 페이지 소스·저장소·data.json에는 없다.
  // 병합: 체크는 기사마다 [상태, 바꾼 시각], '여기까지 확인'은 보고마다 누른 시각 t — 나중에 바꾼 쪽이 이긴다.
  // 맞추는 때: 바꾼 뒤 1초, 화면으로 돌아올 때·창 포커스(15초에 한 번까지), 켜 둔 채 5분마다, 화면을 떠날 때.
  // 연결하지 않은 기기는 api.github.com에 아무 요청도 보내지 않는다(0-25까지와 같은 동작).
  var SYNCSTORE = 'nm-live-sync-v1';       // {token, gist, last}
  var SYNC_FILE = 'newsradar-sync.json';
  var GH = 'https://api.github.com';
  var cfg = {};
  try{ cfg = JSON.parse(localStorage.getItem(SYNCSTORE) || '{}') || {}; }catch(e){ cfg = {}; }
  function saveCfg(){ try{ localStorage.setItem(SYNCSTORE, JSON.stringify(cfg)); }catch(e){} }
  var sync = {busy:false, again:false, timer:null, err:'', lastTry:0};

  function canon(o){
    if(o === null || typeof o !== 'object') return JSON.stringify(o === undefined ? null : o);
    if(Array.isArray(o)) return '[' + o.map(canon).join(',') + ']';
    return '{' + Object.keys(o).sort().map(function(k){ return JSON.stringify(k) + ':' + canon(o[k]); }).join(',') + '}';
  }
  function mergePickt(a, b){
    var out = {};
    [a, b].forEach(function(src){ Object.keys(src || {}).forEach(function(r){ var R = src[r] || {};
      Object.keys(R).forEach(function(s){ var S = R[s] || {};
        Object.keys(S).forEach(function(k){
          var e = S[k]; if(!Array.isArray(e)) return;
          var v = e[0] ? 1 : 0, t = +e[1] || 0;
          out[r] = out[r] || {}; out[r][s] = out[r][s] || {};
          var cur = out[r][s][k];
          if(!cur || t > cur[1] || (t === cur[1] && v > cur[0])) out[r][s][k] = [v, t];
        });
      });
    }); });
    return out;
  }
  function stamp(m){ var t = m && m.t ? T(m.t) : 0; return isNaN(t) ? 0 : t; }
  function mergeSeen(a, b){
    var out = {};
    [a, b].forEach(function(src){ Object.keys(src || {}).forEach(function(r){
      var m = src[r]; if(!m || typeof m !== 'object') return;
      if(!out[r] || stamp(m) > stamp(out[r])) out[r] = {at: m.at || null, prev: m.prev || null, t: m.t || null};
    }); });
    return out;
  }

  function gh(method, path, body){
    var h = {'Accept': 'application/vnd.github+json', 'Authorization': 'Bearer ' + cfg.token};
    if(body) h['Content-Type'] = 'application/json';
    return fetch(GH + path, {method: method, cache: 'no-store', headers: h, body: body ? JSON.stringify(body) : undefined})
      .then(function(r){
        if(!r.ok){ var e = new Error('HTTP ' + r.status); e.status = r.status; throw e; }
        return r.status === 204 ? null : r.json();
      });
  }
  function docBody(p, s){
    var f = {}; f[SYNC_FILE] = {content: JSON.stringify({v: 1, picks: p, seen: s, at: new Date().toISOString()})};
    return f;
  }
  // 첫 연결: 내 gist 중 newsradar-sync.json이 든 것(가장 먼저 만든 것)을 쓴다. 없으면 비공개로 새로 만든다.
  // 그래서 두 번째 기기는 토큰만 넣으면 된다(토큰을 기기마다 따로 만들어도 같은 계정이면 같은 gist).
  function findGist(){
    return gh('GET', '/gists?per_page=100').then(function(list){
      var hit = (list || []).filter(function(g){ return g && g.files && g.files[SYNC_FILE]; })
        .sort(function(a, b){ return T(a.created_at) - T(b.created_at); })[0];
      if(hit) return hit.id;
      return gh('POST', '/gists', {description: '뉴스레이다 기기 연동 기록 (자동 생성 — 지우면 다음 연결 때 새로 만듭니다)',
        public: false, files: docBody({}, {})}).then(function(g){ return g.id; });
    });
  }
  function readRemote(id){
    return gh('GET', '/gists/' + id).then(function(g){
      var f = g && g.files && g.files[SYNC_FILE];
      if(!f) return {};
      if(f.truncated && f.raw_url) return fetch(f.raw_url, {cache: 'no-store'}).then(function(r){ return r.json(); });
      try{ return JSON.parse(f.content || '{}') || {}; }catch(e){ return {}; }
    });
  }
  function apply(id, rem){
    rem = rem && typeof rem === 'object' ? rem : {};
    var mp = pruneRid(mergePickt(pickt, rem.picks), PICK_KEEP_DAYS), ms = pruneRid(mergeSeen(seenMarks, rem.seen), SEEN_KEEP_DAYS);
    var cp = canon(mp), cs = canon(ms);
    if(cp !== canon(pickt) || cs !== canon(seenMarks)){
      pickt = mp; seenMarks = ms; rebuildPicks(); storeLocal();
      if(D){ var y = window.scrollY; pickDefaults(); draw(); window.scrollTo(0, y); }
    }
    if(cp === canon(rem.picks || {}) && cs === canon(rem.seen || {})) return null;
    return gh('PATCH', '/gists/' + id, {files: docBody(mp, ms)});
  }
  function errText(e){
    var st = e && e.status;
    if(st === 401) return '토큰이 틀렸거나 만료됐습니다. 연결을 끊고 새 토큰을 넣어 주세요.';
    if(st === 403 || st === 404 && !cfg.gist) return '토큰에 Gists 읽기·쓰기 권한이 없거나 요청이 막혔습니다(' + st + ').';
    if(st === 422) return 'GitHub이 요청을 거절했습니다(422).';
    if(st) return 'GitHub 응답 ' + st + ' — 잠시 뒤 다시 맞춥니다.';
    return '연결 실패(오프라인?) — 이 기기에 저장해 두고 다음에 맞춥니다.';
  }
  function syncNow(){
    if(!cfg.token) return Promise.resolve();
    if(sync.busy){ sync.again = true; return Promise.resolve(); }
    clearTimeout(sync.timer); sync.timer = null;
    sync.busy = true; sync.again = false; sync.lastTry = Date.now(); syncUI();
    function step(retried){
      var idp = cfg.gist ? Promise.resolve(cfg.gist) : findGist().then(function(id){ cfg.gist = id; saveCfg(); return id; });
      return idp.then(function(id){ return readRemote(id).then(function(rem){ return apply(id, rem); }); })
        .catch(function(e){
          // gist를 지웠으면 한 번만 다시 찾는다(없으면 새로 만든다)
          if(e && e.status === 404 && cfg.gist && !retried){ cfg.gist = ''; saveCfg(); return step(true); }
          throw e;
        });
    }
    return step(false)
      .then(function(){ sync.err = ''; cfg.last = Date.now(); saveCfg(); })
      .catch(function(e){ sync.err = errText(e); })
      .then(function(){ sync.busy = false; syncUI(); if(sync.again) syncNow(); });
  }
  function syncSoon(){
    if(!cfg.token) return;
    clearTimeout(sync.timer); sync.timer = setTimeout(syncNow, 1000);
  }
  function syncIfStale(){ if(cfg.token && Date.now() - sync.lastTry > 15e3) syncNow(); }

  // 머리 버튼 + 설정 창
  var sveil = document.getElementById('sveil'), sbody = document.getElementById('sbody');
  function syncUI(){
    var b = document.getElementById('sync');
    if(!cfg.token){ b.className = 'sync off'; b.textContent = '기기 연동'; }
    else if(sync.busy && !cfg.last){ b.className = 'sync'; b.textContent = '연동 중…'; }
    else if(sync.err){ b.className = 'sync err'; b.textContent = '연동 오류'; }
    else { b.className = 'sync ok'; b.innerHTML = '연동<span class="lg">됨</span>' + (cfg.last ? ' <span class="mono">' + hm(new Date(cfg.last)) + '</span>' : ''); }
    if(!sveil.hidden) drawSyncPanel();
  }
  function drawSyncPanel(){
    var h = '', sub = document.getElementById('ssub');
    if(!cfg.token){
      sub.textContent = '꺼짐 · 이 기기에만 저장 중';
      h = '<p>데스크톱과 폰에서 <b>체크</b>와 <b>여기까지 확인</b>을 이어 쓰려면, 기기마다 한 번 GitHub 토큰을 넣어 주세요. '+
          '기록은 내 GitHub 계정의 <b>비공개 gist</b> 하나(<code>'+SYNC_FILE+'</code>)에 저장됩니다. 토큰은 이 기기 브라우저에만 남습니다.</p>'+
          '<ol><li><a href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noopener">GitHub → Fine-grained token 만들기</a></li>'+
          '<li>Repository access는 기본값 그대로, <b>Account permissions → Gists: Read and write</b> 하나만</li>'+
          '<li>만든 토큰(<code>github_pat_…</code>)을 아래에 붙여 넣고 연결</li></ol>'+
          '<p class="hint">폰에서는 폰 브라우저로 토큰을 하나 더 만들어 넣어도 됩니다 — 같은 계정이면 같은 gist를 찾아 이어집니다. 이 기기에 이미 있는 체크는 그대로 합쳐집니다.</p>'+
          '<div class="row2"><input id="stoken" type="password" placeholder="github_pat_…" autocomplete="off" spellcheck="false" aria-label="GitHub 토큰">'+
          '<button class="btn primary" type="button" id="sconnect">연결</button></div>'+
          (sync.err ? '<span class="hint" style="color:var(--amber-600)">'+esc(sync.err)+'</span>' : '');
    } else {
      sub.textContent = sync.busy ? '맞추는 중…' : sync.err ? '오류' : '켜짐';
      h = '<div class="stat'+(sync.err ? ' err' : '')+'">'+
          (sync.err ? esc(sync.err) : (cfg.last ? '마지막으로 맞춘 시각 <b>'+fmtAt(cfg.last)+'</b>' : '처음 맞추는 중…'))+
          (cfg.gist ? ' · <a href="https://gist.github.com/'+esc(cfg.gist)+'" target="_blank" rel="noopener">gist 보기</a>' : '')+'</div>'+
          '<p>체크·해제와 여기까지 확인·되돌리기가 바로 다른 기기로 가고, 화면으로 돌아올 때마다 받아 옵니다. '+
          '같은 기사를 두 기기에서 다르게 바꿨다면 <b>나중에 바꾼 쪽</b>이 남습니다. 오프라인일 때 바꾼 것은 이 기기에 두었다가 다음에 합칩니다.</p>'+
          '<p class="hint">구간별 보기 펼침·마지막 탭 같은 화면 설정은 기기마다 따로입니다(데스크톱과 폰 화면이 달라서).</p>'+
          '<div class="row2"><button class="btn sec" type="button" id="snow"'+(sync.busy ? ' disabled' : '')+'>지금 맞추기</button>'+
          '<button class="btn" type="button" id="soff">이 기기 연결 끊기</button></div>'+
          '<span class="hint">연결을 끊어도 이 기기의 기록과 gist는 그대로 남습니다. 토큰을 없애려면 GitHub 설정에서 삭제하세요.</span>';
    }
    sbody.innerHTML = h;
  }
  document.getElementById('sync').addEventListener('click', function(){ sveil.hidden = false; drawSyncPanel(); var i = document.getElementById('stoken'); if(i) i.focus(); });
  document.getElementById('sclose').addEventListener('click', function(){ sveil.hidden = true; });
  sveil.addEventListener('click', function(e){
    if(e.target === sveil){ sveil.hidden = true; return; }
    var b = e.target.closest('button'); if(!b) return;
    if(b.id === 'sconnect'){
      var v = (document.getElementById('stoken').value || '').trim();
      if(!v){ document.getElementById('stoken').focus(); return; }
      cfg = {token: v, gist: '', last: 0}; sync.err = ''; saveCfg(); storeLocal(); syncNow();
    } else if(b.id === 'snow'){ syncNow(); }
    else if(b.id === 'soff'){ cfg = {}; sync.err = ''; clearTimeout(sync.timer); saveCfg(); syncUI(); }
  });
  sveil.addEventListener('keydown', function(e){ if(e.key === 'Enter' && e.target.id === 'stoken') document.getElementById('sconnect').click(); });

  // ---------- 데이터 ----------
  function load(force){
    if(!force && Date.now() - lastFetch < 60e3) return;
    lastFetch = Date.now();
    fetch('data.json?t=' + Date.now(), {cache:'no-store'}).then(function(r){
      if(!r.ok) throw new Error(r.status);
      return r.json();
    }).then(function(d){ var y = D ? window.scrollY : 0; D = d; pickDefaults(); draw(); window.scrollTo(0, y); })
      .catch(function(e){
        if(!D){ document.getElementById('main').innerHTML = '<p class="empty">데이터를 불러오지 못했습니다 ('+esc(e.message)+'). 새로고침을 눌러 주세요.</p>'; }
        document.getElementById('fresh').textContent = '불러오기 실패';
      });
  }

  function segState(s, now){ return now < T(s.start) ? 'future' : (now < T(s.end) ? 'live' : 'done'); }

  function pickDefaults(){
    var now = Date.now(), reps = D.reports;
    if(!repManual || !reps.some(function(r){ return r.id === ui.rep; })){
      var cur = reps.filter(function(r){ return T(r.display_from) <= now && now < T(r.display_until); })[0];
      if(!cur){ // 데이터가 오래돼 지금 시각에 맞는 보고가 없으면, 이미 시작된 것 중 마지막
        var started = reps.filter(function(r){ return T(r.display_from) <= now; });
        cur = started.length ? started[started.length-1] : reps[1] || reps[0];
      }
      if(ui.rep !== cur.id){ ui.rep = cur.id; tabManual = false; }
    }
    var tabs = tabList();
    if(!tabManual){
      // 새로고침·재방문: 최근(3시간 이내)에 직접 고른 탭이 이 보고에 있으면 그대로 연다
      try{
        var sv = JSON.parse(localStorage.getItem(TABSTORE) || 'null');
        if(sv && sv.rep === ui.rep && now - sv.at < TAB_KEEP_MS && tabs.some(function(t){ return t.key === sv.tab; })){
          ui.tab = sv.tab; tabManual = true;
        }
      }catch(e){}
    }
    if(!tabManual || !tabs.some(function(t){ return t.key === ui.tab; })){
      // 기본 탭: 본 보고 구간 중 '진행 중'인 것, 없으면 마지막으로 끝난 것
      var own = tabs.filter(function(t){ return !t.over && t.seg; });
      // 진행 중 구간이라도 아직 기사가 하나도 없으면(예: 17:30~18:00 수집 전) 건너뛰고
      // 방금 마감된 구간을 연다 — 빈 화면이 떠서 체크가 사라진 것처럼 보이는 걸 막는다.
      var live = own.filter(function(t){ return segState(t.seg, now) === 'live' && (t.seg.n + (t.seg.dup||0)) > 0; })[0];
      var done = own.filter(function(t){ return segState(t.seg, now) === 'done'; });
      // '넘어감' 탭은 기본으로 고르지 않는다 — 보고 직전·직후(13:30~15:00)엔 아직
      // 본 보고를 쓰는 중이므로 본 보고 구간이 떠야 한다.
      var dflt = live || done[done.length-1] || own[0];
      ui.tab = dflt.key;
      // '여기까지 확인'을 쓰기 시작한 보고는 '전체' 탭으로 연다(0-24)
      var allTab = tabs.filter(function(t){ return t.all; })[0];
      if(allTab && seenAt(ui.rep) != null) ui.tab = allTab.key;
    }
    // 구간 탭이 접혀 있으면 구간 탭은 열지 않는다 → '전체'
    var ct = tabs.filter(function(t){ return t.key === ui.tab; })[0];
    if(!segOpen && (!ct || !tabShown(ct))){
      var at = tabs.filter(function(t){ return t.all; })[0];
      if(at) ui.tab = at.key;
    }
  }

  function curRep(){ return D.reports.filter(function(r){ return r.id === ui.rep; })[0]; }
  function nextRep(){ var i = D.reports.indexOf(curRep()); return D.reports[i+1] || null; }

  // 탭 = 본 보고의 구간들 + '전체' + (시작됐으면) 다음 보고 첫 구간('넘어감')
  function tabList(){
    var r = curRep(), n = nextRep(), now = Date.now(), out = [];
    var circ = '①②③④⑤⑥⑦⑧⑨⑩';
    r.segments.forEach(function(s, i){ out.push({key:r.id+'/'+s.id, rep:r, seg:s, no:circ[i]||String(i+1)}); });
    out.push({key:r.id+'/all', rep:r, all:true});
    if(n && n.segments.length && segState(n.segments[0], now) !== 'future'){
      out.push({key:n.id+'/'+n.segments[0].id, rep:n, seg:n.segments[0], over:true});
    }
    return out;
  }
  function curTab(){ return tabList().filter(function(t){ return t.key === ui.tab; })[0]; }

  function segPickCount(rep, seg){ var o = picks[rep.id] && picks[rep.id][seg.id]; return o ? Object.keys(o).length : 0; }

  // ---------- 그리기 ----------
  function draw(){
    drawHeader(); drawTabs(); drawBody();
    // 가로로 넘치는 탭 줄에서 선택된 탭이 보이게(모바일)
    var on = document.querySelector('.tab[aria-pressed="true"]'), bar = document.getElementById('tabs');
    if(on && (on.offsetLeft < bar.scrollLeft || on.offsetLeft + on.offsetWidth > bar.scrollLeft + bar.clientWidth)){
      bar.scrollLeft = on.offsetLeft - 16;
    }
  }

  function drawHeader(){
    var now = Date.now(), r = curRep();
    document.title = '뉴스레이다 · ' + r.title;
    document.getElementById('title').textContent = r.title;

    // 신선도
    var ls = D.last_seen ? new Date(D.last_seen) : null;
    var mins = ls ? Math.round((now - ls.getTime())/60e3) : null;
    document.getElementById('fresh').textContent = ls ?
      ('마지막 수집 ' + (ymd(ls) === ymd(new Date()) ? '' : (ls.getMonth()+1)+'/'+ls.getDate()+' ') + hm(ls) + (mins >= 0 && mins < 120 ? ' · '+mins+'분 전' : '')) : '수집 기록 없음';
    var h = new Date().getHours(), warn = document.getElementById('warn');
    // 수집(check)은 05~22시에만 돈다. 그 사이에 90분 넘게 새 수집이 없으면 경고.
    if(ls && h >= 6 && h <= 23 && mins > 90){
      warn.textContent = '마지막 수집이 ' + Math.floor(mins/60) + '시간 ' + (mins%60) + '분 전입니다. 수집(check) 실행이 멈췄을 수 있습니다.';
      warn.hidden = false;
    } else warn.hidden = true;

    // 보고 선택 (이전·현재·다음)
    var rb = document.getElementById('reports');
    rb.innerHTML = D.reports.map(function(x){
      var cur = T(x.display_from) <= now && now < T(x.display_until);
      return '<button type="button" data-r="'+x.id+'" aria-pressed="'+(x.id===ui.rep)+'">'+esc(x.title.replace(' 보고',''))+(cur?' ●':'')+'</button>';
    }).join('');

  }

  function drawTabs(){
    var now = Date.now();
    var tabs = tabList();
    var nseg = tabs.filter(function(t){ return t.seg && !t.over; }).length;
    var tog = '<button class="tab tog" type="button" data-tog="1" title="구간 탭 '+(segOpen?'접기':'펼치기')+'"><span class="l">'+
              (segOpen ? '◂ 구간 접기' : '구간별 보기 ▸')+'</span><span class="s">'+(segOpen ? '전체만 보기' : CIRC.slice(0, nseg).split('').join('')+' 구간 탭')+'</span></button>';
    document.getElementById('tabs').innerHTML = tabs.filter(tabShown).map(function(t){
      if(t.all){
        var tot = t.rep.segments.reduce(function(a,s){ return a + s.n; }, 0);
        var pk = allSegs(t.rep).reduce(function(a,s){ return a + segPickCount(t.rep, s); }, 0);
        var nw = newCount(t.rep);
        return '<button class="tab" type="button" data-t="'+t.key+'" aria-pressed="'+(t.key===ui.tab)+'"><span class="l">전체</span>'+
               '<span class="s"><span class="mono">'+tot+'건</span>'+(nw?'<span class="nwt mono">새 '+nw+'</span>':'')+(pk?'<span class="pk mono">✓'+pk+'</span>':'')+'</span></button>';
      }
      var st = segState(t.seg, now), pk = segPickCount(t.rep, t.seg);
      var stl = st === 'live' ? '<span class="st-live">진행 중</span>' : st === 'future' ? '<span class="st-future">대기</span>' : '<span>마감</span>';
      var lab = t.over ? '→ 다음 보고 ' + esc(t.seg.label) : t.no + ' ' + esc(t.seg.label);
      var cnt = st === 'future' ? '' : '<span class="mono">'+t.seg.n+'건</span>'+(t.seg.dup ? '<span class="mono">+중복 '+t.seg.dup+'</span>' : '');
      return '<button class="tab'+(st==='future'?' future':'')+(t.over?' over':'')+'" type="button" data-t="'+t.key+'" aria-pressed="'+(t.key===ui.tab)+'">'+
             '<span class="l">'+lab+'</span><span class="s">'+stl+cnt+(pk?'<span class="pk mono">✓'+pk+'</span>':'')+'</span></button>';
    }).join('') + (nseg > 1 ? tog : '');

    syncBar();
  }

  var CIRC = '①②③④⑤⑥⑦⑧⑨⑩';
  function prep(rep, s, si, gi, ci, ii, it, gname){
    it.uid = rep.id+'|'+s.id+'|'+gi+'-'+ci+'-'+ii; it.g = gname; it.rid = rep.id; it.sid = s.id;
    it.key = it.l || it.t; it.ord = si*1e7 + gi*1e5 + ci*100 + ii; it.si = si; ITEMS[it.uid] = it;
    return it;
  }
  // 묶음 하나(items: 이미 필터 통과한 기사들)를 그린다. 반환: {html, fresh, dup}
  function topicBlock(items, id, merged){
    var fresh = items.filter(function(x){ return !x.d; }).length;
    var nnew = merged ? items.filter(isNew).length : 0;
    var lead = items[0], rest = items.slice(1);
    var cls = 'topic' + (lead.m ? ' pinned' : '') + (lead.m===FLASH ? ' flash' : '');
    var body = row(lead);
    if(rest.length){
      var big = items.length >= AUTO_EXPAND;
      // 합쳐 보기: 접힌 줄 안에 다른 구간 기사가 있으면 구간별 건수를 붙인다 —
      // 이미 본 ① 묶음 밑에 ②의 새 후속보도가 접혀 숨는 걸 알아채게.
      var mix = '';
      if(merged){
        var bySeg = {}, segs = [];
        rest.forEach(function(x){ if(!(x.si in bySeg)){ bySeg[x.si] = 0; segs.push(x.si); } bySeg[x.si]++; });
        if(segs.length > 1 || segs[0] !== lead.si){
          segs.sort(function(a,b){ return a-b; });
          mix = ' · ' + segs.map(function(k){ return CIRC.charAt(k) + bySeg[k]; }).join(' ');
        }
        var rn = rest.filter(isNew).length;
        if(rn) mix += ' · 새 ' + rn;
      }
      body += '<button class="more" type="button" aria-expanded="'+big+'" aria-controls="'+id+'" data-mix="'+esc(mix)+'">'+
              (big ? '접기 ('+rest.length+'건 같은 사안'+mix+')' : '+'+rest.length+'건 같은 사안'+mix)+'</button>'+
              '<div class="dupes" id="'+id+'"'+(big?'':' hidden')+'>'+rest.map(row).join('')+'</div>';
    }
    return {html:'<div class="'+cls+'">'+body+'</div>', fresh:fresh, dup:items.length-fresh, nnew:nnew};
  }
  function section(name, blocks, gc, gdup, gnew){
    return '<section data-g="'+esc(name)+'"><div class="sec-head"><h2>'+esc(name)+'</h2><span class="n mono">'+gc+'건'+(gnew?' · <span class="nwc">새 '+gnew+'</span>':'')+(gdup?' · 이미 나옴 '+gdup:'')+'</span></div>'+blocks+'</section>';
  }

  function drawBody(){
    var now = Date.now();
    ITEMS = {};
    // 보여줄 구간들
    var t = curTab(), segs = t.all ? t.rep.segments.map(function(s){ return {rep:t.rep, seg:s}; }) : [{rep:t.rep, seg:t.seg}];
    MERGED = !!(t.all && t.rep.all);
    // '새 기사' 필터는 '전체' 탭 + 확인 기록이 있을 때만(0-24)
    var fnew = document.getElementById('f-new'), showNew = MERGED && seenAt(t.rep.id) != null;
    fnew.hidden = !showNew;
    if(!showNew && ui.filter === 'new'){
      ui.filter = 'all';
      [].forEach.call(document.querySelectorAll('.seg button'), function(b){ b.setAttribute('aria-pressed', String(b.dataset.f === 'all')); });
    }
    // 출입처 칩: 보여줄 구간들의 그룹 합계
    var gcount = {}, gorder = [];
    if(MERGED) t.rep.all.forEach(function(mg){ gcount[mg.name] = 0; gorder.push(mg.name); });
    segs.forEach(function(x){ x.seg.groups.forEach(function(g){ if(!(g.name in gcount)){ gcount[g.name]=0; gorder.push(g.name); } gcount[g.name]+=g.n; }); });
    if(t.all && t.rep.byline && segState(t.rep.byline, now) !== 'future'){ gcount[t.rep.byline.name] = t.rep.byline.n; gorder.push(t.rep.byline.name); }
    if(ui.beat && !(ui.beat in gcount)) ui.beat = null;
    document.getElementById('beats').innerHTML = gorder.map(function(g){
      return '<button class="beat" type="button" data-b="'+esc(g)+'" aria-pressed="'+(g===ui.beat)+'"><span>'+esc(g)+'</span><span class="c mono">'+gcount[g]+'</span></button>';
    }).join('');

    var html = '';
    if(t.over){
      html += '<p class="note over">이 기사들은 <b>'+esc(t.rep.title)+'</b>의 첫 구간입니다. 여기서 체크한 것은 다음 보고에 담깁니다.</p>';
    }
    if(MERGED){ html += drawMerged(t.rep, now); }
    else segs.forEach(function(x, si){
      var s = x.seg, st = segState(s, now), part = '';
      if(t.all){
        html += '<div class="seghead"><b>'+CIRC.charAt(si)+' '+esc(s.label)+'</b><span>'+
                (st==='future'?'대기':st==='live'?'진행 중':'마감')+(st==='future'?'':' · '+s.n+'건')+'</span></div>';
      }
      if(st === 'future'){ if(!t.all) html += '<p class="empty">아직 시작되지 않은 구간입니다.</p>'; return; }
      s.groups.forEach(function(g, gi){
        if(ui.beat && g.name !== ui.beat) return;
        var blocks = '', gc = 0, gdup = 0;
        g.clusters.forEach(function(c, ci){
          c.forEach(function(it, ii){ prep(x.rep, s, si, gi, ci, ii, it, g.name); });
          var items = c.filter(keep);
          if(!items.length) return;
          var b = topicBlock(items, 'c'+s.id+'-'+gi+'-'+ci, false);
          blocks += b.html; gc += b.fresh; gdup += b.dup;
        });
        if(gc || gdup) part += section(g.name, blocks, gc, gdup);
      });
      if(!part) part = '<p class="empty">'+(s.raw ? '조건에 맞는 기사가 없습니다.' : (st==='live' ? '이 구간에 수집된 기사가 아직 없습니다.' : '이 구간에 수집된 기사가 없습니다.'))+'</p>';
      else if(st === 'live' && D.generated){
        part = '<p class="note">진행 중인 구간 — '+hm(new Date(D.last_seen || D.generated))+' 수집분까지. 다음 수집 때 이어서 늘어납니다.</p>' + part;
      }
      html += part;
    });
    if(t.all && !MERGED) html += drawByline(t.rep, now).html;
    html += '<p class="foot">구간은 기사 <b>수집 시각</b> 기준입니다(발행 시각은 오른쪽 숫자). 끝난 구간에는 늦게 잡힌 기사가 끼어들지 않고 다음 구간에 들어갑니다. 수집 경로만 바뀌어 다시 들어온 기사(직전 24시간에 같은 제목이 이미 수집됨)는 회색 바탕에 “이미 나옴” 배지로 표시하고 건수에서 뺍니다. ‘전체’는 보고 기간 전체를 출입처·기사묶음으로 다시 묶어 보여주고(구간이 달라도 같은 사안이면 한 묶음), 줄 앞 번호가 들어온 구간입니다. 맨 끝 ‘김광일 기자’는 발행 시각 기준(09:00 보고 당일 00:00~, 14:00 보고 당일 08:00~)입니다. 체크한 기사와 ‘여기까지 확인’은 이 기기에 저장되고, 머리줄 <b>기기 연동</b>을 켜면 다른 기기와 맞춰집니다(내 GitHub 비공개 gist). · <a href="../">최신 다이제스트</a> · <a href="../archive/">지난 다이제스트</a></p>';
    document.getElementById('main').innerHTML = html;
    syncBar();
  }

  // 김광일 기자 섹션(0-23) — '전체' 탭 맨 끝. 구간처럼 다룬다(id 'byline', si=BYSI)라
  // 체크 저장·보고 양식이 그대로 동작한다. 기사는 발행 시각순, 묶지 않는다.
  var BYSI = 99;
  function allSegs(r){ return r.byline ? r.segments.concat([r.byline]) : r.segments; }
  function drawByline(r, now){
    var b = r.byline; if(!b) return {html:'', any:false};
    if(ui.beat && ui.beat !== b.name) return {html:'', any:false};
    var st = segState(b, now), blocks = '', gc = 0, gdup = 0;
    (b.groups[0] ? b.groups[0].clusters : []).forEach(function(c, ci){
      c.forEach(function(it, ii){ prep(r, b, BYSI, 0, ci, ii, it, b.name); });
      var items = c.filter(keep); if(!items.length) return;
      var x = topicBlock(items, 'b'+ci, false); blocks += x.html; gc += x.fresh; gdup += x.dup;
    });
    var note = st === 'future' ? b.label.replace('~','')+'부터 모읍니다.' :
               '발행 '+esc(b.label)+(st==='live' ? ' 지금까지' : '')+' · 노컷뉴스 바이라인 기준';
    if(!blocks){
      if(ui.q || ui.filter !== 'all') return {html:'', any:false};
      blocks = '<p class="empty" style="padding:14px 0">'+(st==='future' ? '아직 시작 전입니다.' : '아직 올라온 기사가 없습니다.')+'</p>';
    }
    return {html: section(b.name, '<p class="note" style="margin:6px 0 4px">'+note+'</p>'+blocks, gc, gdup), any: gc + gdup > 0};
  }

  function fmtAt(ms){ var d = new Date(ms); return (ymd(d) === ymd(new Date()) ? '' : (d.getMonth()+1)+'/'+d.getDate()+' ') + hm(d); }
  function seenBar(r){
    var m = seenMarks[r.id], a = seenAt(r.id), last = D.last_seen ? T(D.last_seen) : null;
    var can = last != null && (a == null || last > a);
    var btn = '<button class="btn sec" type="button" id="seen-mark"'+(can?'':' disabled')+'>여기까지 확인'+(last!=null?' <span class="lg">('+hm(new Date(last))+' 수집분)</span>':'')+'</button>';
    var undo = m && m.at ? '<button class="btn" type="button" id="seen-undo">되돌리기</button>' : '';
    var msg;
    if(a == null) msg = '확인 기록 없음 — 다 보고 나서 <b>여기까지 확인</b>을 누르면, 다음에 열 때 그 뒤 들어온 기사에 <span class="nw">새</span> 표시가 붙습니다.';
    else { var n = newCount(r); msg = '마지막 확인 <b>'+fmtAt(a)+' 수집분</b>까지 · 그 뒤 <span class="nwc">새 '+n+'건</span>' + (n ? ' — ‘새 기사’ 버튼으로 그것만 볼 수 있습니다.' : ''); }
    return '<div class="seenbar"><span>'+msg+'</span><span class="sp">'+undo+btn+'</span></div>';
  }

  // '전체' 탭 — 구간을 출입처 → 기사묶음으로 합친다(0-22). 구조는 data.json의 rep.all
  // ([{name, c:[[[si,ci],...],...]}]), 기사는 구간 데이터에서 꺼내므로 체크·'이미 나옴'·
  // 보고 양식은 구간 탭과 같다.
  function drawMerged(r, now){
    var html = '', started = [];
    r.segments.forEach(function(s, si){ if(segState(s, now) !== 'future') started.push(si); });
    // 구간 현황 한 줄(기존 구간 머리줄 대신) — 구간 탭을 펼쳤을 때만(0-24d). 마지막 수집 시각은 머리줄에 있다.
    if(segOpen) html += '<p class="note">' + r.segments.map(function(s, si){
      var st = segState(s, now);
      return '<b>'+CIRC.charAt(si)+'</b> '+esc(s.label)+' '+(st==='future'?'대기':st==='live'?'<span class="st-live">진행 중</span>':'마감')+(st==='future'?'':' '+s.n+'건');
    }).join(' · ') + (r.segments.some(function(s){ return segState(s, now) === 'live'; }) && D.generated ?
      ' — '+hm(new Date(D.last_seen || D.generated))+' 수집분까지' : '') + '</p>';
    html += seenBar(r);
    var any = false;
    r.all.forEach(function(mg, mgi){
      if(ui.beat && mg.name !== ui.beat) return;
      var blocks = '', gc = 0, gdup = 0, gnew = 0;
      mg.c.forEach(function(refs, mi){
        var items = [];
        refs.forEach(function(ref){
          var si = ref[0], gi = ref[1], ci = ref[2], ii = ref[3], s = r.segments[si];
          var g = s && s.groups[gi], c = g && g.clusters[ci], it = c && c[ii];
          if(!it) return;
          prep(r, s, si, gi, ci, ii, it, g.name); if(keep(it)) items.push(it);
        });
        if(!items.length) return;
        // 새 기사를 앞으로(안정 정렬) — '이미 나옴'이 대표 줄로 올라와 새 기사를 접어 숨기지 않게
        // 확인 이후 새 기사 → 나머지 → '이미 나옴' 순(안정 정렬) — 새 후속보도가 본 기사 밑에 접혀 숨지 않게
        items = items.filter(isNew).concat(items.filter(function(x){ return !x.d && !isNew(x); }), items.filter(function(x){ return x.d; }));
        var b = topicBlock(items, 'm'+mgi+'-'+mi, true);
        blocks += b.html; gc += b.fresh; gdup += b.dup; gnew += b.nnew;
      });
      if(gc || gdup){ html += section(mg.name, blocks, gc, gdup, gnew); any = true; }
    });
    var by = drawByline(r, now); if(by.any) any = true; html += by.html;
    if(!any){
      var raw = started.some(function(si){ return r.segments[si].raw; });
      html += '<p class="empty">'+(raw ? '조건에 맞는 기사가 없습니다.' : '이 보고에 수집된 기사가 아직 없습니다.')+'</p>';
    }
    return html;
  }

  function keep(it){
    if(ui.filter==='mark' && !it.m) return false;
    if(ui.filter==='spread' && it.n < 2) return false;
    if(ui.filter==='picked' && !isPicked(it.rid, it.sid, it.key)) return false;
    if(ui.filter==='new' && MERGED && !isNew(it)) return false;
    if(ui.q && (it.t+' '+it.s).toLowerCase().indexOf(ui.q) === -1) return false;
    return true;
  }
  function row(it){
    var on = isPicked(it.rid, it.sid, it.key);
    var tag = it.m===SCOOP ? '<span class="tag scoop">단독</span>' : it.m===FLASH ? '<span class="tag flash">속보</span>' : '';
    return '<div class="row'+(on?' on':'')+(it.d?' rep':'')+'" data-u="'+esc(it.uid)+'"'+(it.d?' title="직전 구간에서 이미 수집된 같은 제목('+esc(it.d)+')"':'')+'>'+
      '<input class="pick" type="checkbox" '+(on?'checked':'')+' aria-label="보고에 포함">'+
      (MERGED && isNew(it) ? '<span class="nw" title="마지막 확인 뒤 수집">새</span>' : '')+
      (MERGED && it.si !== BYSI ? '<span class="sg'+(segState(curRep().segments[it.si], Date.now())==='live'?' live':'')+'" title="'+esc(curRep().segments[it.si].label)+' 구간">'+CIRC.charAt(it.si)+'</span>' : '')+tag+
      '<a class="title" href="'+esc(it.l)+'" target="_blank" rel="noopener">'+esc(it.t)+'</a>'+
      '<span class="meta">'+(it.d ? '<span class="rp">이미 나옴 '+esc(it.d)+'</span>' : '')+
      '<span>'+esc(it.s)+'</span><span class="mono">'+esc(it.p)+'</span>'+
      (it.n>1 ? '<span class="mono">전재'+it.n+'</span>' : '')+'</span></div>';
  }

  function syncBar(){
    var t = curTab(), r = curRep(), nseg = 0, nall = 0;
    if(t.seg) nseg = segPickCount(t.rep, t.seg);
    else nseg = allSegs(r).reduce(function(a,s){ return a + segPickCount(r, s); }, 0);
    nall = allSegs(r).reduce(function(a,s){ return a + segPickCount(r, s); }, 0);
    document.getElementById('n-seg').textContent = nseg;
    document.getElementById('n-all').textContent = nall;
    document.getElementById('make-seg').disabled = !nseg || !!t.all;
    document.getElementById('make-seg').hidden = !!t.all;      // '전체'에선 구간 양식 버튼 숨김(0-24b)
    document.getElementById('segpart').hidden = !!t.all;
    // 하단 '여기까지 확인'(0-24d) — '전체' 탭에서만. 위쪽 확인 바와 같은 동작.
    var bs = document.getElementById('bar-seen');
    bs.hidden = !(t.all && r.all);
    if(!bs.hidden){
      var a = seenAt(r.id), last = D.last_seen ? T(D.last_seen) : null;
      var can = last != null && (a == null || last > a);
      bs.disabled = !can;
      bs.innerHTML = can ? '여기까지 확인' : '확인함 ✓ <span class="lg">'+(a != null ? hm(new Date(a)) : '')+'</span>';
    }
    document.getElementById('make-all').disabled = !nall;
    document.getElementById('all-pre').textContent = t.over ? '현재 보고 ' : '보고 ';
  }

  // ---------- 보고 양식 ----------
  function findItem(rep, seg, key){
    for(var gi=0; gi<seg.groups.length; gi++){
      var g = seg.groups[gi];
      for(var ci=0; ci<g.clusters.length; ci++){
        var c = g.clusters[ci];
        for(var ii=0; ii<c.length; ii++){
          var it = c[ii];
          if((it.l || it.t) === key) return {t:it.t, s:it.s, l:it.l, m:it.m, g:g.name, ord:gi*1e5+ci*100+ii};
        }
      }
    }
    return null;
  }
  // 본인(김광일 기자) 기사 판별(0-23b) — 이 보고의 byline 섹션에 있는 기사면 어느 섹션에서
  // 체크했든 <기처리>로 보낸다. 링크가 같거나, 제목(공백·문장부호 뺀 것)이 같으면 같은 기사.
  function tnorm(t){ return String(t).replace(/[^0-9A-Za-z가-힣]/g, ''); }
  function bylineIndex(rep){
    var m = {links:{}, titles:{}, tag:''}, b = rep.byline;
    if(!b) return m;
    m.tag = String(b.name || '').replace(/\s*기자$/, '');
    (b.groups[0] ? b.groups[0].clusters : []).forEach(function(c, ci){
      c.forEach(function(it){ if(it.l) m.links[it.l] = ci; m.titles[tnorm(it.t)] = ci; });
    });
    return m;
  }
  function buildReport(rep, segs){
    var items = [], mine = [], seen = {}, bx = bylineIndex(rep);
    segs.forEach(function(seg, si){
      var o = picks[rep.id] && picks[rep.id][seg.id]; if(!o) return;
      Object.keys(o).forEach(function(k){
        var it = findItem(rep, seg, k); if(!it) return;
        var bi = (it.l && it.l in bx.links) ? bx.links[it.l] : bx.titles[tnorm(it.t)];
        if(bi != null){
          // 같은 기사를 과기정통부 섹션과 김광일 섹션에서 둘 다 체크해도 한 줄
          if(seen['by' + bi]) return; seen['by' + bi] = 1;
          it.bi = bi; mine.push(it); return;
        }
        var dk = it.t + '|' + it.s; if(seen[dk]) return; seen[dk] = 1;
        it.ord += si * 1e7; items.push(it);
      });
    });
    var rank = function(n){ var i = REPORT_ORDER.indexOf(n); return i === -1 ? REPORT_ORDER.length : i; };
    items.sort(function(a,b){
      var sa = a.m===SCOOP?0:1, sb = b.m===SCOOP?0:1; if(sa!==sb) return sa-sb;
      var ra = rank(a.g), rb = rank(b.g); if(ra!==rb) return ra-rb;
      return a.ord - b.ord;
    });
    var lines = [];
    if(items.length || !mine.length){
      lines.push('<모니터>');
      items.forEach(function(it){ lines.push(it.t + '(' + it.s + ')'); });
    }
    if(mine.length){
      // <기처리>: 본인 기사, 발행순, 매체 대신 이름
      mine.sort(function(a,b){ return a.bi - b.bi; });
      if(lines.length) lines.push('');
      lines.push('<기처리>');
      mine.forEach(function(it){ lines.push(it.t + '(' + bx.tag + ')'); });
    }
    return {text: lines.join('\n'), n: items.length + mine.length};
  }

  var veil = document.getElementById('veil'), report = document.getElementById('report');
  var hint = document.getElementById('copyhint'), hintDefault = hint.textContent, panelSeg = null;
  function openPanel(scope){
    var t = curTab(), r = curRep(), res, sub;
    if(scope === 'seg' && t.seg){
      res = buildReport(t.rep, [t.seg]); sub = (t.over ? '다음 보고 ' : '') + t.seg.label; panelSeg = t;
    } else {
      res = buildReport(r, allSegs(r)); sub = r.title + ' 전체'; panelSeg = null;
    }
    report.value = res.text;
    document.getElementById('psub').textContent = sub + ' · ' + res.n + '건';
    document.getElementById('unpick').hidden = !panelSeg;
    hint.textContent = hintDefault; hint.classList.remove('ok');
    veil.hidden = false; report.focus();
  }
  function closePanel(){ veil.hidden = true; }

  // ---------- 이벤트 ----------
  document.getElementById('reports').addEventListener('click', function(e){
    var b = e.target.closest('button[data-r]'); if(!b) return;
    ui.rep = b.getAttribute('data-r'); repManual = true; tabManual = false; pickDefaults(); draw();
  });
  document.getElementById('tabs').addEventListener('click', function(e){
    var tg = e.target.closest('button[data-tog]');
    if(tg){
      segOpen = !segOpen;
      try{ localStorage.setItem(SEGSTORE, segOpen ? '1' : '0'); }catch(err){}
      if(!segOpen){ var ct = curTab(); if(!ct || !tabShown(ct)){ ui.tab = ui.rep + '/all'; } }
      draw(); return;
    }
    var b = e.target.closest('button[data-t]'); if(!b) return;
    ui.tab = b.getAttribute('data-t'); tabManual = true;
    try{ localStorage.setItem(TABSTORE, JSON.stringify({rep: ui.rep, tab: ui.tab, at: Date.now()})); }catch(e){}
    draw(); window.scrollTo(0, 0);
  });
  document.getElementById('beats').addEventListener('click', function(e){
    var b = e.target.closest('button[data-b]'); if(!b) return;
    var g = b.getAttribute('data-b'); ui.beat = (ui.beat === g) ? null : g; draw();
  });
  [].forEach.call(document.querySelectorAll('.seg button'), function(btn){
    btn.addEventListener('click', function(){
      ui.filter = btn.dataset.f;
      [].forEach.call(document.querySelectorAll('.seg button'), function(b){ b.setAttribute('aria-pressed', String(b===btn)); });
      draw();
    });
  });
  var q = document.getElementById('q'), timer;
  q.addEventListener('input', function(){ clearTimeout(timer); timer = setTimeout(function(){ ui.q = q.value.trim().toLowerCase(); draw(); }, 120); });

  function seenAction(kind){
    var r = curRep(), m = seenMarks[r.id];
    if(kind === 'mark' && D.last_seen){
      if(m && m.at && T(m.at) >= T(D.last_seen)) return;
      seenMarks[r.id] = {at: D.last_seen, prev: m ? m.at : null, t: new Date().toISOString()};
    } else if(kind === 'undo' && m && m.at){
      // 기록이 없어지는 되돌리기도 지우지 않고 at:null로 남긴다 — 다른 기기에 '지웠다'를 전하려고(0-26)
      seenMarks[r.id] = {at: m.prev || null, prev: null, t: new Date().toISOString()};
    }
    saveSeen(); var y = window.scrollY; draw(); window.scrollTo(0, y);
  }
  document.getElementById('bar-seen').addEventListener('click', function(){ seenAction('mark'); });
  var main = document.getElementById('main');
  main.addEventListener('change', function(e){
    var cb = e.target; if(!cb.classList || !cb.classList.contains('pick')) return;
    var rowEl = cb.closest('.row'), it = ITEMS[rowEl.getAttribute('data-u')]; if(!it) return;
    setPick(it.rid, it.sid, it.key, cb.checked);
    rowEl.classList.toggle('on', cb.checked);
    savePicks();
    // 탭의 ✓ 숫자와 하단 바만 갱신한다(본문을 다시 그리면 스크롤·펼침이 초기화된다)
    var tabsEl = document.getElementById('tabs'), sx = tabsEl.scrollLeft;
    drawTabs(); tabsEl.scrollLeft = sx;
  });
  main.addEventListener('click', function(e){
    // '여기까지 확인' / 되돌리기(0-24). 기준은 누른 시각이 아니라 이 화면이 보여주는 마지막
    // 수집 시각 — 화면을 연 뒤 들어온(아직 못 본) 기사가 '본 것'이 되지 않게.
    var sb = e.target.closest('#seen-mark, #seen-undo');
    if(sb){ seenAction(sb.id === 'seen-undo' ? 'undo' : 'mark'); return; }
    var btn = e.target.closest('.more'); if(!btn) return;
    var box = document.getElementById(btn.getAttribute('aria-controls')), open = box.hidden;
    box.hidden = !open; btn.setAttribute('aria-expanded', String(open));
    var mix = btn.getAttribute('data-mix') || '';
    btn.textContent = open ? '접기 ('+box.children.length+'건 같은 사안'+mix+')' : '+'+box.children.length+'건 같은 사안'+mix;
  });

  document.getElementById('make-seg').addEventListener('click', function(){ openPanel('seg'); });
  document.getElementById('make-all').addEventListener('click', function(){ openPanel('all'); });
  document.getElementById('close').addEventListener('click', closePanel);
  veil.addEventListener('click', function(e){ if(e.target === veil) closePanel(); });
  document.addEventListener('keydown', function(e){ if(e.key === 'Escape'){ if(!sveil.hidden) sveil.hidden = true; else if(!veil.hidden) closePanel(); } });
  document.getElementById('unpick').addEventListener('click', function(){
    if(!panelSeg) return;
    var o = picks[panelSeg.rep.id] && picks[panelSeg.rep.id][panelSeg.seg.id];
    Object.keys(o || {}).forEach(function(k){ setPick(panelSeg.rep.id, panelSeg.seg.id, k, false); });
    savePicks(); closePanel(); draw();
  });
  document.getElementById('copy').addEventListener('click', function(){
    var done = function(){ hint.textContent = '복사됐습니다.'; hint.classList.add('ok'); };
    var fail = function(){ report.focus(); report.select(); hint.textContent = '자동 복사가 막혔습니다 — 전체 선택했으니 직접 복사하세요.'; hint.classList.remove('ok'); };
    try{ if(navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(report.value).then(done, fail); else fail(); }catch(err){ fail(); }
  });
  document.getElementById('reload').addEventListener('click', function(){ load(true); });

  // 탭으로 돌아오거나 창에 포커스가 오면 새로 받아온다(1분에 한 번까지). 켜 둔 채로도 5분마다.
  // 기기 연동(0-26): 돌아올 때 받아 오고(15초에 한 번까지), 떠날 때 아직 안 보낸 변경이 있으면 바로 보낸다.
  document.addEventListener('visibilitychange', function(){
    if(document.hidden){ if(sync.timer) syncNow(); return; }
    load(false); syncIfStale();
  });
  window.addEventListener('pagehide', function(){ if(sync.timer) syncNow(); });
  window.addEventListener('focus', function(){ load(false); syncIfStale(); });
  setInterval(function(){ if(!document.hidden){ load(true); if(cfg.token) syncNow(); } }, 5*60e3);
  // 1분마다 시각만 다시 보고(보고 전환·'n분 전'), 데이터는 그대로
  setInterval(function(){
    if(!D || document.hidden) return;
    var before = ui.rep + '#' + ui.tab + '#' + tabList().map(function(t){ return t.key + (t.seg ? segState(t.seg, Date.now()) : ''); }).join();
    pickDefaults();
    var after = ui.rep + '#' + ui.tab + '#' + tabList().map(function(t){ return t.key + (t.seg ? segState(t.seg, Date.now()) : ''); }).join();
    drawHeader();
    if(before !== after && veil.hidden) draw();
  }, 60e3);

  syncUI();
  load(true);
  syncNow();
})();
</script>
</body></html>
"""
