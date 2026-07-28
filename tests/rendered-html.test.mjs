import assert from "node:assert/strict";
import test from "node:test";

async function render(pathname = "/") {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request(`http://localhost${pathname}`, { headers: { accept: "text/html", host: "localhost" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("renders the menu automation laboratory", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>Apex Controller Lab/);
  assert.match(html, /先把进游戏流程/);
  assert.match(html, /开始自动演练/);
  assert.match(html, /模拟活动弹窗/);
  assert.match(html, /首选英雄被占用/);
  assert.match(html, /模式包含枪械页/);
  assert.match(html, /0\.5× · 慢速演示/);
  assert.match(html, /仅模拟页面/);
  assert.match(html, /Windows 版将替换为窗口截图/);
  assert.doesNotMatch(html, /codex-preview|react-loading-skeleton|Your site is taking shape/);
});

test("keeps the heart signal laboratory on its own route", async () => {
  const response = await render("/heart");
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /用手柄信号/);
  assert.match(html, /画笔模式/);
  assert.match(html, /角色模式/);
  assert.match(html, /浏览器内部模拟/);
  assert.match(html, /菜单流程/);
});
