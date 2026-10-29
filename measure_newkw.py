#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""0-78 사전 측정(일회성, 임시 브랜치 전용 — main에는 넣지 않는다).

새 검색어(방미심위·국가AI전략위)를 네이버·구글로 한 번 훑어, 지금 DB에 없는 기사가
얼마나 되는지와 제목 판정 전용 후보어(방심위·방송미디어통신심의위·AI전략위)의 실제 쓰임을 표본으로 남긴다.
DB는 읽기만 한다. 결과는 measure/out.json.
"""
import json, os, sqlite3, datetime, urllib.parse
import news_monitor as m

SINCE = datetime.datetime(2026, 10, 15, tzinfo=m.KST)
SEARCH = ["방송미디어통신심의위원회", "방미심위", "국가인공지능전략위원회", "국가AI전략위원회", "국가AI전략위"]
SAMPLE = ["방심위", "방송미디어통신심의위", "AI전략위"]
MAX_PAGES = 10

conn = sqlite3.connect(m.DB_PATH)
known = {r[0] for r in conn.execute("SELECT id FROM articles")}
calls = 0


def naver(kw):
    global calls
    out = []
    total = None
    for page in range(MAX_PAGES):
        url = ("https://openapi.naver.com/v1/search/news.json?query=" + urllib.parse.quote(kw)
               + f"&display=100&start={page * 100 + 1}&sort=date")
        try:
            raw = m.http_get(url, headers={"X-Naver-Client-Id": m.NAVER_CLIENT_ID,
                                           "X-Naver-Client-Secret": m.NAVER_CLIENT_SECRET})
            calls += 1
            d = json.loads(raw)
        except Exception as e:
            out.append({"error": str(e)[:200], "page": page})
            break
        items = d.get("items", [])
        if page == 0:
            total = d.get("total")
        stop = False
        for it in items:
            link = it.get("originallink") or it.get("link") or ""
            title = m.clean(it.get("title"))
            try:
                pub = datetime.datetime.strptime(it.get("pubDate", ""), "%a, %d %b %Y %H:%M:%S %z").astimezone(m.KST)
            except Exception:
                pub = None
            if pub and pub < SINCE:
                stop = True
                break
            aid = m.article_id(link, title)
            out.append({"t": title, "l": link, "nl": it.get("link") or "", "p": pub.isoformat() if pub else "",
                        "d": m.clean(it.get("description") or "")[:160], "known": aid in known, "id": aid})
        if stop or len(items) < 100:
            break
    return {"total": total, "items": out}


res = {"generated": datetime.datetime.now(m.KST).isoformat(), "since": SINCE.isoformat(),
       "db_rows": len(known), "naver": {}, "google": {}}
for kw in SEARCH + SAMPLE:
    res["naver"][kw] = naver(kw)
    g = m.fetch_google(kw, set())
    res["google"][kw] = [{"t": x["title"], "l": x["link"], "p": x["pub_dt"], "s": x["source"],
                          "known": x["id"] in known, "id": x["id"]} for x in g]
res["calls"] = calls
os.makedirs("measure", exist_ok=True)
with open("measure/out.json", "w", encoding="utf-8") as f:
    json.dump(res, f, ensure_ascii=False, indent=0)
print("calls", calls, {k: len(v["items"]) for k, v in res["naver"].items()},
      {k: len(v) for k, v in res["google"].items()})
