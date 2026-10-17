"""请求 S 端到端验证：漏洞详情弹层渲染图片 POC + 表单上传图片 POC。

桩 HTTP 服务（SimpleHTTPRequestHandler(directory=webui/dist) + /api/* 罐头 JSON），
index.html 注入自动点击脚本，回传 #e2e-diag；msedge --headless=new 两趟
（--dump-dom 读诊断 + --screenshot 绝对路径落盘）。
"""
import base64
import io
import json
import re
import sys
import threading
import urllib.parse
from http.server import HTTPServer, SimpleHTTPRequestHandler

import os

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "..", "webui", "dist")
DIST = os.path.abspath(DIST)

# 1x1 红色 PNG（artifacts/{id}/download 返回）
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

FINDING_IMG = {
    "id": "f-img", "project_id": "p1", "vuln_class": "未授权访问",
    "title": "e2e-image-poc", "severity": "high", "rating_basis": "",
    "impact": "", "remediation": "",
    "summary": "带截图 POC 的漏洞", "affected_assets": "https://t/",
    "test_environment": "prod", "reproduction_steps": "1. 打开页面",
    "verification_result": "见截图", "risk_assessment": "越权读取",
    "pocs": [{"type": "image", "artifact_id": "art-demo-1"}],
    "status": "unverified", "category": "vuln", "evidence": {},
    "target_asset_id": None, "poc_artifact_id": None, "author": "human",
    "created_at": "2026-10-16T10:00:00Z",
}
FINDING_HTTP = {
    "id": "f-http", "project_id": "p1", "vuln_class": "SQL 注入",
    "title": "e2e-http-poc", "severity": "critical", "rating_basis": "",
    "impact": "", "remediation": "",
    "summary": "带 HTTP POC 的漏洞（回归）", "affected_assets": "https://t/",
    "test_environment": "prod", "reproduction_steps": "1. 发包",
    "verification_result": "回显注入点", "risk_assessment": "拖库",
    "pocs": [{"type": "http", "code": "GET /x?id=1' HTTP/1.1\nHost: t"}],
    "status": "unverified", "category": "vuln",
    "evidence": {"repro_steps": [{"desc": "发包观察回显", "type": "http",
                                   "code": "GET /x?id=1' HTTP/1.1\nHost: t",
                                   "expected": "响应含 SQL 报错"}]},
    "target_asset_id": None, "poc_artifact_id": None, "author": "human",
    "created_at": "2026-10-16T09:00:00Z",
}

