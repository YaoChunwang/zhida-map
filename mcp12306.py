#!/usr/bin/env python3
"""railmap 共享数据层 —— 基于 drfccv/mcp-server-12306（Python MCP，venv 内）。

数据层：调 venv 里的 mcp_12306.stdio_server（stdio JSON-RPC，同款收发）。

工具：query-tickets / query-ticket-price / get-train-route-stations / query-transfer
（均为 JSON 输出，不需要正则抠文本）。

限流策略（被封过一次）：
  - 错误分三类：RateLimitError（反爬/限流）、空结果（无直达，走换乘）、QueryError（其他）
  - RateLimiter 全局退避：连续 1-3 次限流各退避 5 分钟，之后指数 10→20→30 分钟封顶
  - MCPClient 调用节流 min_interval；build_matrix 每对 ≥2 秒

server.py / build_matrix.py 共用。Python 3.9 兼容（server 跑系统 python3）。
"""

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
from railconfig import DATA_DIR as DATA, MCP_PYTHON as VENV_PY
MCP_MODULE = "mcp_12306.stdio_server"
GEO_PATH = os.path.join(DATA, "StationGeo.json")

os.makedirs(DATA, exist_ok=True)


# ---------------------------------------------------------------- 限流识别与全局退避

# 反爬/限流特征（新 MCP 的报错文案 + 旧 MCP 残留特征，都认）
RATE_LIMIT_MARKERS = (
    "反爬",
    "接口返回异常",
    "限流",
    "reading 'result'",
    "reading 'middleList'",
    "Cannot read properties of undefined",
)

# 无结果特征（不是错误：无直达 / 无中转）
EMPTY_MARKERS = (
    "未找到该线路",
    "未查到中转",
    "未查到相关",
    "未找到经停站",
    "未找到该车次",
)


class RateLimitError(RuntimeError):
    """12306 限流/反爬（不该立刻重试，走全局退避）。"""


class QueryError(RuntimeError):
    """其他查询错误（站名无效、网络失败等）。"""


def classify_failure(text):
    """错误文本分类：'rate_limit' | 'empty' | 'other'。"""
    t = (text or "").strip()
    if not t:
        return "rate_limit"           # 空响应按限流处理
    if any(m in t for m in RATE_LIMIT_MARKERS):
        return "rate_limit"
    if any(m in t for m in EMPTY_MARKERS):
        return "empty"
    return "other"


