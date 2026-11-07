#!/usr/bin/env python3
"""高铁火车可选路线图 — 本地 Web 服务（纯标准库）

监听 0.0.0.0:8650
  GET /                     -> static/index.html
  GET /static/*             -> 静态文件（vendored Leaflet）
  GET /api/query?from=&to=&date=   -> 调 12306 查询脚本并解析成 JSON（原有接口）
  GET /api/route?from=&to=&train=&date=  -> 车次真实经停折线 [[lat,lng],...]
  GET /api/direct?from=     -> 直达矩阵：A->各城真实直达信息（缺的对后台补查，标 pending）
  GET /api/transfer?from=&to=&date=  -> 无直达时的中转方案（by_time / by_price 两份排序）

结果缓存：内存 + 磁盘 cache/<from>_<to>_<date>.json
矩阵数据：data/matrix.json + data/routes.json（build_matrix.py 每日 04:00 重建）
"""

import json
import os
import queue
import re
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
from railconfig import PORT, CACHE_DIR, DATA_DIR

CACHE = CACHE_DIR

import sys
sys.path.insert(0, BASE)
import graph as railgraph
from mcp12306 import (MCPClient, RateLimitError, QueryError, rate_limiter,
                      matrix_store, routes_store, transfer_store, trainroutes_store,
                      route_retry_store, pair_key, tr_lookup, route_stops_cached,
                      query_pair, query_tickets, query_transfers, query_route_stops,
                      enrich_plan_prices, fetch_route_polyline, hhmm_min,
                      station_coords, _match_stop, slice_route)

os.makedirs(CACHE, exist_ok=True)

