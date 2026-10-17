/* KVMem monitor. Same-origin, no third-party runtime or persisted request content. */
"use strict";
(() => {
  const $ = (s) => document.querySelector(s),
    $$ = (s) => [...document.querySelectorAll(s)];
  const i18n = window.monitorI18n, t = i18n.t;
  const app = $("#app"),
    GiB = 1024 ** 3,
    MiB = 1024 ** 2;
  const number = (n) => typeof n === "number" && Number.isFinite(n);
  const fmt = (n, digits = 0) =>
    number(n)
      ? n.toLocaleString(i18n.language, {
          maximumFractionDigits: digits,
          minimumFractionDigits: digits,
        })
      : "—";
  const ratio = (a, b) =>
    number(a) && number(b) && b > 0 ? (100 * a) / b : null;
  const text = (key, value) => {
    const el = $(`[data-value="${key}"]`);
    if (el) el.textContent = value;
  };
  const storage = (area, key, value) => {
    try {
      if (value === undefined) return area.getItem(key);
      if (value === null) area.removeItem(key);
      else area.setItem(key, value);
    } catch {}
    return null;
  };
  const statuses = new Proxy({
    idle: "空闲",
    queued: "排队",
    reading: "预填充",
    generating: "生成中",
    processing: "处理中",
    completed: "已完成",
    disconnected: "已断开",
    error: "失败",
  }, { get: (map, name) => t(map[name] || "处理中") });
  let theme = storage(localStorage, "kvmem-monitor-theme") || "dark";
  let key = storage(sessionStorage, "kvmem-monitor-key") || "";
  let selected = "",
    followLatest = true,
    detail = "",
    all = false,
    state = null,
    props = null,
    slots = [],
    stats = null;
  let timer,
    controller,
    busy = false,
    failures = 0,
    authNeeded = false,
    lastGood = 0,
    stopped = false;
  let lastError = null, restartNotice = false, consoleRuntime = null;
  let series = {},
    previousDisk = null,
    speedRequest = null,
    currentInstance = null;
  const media = matchMedia("(prefers-color-scheme:dark)");
  function applyTheme() {
    if (!["dark", "blue", "system"].includes(theme)) theme = "dark";
    app.dataset.theme =
      theme === "system" ? (media.matches ? "dark" : "blue") : theme;
    $("[data-theme-select]").value = theme;
  }
  applyTheme();
  let density = storage(localStorage, "kvmem-monitor-density") === "compact" ? "compact" : "comfortable";
  function applyDensity() {
    app.dataset.density = density;
    $("[data-density-select]").value = density;
  }
  applyDensity();
  $("[data-density-select]").onchange = (e) => {
    density = e.target.value === "compact" ? "compact" : "comfortable";
    storage(localStorage, "kvmem-monitor-density", density);
    applyDensity();
  };
  media.addEventListener("change", applyTheme);
  $("[data-theme-select]").onchange = (e) => {
    theme = e.target.value;
    storage(localStorage, "kvmem-monitor-theme", theme);
    applyTheme();
  };
  i18n.apply();
  $("[data-language-select]").onchange = (e) => {
    i18n.apply(e.target.value);
    i18n.save();
    $("[data-all]").textContent = all ? t("收起") : t("显示全部");
    render(false);
    renderConnection();
    window.dispatchEvent(new Event("kvmem-language-change"));
  };
  function page(name) {
    $$("[data-view]").forEach((el) => (el.hidden = el.dataset.view !== name));
    $$("[data-page]").forEach((el) =>
      el.setAttribute("aria-pressed", String(el.dataset.page === name)),
    );
    window.dispatchEvent(new CustomEvent("kvmem-page-change", {detail: name}));
  }
  $$("[data-page]").forEach((el) => (el.onclick = () => page(el.dataset.page)));
  function bar(name, value) {
    $(`[data-bar="${name}"]`).style.width =
      `${number(value) ? Math.max(0, Math.min(100, value)) : 0}%`;
  }
  function capacity(name, used, total) {
    // The storage API uses UINT64_MAX for an unbounded host budget. JSON rounds it to 2^64.
    const unbounded = number(total) && total >= 2 ** 64;
    const amount =
      number(used) && used > 0 && used / GiB < 0.1
        ? "<0.1"
        : fmt(number(used) ? used / GiB : null, 1);
    text(
      name,
      number(used)
        ? unbounded
          ? t("{0} GiB / 未设上限", amount)
          : `${amount} / ${fmt(number(total) ? total / GiB : null, 1)} GiB`
        : t("未采集"),
    );
    bar(name, unbounded ? null : ratio(used, total));
  }
  function options(el, items, value) {
    const signature = JSON.stringify(items);
    if (el.dataset.options !== signature) {
      el.replaceChildren(
        ...items.map(([id, label]) => {
          const o = document.createElement("option");
          o.value = id;
          o.textContent = label;
          return o;
        }),
      );
      el.dataset.options = signature;
    }
    el.value = value;
  }
  function records() {
    return state
      ? [...state.active]
          .sort((a, b) => b.started_at_ms - a.started_at_ms)
          .concat(state.recent)
      : [];
  }
  function currentRequest() {
    return (
      (!followLatest && records().find((r) => r.id === selected)) ||
      records()[0] ||
      null
    );
  }
  $("[data-request-select]").onchange = (e) => {
    selected = e.target.value;
    followLatest = !selected;
    series.speed = [];
    render(false);
  };
  $("[data-hardware]").onclick = () => page("about");
  $("[data-all]").onclick = () => {
    all = !all;
    $("[data-all]").textContent = all ? t("收起") : t("显示全部");
    $("[data-all]").setAttribute("aria-expanded", String(all));
    renderTable();
  };
  $("[data-close]").onclick = () => {
    detail = "";
    $("[data-detail]").hidden = true;
  };
  function renderTable() {
    const rows = records();
    const body = $("[data-requests]");
    body.replaceChildren();
    for (const row of all ? rows : rows.slice(0, 5)) {
      const tr = document.createElement("tr"),
        time = document.createElement("button");
      time.textContent = new Date(row.started_at_ms).toLocaleTimeString(
        i18n.language,
        { hour12: false },
      );
      time.onclick = () => {
        detail = row.id;
        renderDetail();
      };
      let td = document.createElement("td");
      td.append(time);
      tr.append(td);
      td = document.createElement("td");
      const badge = document.createElement("span");
      badge.className = "s-badge";
      badge.dataset.kind = row.status;
      badge.textContent = statuses[row.status] || t("处理中");
      td.append(badge);
      tr.append(td);
      const values = [
        fmt(row.prompt_tokens),
        fmt(row.generated_tokens),
        fmt(row.decode_tps, 1),
        `${fmt(row.elapsed_ms / 1000, 1)}s`,
      ];
      for (const value of values) {
        td = document.createElement("td");
        td.className = "s-num";
        td.textContent = value;
        tr.append(td);
      }
      body.append(tr);
    }
    if (!rows.length) {
      const tr = document.createElement("tr"),
        td = document.createElement("td");
      td.colSpan = 6;
      td.className = "s-empty";
      td.textContent = t("暂无请求，发送一条消息后会显示统计。");
      tr.append(td);
      body.append(tr);
    }
    renderDetail();
  }
  function renderDetail() {
    const row = records().find((r) => r.id === detail);
    $("[data-detail]").hidden = !row;
    if (!row) return;
    $("[data-detail-title]").textContent =
      t("请求 #{0} · {1}", row.id, statuses[row.status] || t("处理中"));
    $("[data-detail-body]").textContent =
      t("入口：{0}\n输入 {1} tokens · 复用 {2} · 输出 {3}\n预填充 {4} tok/s · 输入复用率 {5}%\n输出上限 {6} · 耗时 {7} 秒", row.endpoint, fmt(row.prompt_tokens), fmt(row.cached_tokens), fmt(row.generated_tokens), fmt(row.prefill_tps, 1), fmt(ratio(row.cached_tokens, row.prompt_tokens), 1), fmt(row.limit), fmt(row.elapsed_ms / 1000, 2));
  }
  function gauge(element, percent, label) {
    let arc = element.querySelector("[data-dial-arc]");
    if (!arc) {
      const ns = "http://www.w3.org/2000/svg";
      const svg = document.createElementNS(ns, "svg");
      svg.setAttribute("viewBox", "0 0 120 120");
      svg.setAttribute("aria-hidden", "true");
      for (const name of ["s-dial-track", "s-dial-arc"]) {
        const circle = document.createElementNS(ns, "circle");
        for (const [key, value] of Object.entries({cx: 60, cy: 60, r: 50, pathLength: 100, transform: "rotate(135 60 60)", class: name}))
          circle.setAttribute(key, value);
        circle.setAttribute("stroke-dasharray", "75 100");
        if (name === "s-dial-arc") { circle.dataset.dialArc = ""; arc = circle; }
        svg.append(circle);
      }
      element.prepend(svg);
    }
    const available = number(percent);
    const amount = available ? Math.max(0, Math.min(100, percent)) : 0;
    arc.setAttribute("stroke-dasharray", `${amount * 0.75} 100`);
    // A round line cap would otherwise turn zero or missing data into a colored dot.
    arc.style.visibility = amount > 0 ? "visible" : "hidden";
    element.setAttribute("role", "img");
    element.setAttribute("aria-label", `${label} · ${available ? t("占比 {0}%", fmt(percent, 1)) : t("比例未采集")}`);
  }
  function chart(name, value, append, target = null, ceiling = null) {
    const points = (series[name] ||= []);
    if (append) {
      points.push(number(value) ? value : null);
      if (points.length > 60) points.shift();
    }
    const svg = target || $(`[data-series="${name}"]`);
    if (!svg) return;
    svg.replaceChildren();
    const valid = points.filter(number);
    if (valid.length < 2) {
      svg.setAttribute("aria-label", t("等待更多真实采样"));
      return;
    }
    const max = ceiling || Math.max(1, ...valid) * 1.1;
    let path = "",
      drawing = false;
    points.forEach((p, i) => {
      if (!number(p)) {
        drawing = false;
        return;
      }
      path += `${drawing ? "L" : "M"}${((i * 240) / 59).toFixed(1)},${(30 - (p / max) * 27).toFixed(1)} `;
      drawing = true;
    });
    const line = document.createElementNS("http://www.w3.org/2000/svg", "path");
    line.setAttribute("d", path);
    line.setAttribute("fill", "none");
    line.setAttribute("stroke", "currentColor");
    line.setAttribute("stroke-width", "1.8");
    line.setAttribute("vector-effect", "non-scaling-stroke");
    svg.append(line);
    svg.setAttribute(
      "aria-label",
      t("最近 {0} 次真实采样，当前 {1}", points.length, fmt(value, 1)),
    );
  }
  const pciKey = (value) => typeof value === "string" && /^[0-9a-f]+:[0-9a-f]+:[0-9a-f]+\.[0-7]$/i.test(value)
    ? value.toLowerCase().split(/[:.]/).map((n) => parseInt(n, 16)).join(":") : null;
  function matchesDevice(device, gpu) {
    if (device.uuid) return device.uuid === gpu.uuid;
    return pciKey(device.pci_bus_id) !== null && pciKey(device.pci_bus_id) === pciKey(gpu.pci_bus_id);
  }
  function renderGpus(gpus, fresh, append) {
    const devices = props.execution?.monitor_devices;
    const known = Array.isArray(devices);
    const bound = known ? devices : [];
    const mode = props.execution?.mode;
    const modeLabel = mode === "layer" ? t("层分片") : mode === "tensor" ? t("张量并行") : t("多卡推理");
    text("execution", !known ? t("推理设备待确认") : !bound.length ? t("CPU 推理") : bound.length === 1 ? t("单卡推理") : t("{0} 卡协同 · {1}", bound.length, modeLabel));
    text("gpu-count", known ? t("{0} 张设备参与当前服务", bound.length) : t("设备绑定信息未提供"));
    $("[data-device-note]").textContent = !known ? t("当前服务尚未提供设备绑定信息；不根据负载猜测推理显卡。") : !bound.length ? t("当前模型未使用显卡。") : t("只显示当前服务绑定的显卡 · 整卡读数包含其他进程");
    const grid = $("[data-gpus]");
    grid.dataset.count = String(bound.length);
    const signature = JSON.stringify([i18n.language, bound]);
    if (grid.dataset.devices !== signature) {
      grid.replaceChildren(...bound.map((d) => {
        const card = $("#gpu-card").content.firstElementChild.cloneNode(true);
        card.dataset.device = d.uuid || d.pci_bus_id || d.name;
        return card;
      }));
      grid.dataset.devices = signature;
    }
    bound.forEach((device, index) => {
      const matched = gpus.find((g) => matchesDevice(device, g));
      const g = fresh ? matched || {} : {};
      const card = grid.children[index];
      const values = {
        name: (device.name || matched?.name || t("推理设备")).replace(/^NVIDIA GeForce /, ""),
        id: device.uuid ? `UUID · ${device.uuid.slice(-8)}` : matched?.uuid ? `UUID · ${matched.uuid.slice(-8)}` : device.pci_bus_id ? `PCI · ${device.pci_bus_id}` : t("设备身份未采集"),
        status: !matched ? t("读数未采集") : !fresh ? t("读数已过期") : t("本服务"),
        load: fmt(g.utilization_percent),
        vram: fmt(number(g.used_bytes) ? g.used_bytes / GiB : null, 1),
        total: number(g.total_bytes) ? t("共 {0} GiB", fmt(g.total_bytes / GiB, 1)) : t("容量未采集"),
        capacity: number(ratio(g.used_bytes, g.total_bytes)) ? t("已用 {0}% · 含其他进程", fmt(ratio(g.used_bytes, g.total_bytes))) : t("比例未采集"),
        temp: fmt(g.temperature_c), power: fmt(g.power_w, 1),
        pcie: number(g.pcie_generation) ? `Gen${g.pcie_generation} ×${fmt(g.pcie_width)}` : "—",
        rx: fmt(number(g.pcie_rx_bytes_per_second) ? g.pcie_rx_bytes_per_second / MiB : null, 1),
      };
      for (const [key, value] of Object.entries(values)) card.querySelector(`[data-gpu="${key}"]`).textContent = value;
      const usage = ratio(g.used_bytes, g.total_bytes);
      gauge(card.querySelector('[data-dial="load"]'), g.utilization_percent, t("GPU 负载 {0}%", fmt(g.utilization_percent)));
      gauge(card.querySelector('[data-dial="vram"]'), usage, t("整卡显存 {0} / {1} GiB", values.vram, fmt(number(g.total_bytes) ? g.total_bytes / GiB : null, 1)));
      chart(`gpu:${card.dataset.device}`, g.utilization_percent, append, card.querySelector("[data-gpu-chart]"), 100);
    });
    const hardwareList = $('[data-about="devices"]');
    hardwareList.replaceChildren(...gpus.map((g) => {
      const item = document.createElement("p");
      const role = !known ? t("绑定待确认") : bound.some((d) => matchesDevice(d, g)) ? t("参与当前服务") : t("未参与当前服务");
      item.textContent = `${g.name} · ${role}\n${g.uuid}\n${fresh ? t("{0}% 负载 · {1} / {2} GiB", fmt(g.utilization_percent), fmt(number(g.used_bytes) ? g.used_bytes / GiB : null, 1), fmt(number(g.total_bytes) ? g.total_bytes / GiB : null, 1)) : t("读数已过期")}`;
      return item;
    }));
    if (!gpus.length) hardwareList.textContent = t("硬件读数未采集");
  }
  const sum = (list, field) =>
    list.length && list.every((x) => number(x?.[field]))
      ? list.reduce((n, x) => n + x[field], 0)
      : null;
  function cacheData() {
    if (state.backend === "ninfer")
      return stats?.kvmem || props.kvmem?.usage || {};
    const storage = slots.map((s) => s.kvmem?.storage).filter(Boolean),
      conversations = slots.map((s) => s.kvmem?.conversations).filter(Boolean);
    const c = conversations.some((c) => number(c.resident))
      ? conversations.slice(0, 1)
      : conversations;
    const data = {
      retained_sessions: c.reduce((n, item) => n + (number(item.retained_sessions) ? item.retained_sessions : item.count || 0), 0),
      history_hits: sum(c, "extends"),
      history_evictions: sum(c, "evictions"),
    };
    for (const name of [
      "host_payload_bytes",
      "host_payload_budget_bytes",
      "disk_payload_bytes",
      "disk_payload_budget_bytes",
      "disk_read_bytes",
      "disk_written_bytes",
    ])
      data[name] = sum(storage, name);
    return data;
  }
  function render(append) {
    if (!state || !props) return;
    const r = currentRequest(),
      allRecords = records();
    if (speedRequest !== r?.id) {
      series.speed = [];
      speedRequest = r?.id;
    }
    if (!followLatest && !allRecords.some((x) => x.id === selected)) {
      followLatest = true;
      selected = "";
    }
    options(
      $("[data-request-select]"),
      [
        ["", t("自动跟随最新请求")],
        ...allRecords.map((x) => [
          x.id,
          `#${x.id} · ${statuses[x.status] || t("处理中")}`,
        ]),
      ],
      followLatest ? "" : selected,
    );
    const model = props.model_name || props.model_alias || t("本地模型");
    text(
      "model",
      `${model} · ${state.backend === "ninfer" ? "NInfer" : "llama.cpp"}`,
    );
    const activeStates = new Set(state.active.map((r) => r.status));
    const serviceStatus = !state.active.length ? "idle" :
      ["generating", "reading", "processing", "queued"].find((s) => activeStates.has(s)) || "processing";
    const serviceBadge = $("[data-service-state]");
    serviceBadge.dataset.kind = serviceStatus;
    serviceBadge.textContent = statuses[serviceStatus] + (state.active.length > 1 ? t(" · {0} 个请求", state.active.length) : "");
    const liveRequest = state.active.some((x) => x.id === r?.id);
    const requestScope = !r ? t("暂无请求") : liveRequest ? t("当前请求") : followLatest ? t("上次请求") : t("历史请求");
    text("request-scope", r ? `${requestScope} #${r.id} · ${statuses[r.status] || t("处理中")}` : requestScope);
    $$("[data-request-scope]").forEach((e) => e.textContent = requestScope);
    text(
      "output",
      r
        ? t("已生成 {0} tokens · 输出预算 {1}", fmt(r.generated_tokens), number(r.limit) && r.limit > 0 ? fmt(r.limit) : t("未设上限"))
        : t("等待请求"),
    );
    bar("output", ratio(r?.generated_tokens, r?.limit));
    const meter = $('[role="meter"]');
    if (number(ratio(r?.generated_tokens, r?.limit)))
      meter.setAttribute(
        "aria-valuenow",
        String(Math.min(100, ratio(r.generated_tokens, r.limit))),
      );
    else meter.removeAttribute("aria-valuenow");
    text("decode", fmt(r?.decode_tps, 1));
    text("prefill", fmt(r?.prefill_tps, 0));
    const hardware = state.hardware || {},
      gpus = hardware.gpus || [];
    const hardwareFresh =
      number(hardware.sampled_at_ms) &&
      state.sampled_at_ms - hardware.sampled_at_ms < 5000;
    renderGpus(gpus, hardwareFresh, append);
    text("cpu", fmt(hardwareFresh ? hardware.cpu_percent : null));
    const context = props.default_generation_settings?.n_ctx;
    const used =
      number(r?.prompt_tokens) && number(r?.generated_tokens)
        ? r.prompt_tokens + r.generated_tokens
        : null;
    const percent = ratio(used, context);
    text("context-percent", number(percent) ? `${fmt(percent, percent > 0 && percent < 1 ? 1 : 0)}%` : "—");
    text("context", `${fmt(used)} / ${fmt(context)}`);
    gauge($('[data-dial="context"]'), percent, t("逻辑上下文 {0} / {1} tokens，非 GPU 驻留 KV", fmt(used), fmt(context)));
    const reuse = ratio(r?.cached_tokens, r?.prompt_tokens);
    text("reuse", number(reuse) ? `${fmt(reuse)}%` : t("未采集"));
    text("reused-tokens", `${fmt(r?.cached_tokens)} / ${fmt(r?.prompt_tokens)} tokens`);
    bar("reuse", reuse);
    capacity(
      "ram",
      hardwareFresh ? hardware.ram_used_bytes : null,
      hardware.ram_total_bytes,
    );
    const cache = cacheData();
    capacity("host", cache.host_payload_bytes, cache.host_payload_budget_bytes);
    capacity("ssd", cache.disk_payload_bytes, cache.disk_payload_budget_bytes);
    text(
      "sessions",
      number(cache.retained_sessions) ? fmt(cache.retained_sessions) : t("未采集"),
    );
    text(
      "history",
      `${fmt(cache.history_hits)} / ${fmt(cache.history_evictions)}`,
    );
    let disk = null;
    if (
      append &&
      previousDisk &&
      state.sampled_at_ms > previousDisk.time &&
      number(cache.disk_read_bytes) &&
      cache.disk_read_bytes >= previousDisk.bytes
    )
      disk =
        ((cache.disk_read_bytes - previousDisk.bytes) * 1000) /
        (state.sampled_at_ms - previousDisk.time) /
        MiB;
    if (append)
      previousDisk = number(cache.disk_read_bytes)
        ? { time: state.sampled_at_ms, bytes: cache.disk_read_bytes }
        : null;
    if (append) text("disk", fmt(disk, 1));
    const values = {
      speed: liveRequest ? r?.decode_tps : null,
    };
    for (const [name, value] of Object.entries(values))
      chart(name, value, append);
    for (const [name, value] of Object.entries({
      model,
      backend: state.backend,
      context: `${fmt(context)} tokens`,
      origin: location.origin,
    }))
      $(`[data-about="${name}"]`).textContent = value;
    renderTable();
  }
  function message(value, warning = true) {
    $("[data-banner]").hidden = !value;
    $("[data-banner-text]").textContent = value;
    $("[data-connection]").dataset.offline = String(warning);
  }
  function renderConnection() {
    if (consoleRuntime && consoleRuntime.state !== "ready") {
      const labels = {stopped: "推理未启动", starting: "模型加载中", exited: "推理进程已退出"};
      $("[data-connection]").textContent = t(labels[consoleRuntime.state] || "正在连接");
      message(consoleRuntime.state === "exited" ? t("推理进程已退出，请在启动配置中查看日志。") : "", consoleRuntime.state === "exited");
      return;
    }
    if (lastError) {
      $("[data-connection]").textContent = authNeeded ? t("需要密钥") : t("连接中断");
      message(authNeeded ? t("请输入有效的 API Key。")
        : lastError.status === 404 ? t("当前服务未提供监控接口，请使用包含监控功能的新版本。")
        : lastGood ? t("暂时无法更新，显示的是上次数据（已过期）。将自动重试。")
        : t("暂时无法更新。将自动重试。"));
    } else if (state) {
      message(restartNotice ? t("服务已重启，历史与曲线已重新开始。") : "", false);
      $("[data-connection]").textContent = state.active.length
        ? t("{0} 个请求处理中", state.active.length) : t("服务已连接");
    } else {
      $("[data-connection]").textContent = t("正在连接");
    }
    if (state) $("[data-footer]").textContent =
      t("更新于 {0} · {1}{2}", new Date(state.sampled_at_ms).toLocaleTimeString(i18n.language, { hour12: false }), document.hidden ? t("后台 5 秒刷新") : t("每秒刷新"), state.dropped_records ? t(" · {0} 条记录超出容量", state.dropped_records) : "");
  }
  async function get(path, signal) {
    const response = await fetch(new URL(path, location.href), {
      signal,
      cache: "no-store",
      headers: key ? { Authorization: `Bearer ${key}` } : {},
    });
    if (!response.ok) {
      const e = new Error(`HTTP ${response.status}`);
      e.status = response.status;
      throw e;
    }
    return response.json();
  }
  function schedule(delay) {
    clearTimeout(timer);
    if (!stopped && !authNeeded) timer = setTimeout(poll, delay);
  }
  window.addEventListener("kvmem-runtime-status", (event) => {
    const old = consoleRuntime?.state;
    consoleRuntime = event.detail;
    renderConnection();
    if (old !== consoleRuntime.state && consoleRuntime.state === "ready" && !busy) schedule(0);
  });
  async function poll() {
    if (busy || stopped) return;
    busy = true;
    controller = new AbortController();
    const abort = setTimeout(() => controller.abort(), 8000);
    try {
      const [snapshot, configuration, lanes] = await Promise.all([
        get("../api/monitor/snapshot", controller.signal),
        get("../props", controller.signal),
        get("../slots", controller.signal),
      ]);
      if (
        snapshot.schema_version !== 1 ||
        !Array.isArray(snapshot.active) ||
        !Array.isArray(snapshot.recent) ||
        !Array.isArray(lanes)
      )
        throw new Error(t("不兼容的监控数据"));
      const runtime =
        snapshot.backend === "ninfer"
          ? await get("../stats", controller.signal)
          : null;
      const restarted =
        currentInstance !== null &&
        currentInstance !== snapshot.server_instance;
      if (restarted) {
        series = {};
        previousDisk = null;
        selected = "";
        followLatest = true;
        detail = "";
      }
      currentInstance = snapshot.server_instance;
      state = snapshot;
      props = configuration;
      slots = lanes;
      stats = runtime;
      failures = 0;
      lastGood = Date.now();
      authNeeded = false;
      $("[data-auth]").hidden = true;
      $$("[data-stale]").forEach((e) => delete e.dataset.stale);
      lastError = null;
      restartNotice = restarted;
      renderConnection();
      render(true);
    } catch (error) {
      controller.abort();
      failures++;
      authNeeded = error.status === 401 || error.status === 403;
      $("[data-auth]").hidden = !authNeeded;
      if (authNeeded && !document.querySelector('[data-view="config"]:not([hidden])')) page("monitor");
      lastError = error;
      renderConnection();
      $$(".s-state,.s-metrics,.s-gpus,.s-bottom,.s-cache")
        .filter((e) => e.closest('[data-view="monitor"]'))
        .forEach((e) => (e.dataset.stale = "true"));
      previousDisk = null;
      for (const values of Object.values(series)) {
        values.push(null);
        if (values.length > 60) values.shift();
      }
    } finally {
      clearTimeout(abort);
      busy = false;
      schedule(
        failures
          ? Math.min(30000, 1000 * 2 ** Math.min(failures, 5))
          : document.hidden
            ? 5000
            : 1000,
      );
    }
  }
  $("[data-retry]").onclick = () => {
    authNeeded = false;
    schedule(0);
  };
  $("[data-auth]").onsubmit = (e) => {
    e.preventDefault();
    key = $("#api-key").value.trim();
    storage(sessionStorage, "kvmem-monitor-key", key);
    $("#api-key").value = "";
    authNeeded = false;
    schedule(0);
  };
  $("[data-forget]").onclick = () => {
    key = "";
    storage(sessionStorage, "kvmem-monitor-key", null);
    state = null;
    props = null;
    selected = "";
    series = {};
    previousDisk = null;
    page("monitor");
    authNeeded = false;
    schedule(0);
  };
  document.addEventListener("visibilitychange", () => {
    if (!busy) schedule(document.hidden ? 5000 : 0);
  });
  window.addEventListener("pagehide", () => {
    stopped = true;
    clearTimeout(timer);
    controller?.abort();
  });
  window.addEventListener("pageshow", () => {
    if (stopped) {
      stopped = false;
      schedule(0);
    }
  });
  poll();
})();