INJECT = r"""
<script>
window.addEventListener('load', function () {
  var log = [];
  window.__e2eLog = log;
  window.addEventListener('error', function (e) { log.push('JSERR:' + e.message); });
  var wait = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  function innermost(pred) {
    // 弹层走 portal 挂在 body 下（不在 #root 内），查找范围必须覆盖 body；
    // 注入脚本在 <head>，body 内无 script/style 文本干扰
    var scope = document.body;
    var all = [].filter.call(scope.querySelectorAll('*'), pred);
    return all.filter(function (el) {
      return ![].some.call(el.querySelectorAll('*'), pred);
    })[0] || null;
  }
  function clickInnermost(pred, label) {
    var el = innermost(pred);
    if (!el) { log.push('MISS:' + label); return null; }
    el.click(); log.push('click:' + label + '@' + el.tagName + '.' + (el.className || '').slice(0, 40)); return el;
  }
  async function run() {
    await wait(2500);
    // 0. 页面概况
    log.push('body-len:' + document.body.textContent.length);
    log.push('nav-labels:' + JSON.stringify([].map.call(document.querySelectorAll('.nav-item, button'), function (b) { return (b.textContent || '').trim().slice(0, 8); }).slice(0, 30)));
    // 1. 打开项目卡
    clickInnermost(function (el) {
      return (el.textContent || '').indexOf('e2e-pentest') >= 0;
    }, 'project');
    await wait(2500);
    // 2. 左导航「黑板」
    clickInnermost(function (el) {
      return (el.textContent || '').trim() === '黑板' && el.tagName !== 'HTML' && el.tagName !== 'BODY';
    }, 'board');
    await wait(2500);
    // 3. 图片 POC 发现行
    clickInnermost(function (el) {
      return (el.textContent || '').indexOf('e2e-image-poc') >= 0;
    }, 'finding-img');
    await wait(800);
    var dlg = document.querySelector('[role="dialog"]');
    if (dlg) {
      var imgs = dlg.querySelectorAll('img');
      var srcs = [].map.call(imgs, function (i) { return i.getAttribute('src'); });
      log.push('readonly-imgs:' + JSON.stringify(srcs));
      log.push('readonly-hasPocArtifact:' + srcs.some(function (s) { return s && s.indexOf('art-demo-1') >= 0; }));
    } else { log.push('readonly-dialog:MISS'); }
    if (location.hash === '#readonly') { finish(); return; }
    // 4. 进入编辑态
    clickInnermost(function (el) {
      return (el.tagName === 'BUTTON' || el.getAttribute('role') === 'button') &&
             (el.textContent || '').indexOf('编辑') >= 0;
    }, 'edit');
    await wait(800);
    var sel = [].filter.call(document.querySelectorAll('select'), function (s) {
      return s.value === 'image';
    })[0];
    log.push('form-image-select:' + (sel ? 'OK' : 'MISS'));
    var formImg = document.querySelector('img[src*="art-demo-1"]');
    log.push('form-thumb:' + (formImg ? 'OK' : 'MISS'));
    // 5. 移除图片 → 走隐藏 file input 上传新截图
    clickInnermost(function (el) {
      return el.tagName === 'BUTTON' && (el.textContent || '').trim() === '移除图片';
    }, 'remove-img');
    await wait(400);
    var input = document.querySelector('input[type=file]');
    if (!input) { log.push('file-input:MISS'); return finish(); }
    var dt = new DataTransfer();
    var png = Uint8Array.from(atob(
      'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='),
      function (c) { return c.charCodeAt(0); });
    dt.items.add(new File([png], 'shot.png', { type: 'image/png' }));
    input.files = dt.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
    log.push('upload:dispatched');
    await wait(2000);
    var newImg = document.querySelector('img[src*="art-new-1"]');
    log.push('form-new-thumb:' + (newImg ? 'OK' : 'MISS'));
    if (newImg) newImg.scrollIntoView({ block: 'center' });
    finish();
  }
  function finish() {
    var d = document.createElement('div');
    d.id = 'e2e-diag';
    d.textContent = log.join(' | ');
    document.body.appendChild(d);
    log.push('done');
  }
  run();
});
</script>
"""


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=DIST, **kw)

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p == "/api" or p.startswith("/api/"):
            self._api_get(p)
            return
        if p == "/":
            with open(os.path.join(DIST, "index.html"), "rb") as fh:
                html = fh.read().decode()
            html = html.replace("</head>", INJECT + "</head>", 1)
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def _api_get(self, p):
        if p == "/api/projects":
            self._json([{"id": "p1", "name": "e2e-pentest", "track": "pentest",
                         "directions": ["pentest"], "created_at": "2026-10-16T08:00:00Z"}])
            return
        if p == "/api/projects/trash":
            self._json([])
            return
        if p == "/api/taxonomy":
            self._json({})
            return
        if p == "/api/experts":
            self._json([])
            return
        if p == "/api/intel/overview":
            self._json({"feeds": [], "total": 0, "unread": 0})
            return
        if p == "/api/projects/p1":
            self._json({
                "id": "p1", "name": "e2e-pentest", "track": "pentest",
                "directions": ["pentest"], "created_at": "2026-10-16T08:00:00Z",
                "config": {}, "task_stats": {}, "findings": 2, "assets": 0,
                "assets_clean": 0, "assets_clean_pct": 0,
                "usage": {}, "capability": {"docker": {"available": False, "detail": ""},
                                            "wsl": {"available": False, "detail": ""}, "tools": []},
            })
            return
        if p == "/api/projects/p1/findings":
            self._json([FINDING_IMG, FINDING_HTTP])
            return
        if p == "/api/projects/p1/assets":
            self._json([])
            return
        if p == "/api/projects/p1/artifacts/art-demo-1/download":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG)))
            self.end_headers()
            self.wfile.write(PNG)
            return
        self._json({}, 404)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if p == "/api/projects/p1/attachments":
            self._json({"id": "art-new-1", "path": "artifacts/attachments/abc_shot.png",
                        "name": "shot.png", "size": 70, "sha256": "abc"}, 201)
            return
        self._json({}, 404)


def main():
    srv = HTTPServer(("127.0.0.1", 8765), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    print("READY", flush=True)
    try:
        while True:
            t.join(1)
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