_mem_cache = {}
_cache_lock = threading.Lock()

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def run_query(from_city, to_city, date):
    """查 12306（mcp_12306 数据层），返回 (payload, from_cache)。
    错误分三类：限流（rate_limited=True, 提示稍后重试）/ 无直达（空列表）/ 其他。"""
    key = f"{from_city}_{to_city}_{date}"
    with _cache_lock:
        if key in _mem_cache:
            return _mem_cache[key], True
    disk = os.path.join(CACHE, key + ".json")
    if os.path.exists(disk):
        try:
            with open(disk, encoding="utf-8") as f:
                payload = json.load(f)
            # 限流失败的缓存不回放（只缓存成功结果）
            if not payload.get("rate_limited"):
                with _cache_lock:
                    _mem_cache[key] = payload
                return payload, True
        except (OSError, ValueError):
            pass

    try:
        trains = query_tickets(_mcp, date, from_city, to_city)
    except RateLimitError:
        return {"trains": [], "query": {"from": from_city, "to": to_city, "date": date},
                "error": "12306 限流中，请稍后再试（反爬拦截，约 %d 分钟后自动重试）"
                         % max(rate_limiter.retry_after_min(), 1),
                "rate_limited": True}, False
    except QueryError as e:
        return {"trains": [], "query": {"from": from_city, "to": to_city, "date": date},
                "error": "12306 查询失败: " + str(e)[:200]}, False

    payload = {
        "trains": trains,
        "query": {"from": from_city, "to": to_city, "date": date},
        "error": None,
        "raw_warnings": [],
    }
    _note_new_trains(trains)
    with _cache_lock:
        _mem_cache[key] = payload
    try:
        with open(disk, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except OSError:
        pass
    return payload, False


# ================================================================ 新增：真实路线 / 直达矩阵 / 中转

_mcp = MCPClient()          # 长活 mcp_12306 进程（server 各线程共用，内部有锁）
_backfill_q = queue.Queue()  # 矩阵缺失对的补查队列
_backfill_set = set()        # 已入队的 key，防重复
_backfill_lock = threading.Lock()
MAX_BACKFILL_PER_QUERY = 61

ALLROUTES_EXTRA = os.path.join(DATA_DIR, "allroutes-extra.json")


def _note_new_trains(trains):
    """运行时发现的车次（查询结果里经停库还没有的）回填抓取队列。"""
    try:
        idx = railgraph.get_index()
        known = idx["trains"]
        add = {}
        for t in trains or []:
            code = t.get("code")
            if code and code not in known:
                add[code] = {"from": t.get("from_station") or "",
                             "to": t.get("to_station") or ""}
        if not add:
            return
        with open(ALLROUTES_EXTRA, "a+", encoding="utf-8") as f:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                try:
                    cur = json.load(f)
                except ValueError:
                    cur = {}
                changed = False
                for k, v in add.items():
                    if k not in cur:
                        cur[k] = v
                        changed = True
                if changed:
                    f.seek(0)
                    f.truncate()
                    json.dump(cur, f, ensure_ascii=False)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        pass


def matrix_date():
    return matrix_store.read().get("date") or ""


def _route_by_key(route_key):
    if not route_key:
        return None
    return routes_store.read().get(route_key)


def _route_points_full(train, from_c, to_c, date):
    """A->B 区间经停全序列折线。返回 (pts, names, complete) 或 None（无经停数据）。

    P1 规则：折线必须覆盖经停序列每一站（缺坐标的站会让 complete=False，
    不落缓存；缺经停数据才允许调用方走弧线兜底并标“估算”）。"""
    stops = None
    idx = railgraph.get_index()
    st = idx["trains"].get(train)
    if st:
        stops = [(s[0], s[1], s[2]) for s in st]
    if not stops:
        cached = tr_lookup(trainroutes_store.read(), train)
        if cached:
            stops = [tuple(s) for s in cached]
    if not stops:
        try:
            live = query_route_stops(_mcp, train, date, from_c, to_c)
        except RateLimitError:
            raise
        except Exception:
            live = None
        if live:
            stops = [tuple(s) for s in live]
    if not stops:
        return None
    names = slice_route(stops, from_c, to_c)
    if not names or len(names) < 2:
        return None
    pts, missing = [], 0
    for n in names:
        c = station_coords(n)
        if c:
            pts.append(c)
        else:
            missing += 1
    return pts, names, missing == 0


def api_route(qs):
    """GET /api/route?from=&to=&train=&date= -> 真实经停折线（全序列）"""
    from_c = (qs.get("from") or [""])[0].strip()
    to_c = (qs.get("to") or [""])[0].strip()
    train = (qs.get("train") or [""])[0].strip().upper()
    date = (qs.get("date") or [""])[0].strip() or matrix_date()
    if not from_c or not to_c or not train:
        return 400, {"error": "参数缺失：need from,to,train"}
    cache_key = "%s|%s->%s" % (train, from_c, to_c)
    pts = _route_by_key(cache_key)
    if pts:
        # 防旧缓存短折线（P1：缓存折线必须仍是经停全序列，否则重建）
        idx = railgraph.get_index()
        st = idx["trains"].get(train)
        expected = None
        if st:
            names = slice_route([(s[0], s[1], s[2]) for s in st], from_c, to_c)
            expected = len(names) if names else None
        if expected is None or len(pts) >= expected:
            return 200, {"train": train, "from": from_c, "to": to_c,
                         "route": pts, "n_points": len(pts), "cached": True,
                         "n_stops": expected, "complete": True}
        routes_store.update(lambda d: d.pop(cache_key, None))   # 短缓存作废
    try:
        res = _route_points_full(train, from_c, to_c, date)
    except RateLimitError:
        return 200, {"train": train, "from": from_c, "to": to_c,
                     "route": None, "estimated": True,
                     "error": "12306 限流中，请稍后再试"}
    except Exception as e:
        return 200, {"train": train, "from": from_c, "to": to_c,
                     "route": None, "error": str(e)[:200], "estimated": True}
    if not res:
        return 200, {"train": train, "from": from_c, "to": to_c,
                     "route": None, "estimated": True}
    pts, names, complete = res
    if complete and len(pts) >= 2:
        # 只缓存完整折线（缺坐标的不缓存，geo 补齐后自动重建）
        routes_store.update(lambda d: d.update({cache_key: pts}))
    return 200, {"train": train, "from": from_c, "to": to_c,
                 "route": pts if len(pts) >= 2 else None,
                 "n_points": len(pts), "n_stops": len(names),
                 "complete": complete,
                 "estimated": not complete or len(pts) < 2}


_pairs_memo = {"key": None, "pairs": None}


def _matrix_pairs():
    """matrix pairs 带 mtime 缓存（避免每城市重读 700KB）。"""
    p = matrix_store.path
    try:
        key = os.path.getmtime(p)
    except OSError:
        key = 0
    if _pairs_memo["key"] != key:
        _pairs_memo["pairs"] = matrix_store.read().get("pairs", {})
        _pairs_memo["key"] = key
    return _pairs_memo["pairs"]


def _graph_city_item(from_c, it, cities):
    """graph 城市聚合 -> 面板条目（价格：矩阵缓存优先，缺的用估算并标 est）。"""
    item = {"city": it["city"], "has_direct": True, "pending": False,
            "n_trains": it.get("n_trains"),
            "min_duration_min": it.get("min_duration_min"),
            "min_price2": it.get("min_price2"), "price_est": it.get("price_est", True),
            "fastest": it.get("fastest"),
            "trains": it.get("trains", [])[:6],
            "earliest_arrive": it.get("earliest_arrive"),
            "latest_depart": it.get("latest_depart"),
            "stations": it.get("stations", [])}
    # 矩阵里有真实票价就用真实价（去“估”）
    pairs = _matrix_pairs()
    e = pairs.get(pair_key(from_c, it["city"])) or pairs.get(pair_key(it["city"], from_c))
    if e and e.get("has_direct") and e.get("min_price2") is not None:
        item["min_price2"] = e["min_price2"]
        item["price_est"] = False
        real = {t.get("code"): t.get("price2") for t in e.get("trains", [])}
        for t in item["trains"]:
            if t.get("code") in real and real[t["code"]] is not None:
                t["price2"] = real[t["code"]]
                t["price_est"] = False
    # 最快车次真实走向（经停全序列）
    f = item.get("fastest")
    if f and f.get("code"):
        try:
            res = _route_points_full(f["code"], f.get("from_st") or from_c,
                                     f.get("to_st") or it["city"], matrix_date())
            if res and res[0] and len(res[0]) >= 2:
                item["route"] = res[0]
                item["route_complete"] = res[2]
        except Exception:
            pass
    return item


def api_direct(qs):
    """GET /api/direct?from= -> A 到各城的真实直达信息。
    数据源：全国车次经停图谱（allroutes，秒查）；图谱缺位时回退旧矩阵（补查 pending）。"""
    from_c = (qs.get("from") or [""])[0].strip()
    if not from_c:
        return 400, {"error": "参数缺失：need from"}
    cities = _city_names()
    if from_c not in cities and not railgraph.get_index()["by_station"].get(from_c):
        return 400, {"error": "未知城市: %s" % from_c}

    # 1) 图谱（任意站直达，秒查）
    try:
        g = railgraph.query_from(from_c)
    except Exception:
        g = None
    if g and g.get("cities"):
        out = [_graph_city_item(from_c, it, cities) for it in g["cities"]
               if it["city"] != from_c]
        return 200, {"from": from_c, "date": matrix_date(), "cities": out,
                     "pending_count": 0, "matrix_total": len(out),
                     "source": "graph", "progress": railgraph.progress()}

    # 2) 回退：旧矩阵（62 城）+ 后台补查
    data = matrix_store.read()
    pairs = data.get("pairs", {})
    mdate = data.get("date", "")
    out = []
    missing = []
    for c in cities:
        if c == from_c:
            continue
        k = pair_key(from_c, c)
        e = pairs.get(k)
        if e is None or e.get("error"):
            missing.append(k)
            out.append({"city": c, "pending": True})
            continue
        item = {"city": c, "has_direct": bool(e.get("has_direct")),
                "pending": False, "price_est": False}
        if e.get("has_direct"):
            item.update({
                "n_trains": e.get("n_trains"),
                "min_price2": e.get("min_price2"),
                "min_duration_min": e.get("min_duration_min"),
                "fastest": e.get("fastest"),
                "trains": e.get("trains", [])[:6],
            })
            route = _route_by_key(e.get("route_key"))
            if route:
                item["route"] = route
        out.append(item)
    # 缺失对入补查队列（长活后台线程逐对查）
    for k in missing:
        with _backfill_lock:
            if k not in _backfill_set:
                _backfill_set.add(k)
                _backfill_q.put(k)
    return 200, {"from": from_c, "date": mdate, "cities": out,
                 "pending_count": len(missing),
                 "matrix_total": len(pairs), "source": "matrix"}


def api_transfer(qs):
    """GET /api/transfer?from=&to=&date= -> 中转方案（by_time / by_price）
    数据源优先级：官方 query-transfer -> 矩阵自算 -> 实时枢纽补查。"""
    from_c = (qs.get("from") or [""])[0].strip()
    to_c = (qs.get("to") or [""])[0].strip()
    date = (qs.get("date") or [""])[0].strip()
    if not from_c or not to_c or not DATE_RE.match(date):
        return 400, {"error": "参数缺失或日期格式应为 yyyy-MM-dd"}
    plans = []
    source = None
    rate_limited = False
    cache_key = "%s|%s|%s" % (from_c, to_c, date)

    # 1) 官方换乘（query-transfer，自动分页）
    try:
        plans = query_transfers(_mcp, date, from_c, to_c)
        if plans:
            enrich_plan_prices(_mcp, date, plans)
            source = "official"
            # 成功结果落缓存（限流时可回放）
            transfer_store.update(lambda d: d.update({cache_key: {
                "plans": plans, "saved": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}}))
    except RateLimitError:
        rate_limited = True
        plans = []
    except QueryError:
        plans = []

    # 1b) 限流/失败时回放缓存
    if not plans:
        cached = transfer_store.read().get(cache_key)
        if cached and cached.get("plans"):
            plans = cached["plans"]
            source = "official-cached"

    # 2) 全网图谱自算（任意站皆可中转，等待 0.5~6h）
    if not plans:
        try:
            plans = railgraph.search_transfers(from_c, to_c)
            if plans:
                source = "graph"
        except Exception:
            plans = []

    # 3) 矩阵自算（图谱缺位时兜底）
    if not plans:
        plans = transfer_from_matrix(from_c, to_c)
        if plans:
            source = "matrix"

    # 4) 矩阵也不够 → 战略枢纽实时补查（限流中不打，避免雪上加霜）
    if not plans and not rate_limited and not rate_limiter.limited():
        try:
            plans = transfer_live_hubs(from_c, to_c, date)
            if plans:
                source = "live"
        except RateLimitError:
            rate_limited = True
        except Exception:
            plans = []

    def fmt(p):
        d = dict(p)
        if p.get("total_min") is None and p.get("legs"):
            d["total_min"] = sum(l.get("duration_min") or 0 for l in p["legs"]) + (p.get("wait_min") or 0)
        return d
    plans = [fmt(p) for p in plans if p.get("legs")]
    by_time = sorted([p for p in plans if p.get("total_min") is not None],
                     key=lambda p: p["total_min"])[:8]
    by_price = sorted([p for p in plans if p.get("total_price") is not None],
                      key=lambda p: p["total_price"])[:8]
    resp = {"from": from_c, "to": to_c, "date": date, "source": source,
            "by_time": by_time, "by_price": by_price}
    if rate_limited:
        resp["rate_limited"] = True
        resp["message"] = "12306 限流中，请稍后再试（约 %d 分钟后自动重试）" % max(
            rate_limiter.retry_after_min(), 1)
    return 200, resp


def transfer_from_matrix(from_c, to_c):
    """矩阵自算中转：A->C 且 C->B 都直达的枢纽 C 枚举衔接（等待 0.5~6h）。"""
    data = matrix_store.read()
    pairs = data.get("pairs", {})
    cities = _city_names()
    plans = []
    for hub in cities:
        if hub in (from_c, to_c):
            continue
        e1 = pairs.get(pair_key(from_c, hub))
        e2 = pairs.get(pair_key(hub, to_c))
        if not (e1 and e2 and e1.get("has_direct") and e2.get("has_direct")):
            continue
        cand = connect_legs(hub, e1.get("trains", [])[:30], e2.get("trains", [])[:30])
        cand.sort(key=lambda p: (p["total_min"], p["total_price"] or 1e9))
        plans.extend(cand[:2])   # 每枢纽最多 2 个方案
    return plans


def connect_legs(hub, trains1, trains2):
    """两段车次列表枚举衔接方案（等待 0.5~6h，可跨午夜）。"""
    cand = []
    for t1 in trains1:
        a1 = hhmm_min(t1.get("arrive"))
        d1 = hhmm_min(t1.get("depart"))
        if a1 is None or d1 is None:
            continue
        for t2 in trains2:
            d2 = hhmm_min(t2.get("depart"))
            a2 = hhmm_min(t2.get("arrive"))
            if d2 is None or a2 is None:
                continue
            wait = d2 - a1
            if wait < 0:
                wait += 24 * 60
            if not (30 <= wait <= 360):
                continue
            total = (a2 - d1) % (24 * 60)
            if total <= 0:
                total += 24 * 60
            p1 = t1.get("price2")
            p2 = t2.get("price2")
            price = (p1 + p2) if (p1 is not None and p2 is not None) else None
            cand.append({
                "legs": [
                    {"code": t1["code"], "from_st": t1["from_st"], "to_st": t1["to_st"],
                     "depart": t1["depart"], "arrive": t1["arrive"],
                     "duration_min": t1.get("duration_min"), "price2": p1},
                    {"code": t2["code"], "from_st": t2["from_st"], "to_st": t2["to_st"],
                     "depart": t2["depart"], "arrive": t2["arrive"],
                     "duration_min": t2.get("duration_min"), "price2": p2},
                ],
                "transfer_city": hub, "wait_min": wait,
                "total_min": total, "total_price": price,
            })
    return cand


LIVE_HUBS_MAX = 8          # 实时补查枢纽数上限
LIVE_TRAINS_MAX = 12       # 每段实时车次参与枚举上限


def transfer_live_hubs(from_c, to_c, date):
    """矩阵缺数据时的战略枢纽实时补查：
    枢纽 = 矩阵中 A->C 有直达的城市，按地理绕行度（A-C + C-B 直距）取最优 8 个，
    实时查 C->B 车次后枚举衔接。"""
    pairs = matrix_store.read().get("pairs", {})
    cities = _load_cities()
    a = next((c for c in cities if c["name"] == from_c), None)
    b = next((c for c in cities if c["name"] == to_c), None)
    if not a or not b:
        return []
    import math
    def dist(x, y):
        dx = (x["lng"] - y["lng"]) * math.cos(math.radians((x["lat"] + y["lat"]) / 2))
        dy = x["lat"] - y["lat"]
        return math.hypot(dx, dy)
    direct_len = dist(a, b) or 1
    cands = []
    for c in cities:
        if c["name"] in (from_c, to_c):
            continue
        e = pairs.get(pair_key(from_c, c["name"]))
        if not (e and e.get("has_direct")):
            continue
        detour = (dist(a, c) + dist(c, b)) / direct_len
        cands.append((detour, c["name"]))
    cands.sort()
    plans = []
    for _, hub in cands[:LIVE_HUBS_MAX]:
        e1 = pairs.get(pair_key(from_c, hub))
        t1s = e1.get("trains", [])
        if not t1s:
            continue
        try:
            raw2 = query_tickets(_mcp, date, hub, to_c)
        except RateLimitError:
            raise
        except Exception:
            continue
        raw2.sort(key=lambda t: t.get("duration_min") or 99999)
        # 归一成矩阵 trains 格式（from_st/to_st）
        t2s = [{"code": t["code"], "from_st": t["from_station"], "to_st": t["to_station"],
                "depart": t["depart"], "arrive": t["arrive"],
                "duration_min": t.get("duration_min"), "price2": t.get("price2")}
               for t in raw2[:LIVE_TRAINS_MAX]]
        if not t2s:
            continue
        cand = connect_legs(hub, t1s, t2s)
        cand.sort(key=lambda p: (p["total_min"], p["total_price"] or 1e9))
        plans.extend(cand[:2])
    return plans


# ================================================================ 可直达全站（含小站）

def _route_stops_for(train_code, date, from_st, to_st):
    """取车次经停表（缓存优先）。返回 stops 或 None。"""
    try:
        stops = query_route_stops(_mcp, train_code, date, from_st, to_st)
    except RateLimitError:
        raise
    except Exception:
        return None
    return stops or None

def api_stations(qs):
    """GET /api/stations?from=A -> 从 A 出发可直达的所有车站（含小站）。

    主源：全国车次经停图谱（allroutes，全序列经停 + 秒查）。图谱缺位时回退
    旧矩阵扫描（trainroutes 缓存 + 后台补查，带 progress 轮询）。"""
    from_c = (qs.get("from") or [""])[0].strip()
    if not from_c:
        return 400, {"error": "参数缺失：need from"}

    # 1) 图谱
    try:
        g = railgraph.query_from(from_c)
    except Exception:
        g = None
    if g and (g.get("stations") or g.get("cities")):
        out = []
        for rec in g["stations"]:
            if rec["station"] == from_c:
                continue
            out.append(rec)
        return 200, {"from": from_c, "date": matrix_date(),
                     "stations": out, "paths": g.get("paths", []),
                     "progress": railgraph.progress(),
                     "n_trains": g.get("n_trains", 0), "source": "graph"}

    # 2) 回退：矩阵扫描（原逻辑）
    data = matrix_store.read()
    pairs = data.get("pairs", {})
    mdate = data.get("date") or ""

    # 1) A 为起点的全部对 -> 全部车次（同车次多对只取一次）
    trains = {}          # code -> {from_st, to_st, depart, arrive, duration_min, price2}
    for k, e in pairs.items():
        f, t = k.split("->", 1)
        if f != from_c or not e.get("has_direct"):
            continue
        for tr in e.get("trains", []):
            code = tr.get("code")
            if code and code not in trains:
                trains[code] = tr

    # 2) 每车次经停表 -> 并集站点（trainroutes 缓存一次性读入）
    tr_cache = trainroutes_store.read()
    stations = {}        # name -> {station, lat, lng, trains:[...], ...}
    paths = {}           # 走向折线：按经停序列去重 -> {names, pts, codes}
    done = 0
    pending = []         # 缺经停表的车次
    for code, tr in trains.items():
        stops = tr_lookup(tr_cache, code)
        if not stops:
            pending.append((code, tr))
            continue
        done += 1
        stops = [tuple(s) for s in stops]
        i = _match_stop(stops, tr.get("from_st") or from_c)
        if i is None:
            continue
        dep = hhmm_min(tr.get("depart"))
        seq = []         # 本车 A 之后的停站名（供走向折线去重）
        day = 0          # 跨午夜累计（K 车次可跨多日，逐站顺序推移）
        prev = dep
        for j in range(i + 1, len(stops)):
            sname = re.sub(r"\s+", "", stops[j][0] or "")
            if not sname:
                continue
            seq.append(sname)
            arr_txt = stops[j][1] if stops[j][1] not in (None, "----") else stops[j][2]
            arr = hhmm_min(arr_txt)
            dur = None
            if dep is not None and arr is not None:
                if prev is not None and arr < prev:
                    day += 24 * 60          # 过了午夜
                dur = (arr + day) - dep
                if dur < 0:
                    dur = None
                prev = arr
            rec = stations.setdefault(sname, {
                "station": sname,
                "lat": None, "lng": None,
                "trains": [],
            })
            rec["trains"].append({
                "code": code, "depart": tr.get("depart"),
                "arrive": arr_txt,
                "duration_min": dur, "price2": tr.get("price2"),
                "from_st": tr.get("from_st"), "seat2_status": tr.get("seat2_status"),
            })
        # 走向折线（按经停序列去重，同走廊多车次只留一条）
        key = tuple(seq)
        if len(key) >= 2 and key not in paths:
            paths[key] = {"codes": [code], "names": [from_c] + seq}
        elif len(key) >= 2:
            if len(paths[key]["codes"]) < 5:
                paths[key]["codes"].append(code)

    # 坐标 + 汇总 + 所属城市
    cities = _load_cities()
    for name, rec in stations.items():
        c = station_coords(name)
        if c:
            rec["lat"], rec["lng"] = c[0], c[1]
        rec["trains"].sort(key=lambda t: t.get("duration_min") or 99999)
        rec["n_trains"] = len(rec["trains"])
        rec["min_duration_min"] = rec["trains"][0].get("duration_min")
        prices = [t["price2"] for t in rec["trains"] if t.get("price2") is not None]
        rec["min_price2"] = min(prices) if prices else None
        rec["city"] = _city_of_station(name, cities)

    out = sorted(stations.values(),
                 key=lambda r: (r.get("min_duration_min") or 99999, r["station"]))

    # 折线点坐标（前端画真实走向；断线校验在前端做）
    path_list = []
    for key, p in paths.items():
        kept = []          # (站名, [lat,lng]) —— 坐标缺失的站连名字一起丢，防错位
        for n in p["names"]:
            c = station_coords(n)
            if c:
                kept.append((n, c))
        if len(kept) >= 2:
            path_list.append({"codes": p["codes"],
                              "names": [n for n, _ in kept],
                              "pts": [c for _, c in kept]})

    # 3) 缺经停表的车次 -> 后台补查队列（断点清单 data/route-retry.json）
    for code, tr in pending:
        _route_enqueue(code, tr, mdate)

    progress = {"done": done, "total": len(trains)}
    return 200, {"from": from_c, "date": mdate,
                 "stations": out, "paths": path_list,
                 "progress": progress, "n_trains": len(trains)}


def api_graph(qs):
    """GET /api/graph?from=A        -> 图谱直达（站/城市/走向，秒查）
       GET /api/graph?progress=1   -> 全国经停库抓取进度（轻读）"""
    if (qs.get("progress") or [""])[0] == "1":
        return 200, {"progress": railgraph.progress()}
    from_c = (qs.get("from") or [""])[0].strip()
    if not from_c:
        return 400, {"error": "参数缺失：need from"}
    try:
        g = railgraph.query_from(from_c)
    except Exception as e:
        return 500, {"error": "图谱查询失败: " + str(e)[:200]}
    return 200, {"from": from_c, "date": matrix_date(),
                 "stations": g["stations"], "cities": g["cities"],
                 "paths": g["paths"], "n_trains": g["n_trains"],
                 "progress": railgraph.progress(), "source": "graph"}


def _city_of_station(name, cities):
    """站名 -> 所属城市：精确/前缀匹配优先；再按最近城市（<60km）；
    兜底：本身就是城市主站（无方向后缀）就用自身名。"""
    for c in cities:
        cn = c["name"]
        if name == cn or name.startswith(cn) or cn.startswith(name):
            return cn
    import math
    best, bestd = None, 1e9
    pt = station_coords(name)
    if pt:
        for c in cities:
            dx = (pt[1] - c["lng"]) * math.cos(math.radians((pt[0] + c["lat"]) / 2))
            dy = pt[0] - c["lat"]
            d = math.hypot(dx, dy) * 111
            if d < bestd:
                best, bestd = c["name"], d
    if bestd < 60:
        return best
    # 不在 64 城附近的：去掉方向/站后缀当城市名（滕州东->滕州），太短则用原名（定西、城固）
    base = re.sub(r"(东|南|西|北|站)$", "", name)
    return base if len(base) >= 2 else (name if len(name) <= 4 else None)


# ---- 车次经停后台补查（≤2 次/分钟，断点续跑） ----
_route_q = queue.Queue()
_route_set = set()
_route_lock = threading.Lock()

def _route_enqueue(code, tr, mdate):
    with _route_lock:
        if code in _route_set:
            return
        _route_set.add(code)
        _route_q.put((code, tr.get("from_st") or "", tr.get("to_st") or "", mdate))
    # 断点清单：写 data/route-retry.json
    def upd(d):
        todo = d.setdefault("todo", [])
        if code not in todo:
            todo.append(code)
        d["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        route_retry_store.update(upd)
    except Exception:
        pass

def _route_worker():
    """长活经停补查线程：≤2 次/分钟，限流全局退避。"""
    while True:
        code, from_st, to_st, mdate = _route_q.get()
        if rate_limiter.limited():
            rate_limiter.wait()
        try:
            stops = query_route_stops(_mcp, code, mdate or (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d"),
                                      from_st, to_st)
            def done_upd(d):
                if isinstance(d.get("todo"), list) and code in d["todo"]:
                    d["todo"].remove(code)
                d.setdefault("done", [])
                if code not in d["done"]:
                    d["done"].append(code)
                d["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            route_retry_store.update(done_upd)
            print("[%s] route-fill %s -> %d 站" % (
                datetime.now().strftime("%H:%M:%S"), code, len(stops or [])))
        except RateLimitError as e:
            minutes = rate_limiter.note()
            print("[%s] route-fill %s 限流，退避 %d 分钟: %s" % (
                datetime.now().strftime("%H:%M:%S"), code, minutes, e))
        except Exception as e:
            print("[%s] route-fill %s 失败: %s" % (datetime.now().strftime("%H:%M:%S"), code, e))
        finally:
            with _route_lock:
                _route_set.discard(code)
        time.sleep(30)   # ≤2 次/分钟


def _city_names():
    return [c["name"] for c in _load_cities()]


def _load_cities():
    p = os.path.join(DATA_DIR, "cities.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _backfill_worker():
    """长活补查线程：逐对查缺失矩阵条目（与 build_matrix 共用 flock 存储）。
    限流时全局退避；平时 ≤2 对/分钟（零星补查节奏）。"""
    while True:
        k = _backfill_q.get()
        if rate_limiter.limited():
            rate_limiter.wait()
        f, t = k.split("->", 1)
        try:
            mdate = matrix_date() or (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
            entry = query_pair(_mcp, mdate, f, t, with_route=True)
            def put(data, e=entry, kk=k, d=mdate):
                data.setdefault("pairs", {})[kk] = e
                if not data.get("date"):
                    data["date"] = d
                data["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            matrix_store.update(put)
            print("[%s] backfill %s -> %s" % (
                datetime.now().strftime("%H:%M:%S"), k,
                "直达%d趟" % entry.get("n_trains", 0) if entry.get("has_direct") else "无直达"))
        except RateLimitError as e:
            minutes = rate_limiter.note()
            print("[%s] backfill %s 限流，退避 %d 分钟: %s" % (
                datetime.now().strftime("%H:%M:%S"), k, minutes, e))
        except Exception as e:
            print("[%s] backfill %s 失败: %s" % (datetime.now().strftime("%H:%M:%S"), k, e))
        finally:
            with _backfill_lock:
                _backfill_set.discard(k)
        time.sleep(30)   # 零星补查：≤2 对/分钟


# ================================================================ HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "RailMap/1.0"

    def handle(self):
        # 客户端（尤其 IPv6）中途断连时 sendall 抛 Errno 49/32，吞掉防刷屏防崩线程
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    @staticmethod
    def _fix_qs(qs):
        # 自纠正编码：curl 直接放原始 UTF-8 时，BaseHTTPRequestHandler 会按 latin-1 解码
        try:
            return parse_qs(qs.encode("latin-1").decode("utf-8"))
        except (UnicodeDecodeError, UnicodeEncodeError):
            return parse_qs(qs)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # 反代子路径支持：/railmap -> /railmap/，/railmap/* -> /*
        if path == "/railmap":
            self.send_response(301)
            self.send_header("Location", "/railmap/")
            self.end_headers()
            return
        if path.startswith("/railmap/"):
            path = path[len("/railmap"):]

        qs = self._fix_qs(parsed.query)
        if path == "/api/query":
            return self._api_query(parsed.query)
        if path == "/api/route":
            code, obj = api_route(qs)
            return self._send_json(code, obj)
        if path == "/api/direct":
            code, obj = api_direct(qs)
            return self._send_json(code, obj)
        if path == "/api/transfer":
            code, obj = api_transfer(qs)
            return self._send_json(code, obj)
        if path == "/api/stations":
            code, obj = api_stations(qs)
            return self._send_json(code, obj)
        if path == "/api/graph":
            code, obj = api_graph(qs)
            return self._send_json(code, obj)
        if path == "/api/cities":
            return self._send_json(200, _load_cities())
        if path == "/" or path == "/index.html":
            return self._file(os.path.join(STATIC, "index.html"))
        if path.startswith("/static/"):
            return self._file(os.path.join(STATIC, path[len("/static/"):]))
        self._send(404, b'{"error": "not found"}')

    def _api_query(self, qs):
        qs = self._fix_qs(qs)
        from_city = (qs.get("from") or [""])[0].strip()
        to_city = (qs.get("to") or [""])[0].strip()
        date = (qs.get("date") or [""])[0].strip()
        if not from_city or not to_city or not DATE_RE.match(date):
            self._send(400, json.dumps(
                {"trains": [], "query": {"from": from_city, "to": to_city, "date": date},
                 "error": "参数缺失或日期格式应为 yyyy-MM-dd"}, ensure_ascii=False).encode())
            return
        payload, cached = run_query(from_city, to_city, date)
        payload = dict(payload)
        payload["cached"] = cached
        self._send(200, json.dumps(payload, ensure_ascii=False).encode())

    def _file(self, path):
        # 防目录穿越
        real = os.path.realpath(path)
        if not real.startswith(os.path.realpath(STATIC) + os.sep) and real != os.path.realpath(STATIC):
            self._send(404, b'{"error": "not found"}')
            return
        if not os.path.isfile(real):
            self._send(404, b'{"error": "not found"}')
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".svg": "image/svg+xml",
            ".json": "application/json; charset=utf-8",
        }.get(os.path.splitext(real)[1].lower(), "application/octet-stream")
        with open(real, "rb") as f:
            self._send(200, f.read(), ctype)


if __name__ == "__main__":
    import socket

    threading.Thread(target=_backfill_worker, daemon=True).start()
    threading.Thread(target=_route_worker, daemon=True).start()

    def _prewarm():
        # 预热长活 npx 进程，避免首次请求吃 10-20s 冷启动
        try:
            _mcp.call("get-current-date", {}, attempts=1)
        except Exception:
            pass
    threading.Thread(target=_prewarm, daemon=True).start()

    class DualStackServer(ThreadingHTTPServer):
        address_family = socket.AF_INET6
        def server_bind(self):
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            super().server_bind()

    srv = DualStackServer(("::", PORT), Handler)
    print(f"railmap server listening on [::]:{PORT} (dual-stack)")
    srv.serve_forever()
