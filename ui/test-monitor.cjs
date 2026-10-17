// Model-free browser contract checks. PLAYWRIGHT_MODULE and CHROMIUM may point to local installations.
const fs = require("fs"),
  path = require("path"),
  http = require("http"),
  assert = require("assert/strict");
const { chromium } = process.env.MONITOR_FIXTURE_ONLY ? {} : require(process.env.PLAYWRIGHT_MODULE || "playwright");
const directory = path.join(__dirname, "monitor");
let fixtureScenario = {};
let failure = 0,
  requiredKey = false,
  instance = "browser-fixture",
  requestId = "1",
  generated = 12,
  recent = [];
const record = () => ({
  id: requestId,
  endpoint: "/v1/chat/completions",
  started_at_ms: Date.now() - 1500,
  status: "generating",
  prompt_tokens: 100,
  cached_tokens: 80,
  generated_tokens: generated,
  limit: 128,
  prefill_tps: 50,
  decode_tps: 8,
  elapsed_ms: 1500,
});
const server = http.createServer((req, res) => {
  const fixture = process.env.MONITOR_FIXTURE_FILE && fs.existsSync(process.env.MONITOR_FIXTURE_FILE)
    ? JSON.parse(fs.readFileSync(process.env.MONITOR_FIXTURE_FILE, "utf8").replace(/^\uFEFF/, "")) : fixtureScenario;
  if (fixture.failure) { res.writeHead(fixture.failure); return res.end("{}"); }
  const devices = [{uuid:"GPU-test-a", name:"NVIDIA GeForce RTX 5060 Ti"}, ...(fixture.dual ? [{uuid:"GPU-test-b", name:"NVIDIA GeForce RTX 5060 Ti"}] : [])];
  if (
    req.url.startsWith("/api/") ||
    req.url === "/props" ||
    req.url === "/slots"
  ) {
    if ((requiredKey || fixture.requiredKey) && req.headers.authorization !== "Bearer test-key") {
      res.writeHead(401);
      return res.end("{}");
    }
    if (failure) {
      res.writeHead(failure);
      return res.end("{}");
    }
    res.setHeader("Content-Type", "application/json");
    res.setHeader("Cache-Control", "no-store");
    const payload =
      req.url === "/props"
        ? {
            model_name: fixture.model || '<img src=x onerror="window.injected=1">',
            default_generation_settings: { n_ctx: 1024 },
            kvmem: {},
            execution: fixture.unknown ? {} : {mode:fixture.dual ? "layer" : "single", monitor_devices: fixture.cpu ? [] : devices},
          }
        : req.url === "/slots"
          ? [
              {
                kvmem: {
                  storage: {
                    host_payload_bytes: 1024,
                    host_payload_budget_bytes: 2 ** 64,
                    disk_payload_bytes: 0,
                    disk_payload_budget_bytes: 0,
                    disk_read_bytes: 0,
                  },
                  conversations: { count: 1, extends: 1, evictions: 0 },
                },
              },
            ]
          : {
              schema_version: 1,
              server_instance: instance,
              backend: "llamacpp",
              sampled_at_ms: Date.now(),
              active: fixture.idle || fixture.empty ? [] : [record()],
              recent: fixture.empty ? [] : fixture.idle ? [{...record(), status:"completed"}] : recent,
              hardware: {
                sampled_at_ms: Date.now() - (fixture.stale ? 10000 : 0),
                gpus: [
                  {
                    uuid: "GPU-test-a",
                    name: "NVIDIA GeForce RTX 5060 Ti",
                    utilization_percent: fixture.idle ? 0 : 42,
                    used_bytes: 2 * 1024 ** 3,
                    total_bytes: 8 * 1024 ** 3,
                    temperature_c: 45,
                    power_w: null,
                  },
                  {uuid:"GPU-test-b", name:"NVIDIA GeForce RTX 5060 Ti", utilization_percent:67, used_bytes:7*1024**3, total_bytes:16*1024**3, temperature_c:62, power_w:99},
                  {uuid:"GPU-unused", name:"Other GPU", utilization_percent:98, used_bytes:1024**3, total_bytes:8*1024**3}
                ].filter((g) => !(fixture.missing && g.uuid === "GPU-test-b")),
                cpu_percent: 20,
                ram_used_bytes: 8 * 1024 ** 3,
                ram_total_bytes: 32 * 1024 ** 3,
              },
            };
    return res.end(JSON.stringify(payload));
  }
  const file = path.join(
    directory,
    req.url === "/monitor/" ? "index.html" : req.url.replace("/monitor/", ""),
  );
  if (!file.startsWith(directory) || !fs.existsSync(file)) {
    res.writeHead(404);
    return res.end();
  }
  res.setHeader(
    "Content-Type",
    file.endsWith(".js")
      ? "text/javascript"
      : file.endsWith(".css")
        ? "text/css"
        : file.endsWith(".woff2")
          ? "font/woff2"
          : "text/html",
  );
  res.end(fs.readFileSync(file));
});
if (process.env.MONITOR_FIXTURE_ONLY) {
  server.listen(18412, "127.0.0.1", () => console.log("Monitor fixture: http://127.0.0.1:18412/monitor/"));
} else (async () => {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const url = `http://127.0.0.1:${server.address().port}/monitor/`;
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {}),
  });
  try {
    const page = await browser.newPage({
        viewport: { width: 1440, height: 1100 },
      }),
      errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.goto(url);
    await page.waitForFunction(
      () => document.querySelector('[data-gpu="load"]').textContent === "42",
    );
    assert.equal(await page.locator("#app").getAttribute("data-theme"), "dark");
    assert.equal(await page.locator('[data-gpu="power"]').textContent(), "—");
    assert.equal(await page.locator("[data-gpus] article").count(), 1);
    assert.equal(await page.locator("[data-service-state]").textContent(), "生成中");
    fixtureScenario = {dual: true, idle: true, missing: true};
    await page.waitForFunction(() => document.querySelectorAll("[data-gpus] article").length === 2);
    assert.equal(await page.locator("[data-service-state]").textContent(), "空闲");
    assert.match(await page.locator('[data-value="request-scope"]').textContent(), /上次请求/);
    assert.equal(await page.locator('[data-gpu="load"]').first().textContent(), "0");
    assert.equal(await page.locator('[data-gpu="status"]').nth(1).textContent(), "读数未采集");
    fixtureScenario = {unknown: true};
    await page.waitForFunction(() => document.querySelectorAll("[data-gpus] article").length === 0);
    assert.match(await page.locator("[data-device-note]").textContent(), /不根据负载猜测/);
    fixtureScenario = {};
    await page.waitForFunction(() => document.querySelectorAll("[data-gpus] article").length === 1);
    assert.equal(await page.evaluate(() => window.injected), undefined);
    await page.locator("[data-requests] button").click();
    assert.equal(await page.locator("[data-detail]").isVisible(), true);
    await page.locator("[data-close]").click();
    recent = [{ ...record(), status: "completed" }];
    requestId = "new";
    generated = 13;
    await page.waitForFunction(() =>
      document
        .querySelector('[data-value="output"]')
        .textContent.includes("已生成 13 tokens"),
    );
    await page.selectOption("[data-request-select]", "1");
    assert.ok(
      (await page.locator('[data-value="output"]').textContent()).includes(
        "已生成 12 tokens",
      ),
    );
    await page.selectOption("[data-request-select]", "");
    assert.ok(
      (await page.locator('[data-value="output"]').textContent()).includes(
        "已生成 13 tokens",
      ),
    );
    const shot = process.env.MONITOR_SCREENSHOT_DIR;
    // Language changes preserve the request selection, open details and chart samples.
    await page.selectOption("[data-request-select]", "1");
    await page.locator("[data-requests] button").last().click();
    const samples = await page.locator('[data-gpu-chart] path').count();
    await page.selectOption("[data-language-select]", "en");
    assert.equal(await page.locator("html").getAttribute("lang"), "en");
    assert.equal(await page.locator("[data-request-select]").inputValue(), "1");
    assert.equal(await page.locator("[data-detail]").isVisible(), true);
    assert.match(await page.locator('[data-detail-body]').textContent(), /Endpoint:/);
    assert.match(await page.locator('[data-value="output"]').textContent(), /Generated 12 tokens/);
    assert.equal(await page.locator('[data-gpu="status"]').textContent(), "This service");
    if (samples) assert.equal(await page.locator('[data-gpu-chart] path').count(), samples);
    await page.reload();
    assert.equal(await page.locator("[data-language-select]").inputValue(), "en");
    await page.selectOption("[data-language-select]", "zh-CN");
    await page.waitForFunction(() => document.querySelector('[data-service-state]').textContent === '生成中');
    if (shot) {
      fs.mkdirSync(shot, { recursive: true });
      await page.screenshot({
        path: path.join(shot, "monitor-dark.png"),
        fullPage: true,
      });
    }
    await page.selectOption("[data-theme-select]", "blue");
    await page.reload();
    await page.waitForFunction(
      () => document.querySelector('[data-gpu="load"]').textContent === "42",
    );
    assert.equal(await page.locator("#app").getAttribute("data-theme"), "blue");
    if (shot)
      await page.screenshot({
        path: path.join(shot, "monitor-blue.png"),
        fullPage: true,
      });
    // Adding configuration must not restyle the existing monitor or about view.
    // Compare rendered geometry/styles with the configuration stylesheet disabled.
    for (const theme of ['dark', 'blue']) {
      await page.selectOption('[data-theme-select]', theme);
      for (const width of [360, 768, 1366, 1920, 2560]) {
        await page.setViewportSize({width, height: width === 1366 ? 768 : 1080});
        for (const view of ['monitor', 'about']) {
          await page.locator(`[data-page=${view}]`).click();
          const comparison = await page.evaluate(view => {
            const stylesheet = document.querySelector('link[href="./launcher.css"]');
            const elements = [document.querySelector('.s-header'),
              ...document.querySelectorAll(`[data-view=${view}], [data-view=${view}] *`)];
            function signature() {
              return elements.map(el => {
                const style = getComputedStyle(el), rect = el.getBoundingClientRect();
                return [rect.x, rect.y, rect.width, rect.height,
                  ...['display','gridTemplateColumns','flexDirection','gap','padding',
                      'margin','fontSize','fontFamily','borderRadius','backgroundColor']
                    .map(key => style[key])];
              });
            }
            const enabled = signature();
            stylesheet.disabled = true;
            const disabled = signature();
            stylesheet.disabled = false;
            return {enabled, disabled};
          }, view);
          assert.deepEqual(comparison.enabled, comparison.disabled,
            `configuration CSS leaked into ${view}: ${theme} ${width}px`);
        }
      }
    }
    await page.locator('[data-page=monitor]').click();
    for (const width of [360, 768, 1440]) {
      await page.setViewportSize({ width, height: 1000 });
      assert.ok(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        `horizontal overflow ${width}`,
      );
    }
    await page.setViewportSize({width:1366,height:768});
    const comfortableHeight=(await page.locator('[data-view=monitor]').boundingBox()).height;
    await page.locator('[data-density-select]').selectOption('compact');
    assert.ok((await page.locator('[data-view=monitor]').boundingBox()).height < comfortableHeight * .9, 'compact monitor meaningfully reduces height');
    for(const theme of ['dark','blue']) {
      await page.locator('[data-theme-select]').selectOption(theme);
      for(const width of [360,768,1366,1920]) {
        await page.setViewportSize({width,height:768});
        assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'compact monitor overflow '+width);
      }
    }
    await page.reload();await page.locator('[data-gpu=name]').first().waitFor();
    assert.equal(await page.locator('#app').getAttribute('data-density'),'compact');
    await page.locator('[data-density-select]').selectOption('comfortable');
    failure = 503;
    await page.waitForFunction(
      () =>
        document.querySelector("[data-connection]").textContent === "连接中断",
    );
    assert.equal(
      await page.locator(".s-metrics").getAttribute("data-stale"),
      "true",
    );
    failure = 0;
    instance = "restarted";
    requestId = "2";
    generated = 1;
    await page.locator("[data-retry]").click();
    await page.waitForFunction(() =>
      document
        .querySelector('[data-value="output"]')
        .textContent.includes("已生成 1 tokens"),
    );
    assert.ok(
      (await page.locator("[data-banner-text]").textContent()).includes("重启"),
    );
    requiredKey = true;
    await page.reload();
    await page.locator("#api-key").waitFor({ state: "visible" });
    await page.fill("#api-key", "test-key");
    await page.locator("[data-auth] button").click();
    await page.waitForFunction(
      () => document.querySelector('[data-gpu="load"]').textContent === "42",
    );
    assert.equal(
      await page.evaluate(() => localStorage.getItem("kvmem-monitor-key")),
      null,
    );
    assert.equal(new URL(page.url()).search, "");
    await page.locator('[data-page="about"]').click();
    await page.locator("[data-forget]").click();
    await page.locator("#api-key").waitFor({ state: "visible" });
    assert.equal(
      await page.evaluate(() => sessionStorage.getItem("kvmem-monitor-key")),
      null,
    );
    assert.deepEqual(errors, []);
    console.log(
      "PASS: themes, real-value rendering, missing data, layout, details, reconnect, restart, API-key isolation, XSS escaping.",
    );
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => {
  console.error(e);
  server.close();
  process.exitCode = 1;
});
