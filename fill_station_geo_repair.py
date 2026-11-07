#!/usr/bin/env python3
"""定点修复 StationGeo 疑难站坐标（fill_station_geo2 的补充）。

场景：Nominatim 首选结果是同名异地 POI（夏格庄→西藏、桐城东→台湾…），
邻站锚点也可能被坏点污染。做法：
  1) 回滚误隔离（北京南/烟台 这类被坏邻居连坐的正确坐标），
     只保留确认错配的（夏格庄、河东机场）在隔离区；
  2) 对每个疑难站：Nominatim top-5 候选 × display_name 关键词白名单
     （站所在县/市/区名）双重过滤，取离邻站锚点最近者。
每一步都打印候选 display 供人工核对。
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
from railconfig import DATA_DIR as DATA
GEO_PATH = os.path.join(DATA, "StationGeo.json")
QUAR_PATH = os.path.join(DATA, "StationGeo-quarantine.json")
sys.path.insert(0, BASE)
from fill_station_geo2 import hav_km, load_seqs

# 确认错配（原坐标真实指向同名异地 POI），保留隔离
KEEP_QUARANTINED = {"夏格庄", "河东机场"}

# 疑难站 -> display_name 必须命中的关键词（站所在县/市/区，防同名异地）
NAME_KEYWORDS = {
    "夏格庄": r"莱西|夏格庄",
    "河东机场": r"银川|河东",
    "沙湖": r"平罗|石嘴山|沙湖",
    "桐城东": r"桐城",
    "观音机场": r"睢宁|观音|徐州",
    "乳山南": r"乳山",
    "惠农南": r"惠农|石嘴山",
    "白银南": r"白银",
    "平川西": r"平川|白银",
    "靖远北": r"靖远|白银",
    "北滩": r"靖远|北滩|白银",
    "长治南": r"长治|上党",
    "祁门南": r"祁门|黄山",
    "巴彦淖尔": r"临河|巴彦淖尔",
    "威海南海": r"文登|威海|乳山",
    "海东": r"海东|乐都|平安",
    "惠安堡": r"惠安堡|盐池|吴忠",
    "铜川": r"铜川|王益|印台",
    "白山": r"白山|浑江",
}


def nominatim_candidates(query, limit=5):
    url = ("https://nominatim.openstreetmap.org/search?format=json&limit=%d&q=%s"
           % (limit, urllib.parse.quote(query + ", 中国")))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "railmap-fill-repair/1.0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print("  查询失败 %s: %s" % (query, e))
        return []


def anchor_for(n, seqs, geo):
    pts = []
    for seq in seqs:
        if n not in seq:
            continue
        i = seq.index(n)
        for j in (i - 1, i + 1):
            if 0 <= j < len(seq):
                nb = geo.get(seq[j])
                if nb:
                    pts.append((nb[1], nb[0]))
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def main():
    geo = json.load(open(GEO_PATH, encoding="utf-8"))
    routes = json.load(open(os.path.join(DATA, "allroutes.json"), encoding="utf-8"))
    seqs = load_seqs(routes)

    # 1) 回滚误隔离
    try:
        quar = json.load(open(QUAR_PATH, encoding="utf-8"))
    except (OSError, ValueError):
        quar = {}
    for n in list(quar.keys()):
        if n not in KEEP_QUARANTINED:
            geo[n] = quar[n]["pt"]
            del quar[n]
            print("回滚误隔离：%s" % n)
    json.dump(quar, open(QUAR_PATH, "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(geo, open(GEO_PATH, "w", encoding="utf-8"), ensure_ascii=False)

    # 2) 疑难站：关键词白名单 + 锚点最近
    missing = sorted({n for seq in seqs for n in seq} - set(geo.keys()))
    todo = [n for n in missing if n in NAME_KEYWORDS] + \
           [n for n in missing if n not in NAME_KEYWORDS]
    fixed = 0
    for n in todo:
        kw = NAME_KEYWORDS.get(n)
        expect = anchor_for(n, seqs, geo)
        found = None
        for q in (n + "站", n):
            for c in nominatim_candidates(q):
                disp = c.get("display_name", "")
                if kw and not re.search(kw, disp):
                    continue
                p = (float(c["lat"]), float(c["lon"]))
                d = hav_km(p, expect) if expect else 0
                if expect and d > 300:
                    continue
                if found is None or d < found[0]:
                    found = (d, p, disp[:70])
            if found:
                break
            time.sleep(1.1)
        time.sleep(1.1)
        if found:
            geo[n] = [round(found[1][1], 6), round(found[1][0], 6)]
            fixed += 1
            print("修复 %s -> [%.5f, %.5f] 距锚点 %.0fkm | %s" % (
                n, found[1][1], found[1][0], found[0], found[2]))
        else:
            print("未修复 %s（无关键词匹配候选）" % n)
    json.dump(geo, open(GEO_PATH, "w", encoding="utf-8"), ensure_ascii=False)
    print("修复 %d 个；仍缺 %d 个: %s" % (
        fixed, len(set(missing) - set(geo.keys()) | (set(missing) & set(geo.keys())) - set(geo.keys())),
        sorted(set(missing) - set(geo.keys()))))


if __name__ == "__main__":
    main()
