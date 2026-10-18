"""고른 기사 기록 수확 (인수인계 37항 다음 단계, 0-30)

뉴스레이다 라이브 페이지에서 체크한 기사(=보고 양식으로 복사한 기사)는 기기 연동 gist
(newsradar-sync.json)에 쌓이지만 60일이 지나면 지워진다. 또 '그때 화면에 떴지만 안 고른
기사'를 복원하려면 DB의 articles가 필요한데, 이것도 RETENTION_DAYS(90일)가 지나면 지워진다.
그래서 끝난 보고마다 아래 두 가지를 picks/YYYY/<보고id>.json 한 파일로 저장소에 남긴다.

  picks : gist의 체크 기록 그대로 {구간id: {기사키: [1|0, 바꾼 시각 ms]}} (해제 기록 포함)
  items : 그 보고 화면에 떴던 기사 전부 — 보고 마감 5분 전 시점으로 build_live_data()를
          다시 돌려 만든다(10/13 검증: 19회 보고의 체크 86건 전부 재구성 화면에서 찾아짐).

동작 규칙
- gist는 '비밀(secret) gist' — 목록엔 안 나오지만 id를 알면 누구나 읽을 수 있다. 그래서
  읽기에는 토큰이 필요 없다. id는 Actions 비밀값 NEWSRADAR_GIST_ID로 받는다.
- 이미 파일이 있는 보고는 화면 목록(items)을 다시 만들지 않고 체크만 병합한다
  (기사·바꾼 시각 단위로 나중 기록이 이긴다). 바뀐 게 없으면 파일을 건드리지 않는다.
- 보고 마감(09:00/14:00) 30분 뒤부터 '끝난 보고'로 본다. 진행 중인 보고는 안 쓴다.
- DB 보관 기한이 거의 지난 보고(SNAPSHOT_MAX_AGE_DAYS 초과)는 화면 목록 없이 체크만 남긴다.
- --revisions: gist 리비전 기록을 날짜별 마지막 판 하나씩 훑어, 최신판에선 이미 지워진
  체크까지 되살린다(수동 harvest 모드에서 한 번 쓰는 용도).

사용: python harvest_picks.py [--revisions] [--sync-file 파일]   (--sync-file은 시험용)
"""
import datetime
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import news_monitor as nm

KST = nm.KST
SYNC_FILE = "newsradar-sync.json"
PICKS_DIR = Path(__file__).resolve().parent / "picks"
FINISH_GRACE_MIN = 30
SNAPSHOT_MAX_AGE_DAYS = 80          # RETENTION_DAYS(90)보다 여유 있게
COLS = ["sid", "group", "cluster", "pos", "size", "mark", "n", "dup", "source", "title", "link"]
COLS_NOTE = ("sid=구간id(byline=김광일 기자), group=출입처, cluster='전체' 탭 출입처 안 묶음 순서(0=맨 위), "
             "pos=묶음 안 순서(0=대표줄), size=묶음 기사 수, mark=🔥단독/⚡속보, n=전재 매체 수, "
             "dup=이미 나옴(최초 수집 시각), source=매체, title=제목, link=기사 키(체크 기록의 키와 같음)")


# ---------------------------------------------------------------- gist 읽기
def _get(url, accept="application/vnd.github+json"):
    h = {"Accept": accept, "User-Agent": "news-monitor-harvest"}
    tok = os.environ.get("GIST_READ_TOKEN", "").strip()
    if tok and "api.github.com" in url:
        h["Authorization"] = "Bearer " + tok
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _file_content(g):
    f = (g.get("files") or {}).get(SYNC_FILE)
    if not f:
        return {}
    if f.get("truncated") and f.get("raw_url"):
        return json.loads(_get(f["raw_url"], accept="*/*"))
    return json.loads(f.get("content") or "{}")


def fetch_latest(gid):
    """API로 최신판. API가 막히면(비인증 한도 60회/시간 등) raw 주소로 한 번 더."""
    try:
        return _file_content(json.loads(_get(f"https://api.github.com/gists/{gid}")))
    except urllib.error.HTTPError as e:
        if e.code not in (403, 429):
            raise
        owner = os.environ.get("GIST_OWNER", "tolibre")
        print(f"[harvest] gist API {e.code} — raw 주소로 다시 시도")
        return json.loads(_get(f"https://gist.githubusercontent.com/{owner}/{gid}/raw/{SYNC_FILE}", accept="*/*"))


