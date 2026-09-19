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


def render_html(label, start, end, raw_count, groups):
    """groups: build_groups_data()의 반환값. 시안 HTML을 그대로 옮기고
    <script id="data"> 안의 JSON만 교체한다. CSS/구조/JS는 시안과 동일하게 유지 —
    D 세션은 렌더링(데이터 주입)만 한다."""
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

    return _TEMPLATE.replace("__LABEL__", label_esc).replace("__DATA_JSON__", data_json_safe)


def save_page(html_text, out_path=PAGE_PATH):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_text)
    return out_path


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


if __name__ == "__main__":
    # 수동 스모크 테스트용 — 실제 배선은 news_monitor.py의 PAGE_MODE 분기에서 호출된다.
    demo = build_groups_data([("테스트그룹", [])])
    out = render_html("테스트 다이제스트", "09/17 13:30", "09/17 17:30", 0, demo)
    path = save_page(out)
    print(f"페이지 저장: {path}")
