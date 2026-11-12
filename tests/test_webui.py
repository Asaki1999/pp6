"""HTTP 接口：桌面 App 与网页版共用同一套请求。

前端全部走这些接口，所以这组用例是「界面点了有反应」的底线。
服务在模块内惰性启动一次，用例之间共享（省 20 次冷启动）。
"""
from __future__ import annotations

import atexit
import json
import threading
import urllib.error
import urllib.parse
import urllib.request

from pp6kit import spec as S
from tests._harness import case, field, native_zip, simple_package, xlsx

_SERVER: dict = {}


def server() -> str:
    """起一个只监听本机的服务，返回 base url。"""
    if "url" in _SERVER:
        return _SERVER["url"]
    from pp6kit import webui

    httpd, url = webui.start_background(8912)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _SERVER["url"] = url.rstrip("/")
    _SERVER["httpd"] = httpd

    @atexit.register
    def _stop():
        try:
            httpd.shutdown()
        except Exception:
            pass
    return _SERVER["url"]


def req(path: str, data: bytes | None = None, headers: dict | None = None,
        method: str | None = None):
    """返回 (状态码, 响应头(小写 key), 响应体)。"""
    url = server() + path
    r = urllib.request.Request(url, data=data, headers=headers or {},
                               method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def jpost(path: str, payload: dict):
    return req(path, json.dumps(payload).encode("utf-8"),
               {"Content-Type": "application/json"})


def package() -> bytes:
    return xlsx({
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract"}],
        S.S_FIELD: [field("contract", "名称", "字符串")],
        S.S_FORM: [{"表单key": "create_contract", "表单名称": "新建合同",
                    "对象编码": "contract", "表单类型": "新建表单"}],
        S.S_FORMFIELD: [{"表单key": "create_contract", "字段编码": "mingCheng",
                         "显示标签": "名称", "控件类型": "text"}],
    })


@case("WEB01", "webui", "首页能打开", "P0", ["返回 HTML", "含应用标题"])
def _():
    status, headers, body = req("/")
    assert status == 200, status
    assert "text/html" in headers.get("content-type", "")
    assert "PP6" in body.decode("utf-8", "ignore")


@case("WEB02", "webui", "静态脚本能加载", "P0", ["/static/app.js", "JS 类型"])
def _():
    status, headers, body = req("/static/app.js")
    assert status == 200, status
    assert "javascript" in headers.get("content-type", "")
    assert len(body) > 1000


@case("WEB03", "webui", "静态资源路径穿越被拦住", "P0",
      ["/static/../webui.py", "不能读到源码"])
def _():
    for bad in ("/static/../webui.py", "/static/..%2fwebui.py",
                "/static/%2e%2e/webui.py"):
        status, _, body = req(bad)
        assert status == 404, f"{bad} 返回 {status}"
        assert b"def do_GET" not in body, f"{bad} 读到了源码"


@case("WEB04", "webui", "未知路径返回 404", "P2", ["不返回 200 的空页面"])
def _():
    status, _, _ = req("/no/such/path")
    assert status == 404, status


@case("WEB05", "webui", "解析接口返回对象与统计", "P0",
      ["/api/parse", "ok=true", "objects 有内容"])
def _():
    status, _, body = req("/api/parse", package(),
                          {"Content-Type": "application/octet-stream"})
    assert status == 200, body[:200]
    data = json.loads(body)
    assert data["ok"] is True
    assert data["objects"][0]["code"] == "contract"
    assert data["stats"]["字段"] == 1


@case("WEB06", "webui", "解析非 Excel 文件不崩", "P1",
      ["返回 ok=false + 错误文案"])
def _():
    status, _, body = req("/api/parse", b"this is not an excel",
                          {"Content-Type": "application/octet-stream"})
    data = json.loads(body)
    assert data["ok"] is False
    assert data["error"], data


@case("WEB07", "webui", "出包接口返回 zip 与文件名", "P0",
      ["/api/build", "X-Filename", "content-type=application/zip"])
def _():
    status, headers, body = req("/api/build", package(),
                                {"Content-Type": "application/octet-stream"})
    assert status == 200, body[:200]
    assert headers.get("content-type", "").startswith("application/zip")
    assert body[:2] == b"PK"
    name = urllib.parse.unquote(headers.get("x-filename", ""))
    assert name.endswith(".zip"), name


@case("WEB08", "webui", "出包响应带可解析的报告头", "P1",
      ["X-Build-Report", "counts/entries"])
def _():
    _, headers, _ = req("/api/build", package(),
                        {"Content-Type": "application/octet-stream"})
    raw = headers.get("x-build-report")
    assert raw, "没有报告头"
    # 头只能写 latin-1，报告是 base64(UTF-8 JSON)，不是 URL 编码
    import base64

    report = json.loads(base64.b64decode(raw).decode("utf-8"))
    assert report["ok"] is True
    assert report["counts"]["bo"] == 1


@case("WEB09", "webui", "校验有错时出包返回 400", "P0",
      ["不返回半成品 zip", "错误里说清哪一条"])
def _():
    bad = xlsx({
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract", "物理表名": "PP_Contract"}],
        S.S_FIELD: [field("contract", "名称", "字符串")],
    })
    status, _, body = req("/api/build", bad,
                          {"Content-Type": "application/octet-stream"})
    assert status == 400, status
    assert "校验错误" in json.loads(body)["error"]


@case("WEB10", "webui", "反解接口返回填写模板", "P0",
      ["/api/inspect", "返回 xlsx", "X-Package-Summary"])
def _():
    status, headers, body = req("/api/inspect", native_zip(),
                                {"Content-Type": "application/octet-stream"})
    assert status == 200, body[:200]
    assert body[:2] == b"PK"
    assert "spreadsheet" in headers.get("content-type", "")
    assert headers.get("x-package-summary"), "缺少包摘要"


@case("WEB11", "webui", "反解坏包返回 400 且带摘要", "P1",
      ["不让前端卡在转圈"])
def _():
    status, headers, body = req("/api/inspect", b"not a zip",
                                {"Content-Type": "application/octet-stream"})
    assert status == 400, status
    assert json.loads(body)["ok"] is False
    assert headers.get("x-package-summary"), "失败时也应带摘要"


@case("WEB12", "webui", "改包解析接口列出对象", "P0",
      ["/api/patch/inspect"])
def _():
    status, _, body = req("/api/patch/inspect", native_zip(),
                          {"Content-Type": "application/octet-stream"})
    assert status == 200, body[:200]
    data = json.loads(body)
    assert data["ok"]
    assert data["objects"][0]["code"] == "project"


@case("WEB13", "webui", "改包预览接口返回变更明细", "P0",
      ["/api/patch/dry-run", "不产生 zip", "用 token 复用已上传的包"])
def _():
    _, _, body = req("/api/patch/inspect", native_zip(),
                     {"Content-Type": "application/octet-stream"})
    info = json.loads(body)
    status, _, res = jpost("/api/patch/dry-run", {
        "token": info["token"],
        "targets": [info["objects"][0]["file"]],
        "scope": "full", "mode": "skip",
        "fields": [{"code": "riskLevel", "name": "风险等级", "type": "varchar"}],
    })
    assert status == 200, res[:200]
    data = json.loads(res)
    assert data["ok"]
    added = data["changed"][info["objects"][0]["file"]]["added"]
    assert added[0]["code"] == "riskLevel", added


@case("WEB14", "webui", "改包出包接口能下载新 zip", "P0",
      ["/api/patch/build", "返回 PK 开头", "带 X-Filename"])
def _():
    _, _, body = req("/api/patch/inspect", native_zip(),
                     {"Content-Type": "application/octet-stream"})
    info = json.loads(body)
    status, headers, res = req(
        "/api/patch/build",
        json.dumps({"token": info["token"],
                    "targets": [info["objects"][0]["file"]],
                    "scope": "full", "mode": "skip",
                    "fields": [{"code": "riskLevel", "name": "风险等级",
                                "type": "varchar"}]}).encode("utf-8"),
        {"Content-Type": "application/json"})
    assert status == 200, res[:200]
    assert res[:2] == b"PK"
    assert headers.get("x-filename"), "没有文件名"


@case("WEB15", "webui", "空模板可下载", "P1", ["/api/template", "xlsx"])
def _():
    status, headers, body = req("/api/template")
    assert status == 200, status
    assert body[:2] == b"PK"
    assert "spreadsheet" in headers.get("content-type", "")


@case("WEB16", "webui", "新增字段表模板可下载", "P1",
      ["/api/patch/fields-template"])
def _():
    status, headers, body = req("/api/patch/fields-template")
    assert status == 200, status
    assert body[:2] == b"PK"


@case("WEB17", "webui", "业务对象目录可查询", "P1",
      ["/api/objects", "返回列表", "未接库时用随包快照"])
def _():
    status, _, body = req("/api/objects")
    assert status == 200, status
    data = json.loads(body)
    assert data["ok"] is True
    assert isinstance(data["objects"], list)


@case("WEB18", "webui", "空请求体给出明确错误", "P1",
      ["不抛 500", "提示没有收到文件"])
def _():
    status, _, body = req("/api/parse", b"",
                          {"Content-Type": "application/octet-stream"})
    data = json.loads(body)
    assert data["ok"] is False
    assert "文件" in data["error"] or "收到" in data["error"], data


@case("WEB19", "webui", "超过 30MB 的文件被拒绝", "P2",
      ["不把内存吃满", "提示精简后再传"])
def _():
    import http.client

    url = urllib.parse.urlparse(server())
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=30)
    conn.putrequest("POST", "/api/parse")
    conn.putheader("Content-Type", "application/octet-stream")
    conn.putheader("Content-Length", str(31 * 1024 * 1024))
    conn.endheaders()
    # 服务端读到 Content-Length 就拒绝，不等 body
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    # /api/parse 的错误统一走 200 + {ok:false}（前端按 ok 判断），
    # 关键是别真去读这 31MB
    assert resp.status in (200, 400), resp.status
    data = json.loads(body)
    assert data["ok"] is False, data
    assert "30MB" in data["error"] or "精简" in data["error"], data


@case("WEB20", "webui", "中文包名在响应头里正确编码", "P1",
      ["X-Filename 需 URL 编码", "否则浏览器存成乱码"])
def _():
    _, headers, _ = req("/api/build", simple_package(),
                        {"Content-Type": "application/octet-stream"})
    raw = headers.get("x-filename", "")
    # 原始头必须是 ASCII（URL 编码后），解码后才有中文
    assert raw.isascii(), f"响应头里有非 ASCII：{raw!r}"
    assert "项目" in urllib.parse.unquote(raw)

@case("WEB21", "webui", "改包接口拒绝伪造的缓存标识", "P1",
      ["token 必须是 20 位 hex", "防止拿路径读别人的文件"])
def _():
    status, _, body = jpost("/api/patch/dry-run", {
        "token": "../../etc/passwd", "targets": ["bo/001-x.xml"],
        "scope": "full", "mode": "skip", "fields": [],
    })
    assert status == 400, status
    assert "无效" in json.loads(body)["error"] or "过期" in json.loads(body)["error"]
