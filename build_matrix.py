#!/usr/bin/env python3
"""62 城市双向直达矩阵构建器 —— 可挂定时任务每日凌晨跑，可断点续跑。

⚠️ 防 12306 反爬限流（2026-11 被封过一次）：
  - 每对查询间隔 ≥20 秒（CALL_GAP），单对内两次 tools/call 间隔 ≥8 秒（MCPClient min_interval）
  - 遇限流错误（'reading result'/空响应）：该对记入 data/matrix-retry.json（待重试），
    全局退避 5 分钟再继续；连续 3 次退避后指数增长 10→20→30 分钟封顶
  - 白天不做全量扫描（只由 server 后台零星补查，≤2 对/分钟）

Usage:
  python3 build_matrix.py                  # 全量 64×63 对（夜间跑）
  python3 build_matrix.py --from 盐城      # 只跑盐城出发的对
  python3 build_matrix.py --limit 5        # 最多处理 5 对后退出
  python3 build_matrix.py --retry          # 只跑待重试清单
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp12306 import (MCPClient, RateLimitError, rate_limiter,
                      matrix_store, routes_store, trainroutes_store,
                      route_retry_store, pair_key, tr_lookup,
                      query_pair, query_route_stops, query_prices)

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static", "index.html")
from railconfig import DATA_DIR

CITIES_PATH = os.path.join(DATA_DIR, "cities.json")
LOG_PATH = os.path.join(DATA_DIR, "matrix.log")
RETRY_PATH = os.path.join(DATA_DIR, "matrix-retry.json")

WRITE_EVERY = 20          # 每完成 N 对写一次盘
CALL_GAP = 2.5            # 每对 ≥2 秒（mcp_12306 有会话保持+重试，节奏可放宽到 2-3s）
INNER_CALL_GAP = 0.8      # 单对内多次 tools/call 的最小间隔
RETRY_MAX_FAILS = 500     # 待重试清单上限（防无限膨胀）
ROUTE_GAP = 2.5           # 经停补查每车次间隔（夜间全量，同矩阵节奏）


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_cities():
    """城市清单：优先读 data/cities.json（342 城，build_cities.py 产物）。
    旧版从 index.html 解析 CITIES 数组的逻辑仅作兜底（前端已不再内置数组）。"""
    if os.path.exists(CITIES_PATH):
        try:
            cities = json.load(open(CITIES_PATH, encoding="utf-8"))
            if isinstance(cities, list) and len(cities) >= 50:
                return cities
        except (OSError, ValueError):
            pass
    with open(STATIC, encoding="utf-8") as f:
        html = f.read()
    m = re.search(r"const CITIES = \[(.*?)\];", html, re.S)
    if not m:
        raise RuntimeError("无法在 index.html 里找到 CITIES 数组")
    cities = [{"name": n, "lat": float(la), "lng": float(lo)}
              for n, la, lo in re.findall(
                  r'\{\s*name:\s*"([^"]+)",\s*lat:\s*([\d.]+),\s*lng:\s*([\d.]+)\s*\}', m.group(1))]
    if len(cities) < 60:
        raise RuntimeError("CITIES 解析数量异常: %d" % len(cities))
    with open(CITIES_PATH, "w", encoding="utf-8") as f:
        json.dump(cities, f, ensure_ascii=False)
    return cities


# ---------------------------------------------------------------- 待重试清单

def retry_load():
    try:
        with open(RETRY_PATH, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, list) else []
    except (OSError, ValueError):
        return []


def retry_save(keys):
    keys = sorted(set(keys))[:RETRY_MAX_FAILS]
    tmp = RETRY_PATH + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(keys, f, ensure_ascii=False)
    os.replace(tmp, RETRY_PATH)


def retry_add(key):
    keys = retry_load()
    keys.append(key)
    retry_save(keys)


def retry_remove(key):
    keys = [k for k in retry_load() if k != key]
    retry_save(keys)


# ---------------------------------------------------------------- 夜间经停补查（route-retry.json）

def collect_route_todo():
    """扫矩阵全部 unique 车次，diff trainroutes 缓存 -> 待补查清单（写 route-retry.json，断点续跑）。"""
    pairs = matrix_store.read().get("pairs", {})
    tr_cache = trainroutes_store.read()
    todo = []
    for k, e in pairs.items():
        if not e.get("has_direct"):
            continue
        for t in e.get("trains", []):
            code = t.get("code")
            if code and not tr_lookup(tr_cache, code):
                todo.append(code)
    todo = sorted(set(todo))
    def upd(d):
        existing = [c for c in d.get("todo", []) if c in set(todo)]
        d["todo"] = sorted(set(existing) | set(todo))
        d["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    route_retry_store.update(upd)
    return todo


def fill_routes(mcp, date, limit=0):
    """补全所有 unique 车次的经停缓存（data/route-retry.json 断点续跑，≤1 次/2.5s）。"""
    todo = collect_route_todo()
    log("经停补查启动：待补 %d 车次（date=%s，间隔 %.0fs）" % (len(todo), date, ROUTE_GAP))
    # 车次 -> (from_st, to_st) 一次建好（get-train-route-stations 要用）
    pair_trains = {}
    for k, e in matrix_store.read().get("pairs", {}).items():
        for t in e.get("trains", []):
            if t.get("code") and t["code"] not in pair_trains:
                pair_trains[t["code"]] = (t.get("from_st") or "", t.get("to_st") or "")
    done = 0
    t0 = time.time()
    for code in todo:
        if limit and done >= limit:
            break
        # 断点续跑：缓存里已有就跳过
        if tr_lookup(trainroutes_store.read(), code):
            route_retry_store.update(lambda d, c=code: d.get("todo", []) and d["todo"].remove(c))
            continue
        try:
            from_st, to_st = pair_trains.get(code, ("", ""))
            stops = query_route_stops(mcp, code, date, from_st, to_st)
            def done_upd(d, c=code):
                if isinstance(d.get("todo"), list) and c in d["todo"]:
                    d["todo"].remove(c)
                d.setdefault("done", [])
                if c not in d["done"]:
                    d["done"].append(c)
                d["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            route_retry_store.update(done_upd)
            done += 1
            log("经停 %d/%d %s -> %d 站" % (done, len(todo), code, len(stops or [])))
        except RateLimitError as e:
            minutes = rate_limiter.note()
            log("经停 %s 限流，全局退避 %d 分钟（连续第 %d 次）: %s" % (
                code, minutes, rate_limiter.consec, e))
            rate_limiter.wait()
        except Exception as e:
            log("经停 %s 失败: %s" % (code, e))
        time.sleep(ROUTE_GAP)
    log("经停补查结束：成功 %d/%d，耗时 %.1f 分钟" % (done, len(todo), (time.time() - t0) / 60))


def warm_prices(mcp, date, limit=300):
    """价格缓存预热（矩阵扫描退役后的夜间任务）：只对热点城市对拉票价。

    热点 = 现有矩阵里直达趟数最多的前 limit 对。每对一次 query-ticket-price，
    节奏 CALL_GAP；写 pricecache（带日期 + 无日期两级 key）。"""
    pairs = matrix_store.read().get("pairs", {})
    hot = sorted(((k, e) for k, e in pairs.items() if e.get("has_direct")),
                 key=lambda kv: -(kv[1].get("n_trains") or 0))[:limit]
    log("价格预热启动：%d 对（date=%s，间隔 %.0fs）" % (len(hot), date, CALL_GAP))
    done = 0
    t0 = time.time()
    for k, _e in hot:
        f, t = k.split("->", 1)
        try:
            pr = query_prices(mcp, date, f, t)
            done += 1
            if done % 25 == 0:
                log("价格预热 %d/%d %s -> %d 车次" % (done, len(hot), k, len(pr)))
        except RateLimitError as e:
            minutes = rate_limiter.note()
            log("价格预热 %s 限流，退避 %d 分钟: %s" % (k, minutes, e))
            rate_limiter.wait()
        except Exception as e:
            log("价格预热 %s 失败: %s" % (k, e))
        time.sleep(CALL_GAP)
    log("价格预热结束：成功 %d/%d，耗时 %.1f 分钟" % (done, len(hot), (time.time() - t0) / 60))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="from_city", default=None, help="只跑该出发城市")
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 对后退出（0=不限）")
    ap.add_argument("--date", default=None, help="查询日期 yyyy-MM-dd（默认运行日+2）")
    ap.add_argument("--retry", action="store_true", help="只跑待重试清单")
    ap.add_argument("--routes-only", action="store_true", help="只补经停缓存（不跑矩阵）")
    ap.add_argument("--prices-only", action="store_true",
                    help="价格缓存预热器模式：只对热点城市对拉票价（夜间任务）")
    args = ap.parse_args()

    date = args.date or (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    cities = load_cities()
    names = [c["name"] for c in cities]
    if args.prices_only:
        mcp = MCPClient(min_interval=INNER_CALL_GAP)
        try:
            warm_prices(mcp, date, limit=300)
        finally:
            mcp.close()
        return
    if args.routes_only:
        mcp = MCPClient(min_interval=INNER_CALL_GAP)
        try:
            fill_routes(mcp, date, limit=args.limit)
        finally:
            mcp.close()
        return
    log("矩阵构建启动：date=%s 城市=%d 对=%d 每对间隔≥%.0fs" % (
        date, len(names), len(names) * (len(names) - 1), CALL_GAP))

    # 待办有序对：待重试清单优先
    todo = []
    pending_retry = [k for k in retry_load() if "->" in k]
    if args.retry:
        todo = [tuple(k.split("->", 1)) for k in pending_retry]
    else:
        origins = [args.from_city] if args.from_city else names
        for k in pending_retry:            # 待重试的先跑
            f, t = k.split("->", 1)
            if (not args.from_city or f == args.from_city) and f in names and t in names:
                todo.append((f, t))
        seen = set(pair_key(f, t) for f, t in todo)
        for f in origins:
            for t in names:
                if f != t and pair_key(f, t) not in seen:
                    todo.append((f, t))
    total = len(todo)

    mcp = MCPClient(min_interval=INNER_CALL_GAP)   # 单对内两次调用也拉开
    done = 0
    processed = 0
    t0 = time.time()
    try:
        for f, t in todo:
            key = pair_key(f, t)
            # 断点续跑：已有当日数据的对跳过（旧版只存前6班的条目视为不完整，重查覆盖）
            existing = matrix_store.read().get("pairs", {}).get(key)
            if (existing and existing.get("date") == date and not existing.get("error")
                    and len(existing.get("trains", [])) >= existing.get("n_trains", 0)):
                done += 1
                retry_remove(key)
                continue
            pair_t0 = time.time()
            try:
                entry = query_pair(mcp, date, f, t, with_route=True)
            except RateLimitError as e:
                # 限流：该对记为待重试，全局退避后继续（不写伪数据）
                retry_add(key)
                minutes = rate_limiter.note()
                log("限流 %s -> 待重试，全局退避 %d 分钟（连续第 %d 次）: %s" % (
                    key, minutes, rate_limiter.consec, e))
                rate_limiter.wait()
                continue
            except Exception as e:  # 其他单对失败：不写盘（不伪造无直达），留待重试清单
                log("失败 %s: %s" % (key, e))
                retry_add(key)
                processed += 1
                if args.limit and processed >= args.limit:
                    break
                continue

            def put(data, e=entry, k=key):
                data.setdefault("pairs", {})[k] = e
                data["date"] = date
                data["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            matrix_store.update(put)
            retry_remove(key)
            done += 1
            processed += 1
            mark = "直达%d趟" % entry.get("n_trains", 0) if entry.get("has_direct") else "无直达"
            route_mark = "折线%d点" % entry["route_npts"] if entry.get("route_key") else "无折线"
            log("进度 %d/%d %s %s %s" % (done, total, key, mark, route_mark))
            if processed % WRITE_EVERY == 0:
                log("checkpoint 写盘 (%d/%d, %.1f 对/分, 待重试 %d)" % (
                    done, total, processed / max(time.time() - t0, 1) * 60,
                    len(retry_load())))
            # 每对 ≥20 秒（含本对耗时）
            elapsed = time.time() - pair_t0
            if elapsed < CALL_GAP:
                time.sleep(CALL_GAP - elapsed)
            if args.limit and processed >= args.limit:
                log("达到 --limit=%d，退出" % args.limit)
                break
    except KeyboardInterrupt:
        log("手动中断矩阵构建")
    try:
        # 全量跑完后自动补全所有 unique 车次的经停缓存（断点续跑，data/route-retry.json）
        if not args.limit:
            fill_routes(mcp, date)
    finally:
        mcp.close()

    elapsed = time.time() - t0
    log("本轮结束：处理 %d 对，累计 %d/%d，待重试 %d，耗时 %.1f 分钟" % (
        processed, done, total, len(retry_load()), elapsed / 60))


if __name__ == "__main__":
    main()
