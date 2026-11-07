#!/usr/bin/env python3
"""补齐/清洗 StationGeo.json 车站坐标（P1：折线必须覆盖经停全序列）。

范围：data/allroutes.json 全部经停站。
流程（每轮，共 3 轮，邻站互相解锁）：
  1) 清洗（scrub）：坐标偏离“前后邻站连线”>200km 的站判为错配
     （如 夏格庄→西藏、河东机场→山东 这类同名异地 POI 事故），
     原值挪进 data/StationGeo-quarantine.json 留证，再重新解析。
  2) 解析：高德（AMAP_API_KEY）→ Nominatim top-5 候选按“离邻站锚点最近”挑选
     （锚点=已知邻站坐标均值），防 Nominatim 首选结果异地错配。
  3) 校验：候选须在锚点 150km 内（无锚点无邻站时不校验，直接采信）。
结果 WGS-84 落盘（StationGeo 格式 [lng, lat]；高德 GCJ-02 先纠偏）。

Usage: python3 fill_station_geo2.py [--limit N] [--scrub-only]
"""

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
from railconfig import DATA_DIR as DATA, amap_key
GEO_PATH = os.path.join(DATA, "StationGeo.json")
QUAR_PATH = os.path.join(DATA, "StationGeo-quarantine.json")
sys.path.insert(0, BASE)
from build_cities import gcj2wgs   # 复用 GCJ-02 -> WGS-84

SEG_DEV_KM = 200.0      # 偏离前后邻站连线的判定阈值
NEAR_KM = 150.0         # 候选与锚点最大距离


def norm(n):
    return re.sub(r"\s+", "", n or "")


