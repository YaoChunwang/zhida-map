#!/usr/bin/env python3
"""全国车次经停图谱 —— data/allroutes.json → 任意站直达秒查 / 全网转乘。

结构（内存索引，mtime 缓存）：
  trains:   code -> [stop]，stop = (station, arrive, depart)
  by_station: 站名 -> [(code, idx)]
  city_of:  站名 -> 城市名（同名/前缀/近邻三级匹配，build 时一次算好）
  city_stations: 城市名 -> [站名]

对外：
  query_from(from_name)  -> {stations, cities, paths, n_trains}（按 from 缓存）
  search_transfers(a, b) -> [plan]（任意站中转，等待 0.5~6h）
  progress()             -> 抓取进度（轻读，不建索引）
"""

import json
import math
import os
import re
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
from railconfig import DATA_DIR as DATA
ALLROUTES_PATH = os.path.join(DATA, "allroutes.json")
PROGRESS_PATH = os.path.join(DATA, "allroutes-progress.json")
CITIES_PATH = os.path.join(DATA, "cities.json")
GEO_PATH = os.path.join(DATA, "StationGeo.json")

_lock = threading.Lock()
_state = {"key": None, "trains": None, "by_station": None,
          "city_of": None, "city_stations": None, "queries": {}}


# ---------------------------------------------------------------- 基础
def _norm(n):
    return re.sub(r"\s+", "", n or "")


