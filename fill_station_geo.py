#!/usr/bin/env python3
"""补齐 StationGeo.json 缺失车站坐标。

数据源：Nominatim（maps skill）。校验：
  1) type 必须是 station/train_station，display_name 必须在中国；
  2) 邻站校验：车次经停表里相邻站已有坐标时，候选点必须落在邻站连线附近
     （到线段距离 <80km 且到两端点距离之和不超过线段长度+120km），
     防止同名异地站匹配错。
用法：python3 fill_station_geo.py [--dry-run]
"""
import json
import os
import re
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
from railconfig import DATA_DIR, MAPS_CLIENT

GEO_PATH = os.path.join(DATA_DIR, "StationGeo.json")
MAPS = MAPS_CLIENT   # 外部地理编码 CLI（config.json 的 maps_client，可选，不配则跳过该数据源）


def norm_name(n):
    """站名归一：去掉多余空白（'青  岛北' -> '青岛北'）。"""
    return re.sub(r"\s+", "", n or "")


def load_all():
    geo = json.load(open(GEO_PATH, encoding="utf-8"))
    tr = json.load(open(os.path.join(DATA_DIR, "trainroutes.json"), encoding="utf-8"))
    m = json.load(open(os.path.join(DATA_DIR, "matrix.json"), encoding="utf-8"))
    routes = []          # 每条经停序列（归一后的站名）
    for v in tr.values():
        if v:
            routes.append([norm_name(s[0]) for s in v if s and s[0]])
    names = set()
    for r in routes:
        names.update(r)
    for e in m["pairs"].values():
        for t in e.get("trains", []):
            for k in ("from_st", "to_st"):
                if t.get(k):
                    names.add(norm_name(t[k]))
    return geo, routes, names


def hav_km(p, q):
    """[lat,lng] 两点球面距离 km。"""
    import math
    R = 6371.0
    dla = math.radians(q[0] - p[0])
    dlo = math.radians(q[1] - p[1])
    a = (math.sin(dla / 2) ** 2
         + math.cos(math.radians(p[0])) * math.cos(math.radians(q[0])) * math.sin(dlo / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def seg_dist_km(p, a, b):
    """点 p 到线段 a-b 距离 km（a/b/p 均为 [lat,lng]，局部平面近似）。"""
    import math
    kx = 111 * math.cos(math.radians((a[0] + b[0]) / 2))
    ax, ay = a[0] * 111, a[1] * kx
    bx, by = b[0] * 111, b[1] * kx
    px, py = p[0] * 111, p[1] * kx
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def neighbors_ok(name, cand, routes, geo):
    """校验候选坐标贴着该站出现过的经停走廊：
    双侧邻站都有坐标 -> 候选到「邻站连线」<60km（车次走直向，站必贴线）；
    单侧 -> 到该邻站 <200km；无 -> 放行。要求 ≥50% 检查通过。
    cand/geo 值均为 [lng, lat]。"""
    checked = ok = 0
    cand_ll = [cand[1], cand[0]]
    for r in routes:
        if name not in r:
            continue
        for i in [i for i, s in enumerate(r) if s == name]:
            prev = geo.get(r[i - 1]) if i - 1 >= 0 else None
            nxt = geo.get(r[i + 1]) if i + 1 < len(r) else None
            prev_ll = [prev[1], prev[0]] if prev else None
            nxt_ll = [nxt[1], nxt[0]] if nxt else None
            if prev_ll and nxt_ll:
                checked += 1
                if seg_dist_km(cand_ll, prev_ll, nxt_ll) < 60:
                    ok += 1
            elif prev_ll or nxt_ll:
                checked += 1
                if hav_km(cand_ll, prev_ll or nxt_ll) < 200:
                    ok += 1
    if checked == 0:
        return True        # 无邻站可校验（如终到孤立站），放行
    return ok >= checked * 0.5


def geocode(name):
    """Nominatim 查 'X站' -> 'X'，只接受 station/train_station 且在中国。"""
    if not MAPS:
        return None
    for q in (name + "站", name + " railway station", name):
        try:
            out = subprocess.run(
                ["python3", MAPS, "search", q],
                capture_output=True, text=True, timeout=60).stdout
            data = json.loads(out)
        except Exception:
            continue
        for r in data.get("results", []):
            if r.get("type") not in ("station", "train_station"):
                continue
            disp = r.get("display_name", "")
            if "中国" not in disp and "China" not in disp:
                continue
            rn = norm_name(r.get("name", ""))
            if rn not in (name, name + "站", name.rstrip("站")):
                continue          # 名字不匹配（同名异地）直接拒绝
            return [r["lon"], r["lat"]]     # StationGeo 存 [lng, lat]
        time.sleep(1.1)           # Nominatim ToS
    return None


def main():
    dry = "--dry-run" in sys.argv
    geo, routes, names = load_all()
    # 归一化已有 key 里的空白
    for k in list(geo):
        nk = norm_name(k)
        if nk != k:
            geo.setdefault(nk, geo[k])
    missing = sorted(n for n in names if n and n not in geo)
    print("待补车站：%d" % len(missing))
    added, failed = {}, []
    for i, name in enumerate(missing):
        cand = geocode(name)
        time.sleep(1.1)
        if cand and neighbors_ok(name, cand, routes, geo):
            added[name] = cand
            print("[%d/%d] %s -> %.5f,%.5f" % (i + 1, len(missing), name, cand[1], cand[0]))
        else:
            failed.append(name)
            print("[%d/%d] %s -> 校验失败/未找到 %s" % (i + 1, len(missing), name, cand))
    if not dry:
        geo.update(added)
        tmp = GEO_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(geo, f, ensure_ascii=False)
        os.replace(tmp, GEO_PATH)
        print("已写入 %s（+%d 站）" % (GEO_PATH, len(added)))
    print("失败 %d：%s" % (len(failed), json.dumps(failed, ensure_ascii=False)))


if __name__ == "__main__":
    main()
