# -*- coding: utf-8 -*-
"""
merge_db.py — news_monitor.db 두 개를 행 단위로 합친다 (0-18, 2026-09-20)

왜 필요한가
-----------
check와 digest가 같은 30분 경계에 뜨면, 나중에 끝난 쪽이 push할 때 원격이 이미
앞서 있다. 예전 워크플로는 `git pull --rebase`로 따라잡으려 했는데,
news_monitor.db는 43MB 바이너리라 git이 병합하지 못하고 반드시

    warning: Cannot merge binary files: news_monitor.db
    CONFLICT (content): Merge conflict in news_monitor.db

로 끝난다. 재시도해도 같은 충돌이라 5회 전부 실패하고, 그 실행이 만든 다이제스트
페이지가 통째로 유실된다(2026-09-20 22:00 실측).

git에게 바이너리 병합을 시키는 대신, **SQLite 수준에서 합집합을 만든다.**
이 DB의 테이블은 전부 PK가 있고 사실상 append-only라 합치기가 안전하다.

  articles       : id PK        → INSERT OR IGNORE (기사 합집합)
  excluded_log   : id PK        → INSERT OR IGNORE
  alerted_topics : key PK       → 새 키는 INSERT, 겹치는 키는 보수적으로 병합
                                  (first_dt=이른 쪽, last_dt=늦은 쪽,
                                   tier=낮은 쪽=중요한 쪽, hits=큰 쪽)
  api_calls      : day PK, cnt  → 큰 쪽(MAX). 합(SUM)이 아니다 — 두 사본은 같은
                                  기준값에서 갈라져 나왔으므로 더하면 공통분이
                                  이중계산된다. MAX가 실제값에 가장 가깝고,
                                  쿼터 가드 용도상 과소집계보다 안전하다.

사용법
------
    python merge_db.py <ours.db> <base.db>

<base.db>(원격에서 받아온 정본)에 <ours.db>(이번 실행이 만든 사본)의 행을 부어
넣는다. <base.db>가 제자리에서 갱신된다.
"""

import os
import sqlite3
import sys


def _tables(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def merge(ours_path, base_path):
    if not os.path.exists(ours_path):
        print(f"[merge_db] 우리 DB 없음: {ours_path} — 병합 생략")
        return
    if not os.path.exists(base_path):
        print(f"[merge_db] 기준 DB 없음: {base_path} — 우리 것을 그대로 쓴다")
        return

    conn = sqlite3.connect(base_path)
    conn.execute("ATTACH DATABASE ? AS ours", (ours_path,))
    have = _tables(conn)
    added = {}

    # ---- 단순 합집합 (PK 충돌은 기준 DB 쪽을 남긴다) ----
    for t in ("articles", "excluded_log"):
        if t not in have:
            continue
        before = conn.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0]
        conn.execute(f"INSERT OR IGNORE INTO main.{t} SELECT * FROM ours.{t}")
        after = conn.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0]
        added[t] = after - before

    # ---- alerted_topics: 새 키는 넣고, 겹치는 키는 보수적으로 합친다 ----
    if "alerted_topics" in have:
        before = conn.execute("SELECT COUNT(*) FROM main.alerted_topics").fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO main.alerted_topics SELECT * FROM ours.alerted_topics")
        after = conn.execute("SELECT COUNT(*) FROM main.alerted_topics").fetchone()[0]
        added["alerted_topics"] = after - before
        # 억제 창은 '가장 이른 최초 알림'이 기준이라야 과소억제(=재알림 누수)가 없다.
        conn.execute("""
            UPDATE main.alerted_topics AS m
               SET first_dt = MIN(m.first_dt, (SELECT o.first_dt FROM ours.alerted_topics o WHERE o.key=m.key)),
                   last_dt  = MAX(m.last_dt,  (SELECT o.last_dt  FROM ours.alerted_topics o WHERE o.key=m.key)),
                   tier     = MIN(m.tier,     (SELECT o.tier     FROM ours.alerted_topics o WHERE o.key=m.key)),
                   hits     = MAX(m.hits,     (SELECT o.hits     FROM ours.alerted_topics o WHERE o.key=m.key))
             WHERE EXISTS (SELECT 1 FROM ours.alerted_topics o WHERE o.key=m.key)
        """)

    # ---- api_calls: 큰 쪽 ----
    if "api_calls" in have:
        conn.execute("""
            INSERT INTO main.api_calls(day, cnt)
                 SELECT day, cnt FROM ours.api_calls
              WHERE true
            ON CONFLICT(day) DO UPDATE SET cnt = MAX(cnt, excluded.cnt)
        """)

    conn.commit()
    conn.execute("DETACH DATABASE ours")
    conn.close()

    summary = ", ".join(f"{t} +{n}" for t, n in sorted(added.items())) or "추가 없음"
    print(f"[merge_db] 병합 완료: {summary}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("사용법: python merge_db.py <ours.db> <base.db>")
    merge(sys.argv[1], sys.argv[2])