def hhmm_min(s):
    m = re.match(r"^(\d{1,2}):(\d{2})$", (s or "").strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def fmt_abs(minutes):
    """绝对分钟 -> 'HH:MM' / '次日HH:MM' / '第N日HH:MM'。"""
    if minutes is None:
        return None
    days, m = divmod(int(minutes), 1440)
    t = "%02d:%02d" % (m // 60, m % 60)
    if days == 0:
        return t
    if days == 1:
        return "次日" + t
    return "第%d日%s" % (days + 1, t)


def stop_abs_times(stops):
    """逐站绝对分钟（跨午夜单调递增）。stops: [(station, arrive, depart)] -> [(arr, dep)]。"""
    out = []
    prev = None
    for _s, arr, dep in stops:
        a, d = hhmm_min(arr), hhmm_min(dep)
        for v in (a, d):
            pass
        pair = [a, d]
        for k in (0, 1):
            v = pair[k]
            if v is None:
                continue
            if prev is not None:
                while v + 20 < prev:     # 过了午夜（容许 20 分钟内的抖动）
                    v += 24 * 60
            pair[k] = v
        vals = [v for v in pair if v is not None]
        if vals:
            prev = max(prev or 0, max(vals))
        out.append((pair[0], pair[1]))
    return out


def load_geo():
    try:
        with open(GEO_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def station_coords(name, geo=None):
    geo = geo if geo is not None else load_geo()
    pt = geo.get(_norm(name)) or geo.get(name)
    return [pt[1], pt[0]] if pt else None   # [lat, lng]


# ---------------------------------------------------------------- 索引
def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return json.loads(json.dumps(default))


def progress():
    p = _load_json(PROGRESS_PATH, {})
    if not p:
        n = len(_load_json(ALLROUTES_PATH, {}))
        p = {"phase": "crawl", "done": n, "total": n, "failed": 0,
             "pending": 0, "updated": ""}
    return p


def _city_mapping(stations, cities, geo):
    """站名 -> 城市：同名/前缀（忽略方位后缀）优先；近邻（<50km）次之；去后缀兜底。"""
    city_of = {}
    by_exact = {c["name"]: c for c in cities}
    # 城市坐标点
    pts = [(c["name"], c["lat"], c["lng"]) for c in cities if c.get("lat") is not None]
    for name in stations:
        n = _norm(name)
        hit = by_exact.get(n)
        if hit:
            city_of[name] = hit["name"]
            continue
        # 站名以城市名开头（盐城北->盐城、襄阳东->襄阳）
        found = None
        for cn, c in by_exact.items():
            if n.startswith(cn) and len(n) - len(cn) <= 3:
                found = cn
                break
        if found:
            city_of[name] = found
            continue
        # 近邻城市（<50km）
        pt = station_coords(name, geo)
        best, bestd = None, 1e9
        if pt:
            for cn, la, ln in pts:
                dx = (pt[1] - ln) * math.cos(math.radians((pt[0] + la) / 2))
                dy = pt[0] - la
                d = math.hypot(dx, dy) * 111
                if d < bestd:
                    best, bestd = cn, d
        if bestd < 50:
            city_of[name] = best
        else:
            base = re.sub(r"(东|南|西|北|站)$", "", n)
            city_of[name] = base if len(base) >= 2 else name
    return city_of


def _build_index():
    routes = _load_json(ALLROUTES_PATH, {})
    cities = _load_json(CITIES_PATH, [])
    geo = load_geo()
    trains = {}
    for code, stops in routes.items():
        norm = []
        for s in stops:
            st = _norm(s.get("station"))
            if st:
                norm.append((st, s.get("arrive") or "", s.get("depart") or ""))
        if len(norm) >= 2:
            trains[code] = norm
    by_station = {}
    for code, stops in trains.items():
        for i, (st, _a, _d) in enumerate(stops):
            by_station.setdefault(st, []).append((code, i))
    all_names = list(by_station.keys())
    city_of = _city_mapping(all_names, cities, geo)
    city_stations = {}
    for name in all_names:
        city_stations.setdefault(city_of.get(name, name), []).append(name)
    return trains, by_station, city_of, city_stations


def get_index():
    """索引（按 allroutes.json mtime 缓存）。"""
    try:
        key = (os.path.getmtime(ALLROUTES_PATH), os.path.getmtime(CITIES_PATH))
    except OSError:
        key = (0, 0)
    with _lock:
        if _state["key"] != key:
            t0 = time.time()
            (_state["trains"], _state["by_station"], _state["city_of"],
             _state["city_stations"]) = _build_index()
            _state["key"] = key
            _state["queries"] = {}
            _state["built"] = time.time() - t0
        return _state


# ---------------------------------------------------------------- 估算价
def _hav_km(a, b):
    R = 6371.0
    dla = math.radians(b[0] - a[0])
    dlo = math.radians(b[1] - a[1])
    x = (math.sin(dla / 2) ** 2 +
         math.cos(math.radians(a[0])) * math.cos(math.radians(b[0])) * math.sin(dlo / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(x))


def est_price(p1, p2):
    """二等座估算：铁路里程≈直距×1.25，0.5 元/km。坐标缺失返回 None。"""
    try:
        if (not p1 or not p2 or p1[0] is None or p1[1] is None
                or p2[0] is None or p2[1] is None):
            return None
        return round(_hav_km(p1, p2) * 1.25 * 0.5, 1)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 任意站直达
def query_from(from_name):
    """从 from_name（城市名或站名）出发的全部直达站/城市/走向。"""
    st = get_index()
    key = ("q", _norm(from_name))
    if key in st["queries"]:
        return st["queries"][key]
    trains, by_station, city_of, city_stations = (
        st["trains"], st["by_station"], st["city_of"], st["city_stations"])
    geo = load_geo()
    cities = _load_json(CITIES_PATH, [])
    city_coords = {c["name"]: (c["lat"], c["lng"]) for c in cities}

    fn = _norm(from_name)
    # from 的站点集：精确站名优先；否则城市全部站（同名/近邻已归并）
    if fn in by_station:
        origin_stations = [fn]
    else:
        origin_stations = city_stations.get(fn, [])
        if not origin_stations:
            # 城市名 -> 站名前缀（襄阳 -> 襄阳东/襄阳）
            origin_stations = [s for s in by_station
                               if s.startswith(fn) and len(s) - len(fn) <= 3]

    stations = {}       # 站名 -> 聚合
    paths = {}          # 走向去重
    seen_codes = set()
    for origin in origin_stations:
        o_pts = station_coords(origin, geo)
        for code, i in by_station.get(origin, []):
            stops = trains[code]
            seen_codes.add(code)
            absT = stop_abs_times(stops)
            dep_a = absT[i][1] if absT[i][1] is not None else absT[i][0]
            clock_dep = stops[i][2] or stops[i][1]
            seq = []
            for j in range(i + 1, len(stops)):
                name = stops[j][0]
                arr = absT[j][0] if absT[j][0] is not None else absT[j][1]
                dur = (arr - dep_a) if (arr is not None and dep_a is not None) else None
                if dur is not None and dur < 0:
                    dur = None
                seq.append(name)
                rec = stations.setdefault(name, {
                    "station": name, "lat": None, "lng": None, "trains": []})
                rec["trains"].append({
                    "code": code, "from_st": origin, "to_st": name,
                    "depart": clock_dep, "arrive": fmt_abs(arr),
                    "duration_min": dur,
                })
            sig = tuple(seq)
            if len(sig) >= 1 and sig not in paths:
                paths[sig] = {"codes": [code], "names": [origin] + seq}
            elif len(sig) >= 1 and len(paths[sig]["codes"]) < 4:
                paths[sig]["codes"].append(code)

    # 汇总 + 坐标 + 所属城市 + 估价
    city_items = {}
    for name, rec in stations.items():
        c = station_coords(name, geo)
        if c:
            rec["lat"], rec["lng"] = c[0], c[1]
        rec["trains"].sort(key=lambda t: t.get("duration_min") or 99999)
        rec["n_trains"] = len(rec["trains"])
        rec["min_duration_min"] = rec["trains"][0].get("duration_min")
        p1 = station_coords(rec["trains"][0]["from_st"], geo)
        p2 = c or (city_coords.get(city_of.get(name, "")) or (None, None))
        rec["min_price2"] = est_price(p1 or (None, None), p2)
        rec["price_est"] = True
        rec["earliest_arrive"] = min([t["arrive"] for t in rec["trains"] if t.get("arrive")],
                                     key=lambda s: s or "z", default=None)
        rec["latest_depart"] = max([t["depart"] for t in rec["trains"] if t.get("depart")],
                                   key=lambda s: s or "", default=None)
        rec["city"] = city_of.get(name)

        city = rec["city"] or name
        it = city_items.setdefault(city, {
            "city": city, "has_direct": True, "pending": False,
            "stations": [], "trains": [], "n_trains": 0})
        it["stations"].append(name)
        it["trains"].extend(rec["trains"])
        it["n_trains"] += rec["n_trains"]

    for city, it in city_items.items():
        it["trains"].sort(key=lambda t: t.get("duration_min") or 99999)
        # 车次去重（同车次多站）
        seen = set()
        uniq = []
        for t in it["trains"]:
            k = t["code"]
            if k in seen:
                continue
            seen.add(k)
            uniq.append(t)
        it["n_trains"] = len(uniq)
        it["min_duration_min"] = uniq[0].get("duration_min") if uniq else None
        it["fastest"] = {"code": uniq[0]["code"], "from_st": uniq[0]["from_st"],
                         "to_st": uniq[0]["to_st"], "depart": uniq[0]["depart"],
                         "arrive": uniq[0]["arrive"]} if uniq else None
        it["trains"] = uniq[:8]
        p2 = city_coords.get(city)
        p1 = None
        for o in origin_stations:
            p1 = station_coords(o, geo)
            if p1:
                break
        it["min_price2"] = est_price(p1, p2)
        it["price_est"] = True
        arrs = [t["arrive"] for t in uniq if t.get("arrive")]
        deps = [t["depart"] for t in uniq if t.get("depart")]
        it["earliest_arrive"] = min(arrs, default=None) if arrs else None
        it["latest_depart"] = max(deps, default=None) if deps else None

    out_st = sorted(stations.values(),
                    key=lambda r: (r.get("min_duration_min") or 99999, r["station"]))

    # 走向折线（经停全序列坐标；缺坐标的站整条线标记不完整，前端回退弧线）
    geo_paths = []
    for sig, p in list(paths.items())[:400]:
        kept = []
        missing = 0
        for n in p["names"]:
            c = station_coords(n, geo)
            if c:
                kept.append(c)
            else:
                missing += 1
        if len(kept) >= 2:
            geo_paths.append({"codes": p["codes"], "names": p["names"],
                              "pts": kept, "complete": missing == 0})

    result = {
        "stations": out_st,
        "cities": sorted(city_items.values(),
                         key=lambda r: (r.get("min_duration_min") or 99999, r["city"])),
        "paths": geo_paths,
        "n_trains": len(seen_codes),
    }
    st["queries"][key] = result
    return result


# ---------------------------------------------------------------- 全网转乘
WAIT_MIN, WAIT_MAX = 30, 360


def search_transfers(from_name, to_name, max_plans=10):
    """任意站中转搜索：A->hub->B，等待 0.5~6h，hub 为任意经停站。"""
    st = get_index()
    trains, by_station, city_of, city_stations = (
        st["trains"], st["by_station"], st["city_of"], st["city_stations"])
    geo = load_geo()

    def origins(nm):
        fn = _norm(nm)
        if fn in by_station:
            return [fn]
        out = city_stations.get(fn, [])
        return out or [s for s in by_station if s.startswith(fn) and len(s) - len(fn) <= 3]

    o_sts, d_sts = origins(from_name), origins(to_name)
    if not o_sts or not d_sts:
        return []

    # 第一段：A 出发（(hub -> leg 信息)）
    hub1 = {}
    for origin in o_sts:
        for code, i in by_station.get(origin, []):
            stops = trains[code]
            absT = stop_abs_times(stops)
            dep0 = absT[i][1] if absT[i][1] is not None else absT[i][0]
            for j in range(i + 1, len(stops)):
                hub = stops[j][0]
                arr = absT[j][0] if absT[j][0] is not None else absT[j][1]
                if arr is None or dep0 is None:
                    continue
                hub1.setdefault(hub, []).append({
                    "code": code, "from_st": origin, "to_st": hub,
                    "dep0": dep0, "dep_clock": stops[i][2] or stops[i][1],
                    "arr": arr, "arr_clock": stops[j][1] or stops[j][2],
                    "dur": arr - dep0,
                })
    # 第二段：到 B
    hub2 = {}
    for dest in d_sts:
        for code, j in by_station.get(dest, []):
            stops = trains[code]
            absT = stop_abs_times(stops)
            arrB = absT[j][0] if absT[j][0] is not None else absT[j][1]
            for i2 in range(0, j):
                hub = stops[i2][0]
                dep = absT[i2][1] if absT[i2][1] is not None else absT[i2][0]
                if dep is None or arrB is None:
                    continue
                hub2.setdefault(hub, []).append({
                    "code": code, "from_st": hub, "to_st": dest,
                    "dep": dep, "dep_clock": stops[i2][2] or stops[i2][1],
                    "arrB": arrB, "arr_clock": stops[j][1] or stops[j][2],
                    "dur": arrB - dep,
                })

    plans = []
    for hub, l1 in hub1.items():
        l2 = hub2.get(hub)
        if not l2:
            continue
        l1 = sorted(l1, key=lambda x: x["arr"])[:60]
        l2 = sorted(l2, key=lambda x: x["dep"])[:60]
        cand = []
        for t1 in l1:
            for t2 in l2:
                wait = t2["dep"] - t1["arr"]
                if wait < 0:
                    continue
                if not (WAIT_MIN <= wait <= WAIT_MAX):
                    continue
                total = t2["arrB"] - t1["dep0"]
                if total <= 0:
                    continue
                p1 = est_price(station_coords(t1["from_st"], geo),
                               station_coords(t1["to_st"], geo))
                p2 = est_price(station_coords(t2["from_st"], geo),
                               station_coords(t2["to_st"], geo))
                cand.append({
                    "legs": [
                        {"code": t1["code"], "from_st": t1["from_st"], "to_st": t1["to_st"],
                         "depart": t1["dep_clock"], "arrive": t1["arr_clock"],
                         "duration_min": t1["dur"], "price2": p1, "price_est": True},
                        {"code": t2["code"], "from_st": t2["from_st"], "to_st": t2["to_st"],
                         "depart": t2["dep_clock"], "arrive": t2["arr_clock"],
                         "duration_min": t2["dur"], "price2": p2, "price_est": True},
                    ],
                    "transfer_city": city_of.get(hub, hub),
                    "transfer_station": hub,
                    "wait_min": wait,
                    "total_min": total,
                    "total_price": (round(p1 + p2, 1)
                                    if (p1 is not None and p2 is not None) else None),
                })
        cand.sort(key=lambda p: (p["total_min"], p["total_price"] or 1e9))
        plans.extend(cand[:2])
    # 去重（同对车次）
    seen, out = set(), []
    for p in sorted(plans, key=lambda p: (p["total_min"], p["total_price"] or 1e9)):
        k = (p["legs"][0]["code"], p["legs"][1]["code"], p["transfer_station"])
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
        if len(out) >= max_plans * 2:
            break
    return out