def hav_km(p, q):
    R = 6371.0
    dla = math.radians(q[0] - p[0])
    dlo = math.radians(q[1] - p[1])
    a = (math.sin(dla / 2) ** 2 +
         math.cos(math.radians(p[0])) * math.cos(math.radians(q[0])) * math.sin(dlo / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def seg_dist_km(p, a, b):
    ax, ay = a[1], a[0]
    bx, by = b[1], b[0]
    px, py = p[1], p[0]
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return hav_km(p, a)
    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return hav_km(p, (ay + t * dy, ax + t * dx))


def amap_geocode(query, key):
    url = ("https://restapi.amap.com/v3/geocode/geocode?address=%s&key=%s"
           % (urllib.parse.quote(query), key))
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            obj = json.loads(r.read().decode("utf-8"))
        g = (obj.get("geocodes") or [])
        if obj.get("status") != "1" or not g:
            return None
        loc = (g[0].get("location") or "").split(",")
        if len(loc) != 2:
            return None
        lat, lng = gcj2wgs(float(loc[1]), float(loc[0]))
        return (lat, lng)
    except Exception:
        return None


def nominatim_geocode(query, expect=None):
    """Nominatim top-5 候选按离锚点最近挑选（首选结果常是同名异地 POI）。"""
    url = ("https://nominatim.openstreetmap.org/search?format=json&limit=5&q=%s"
           % urllib.parse.quote(query + ", 中国"))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "railmap-fill-station-geo/1.0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            arr = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    if not arr:
        return None
    if expect is None:
        return (float(arr[0]["lat"]), float(arr[0]["lon"]))
    best = None
    for a in arr:
        p = (float(a["lat"]), float(a["lon"]))
        d = hav_km(p, expect)
        if best is None or d < best[0]:
            best = (d, p)
    return best[1]


def load_seqs(routes):
    seqs = []
    for _code, stops in routes.items():
        seq = [norm(s.get("station")) for s in stops if s.get("station")]
        if len(seq) >= 2:
            seqs.append(seq)
    return seqs


def scrub(geo, seqs):
    """坐标偏离前后邻站连线 >200km 的站 -> 只报告不删点（data/StationGeo-scrub-report.json）。

    ⚠️ 不做自动删除：站名一码多站（清河=北京/河北）与坏邻居连坐会让清洗误伤
    （北京南被连坐过两次），错配的定点修复走 fill_station_geo_repair.py。"""
    suspects = {}
    for seq in seqs:
        for i, n in enumerate(seq):
            pt = geo.get(n)
            if not pt:
                continue
            p = (pt[1], pt[0])
            prev = next((geo.get(seq[j]) for j in range(i - 1, -1, -1) if geo.get(seq[j])), None)
            nxt = next((geo.get(seq[j]) for j in range(i + 1, len(seq)) if geo.get(seq[j])), None)
            if prev and nxt:
                dev = seg_dist_km(p, (prev[1], prev[0]), (nxt[1], nxt[0]))
                if dev > SEG_DEV_KM:
                    suspects[n] = {"pt": pt, "dev_km": round(dev, 1),
                                   "reason": "偏离走廊", "seq": seq[max(0, i - 2):i + 3]}
    if suspects:
        json.dump(suspects, open(os.path.join(DATA, "StationGeo-scrub-report.json"), "w",
                                 encoding="utf-8"), ensure_ascii=False)
    return set()


def expected_point(n, seqs, geo):
    pts = []
    for seq in seqs:
        if n not in seq:
            continue
        for idx, s in enumerate(seq):
            if s != n:
                continue
            for j in (idx - 1, idx + 1):
                if 0 <= j < len(seq):
                    nb = geo.get(seq[j])
                    if nb:
                        pts.append((nb[1], nb[0]))
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def neighbor_dists_ok(n, seqs, geo, cand):
    checked = 0
    for seq in seqs:
        if n not in seq:
            continue
        for idx, s in enumerate(seq):
            if s != n:
                continue
            for j in (idx - 1, idx + 1):
                if 0 <= j < len(seq):
                    nb = geo.get(seq[j])
                    if nb:
                        checked += 1
                        if hav_km(cand, (nb[1], nb[0])) > NEAR_KM:
                            return False
        if checked >= 3:
            break
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--scrub-only", action="store_true")
    args = ap.parse_args()

    geo = json.load(open(GEO_PATH, encoding="utf-8"))
    routes = json.load(open(os.path.join(DATA, "allroutes.json"), encoding="utf-8"))
    seqs = load_seqs(routes)

    bad = scrub(geo, seqs)
    if bad:
        print("清洗：隔离 %d 个偏离走廊的坐标: %s" % (len(bad), sorted(bad)[:10]))
        json.dump(geo, open(GEO_PATH, "w", encoding="utf-8"), ensure_ascii=False)
    if args.scrub_only:
        return

    names = sorted({n for seq in seqs for n in seq} - set(geo.keys()))
    print("经停站 %d 个名字，缺坐标 %d 个" % (
        len({n for seq in seqs for n in seq}), len(names)))
    if args.limit:
        names = names[:args.limit]

    key = amap_key()
    filled = 0
    for _pass in range(3):
        todo = [n for n in names if n not in geo]
        if not todo:
            break
        for i, n in enumerate(todo):
            expect = expected_point(n, seqs, geo)
            cand = None
            if key:
                for q in (n, n + "站"):
                    cand = amap_geocode(q, key)
                    time.sleep(0.36)
                    if cand:
                        break
            if not cand:
                for q in (n + "站", n):
                    cand = nominatim_geocode(q, expect=expect)
                    time.sleep(1.1)
                    if cand:
                        break
            if not cand:
                print("  [p%d %d/%d] %s 无结果" % (_pass + 1, i + 1, len(todo), n))
                continue
            if expect and hav_km(cand, expect) > NEAR_KM:
                print("  [p%d %d/%d] %s 候选距锚点 %dkm 过远，弃用" % (
                    _pass + 1, i + 1, len(todo), n, hav_km(cand, expect)))
                continue
            if not neighbor_dists_ok(n, seqs, geo, cand):
                print("  [p%d %d/%d] %s 邻站校验不过，弃用候选" % (_pass + 1, i + 1, len(todo), n))
                continue
            geo[n] = [round(cand[1], 6), round(cand[0], 6)]
            filled += 1
            if filled % 20 == 0:
                json.dump(geo, open(GEO_PATH, "w", encoding="utf-8"), ensure_ascii=False)
                print("  checkpoint：%d 已补" % filled)
    json.dump(geo, open(GEO_PATH, "w", encoding="utf-8"), ensure_ascii=False)
    # 最后兜底：仍缺坐标但前后邻站都有坐标的站 -> 走廊中点插值
    # （仅用于折线/小点渲染；插值来源记 data/StationGeo-interp.json 可审计）
    interp = {}
    try:
        interp = json.load(open(os.path.join(DATA, "StationGeo-interp.json"), encoding="utf-8"))
    except (OSError, ValueError):
        interp = {}
    extra = 0
    for _pass in range(2):
        for seq in seqs:
            for i, n in enumerate(seq):
                if n in geo:
                    continue
                prev = next((geo.get(seq[j]) for j in range(i - 1, -1, -1) if geo.get(seq[j])), None)
                nxt = next((geo.get(seq[j]) for j in range(i + 1, len(seq)) if geo.get(seq[j])), None)
                if prev and nxt:
                    geo[n] = [round((prev[0] + nxt[0]) / 2, 6), round((prev[1] + nxt[1]) / 2, 6)]
                    interp[n] = {"pt": geo[n], "src": "interp", "between": ["prev", "next"]}
                    extra += 1
    if extra:
        json.dump(geo, open(GEO_PATH, "w", encoding="utf-8"), ensure_ascii=False)
        json.dump(interp, open(os.path.join(DATA, "StationGeo-interp.json"), "w",
                               encoding="utf-8"), ensure_ascii=False)
    print("完成：补 %d 个站坐标，插值 %d 个，隔离 %d 个，StationGeo 现有 %d 条" % (
        filled, extra, len(bad), len(geo)))


if __name__ == "__main__":
    main()