def fetch_daily_revisions(gid):
    """리비전 목록에서 날짜(KST)별 마지막 판만 받아 온다. 체크는 최소 14일은 남아 있었으므로
    하루 한 판이면 지워지기 전 상태를 빠짐없이 본다."""
    revs, page = [], 1
    while True:
        lst = json.loads(_get(f"https://api.github.com/gists/{gid}/commits?per_page=100&page={page}"))
        if not lst:
            break
        revs.extend(lst)
        if len(lst) < 100:
            break
        page += 1
    last_of_day = {}
    for r in revs:   # 최신순
        d = datetime.datetime.fromisoformat(r["committed_at"].replace("Z", "+00:00")).astimezone(KST).date()
        last_of_day.setdefault(d, r["version"])
    out = []
    for d in sorted(last_of_day):
        try:
            out.append(_file_content(json.loads(_get(f"https://api.github.com/gists/{gid}/{last_of_day[d]}"))))
        except Exception as e:   # 리비전 하나 실패는 건너뛴다
            print(f"[harvest] 리비전 {d} 읽기 실패: {e}")
    print(f"[harvest] 리비전 {len(revs)}개 중 날짜별 {len(out)}개 훑음")
    return out


# ---------------------------------------------------------------- 병합
def merge_picks(dst, src):
    """{구간: {키: [v, t]}} 둘을 합친다. 같은 키는 바꾼 시각이 나중인 쪽. 반환: 바뀌었는지."""
    changed = False
    for sid, keys in (src or {}).items():
        for k, e in (keys or {}).items():
            if not (isinstance(e, list) and len(e) == 2):
                continue
            cur = dst.setdefault(sid, {}).get(k)
            if cur is None or e[1] > cur[1]:
                if cur != e:
                    dst[sid][k] = [int(e[0]), int(e[1])]
                    changed = True
    return changed


# ---------------------------------------------------------------- 화면 재구성
def report_deadline(rid):
    d = datetime.datetime.strptime(rid[:8], "%Y%m%d")
    h = 9 if rid.endswith("-am") else 14
    return datetime.datetime(d.year, d.month, d.day, h, 0, tzinfo=KST)


def snapshot_items(conn, rid, picks=None):
    """보고 마감 5분 전 시점의 화면 기사 목록. 보고를 못 찾으면 None.
    당직 섹션(0-44, 뉴스레이다 '당직' 스위치를 켜야 보임)은 그 섹션에서 고른 기사가 있을 때만 싣는다 —
    스위치를 안 켠 날엔 화면에 없던 기사라 '노출됐지만 안 고름'으로 세면 안 되고, 하루 수천 건이라
    picks/ 파일이 몇 배로 커진다. picks = 이 보고의 체크 기록({구간id: {키: [1|0, 시각]}})."""
    now = report_deadline(rid) - datetime.timedelta(minutes=5)
    data = nm.build_live_data(conn, now)
    # 당직 날 보고 시간표(0-48)의 보고도 찾는다 — 휴일 당일 보고(20261018-am 등)는 당직판에만 있고,
    # 다음 업무일 09:00 보고는 평소판·당직판이 같은 id라 체크 기록의 구간 id가 더 많이 겹치는 쪽을 쓴다(같으면 평소판).
    cands = [r for r in data["reports"] if r["id"] == rid]
    if hasattr(nm, "resolve_duty_reports"):
        for reps in nm.resolve_duty_reports(data).values():
            cands += [r for r in reps if r["id"] == rid and all(r is not x for x in cands)]
    if not cands:
        return None
    psegs = set((picks or {}).keys())
    rep = max(cands, key=lambda r: len(psegs & {s["id"] for s in r["segments"]}))
    # '전체' 탭 묶음 순서: (구간 si, gi, ci, ii) → (묶음 순번, 묶음 안 순번, 묶음 크기)
    pos = {}
    for mg in rep["all"]:
        for mi, refs in enumerate(mg["c"]):
            for k, ref in enumerate(refs):
                pos[tuple(ref)] = (mi, k, len(refs))
    picked = {k for keys in (picks or {}).values() for k, e in keys.items() if e and e[0]}
    duty = set(data.get("duty") or [])
    keep_duty = set()
    for seg in rep["segments"]:
        for g in seg["groups"]:
            if g["name"] in duty and any((it.get("l") or it.get("t")) in picked
                                         for clu in g["clusters"] for it in clu):
                keep_duty.add(g["name"])
    items = []
    for si, seg in enumerate(rep["segments"]):
        for gi, g in enumerate(seg["groups"]):
            if g["name"] in duty and g["name"] not in keep_duty:
                continue
            for ci, clu in enumerate(g["clusters"]):
                for ii, it in enumerate(clu):
                    mi, k, size = pos.get((si, gi, ci, ii), (None, None, None))
                    items.append([seg["id"], g["name"], mi, k, size, it.get("m", ""), it.get("n", 1),
                                  it.get("d", ""), it.get("s", ""), it.get("t", ""), it.get("l") or it.get("t", "")])
    by = rep.get("byline") or {}
    for g in by.get("groups", []):
        for clu in g.get("clusters", []):
            for it in clu:
                items.append(["byline", g.get("name", ""), None, None, None, it.get("m", ""), it.get("n", 1),
                              "", it.get("s", ""), it.get("t", ""), it.get("l") or it.get("t", "")])
    return items


