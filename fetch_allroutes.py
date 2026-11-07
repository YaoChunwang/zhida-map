#!/usr/bin/env python3
"""全国车次经停库抓取器 —— data/allroutes.json（车次号→[{station,arrive,depart,order}]）。

数据源（轻接口，无 leftTicket/无 search，防反爬）：
  1) 车次发现：GET /otn/queryTrainInfo/getTrainName?date=D
     一次返回当日全国车次清单（车次号(起讫站) + 官方 train_no），14900+ 车次。
     跑 5 个日期并集 = 未来 15 天车次号 + 各自行车日期 + 现行 train_no。
  2) 经停表：GET /otn/queryTrainInfo/query?leftTicketDTO.train_no=<官方编号>
     &leftTicketDTO.train_date=<该车开行日>&rand_code=  → 完整经停表。
节奏：ROUTE_GAP=2.5s/车次 + 每 60 车次歇 20s + mcp12306.RateLimiter 全局限流退避。
断点续跑：allroutes.json 每 25 车次落盘（flock 原子写）；失败清单
data/allroutes-retry.json（换日期轮换重试，MAX_ATTEMPTS 后放弃）。
进度：data/allroutes-progress.json（UI 可见）。运行时新发现车次：
data/allroutes-extra.json（server 回填）也并入抓取。

Usage:
  python3 fetch_allroutes.py                # 全量跑（可挂后台/定时任务）
  python3 fetch_allroutes.py --limit 500    # 只拉 500 车次（冒烟样本）
  python3 fetch_allroutes.py --retry        # 只重试失败清单
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp12306 import RateLimitError, QueryError, rate_limiter, classify_failure

BASE = os.path.dirname(os.path.abspath(__file__))
from railconfig import DATA_DIR as DATA
TRAINLIST_PATH = os.path.join(DATA, "trainlist.json")
ALLROUTES_PATH = os.path.join(DATA, "allroutes.json")
RETRY_PATH = os.path.join(DATA, "allroutes-retry.json")
PROGRESS_PATH = os.path.join(DATA, "allroutes-progress.json")
EXTRA_PATH = os.path.join(DATA, "allroutes-extra.json")
LOG_PATH = os.path.join(DATA, "allroutes.log")

ROUTE_GAP = 2.5          # 每车次 ≥2.5s
WRITE_EVERY = 25         # 每 25 车次落盘一次（断点续跑粒度）
MAX_ATTEMPTS = 4         # 每车次最多换 4 个开行日期
STALE_TRAINLIST_DAYS = 1  # trainlist 超过 N 天重新发现
DISCOVER_DATES = (2, 5, 8, 11, 14)   # 发现用车次日期（覆盖 15 天窗口）

H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
     "Referer": "https://kyfw.12306.cn/otn/queryTrainInfo/init",
     "Accept": "application/json, text/javascript, */*; q=0.01"}


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- JSON 存储（flock 原子写）
def atomic_write(path, obj):
    import fcntl
    lock = path + ".lock"
    with open(lock, "a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            tmp = path + ".tmp.%d" % os.getpid()
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)
            os.replace(tmp, path)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def atomic_read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return json.loads(json.dumps(default))


# ---------------------------------------------------------------- 1) 车次发现
def http_get(url, timeout=30):
    """GET -> body 文本；限流特征 -> RateLimitError。"""
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, headers=H), timeout=timeout) as r:
            return r.read().decode("utf-8")
    except Exception as e:
        kind = classify_failure(str(e))
        if kind == "rate_limit":
            raise RateLimitError("12306 限流/反爬: " + str(e)[:160])
        raise QueryError("HTTP 失败: %s" % e)


def discover_trains(date):
    """getTrainName?date=D -> {code: {train_no, from, to}}（当日全国车次）。"""
    url = ("https://kyfw.12306.cn/otn/queryTrainInfo/getTrainName?date=%s" % date)
    body = http_get(url, timeout=60)
    if not body.strip():
        raise RateLimitError("12306 限流/反爬: 空响应")
    try:
        obj = json.loads(body)
    except ValueError:
        kind = classify_failure(body)
        if kind == "rate_limit":
            raise RateLimitError("12306 限流/反爬: " + body[:160])
        raise QueryError("getTrainName 响应不是 JSON: " + body[:160])
    out = {}
    for it in (obj.get("data") or []):
        stc = it.get("station_train_code") or ""
        mm = re.match(r"^([A-Z]*\d+)\((.+?)-(.+?)\)$", stc)
        if not mm:
            continue
        code, f, t = mm.groups()
        e = out.setdefault(code, {"train_no": it.get("train_no") or "",
                                  "from": f, "to": t})
        if it.get("train_no") and it["train_no"] != e["train_no"]:
            e.setdefault("alt_train_nos", [])
            if e["train_no"] not in e["alt_train_nos"]:
                e["alt_train_nos"].append(e["train_no"])
            e["train_no"] = it["train_no"]      # 以最新为准
    return out


def ensure_trainlist():
    """trainlist.json：缺失/过期 -> getTrainName 多日期发现（现行数据优先）；
    合并 server 运行时发现的 extra。返回 {code: meta}。"""
    tl = {}
    fresh = False
    if os.path.exists(TRAINLIST_PATH):
        age_days = (time.time() - os.path.getmtime(TRAINLIST_PATH)) / 86400
        if age_days < STALE_TRAINLIST_DAYS:
            tl = atomic_read(TRAINLIST_PATH, {})
            fresh = True
    if not fresh or not tl:
        base = datetime.now()
        got = 0
        for off in DISCOVER_DATES:
            date = (base + timedelta(days=off)).strftime("%Y-%m-%d")
            try:
                found = discover_trains(date)
            except (RateLimitError, QueryError) as e:
                log("车次发现 %s 失败: %s" % (date, e))
                continue
            got += 1
            for code, meta in found.items():
                e = tl.setdefault(code, {"train_no": "", "from": "", "to": "",
                                         "dates": [], "train_nos": []})
                if meta.get("train_no"):
                    if e.get("train_no") and e["train_no"] != meta["train_no"]:
                        e.setdefault("train_nos", [])
                        if e["train_no"] not in e["train_nos"]:
                            e["train_nos"].append(e["train_no"])
                    e["train_no"] = meta["train_no"]
                e["from"] = meta.get("from") or e.get("from") or ""
                e["to"] = meta.get("to") or e.get("to") or ""
                if date not in e["dates"]:
                    e["dates"].append(date)
            log("车次发现 %s：%d 车次（累计 %d）" % (date, len(found), len(tl)))
            time.sleep(1.5)
        if got:
            atomic_write(TRAINLIST_PATH, tl)
    # 运行时发现的新车次（server 回填队列）并入
    extra = atomic_read(EXTRA_PATH, {})
    changed = False
    for code, meta in (extra.items() if isinstance(extra, dict) else []):
        if code not in tl:
            tl[code] = {"train_no": "", "from": meta.get("from", ""),
                        "to": meta.get("to", ""), "dates": [], "train_nos": [],
                        "src": "live"}
            changed = True
    if changed:
        atomic_write(TRAINLIST_PATH, tl)
    return tl


# ---------------------------------------------------------------- 2) 逐车次拉经停
def fetch_stops(train_no, date):
    """queryTrainInfo/query（轻接口）-> [{station,arrive,depart,order}]（完整经停表）。"""
    url = ("https://kyfw.12306.cn/otn/queryTrainInfo/query?leftTicketDTO.train_no=%s"
           "&leftTicketDTO.train_date=%s&rand_code=" % (train_no, date))
    body = http_get(url)
    if not body.strip():
        raise RateLimitError("12306 限流/反爬: 空响应")
    try:
        obj = json.loads(body)
    except ValueError:
        kind = classify_failure(body)
        if kind == "rate_limit":
            raise RateLimitError("12306 限流/反爬: " + body[:160])
        raise QueryError("queryTrainInfo 响应不是 JSON: " + body[:160])
    data = obj.get("data") or {}
    stations = data.get("data") or []
    if not stations and "middleList" in data:        # 兼容分段结构
        stations = []
        for m in data["middleList"]:
            stations.extend(m.get("fullList") or [])
    if not stations and "fullList" in data:
        stations = data["fullList"]
    out = []
    for i, s in enumerate(stations):
        name = (s.get("station_name") or "").strip()
        if not name:
            continue
        out.append({
            "station": name,
            "arrive": s.get("arrive_time") or "",
            "depart": s.get("start_time") or "",
            "order": i + 1,
        })
    return out


def pick_run_date(meta, attempts):
    """选一个该车开行、且没试过的日期。"""
    today = datetime.now()
    cand = meta.get("dates") or []
    good = []
    for d in cand:
        if d <= today.strftime("%Y-%m-%d"):
            continue
        if d not in attempts:
            good.append(d)
    if good:
        return sorted(good)[0]
    for off in (2, 3, 5, 8):
        d = (today + timedelta(days=off)).strftime("%Y-%m-%d")
        if d not in attempts:
            return d
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 车次后退出")
    ap.add_argument("--retry", action="store_true", help="只重试失败清单")
    args = ap.parse_args()

    tl = ensure_trainlist()
    routes = atomic_read(ALLROUTES_PATH, {})
    retry = atomic_read(RETRY_PATH, {"failed": {}, "updated": ""})
    failed = retry.get("failed", {}) if isinstance(retry.get("failed"), dict) else {}

    # 现行车次（矩阵见过的）优先抓
    live_meta = {}
    try:
        for e in atomic_read(os.path.join(DATA, "matrix.json"), {"pairs": {}}).get("pairs", {}).values():
            for t in e.get("trains", []):
                c = t.get("code")
                if c and c not in live_meta:
                    live_meta[c] = {"train_no": "", "from": t.get("from_st") or "",
                                    "to": t.get("to_st") or "", "dates": [],
                                    "train_nos": [], "src": "live"}
    except Exception:
        pass
    for code, meta in live_meta.items():
        merged = dict(tl.get(code) or {})
        merged.update({k: v for k, v in meta.items() if v})
        tl[code] = merged

    if args.retry:
        todo = sorted(failed.keys())
    else:
        pend = [c for c in sorted(tl.keys()) if c not in routes]
        todo = [c for c in pend if c in live_meta] + [c for c in pend if c not in live_meta]
    total = len(tl)
    log("经停库抓取启动：候选 %d，已有 %d，待抓 %d，节奏 %.1fs/车次" % (
        total, len(routes), len(todo), ROUTE_GAP))

    done = 0
    processed = 0
    t0 = time.time()
    try:
        for code in todo:
            if args.limit and processed >= args.limit:
                break
            if code in routes:
                continue
            meta = dict(tl.get(code) or {})
            attempts = (failed.get(code) or {}).get("attempts", [])
            if len(attempts) >= MAX_ATTEMPTS and not args.retry:
                continue
            ok = False
            t_call = time.time()
            for _try in range(MAX_ATTEMPTS - len(attempts)):
                date = pick_run_date(meta, attempts)
                if not date:
                    break
                t_call = time.time()
                try:
                    tno = meta.get("train_no") or ""
                    stops = fetch_stops(tno, date) if tno else []
                    if not stops:
                        # train_no 失效（跨季度改号）-> 试备用号
                        for alt in (meta.get("train_nos") or []):
                            stops = fetch_stops(alt, date)
                            if stops:
                                meta["train_no"] = alt
                                break
                except RateLimitError as e:
                    minutes = rate_limiter.note()
                    log("限流 %s：全局退避 %d 分钟（连续第 %d 次）: %s" % (
                        code, minutes, rate_limiter.consec, e))
                    rate_limiter.wait()
                    continue
                except (QueryError, ValueError, OSError) as e:
                    log("失败 %s @%s: %s" % (code, date, e))
                    attempts = attempts + [date]
                    time.sleep(ROUTE_GAP)
                    continue
                if stops:
                    routes[code] = stops
                    done += 1
                    ok = True
                    break
                attempts = attempts + [date]
                elapsed = time.time() - t_call
                if elapsed < ROUTE_GAP:
                    time.sleep(ROUTE_GAP - elapsed)
            processed += 1
            if not ok:
                failed[code] = {"train_no": meta.get("train_no", ""),
                                "from": meta.get("from", ""),
                                "to": meta.get("to", ""),
                                "attempts": attempts[:MAX_ATTEMPTS]}
            elif code in failed:
                failed.pop(code, None)
            # 节奏：每车次 ≥2.5s；每 60 车次喘口气 20s（防连续触发反爬）
            elapsed = time.time() - t_call
            if ok and elapsed < ROUTE_GAP:
                time.sleep(ROUTE_GAP - elapsed)
            if processed % 60 == 0:
                time.sleep(20)
            # 每 25 车次落盘 + 进度
            if processed % WRITE_EVERY == 0 or (args.limit and processed >= args.limit):
                atomic_write(ALLROUTES_PATH, routes)
                atomic_write(RETRY_PATH, {"failed": failed,
                                          "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                per_min = processed / max(time.time() - t0, 1) * 60
                atomic_write(PROGRESS_PATH, {
                    "phase": "crawl", "done": len(routes), "total": total,
                    "failed": len(failed), "pending": max(total - len(routes) - len(failed), 0),
                    "per_min": round(per_min, 1),
                    "eta_min": round(max(total - len(routes) - len(failed), 0) / max(per_min, 0.1), 1),
                    "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                log("checkpoint %d/%d（本轮 %d，失败 %d，%.1f 车次/分）" % (
                    len(routes), total, processed, len(failed), per_min))
    except KeyboardInterrupt:
        log("手动中断")
    finally:
        atomic_write(ALLROUTES_PATH, routes)
        atomic_write(RETRY_PATH, {"failed": failed,
                                  "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        atomic_write(PROGRESS_PATH, {
            "phase": "crawl", "done": len(routes), "total": total,
            "failed": len(failed), "pending": max(total - len(routes) - len(failed), 0),
            "per_min": round(processed / max(time.time() - t0, 1) * 60, 1),
            "eta_min": 0,
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    log("本轮结束：新增 %d，累计 %d/%d，失败 %d，耗时 %.1f 分钟" % (
        done, len(routes), total, len(failed), (time.time() - t0) / 60))
    # 收尾：补全新经停站坐标（P1 折线全序列要求每站有坐标）
    if not args.limit:
        try:
            import subprocess
            log("补站坐标（fill_station_geo2 + repair）…")
            subprocess.run([sys.executable, os.path.join(BASE, "fill_station_geo2.py")],
                           timeout=3600)
            subprocess.run([sys.executable, os.path.join(BASE, "fill_station_geo_repair.py")],
                           timeout=1800)
        except Exception as e:
            log("补站坐标失败: %s" % e)


if __name__ == "__main__":
    main()
