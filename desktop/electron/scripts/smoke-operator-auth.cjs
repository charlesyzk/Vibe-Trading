const assert = require("node:assert/strict");
const http = require("node:http");
const { app, BrowserWindow } = require("electron");
const { applyBackendAuthorization } = require("../dist/request-authorization.js");

app.whenReady().then(async () => {
  const server = http.createServer((req, res) => {
    if (req.url === "/") {
      res.writeHead(200, { "Content-Type": "text/html" });
      res.end("<!doctype html><title>authorization smoke</title>");
    } else {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ authorization: req.headers.authorization || null }));
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const window = new BrowserWindow({ show: false, webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true } });
  window.webContents.session.webRequest.onBeforeSendHeaders({ urls: ["http://127.0.0.1/*"] }, (details, callback) => {
    if (new URL(details.url).origin === origin) applyBackendAuthorization(details.requestHeaders, "internal-test-key", details.url);
    callback({ requestHeaders: details.requestHeaders });
  });
  try {
    await window.loadURL(origin);
    const result = await window.webContents.executeJavaScript(`(async () => {
      const headers = { Authorization: 'Bearer vtpo_fixture-123' };
      const paper = await (await fetch('/api/execution/paper/start', { headers })).json();
      const settings = await (await fetch('/api/settings', { headers })).json();
      return { paper, settings };
    })()`);
    assert.equal(result.paper.authorization, "Bearer vtpo_fixture-123");
    assert.equal(result.settings.authorization, "Bearer internal-test-key");
    console.log("Electron webRequest preserved PAPER operator identity and routed settings to internal key");
  } finally {
    window.destroy();
    await new Promise((resolve) => server.close(resolve));
  }
  app.exit(0);
}).catch((error) => { console.error(error); app.exit(1); });