# ---------------------------------------------------------------- 파일
def path_for(rid):
    return PICKS_DIR / rid[:4] / f"{rid}.json"


def dump(doc):
    """사람이 읽고 diff로 보기 좋게: 기사 한 건이 한 줄."""
    head = {k: v for k, v in doc.items() if k != "items"}
    s = json.dumps(head, ensure_ascii=False, indent=1, sort_keys=True)
    if doc.get("items") is None:
        return s[:-1].rstrip() + ',\n "items": null\n}\n'
    rows = ",\n".join("  " + json.dumps(r, ensure_ascii=False) for r in doc["items"])
    return s[:-1].rstrip() + ',\n "items": [\n' + rows + "\n ]\n}\n"


def unmatched(doc):
    if not doc.get("items"):
        return None
    shown = {(r[0], r[10]) for r in doc["items"]}
    return sum(1 for sid, keys in doc["picks"].items() for k, e in keys.items()
               if e[0] and (sid, k) not in shown)


def harvest(sync_docs, now=None, conn=None):
    now = now or datetime.datetime.now(KST)
    picks = {}
    for d in sync_docs:
        for rid, segs in ((d or {}).get("picks") or {}).items():
            merge_picks(picks.setdefault(rid, {}), segs)
    own_conn = conn is None
    conn = conn or nm.db()
    stats = {"new": 0, "updated": 0, "same": 0, "pending": 0, "no_items": 0}
    try:
        for rid in sorted(picks):
            if len(rid) != 11 or rid[8:] not in ("-am", "-pm"):
                continue
            if now < report_deadline(rid) + datetime.timedelta(minutes=FINISH_GRACE_MIN):
                stats["pending"] += 1
                continue
            p = path_for(rid)
            if p.exists():
                doc = json.loads(p.read_text(encoding="utf-8"))
                if not merge_picks(doc["picks"], picks[rid]):
                    stats["same"] += 1
                    continue
                doc["picks_updated"] = now.isoformat(timespec="seconds")
                doc["unmatched"] = unmatched(doc)
                stats["updated"] += 1
            else:
                age = (now.date() - report_deadline(rid).date()).days
                items = snapshot_items(conn, rid, picks[rid]) if age <= SNAPSHOT_MAX_AGE_DAYS else None
                if items is None:
                    stats["no_items"] += 1
                doc = {"v": 1, "rid": rid, "cols": COLS, "cols_note": COLS_NOTE,
                       "built": now.isoformat(timespec="seconds"),
                       "built_from": "build_live_data(마감 5분 전) 재구성" if items is not None else "화면 목록 없음(DB 보관 기한 지남)",
                       "picks": {}, "items": items}
                merge_picks(doc["picks"], picks[rid])
                doc["unmatched"] = unmatched(doc)
                stats["new"] += 1
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(dump(doc), encoding="utf-8")
            n_pick = sum(1 for keys in doc["picks"].values() for e in keys.values() if e[0])
            n_items = len(doc["items"]) if doc.get("items") else 0
            print(f"[harvest] {rid}: 체크 {n_pick}건 · 화면 {n_items}건 · 못 찾음 {doc['unmatched']}")
    finally:
        if own_conn:
            conn.close()
    print(f"[harvest] 새 보고 {stats['new']} · 체크 갱신 {stats['updated']} · 변화 없음 {stats['same']} · "
          f"진행 중 {stats['pending']} · 화면 목록 없음 {stats['no_items']}")
    return stats


def main(argv):
    if "--sync-file" in argv:
        docs = [json.loads(Path(argv[argv.index("--sync-file") + 1]).read_text(encoding="utf-8"))]
    else:
        gid = os.environ.get("NEWSRADAR_GIST_ID", "").strip()
        if not gid:
            print("[harvest] NEWSRADAR_GIST_ID 비밀값이 없어 건너뜁니다.")
            return 0
        docs = [fetch_latest(gid)]
        if "--revisions" in argv:
            docs = fetch_daily_revisions(gid) + docs
    harvest(docs)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