class RateLimiter:
    """全局限流退避状态（进程内共享）。
    策略：连续 1-3 次限流各退避 5 分钟；之后指数增长 10→20→30 分钟封顶。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.consec = 0
        self.until = 0.0
        self.last_backoff_min = 0

    def note(self):
        """记录一次限流，返回本次退避分钟数。"""
        with self._lock:
            self.consec += 1
            minutes = 5 if self.consec <= 3 else min(5 * (2 ** (self.consec - 3)), 30)
            self.until = max(self.until, time.time() + minutes * 60)
            self.last_backoff_min = minutes
            return minutes

    def ok(self):
        """一次成功调用：清零连续计数。"""
        with self._lock:
            self.consec = 0

    def limited(self):
        return time.time() < self.until

    def retry_after_min(self):
        return max(0, int((self.until - time.time() + 59) // 60))

    def wait(self):
        """阻塞到退避窗口结束。"""
        delta = self.until - time.time()
        if delta > 0:
            time.sleep(delta)


rate_limiter = RateLimiter()


# ---------------------------------------------------------------- MCP 客户端（Python 版）

class MCPClient:
    """长活 mcp_12306.stdio_server 进程，顺序收发 JSON-RPC（带锁，线程安全）。
    min_interval：任意两次 tools/call 之间强制的最小间隔秒数（防反爬）。"""

    def __init__(self, min_interval=0.5):
        self._lock = threading.Lock()
        self._proc = None
        self._id = 0
        self._min_interval = max(min_interval, 0.0)
        self._last_call = 0.0

    def _spawn(self):
        env = dict(os.environ)
        env.setdefault("PYTHONUNBUFFERED", "1")
        self._proc = subprocess.Popen(
            [VENV_PY, "-m", MCP_MODULE],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, cwd=BASE,
        )
        self._send({"jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "railmap", "version": "3.0"}}})
        init_id = self._id
        while True:
            msg = self._recv()
            if msg.get("id") == init_id:
                break
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _next_id(self):
        self._id += 1
        return self._id

    def _send(self, obj):
        self._proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
        self._proc.stdin.flush()

    def _recv(self):
        line = self._proc.stdout.readline()
        if not line:
            raise QueryError("mcp_12306 进程退出，无响应")
        return json.loads(line)

    def _call_once(self, name, args):
        if self._proc is None or self._proc.poll() is not None:
            self._spawn()
        gap = self._min_interval - (time.time() - self._last_call)
        if gap > 0:
            time.sleep(gap)
        self._last_call = time.time()
        self._send({"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/call",
                    "params": {"name": name, "arguments": args}})
        want = self._id
        while True:
            msg = self._recv()
            if msg.get("id") == want:
                break
        result = msg.get("result", {})
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        text = "\n".join(texts)
        if result.get("isError"):
            kind = classify_failure(text)
            if kind == "rate_limit":
                raise RateLimitError("12306 限流/反爬: " + (text[:160] or "空响应"))
            raise QueryError("mcp_12306 错误: " + (text or "unknown"))
        return text

    def call(self, name, args, attempts=3):
        """调用工具，返回文本。限流错误不重试（直接上报）；其他错误退避重试。"""
        with self._lock:
            last = None
            for attempt in range(max(attempts, 1)):
                try:
                    text = self._call_once(name, args)
                    rate_limiter.ok()
                    return text
                except RateLimitError:
                    raise
                except (QueryError, OSError, ValueError) as e:
                    kind = classify_failure(str(e))
                    if kind == "rate_limit":
                        raise RateLimitError("12306 限流/反爬: " + str(e)[:160])
                    last = e
                    self.close()
                    if attempt + 1 < attempts:
                        time.sleep(1.5 * (attempt + 1))
            raise last

    def call_obj(self, name, args, attempts=3):
        """调用工具并解析 JSON 信封 {success, ...}。
        success=false 时按错误分类：限流→RateLimitError，无结果→返回原对象，其他→QueryError。"""
        text = self.call(name, args, attempts=attempts)
        try:
            obj = json.loads(text)
        except ValueError:
            kind = classify_failure(text)
            if kind == "rate_limit":
                raise RateLimitError("12306 限流/反爬: " + (text[:160] or "空响应"))
            raise QueryError("响应不是 JSON: " + (text[:160] or "empty"))
        if isinstance(obj, dict) and obj.get("success") is False:
            err = str(obj.get("error") or text)
            kind = classify_failure(err)
            if kind == "rate_limit":
                raise RateLimitError("12306 限流/反爬: " + err[:160])
            if kind == "empty":
                return obj            # 无直达/无中转：调用方自己看 count/列表
            raise QueryError(err)
        return obj

    def close(self):
        if self._proc is not None:
            try:
                self._proc.terminate()
            except OSError:
                pass
            self._proc = None


# ---------------------------------------------------------------- 基础工具

def _dur_min(lishi):
    """'02:07' / '31:3' -> 分钟数"""
    m = re.match(r"^(\d+):(\d+)$", (lishi or "").strip())
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def hhmm_min(s):
    """'08:21' -> 当日分钟数"""
    m = re.match(r"^(\d{1,2}):(\d{2})$", (s or "").strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


_SEAT_NAME = {
    "business": "商务座", "first_class": "一等座", "second_class": "二等座",
    "advanced_soft_sleeper": "高级软卧", "soft_sleeper": "软卧", "hard_sleeper": "硬卧",
    "soft_seat": "软座", "hard_seat": "硬座", "no_seat": "无座", "dongwo": "动卧",
}


def _seat_status(val):
    v = (val or "").strip()
    if v == "有":
        return "有票"
    if v == "无":
        return "无票"
    if v.isdigit():
        return "剩余%s张票" % v
    return v


# ---------------------------------------------------------------- 语义封装

def query_tickets(mcp, date, from_s, to_s, with_prices=True):
    """query-tickets(+query-ticket-price 合并) -> 车次列表（legacy 面板格式 + price2）。
    无直达返回 []；限流抛 RateLimitError。"""
    obj = mcp.call_obj("query-tickets", {
        "from_station": from_s, "to_station": to_s, "train_date": date})
    trains_raw = obj.get("trains") or []
    prices = {}
    if trains_raw and with_prices:
        prices = query_prices(mcp, date, from_s, to_s)
    out = []
    for t in trains_raw:
        code = t.get("train_no") or ""
        pr = prices.get(code, {})
        price2 = pr.get("二等座")
        if price2 is None and pr:
            try:
                price2 = min(float(v) for v in pr.values())
            except ValueError:
                price2 = None
        seats = {}
        for k, v in (t.get("seats") or {}).items():
            name = _SEAT_NAME.get(k)
            if not name:
                continue
            seats[name] = {"status": _seat_status(v), "price": pr.get(name)}
        out.append({
            "code": code,
            "from_station": t.get("from_station"),
            "to_station": t.get("to_station"),
            "depart": t.get("start_time"),
            "arrive": t.get("arrive_time"),
            "duration_min": _dur_min(t.get("duration")),
            "price2": float(price2) if price2 is not None else None,
            "seats": seats,
        })
    return out


def query_prices(mcp, date, from_s, to_s):
    """query-ticket-price -> {train_code: {席别名: 价格float}}。磁盘缓存（票价静态）。
    缓存两级：带日期 key + 无日期 key（预热器写无日期 key，跨日期命中）。"""
    cache_key = "%s|%s|%s" % (from_s, to_s, date)
    bare_key = "%s|%s|" % (from_s, to_s)
    store = price_store.read()
    cached = store.get(cache_key)
    if cached is None:
        cached = store.get(bare_key)   # 票价不随日期变，无日期缓存可复用
    if cached is not None:
        return cached
    obj = mcp.call_obj("query-ticket-price", {
        "from_station": from_s, "to_station": to_s, "train_date": date})
    out = {}
    for it in (obj.get("data") or []):
        code = it.get("train_code")
        if not code:
            continue
        pr = {}
        for name, val in (it.get("prices") or {}).items():
            try:
                pr[name] = float(val)
            except (TypeError, ValueError):
                continue
        out[code] = pr
    if out:
        price_store.update(lambda d: (d.update({cache_key: out}),
                                      d.update({bare_key: out})))
    return out


def tr_lookup(store, code):
    """在经停缓存 dict 里查车次（key=车次号，兼容旧 key 'code|date' / 'code|from->to|date'）。"""
    v = store.get(code)
    if v:
        return v
    prefix = code + "|"
    for k, vv in store.items():
        if k.startswith(prefix) and vv:
            return vv
    return None

def route_stops_cached(train_code):
    """车次经停缓存查询（读盘）。"""
    store = trainroutes_store.read()
    return tr_lookup(store, train_code)

def query_route_stops(mcp, train_code, date, from_s, to_s):
    """get-train-route-stations -> [(站名, 到, 开)]（车次完整经停表）。
    磁盘缓存 key=车次号（同一车次经停表稳定，不随查询区间/日期变）。"""
    store = trainroutes_store.read()
    cached = tr_lookup(store, train_code)
    if cached:
        if train_code not in store:
            # 旧 key 迁移到新 key（仅命中旧 key 时写一次，别每次命中都重写盘）
            trainroutes_store.update(lambda d: d.setdefault(train_code, cached))
        return [tuple(x) for x in cached]
    obj = mcp.call_obj("get-train-route-stations", {
        "train_no": train_code, "from_station": from_s,
        "to_station": to_s, "train_date": date})
    stops = [(s.get("station_name"), s.get("arrive_time"), s.get("start_time"))
             for s in (obj.get("stations") or [])]
    if stops:
        trainroutes_store.update(lambda d: d.update({train_code: [list(s) for s in stops]}))
    return stops


def query_transfers(mcp, date, from_s, to_s):
    """query-transfer（官方换乘，自动分页） -> [plan]（price2 暂缺，见 enrich_plan_prices）。
    plan = {legs:[{code,from_st,to_st,depart,arrive,duration_min,price2}],
            transfer_city, wait_min, total_min, total_price}"""
    obj = mcp.call_obj("query-transfer", {
        "from_station": from_s, "to_station": to_s, "train_date": date})
    out = []
    for item in (obj.get("transfers") or []):
        segs = item.get("segments") or []
        if len(segs) < 2:
            continue
        legs = [{
            "code": s.get("train_code"),
            "from_st": s.get("from_station"),
            "to_st": s.get("to_station"),
            "depart": s.get("start_time"),
            "arrive": s.get("arrive_time"),
            "duration_min": _dur_min(s.get("duration")),
            "price2": None,
        } for s in segs]
        wait = _wait_between(legs[0], legs[1], item.get("wait_time"))
        total = _dur_min(item.get("total_duration"))
        if total is None:
            total = sum(l.get("duration_min") or 0 for l in legs) + (wait or 0)
        out.append({
            "legs": legs,
            "transfer_city": item.get("middle_station") or "",
            "wait_min": wait,
            "total_min": total,
            "total_price": None,
        })
    return out


def enrich_plan_prices(mcp, date, plans, budget=10):
    """给官方换乘方案补票价：每条腿按 (出发站,到达站) 查一次 query-ticket-price，
    按车次号对号入座。最多 budget 条不同区间，其余保持 price2=None。"""
    need = {}
    for p in plans:
        for leg in p.get("legs", []):
            if leg.get("price2") is None and leg.get("code"):
                need.setdefault((leg["from_st"], leg["to_st"]), []).append(leg)
    for route in list(need.keys())[:budget]:
        try:
            prices = query_prices(mcp, date, route[0], route[1])
        except (RateLimitError, QueryError):
            continue
        for leg in need[route]:
            pr = prices.get(leg["code"], {})
            v = pr.get("二等座")
            if v is None and pr:
                try:
                    v = min(float(x) for x in pr.values())
                except ValueError:
                    v = None
            leg["price2"] = float(v) if v is not None else None
    for p in plans:
        ps = [l["price2"] for l in p["legs"]]
        p["total_price"] = round(sum(ps), 1) if all(x is not None for x in ps) else None
    return plans


def _wait_between(leg1, leg2, wait_text=None):
    """两腿中转等待分钟数：优先按时刻推（可跨午夜），失败退回 wait_time 文本。"""
    a = hhmm_min(leg1.get("arrive"))
    d = hhmm_min(leg2.get("depart"))
    if a is not None and d is not None:
        wait = d - a
        if wait < 0:
            wait += 24 * 60
        return wait
    if wait_text:
        m = re.search(r"(\d+)\s*小时", wait_text)
        n = re.search(r"(\d+)\s*分", wait_text)
        if m or n:
            return (int(m.group(1)) * 60 if m else 0) + (int(n.group(1)) if n else 0)
    return None


# ---------------------------------------------------------------- 车站坐标

_GEO = None
_GEO_LOCK = threading.Lock()


def load_geo():
    global _GEO
    with _GEO_LOCK:
        if _GEO is None:
            with open(GEO_PATH, encoding="utf-8") as f:
                _GEO = json.load(f)
        return _GEO


def station_coords(name):
    """站名 -> [lat, lng]；StationGeo.json 存 [lng, lat]，这里翻转。查不到返回 None。
    站名内多余空白归一（'青  岛北' -> '青岛北'）。"""
    geo = load_geo()
    n = re.sub(r"\s+", "", name or "")
    pt = geo.get(n) or geo.get(name)
    if pt:
        return [pt[1], pt[0]]
    return None


# ---------------------------------------------------------------- 数据存储

class JsonStore:
    """flock 保护的 JSON 读写（跨进程安全：server 后台补查 与 build_matrix 同时写）。"""

    def __init__(self, path, default):
        self.path = path
        self.lock_path = path + ".lock"
        self.default = default

    def read(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return json.loads(json.dumps(self.default))

    def update(self, fn):
        """fn(data) 就地修改；持锁读-改-原子写。"""
        import fcntl
        with open(self.lock_path, "a+") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                data = self.read()
                fn(data)
                tmp = self.path + ".tmp.%d" % os.getpid()
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(tmp, self.path)
                return data
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)


MATRIX_PATH = os.path.join(DATA, "matrix.json")
ROUTES_PATH = os.path.join(DATA, "routes.json")
TRAINROUTES_PATH = os.path.join(DATA, "trainroutes.json")
PRICE_PATH = os.path.join(DATA, "pricecache.json")
TRANSFER_CACHE_PATH = os.path.join(DATA, "transfercache.json")
ROUTE_RETRY_PATH = os.path.join(DATA, "route-retry.json")

matrix_store = JsonStore(MATRIX_PATH, {"date": "", "updated": "", "pairs": {}})
routes_store = JsonStore(ROUTES_PATH, {})
trainroutes_store = JsonStore(TRAINROUTES_PATH, {})
price_store = JsonStore(PRICE_PATH, {})
transfer_store = JsonStore(TRANSFER_CACHE_PATH, {})
route_retry_store = JsonStore(ROUTE_RETRY_PATH, {"todo": [], "failed": [], "updated": ""})


# ---------------------------------------------------------------- 真实路线

def _match_stop(stops, name, start=0, end=None):
    """在经停表 [start, end) 里找 name 对应的站索引：先精确，后前后缀（南京->南京南）。
    站名内多余空白忽略（'青  岛北'=='青岛北'）。"""
    end = len(stops) if end is None else end
    name = re.sub(r"\s+", "", name or "")
    for i in range(start, end):
        if re.sub(r"\s+", "", stops[i][0] or "") == name:
            return i
    for i in range(start, end):
        sn = re.sub(r"\s+", "", stops[i][0] or "")
        if sn and (sn.startswith(name) or name.startswith(sn)):
            return i
    return None


def slice_route(stops, from_name, to_name):
    """截取 from 站到 to 站之间的停站序列（含两端）。找不到返回 None。"""
    i = _match_stop(stops, from_name)
    if i is None:
        return None
    j = _match_stop(stops, to_name, start=i + 1)
    if j is None:
        return None
    return [s[0] for s in stops[i:j + 1]]


def fetch_route_polyline(mcp, train_code, date, from_name, to_name):
    """真实折线 [[lat,lng],...]；缓存 key=车次|区间。失败/点数不足返回 None。"""
    cache_key = "%s|%s->%s" % (train_code, from_name, to_name)
    cached = routes_store.read().get(cache_key)
    if cached:
        return cached
    stops = query_route_stops(mcp, train_code, date, from_name, to_name)
    if not stops:
        return None
    names = slice_route(stops, from_name, to_name)
    if not names:
        return None
    pts = []
    for n in names:
        c = station_coords(n)
        if c:
            pts.append(c)
    if len(pts) < 2:
        return None
    routes_store.update(lambda d: d.update({cache_key: pts}))
    return pts


# ---------------------------------------------------------------- 直达矩阵

def pair_key(f, t):
    return "%s->%s" % (f, t)


def summarize_pair(trains):
    """车次列表 -> 矩阵条目（不含 route）。trains 按历时升序。
    ⚠️ 全量存所有车次（2026-11 起：小站点上图需要扫全部车次经停）。"""
    if not trains:
        return {"has_direct": False, "n_trains": 0, "trains": []}
    prices = [t["price2"] for t in trains if t.get("price2") is not None]
    fast = trains[0]
    def row(t):
        r = {"code": t["code"], "from_st": t["from_station"], "to_st": t["to_station"],
             "depart": t["depart"], "arrive": t["arrive"],
             "duration_min": t.get("duration_min"), "price2": t.get("price2")}
        s2 = (t.get("seats") or {}).get("二等座") or (t.get("seats") or {}).get("二等座重联")
        if s2:
            r["seat2_status"] = s2.get("status")
        return r
    return {
        "has_direct": True,
        "n_trains": len(trains),
        "min_duration_min": fast.get("duration_min"),
        "min_price2": min(prices) if prices else None,
        "fastest": {"code": fast["code"], "from_st": fast["from_station"],
                    "to_st": fast["to_station"],
                    "depart": fast["depart"], "arrive": fast["arrive"]},
        "trains": [row(t) for t in trains],
    }


def query_pair(mcp, date, from_city, to_city, with_route=True):
    """查一对城市：query-tickets(+票价) -> 矩阵条目（含最快车次真实折线）。"""
    trains = query_tickets(mcp, date, from_city, to_city)
    trains.sort(key=lambda t: t.get("duration_min") or 99999)
    entry = summarize_pair(trains)
    entry["date"] = date
    if with_route and entry.get("has_direct"):
        f = entry["fastest"]
        try:
            pts = fetch_route_polyline(mcp, f["code"], date, f["from_st"], f["to_st"])
        except (QueryError, OSError, ValueError):
            pts = None
        if pts:
            entry["route_key"] = "%s|%s->%s" % (f["code"], f["from_st"], f["to_st"])
            entry["route_npts"] = len(pts)
    return entry
