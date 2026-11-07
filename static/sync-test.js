/* railmap 渲染同步回归测试（浏览器控制台 / 无头浏览器可跑）
   用法：页面加载完后  await runSyncTest()  → 返回 JSON 报告
   断言：城市点/小站点屏幕坐标 vs map.latLngToContainerPoint(gj(纠偏后坐标)) ≤1px
   覆盖：z=4/5/6.5/8、拖动(rawPanBy 多步)、panBy、setZoom 无动画/带动画、pinch 逐帧、
        幽灵元素、重要度分级显隐、点位中心、标签去重、估算卡移除 */
window.runSyncTest = async function () {
  const R = { asserts: [], counts: {}, errors: [] };
  const TOL = 1.0;                 // 静止断言容差（px）
  const TOL_DYN = 2.0;             // 动态帧容差（含取整，px）
  const testCities = ["盐城", "北京", "乌鲁木齐", "广州", "上海"];
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  function assert(name, ok, detail) {
    R.asserts.push({ name, ok: !!ok, detail: String(detail == null ? "" : detail) });
    if (!ok) R.errors.push(name + " :: " + detail);
  }

  /* 画布绘制点 → 当前屏幕(container)坐标。style 模式：用 setTransform 的确定值
     （无头下 CSS 动画冻结，不用 computed/rect）；rect 模式：真实 DOM 测量（静止时用） */
  function canvasScreen(name, mode) {
    const d = (window.__cityDebug && window.__cityDebug.drawn || {})[name];
    if (!d) return null;
    let tx = 0, ty = 0, s = 1;
    if (mode === "rect") {
      const cr = cityCanvas.getBoundingClientRect();
      const mr = map.getContainer().getBoundingClientRect();
      const st = getComputedStyle(cityCanvas).transform;
      const m = st && st.match(/matrix\(([^)]+)\)/);
      if (m) s = parseFloat(m[1].split(",")[0]);
      return { x: cr.left - mr.left + s * d.x, y: cr.top - mr.top + s * d.y };
    }
    const m = (cityCanvas.style.transform || "").match(
      /translate3d\(([-\d.]+)px,\s*([-\d.]+)px,\s*[-\d.]+px\)(?:\s*scale\(([-\d.]+)\))?/);
    if (m) { tx = +m[1]; ty = +m[2]; s = m[3] ? +m[3] : 1; }
    const pp = map._getMapPanePos();
    return { x: pp.x + tx + s * d.x, y: pp.y + ty + s * d.y };
  }
  function checkCities(tag, mode, tol) {
    tol = tol || TOL;
    let n = 0, maxErr = 0;
    for (const nm of testCities) {
      const p = canvasScreen(nm, mode);
      if (!p) continue;
      const c = CITY_MAP[nm];
      const e = map.latLngToContainerPoint(gj([c.lat, c.lng]));
      const err = Math.hypot(p.x - e.x, p.y - e.y);
      maxErr = Math.max(maxErr, err);
      assert(`城市点[${nm}]@${tag}`, err <= tol, `err=${err.toFixed(2)}px (tol ${tol})`);
      n++;
    }
    R.counts[tag + ":checked"] = n;
    R.counts[tag + ":maxErr"] = +maxErr.toFixed(2);
    if (n === 0) assert(`城市点覆盖@${tag}`, false, "没有任何测试城市被绘制（断言无效）");
    return n;
  }

  /* ===== Phase 1：z=4/5/6.5/8 × pan 前后 ===== */
  for (const z of [4, 5, 6.5, 8]) {
    for (const nm of testCities) {
      map.setView(gj([CITY_MAP[nm].lat, CITY_MAP[nm].lng]), z, { animate: false });
      forceCityDraw();
      checkCities(`z${z}/${nm}静止`, "style");
      // 拖动帧模拟：Draggable 等价（mapPane 平移 + move 事件），旧实现在此翻车
      for (let i = 0; i < 3; i++) {
        map._rawPanBy(new L.Point(-23, 17));
        map.fire("move");
        forceCityDraw();
      }
      checkCities(`z${z}/${nm}拖动3帧`, "style");
      map.panBy([69, -51], { animate: false });   // 松手等价（rawPanBy + move + moveend）
      forceCityDraw();
      checkCities(`z${z}/${nm}panBy后`, "style");
    }
  }

  /* ===== Phase 2：setZoom 无动画（每次先居中到测试城市，保证断言有对象） ===== */
  map.setView(gj([32.5, 112]), 5, { animate: false });
  forceCityDraw();
  const zl2 = [6.5, 8, 4.25, 5.5];
  for (let i = 0; i < zl2.length; i++) {
    const z = zl2[i], nm = testCities[i % testCities.length];
    map.setView(gj([CITY_MAP[nm].lat, CITY_MAP[nm].lng]), z, { animate: false });
    forceCityDraw();
    assert(`无极缩放 setZoom(${z}) 保持`, map.getZoom() === z, `zoom=${map.getZoom()}`);
    checkCities(`setZoom${z}无动画`, "style");
  }

  /* ===== Phase 3：setZoom 带动画（无头下用 setTransform 目标值断言 + 兜底结束后静止断言） ===== */
  const zl3 = [7.25, 5];
  for (let i = 0; i < zl3.length; i++) {
    const z = zl3[i], nm = testCities[(i + 2) % testCities.length];
    map.setView(gj([CITY_MAP[nm].lat, CITY_MAP[nm].lng]), map._limitZoom(z - 1), { animate: false });
    forceCityDraw();
    map.setZoom(z, { animate: true });
    checkCities(`setZoom${z}动画中(目标变换)`, "style");
    await sleep(330);                 // Leaflet setTimeout(250) 兜底结束动画
    forceCityDraw();
    assert(`setZoom(${z},动画后) 到位`, map.getZoom() === z, `zoom=${map.getZoom()}`);
    checkCities(`setZoom${z}动画后`, "style");
    checkCities(`setZoom${z}动画后(rect)`, "rect");
  }

  /* ===== Phase 4：pinch 逐帧（TouchZoom 同款 _move 调用） + 松手 _animateZoom ===== */
  map.setView(gj([31.23, 121.47]), 6, { animate: false });
  forceCityDraw();
  const z0 = map.getZoom();
  for (let i = 1; i <= 8; i++) {
    const z = z0 + i * 0.2;
    const c = map.unproject(map.project(map.getCenter(), z).subtract(new L.Point(i * 6, i * 4)), z);
    map._move(c, z, { pinch: true, round: false });
    checkCities(`pinch帧${i}(z=${z.toFixed(2)})`, "style", TOL_DYN);
  }
  map._animateZoom(map.getCenter(), map._limitZoom(z0 + 1.6), true, map.options.zoomSnap);
  checkCities("pinch松手(目标变换)", "style", TOL_DYN);
  await sleep(330);
  forceCityDraw();
  checkCities("pinch结束后", "style");

  /* ===== Phase 5：选择出发地（盐城）→ 小站点加载 + 幽灵检查 ===== */
  onCityClick(CITY_MAP["盐城"]);
  for (let i = 0; i < 60 && Object.keys(stMarkers).length === 0; i++) await sleep(250);
  const N = Object.keys(stMarkers).length;
  R.counts.stations_total = N;
  assert("小站点常显(加载到站点)", N > 0, `N=${N}`);
  // 快速翻缩放（旧 bug：label 残影/重复）
  for (const z of [4, 7.5, 6.9, 7.1, 8, 3.5, 7.5, 4]) {
    map.setZoom(z, { animate: false });
    forceCityDraw();
    applyZoomRules();
  }
  await sleep(120);
  const wraps = Array.from(document.querySelectorAll(".leaflet-marker-pane .st-icon .st-wrap"));
  R.counts.dom_markers = wraps.length;
  const names = wraps.map(w => w.dataset.st);
  assert("marker 数稳定(无幽灵 DOM)", wraps.length === N, `dom=${wraps.length} book=${N}`);
  assert("无重复 data-st/label", new Set(names).size === names.length,
    `unique=${new Set(names).size}/${names.length}`);

  /* ===== Phase 6：重要度分级显隐（z<5 全显 / 5-7 ~60% / z>=7 ~20%） ===== */
  function visibleStations() {
    return Object.values(stMarkers).filter(o => {
      const el = o.marker.getElement();
      const w = el && el.querySelector(".st-wrap");
      return w && !w.classList.contains("st-off");
    }).length;
  }
  map.setView(gj([33.4, 120.2]), 4, { animate: false });
  forceCityDraw(); applyZoomRules();
  R.counts.z4_visible = visibleStations();
  assert("z=4 全网小站点全显", R.counts.z4_visible === N, `${R.counts.z4_visible}/${N}`);
  map.setZoom(5.5, { animate: false }); forceCityDraw(); applyZoomRules();
  R.counts.z5_5_visible = visibleStations();
  const n60 = Math.max(Math.ceil(N * 0.2), Math.ceil(N * 0.6));
  assert("z=5.5 ≈60%", Math.abs(R.counts.z5_5_visible - n60) <= 2,
    `${R.counts.z5_5_visible} vs ${n60}`);
  map.setZoom(7.5, { animate: false }); forceCityDraw(); applyZoomRules();
  R.counts.z7_5_visible = visibleStations();
  const n20 = Math.max(1, Math.ceil(N * 0.2));
  assert("z=7.5 ≈20%", Math.abs(R.counts.z7_5_visible - n20) <= 2,
    `${R.counts.z7_5_visible} vs ${n20}`);
  assert("z=7.5 数量明显减少", R.counts.z7_5_visible <= R.counts.z4_visible * 0.35,
    `${R.counts.z7_5_visible} <= 35% of ${R.counts.z4_visible}`);
  assert("分级单调(20%⊂60%⊂全部)", R.counts.z7_5_visible <= R.counts.z5_5_visible &&
    R.counts.z5_5_visible <= R.counts.z4_visible,
    `${R.counts.z7_5_visible}/${R.counts.z5_5_visible}/${R.counts.z4_visible}`);

  /* ===== Phase 7：点位中心没偏（尺寸改版回归） ===== */
  map.setView(gj([32.5, 112]), 5.5, { animate: false });
  forceCityDraw(); applyZoomRules();
  for (const nm of testCities) {
    const d = (window.__cityDebug.drawn || {})[nm];
    if (!d) continue;
    const c = CITY_MAP[nm];
    const e = map.latLngToLayerPoint(gj([c.lat, c.lng]));
    assert(`城市点中心[${nm}]==绘制点`, Math.hypot(d.x - e.x, d.y - e.y) <= 0.01,
      `drawn=(${d.x},${d.y}) expect=(${e.x},${e.y})`);
  }
  const sample = Object.values(stMarkers).filter(o => o.st.lat != null).slice(0, 6);
  for (const o of sample) {
    const el = o.marker.getElement();
    const dot = el && el.querySelector(".st-dot");
    if (!dot) continue;
    const r = dot.getBoundingClientRect();
    const mr = map.getContainer().getBoundingClientRect();
    const e = map.latLngToContainerPoint(gj([o.st.lat, o.st.lng]));
    const err = Math.hypot(r.left + r.width / 2 - mr.left - e.x, r.top + r.height / 2 - mr.top - e.y);
    assert(`小站点中心[${o.st.station}]`, err <= 1.5, `err=${err.toFixed(2)}px`);
  }

  /* ===== Phase 8：城市标签去重 + 悬浮标签（只允许 from/to/hover） ===== */
  function labelSetOk(extra) {
    const allowed = new Set(extra || []);
    if (selFrom && !selFrom.isStation) allowed.add(selFrom.name);
    if (selTo && !selTo.isStation) allowed.add(selTo.name);
    return (window.__cityDebug.labels || []).every(n => allowed.has(n));
  }
  forceCityDraw();
  const labels0 = (window.__cityDebug.labels || []).slice();
  assert("标签只含交互态(无hover)", labelSetOk([]), JSON.stringify(labels0));
  assert("出发地城市出标签(盐城)", labels0.includes("盐城"), JSON.stringify(labels0));
  map.fire("mousemove", { containerPoint: map.latLngToContainerPoint(gj([CITY_MAP["南京"].lat, CITY_MAP["南京"].lng])) });
  forceCityDraw();
  const labels1 = (window.__cityDebug.labels || []).slice();
  assert("悬浮城市出标签(南京)", labels1.includes("南京"), JSON.stringify(labels1));
  assert("悬浮时标签仍只含交互态", labelSetOk(["南京"]), JSON.stringify(labels1));
  map.fire("mouseout", {});
  forceCityDraw();
  assert("mouseout 后悬浮标签消失", !(window.__cityDebug.labels || []).includes("南京"),
    JSON.stringify(window.__cityDebug.labels));

  /* ===== Phase 9：回归杂项 ===== */
  assert("zoomSnap 0.25 无极缩放", map.options.zoomSnap === 0.25, String(map.options.zoomSnap));
  assert("走廊折线存在(贴铁路)", Object.keys(lineRenderer._layers).length >= 9,
    `lineRenderer layers=${Object.keys(lineRenderer._layers).length}`);
  assert("经停走向线存在", Object.keys(pathLayer._layers).length > 0,
    `pathLayer layers=${Object.keys(pathLayer._layers).length}`);

  /* ===== Phase 10：面板无估算卡（点目的地触发真实查询渲染） ===== */
  onCityClick(CITY_MAP["南京"]);
  await sleep(800);
  const ptxt = document.getElementById("pBody").textContent;
  assert("面板无估算卡(.est-grid)", !document.querySelector(".est-grid"), "");
  assert("面板无'路线估算'文案", !ptxt.includes("路线估算") && !ptxt.includes("铁路里程 km（估算）"),
    ptxt.slice(0, 80));

  R.counts.assert_total = R.asserts.length;
  R.counts.assert_failed = R.errors.length;
  R.pass = R.errors.length === 0;
  return R;
};
