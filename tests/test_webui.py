"""HTTP 接口：桌面 App 与网页版共用同一套请求。

前端全部走这些接口，所以这组用例是「界面点了有反应」的底线。
服务在模块内惰性启动一次，用例之间共享（省 20 次冷启动）。
"""
from __future__ import annotations

import atexit
import json
import os
import tempfile
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


@case("WEB22", "webui", "解析接口带回流程预览数据", "P1",
      ["/api/parse", "workflows[]", "节点/流转形状"])
def _():
    """左侧「流程」工作区靠这份数据渲染，字段名与 Workflow/WfNode 对应。

    这里刻意断言**形状**而不只是「有 workflows 这个键」：前端按
    node.kind / node.state / edge.condition 直接展示，少一个就是空白单元格。
    """
    wf_xlsx = xlsx({
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract",
                  "物理表名": "pp_contract", "流程驱动": "是"}],
        S.S_FIELD: [field("contract", "名称", "字符串")],
        S.S_WF: [{"流程名称": "合同审批", "对象编码": "contract"}],
        S.S_WF_NODE: [
            {"流程名称": "合同审批", "节点序号": 1, "节点名称": "申请人", "节点类型": "开始"},
            {"流程名称": "合同审批", "节点序号": 2, "节点名称": "部门审批", "节点类型": "审批",
             "处理人": "部门负责人", "处理人ID": "1567950168366514176"},
            {"流程名称": "合同审批", "节点序号": 3, "节点名称": "通过", "节点类型": "通过"},
        ],
    })
    status, _, body = req("/api/parse", wf_xlsx,
                          {"Content-Type": "application/octet-stream"})
    assert status == 200, body[:300]
    data = json.loads(body)
    wfs = data.get("workflows")
    assert isinstance(wfs, list) and len(wfs) == 1, wfs

    wf = wfs[0]
    assert wf["name"] == "合同审批", wf
    assert wf["boCode"] == "contract" and wf["boName"] == "合同", wf
    assert wf["nodeCount"] == 3, wf
    # 11_流程流转 没填 → 生成器会自动串直线，预览必须显示「将要生成」的 2 条，
    # 而不是表里的 0 行（否则用户会以为流程断了）
    assert wf["edgeCount"] == 2, wf["edges"]
    assert wf["edges"][0]["src"] == "申请人" and wf["edges"][0]["name"] == "提交", wf["edges"]
    assert wf["edges"][1]["dst"] == "通过" and wf["edges"][1]["name"] == "同意", wf["edges"]

    n0, n1, n2 = wf["nodes"]
    assert (n0["kind"], n0["state"]) == ("开始", "待发起"), n0
    assert n1["handler"] == "部门负责人" and n1["days"] == 2, n1
    assert (n2["kind"], n2["state"]) == ("通过", "已完成"), n2
    # 判断节点/终态节点没有办理时限，前端显示「—」靠 null 区分（不是 0）
    assert n0["days"] is None and n2["days"] is None, (n0, n2)


@case("WEB23", "webui", "没有流程时 workflows 是空数组而不是缺字段", "P2",
      ["前端按数组渲染", "不会 TypeError"])
def _():
    status, _, body = req("/api/parse", package(),
                          {"Content-Type": "application/octet-stream"})
    assert status == 200
    data = json.loads(body)
    assert data.get("workflows") == [], data.get("workflows")


@case("WEB24", "webui", "解析接口带回 projectAlign，且预览里的字段编码已是对齐后的（预览=产物）",
      "P0", ["前端零业务规则", "linked/summary/items 三键齐全", "预览即产物"])
def _():
    linked = xlsx({
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract",
                  "对象类型": "object", "记录变更日志": "是", "流程驱动": "否",
                  "关联项目": "是"}],
        S.S_FIELD: [field("contract", "项目编号", "字符串"),
                    field("contract", "项目状态", "对象",
                          **{"关联对象/字典": "project_state"})],
    })
    status, _, body = req("/api/parse", linked,
                          {"Content-Type": "application/octet-stream"})
    assert status == 200, body[:300]
    data = json.loads(body)
    vm = data["projectAlign"]
    for key in ("status", "snapshotSource", "snapshotSourceLabel", "warnings",
                "summary", "items"):
        assert key in vm, key
    item = vm["items"][0]
    assert item["linked"] is True
    assert item["linkField"]["action"] in ("add", "reuse"), item["linkField"]
    assert "exact" in item and "similar" in item and "conflicts" in item
    # 页签说的是「会变成 stateId」，对象卡片里就必须已经是 stateId——
    # 否则用户看到两套编码，而只有出包那份是真的（预览≠产物）
    codes = {f["code"] for f in data["objects"][0]["fields"]}
    assert "stateId" in codes, codes
    assert "code" in codes, codes
    # 链接字段在预览里也要在（出包时 builder 不会再补第二次）
    assert "projectId" in codes, codes


@case("WEB25", "webui", "X-Project-Link 不是合法 JSON → 400，且不产出半份包", "P1",
      ["错误可读", "不静默忽略"])
def _():
    status, _, body = req("/api/build", package(),
                          {"X-Project-Link": "{not json"})
    assert status == 400, status
    text = body.decode("utf-8", "replace")
    assert "X-Project-Link" in text and "JSON" in text, text


@case("WEB21", "webui", "文档提取接口：Word/PDF 正常返回，旧 .doc 明确报错", "P1",
      ["Word 正文/表格", "PDF 文字", "旧 .doc 不静默失败"])
def _():
    from tests.test_doc_text import _docx, _pdf
    doc = _docx(["合同台账需求", "记录合同名称、金额。"],
                [["字段", "类型"], ["合同名称", "文本"]])
    status, headers, body = req(
        "/api/extract", doc,
        {"Content-Type": "application/octet-stream",
         "X-Filename": urllib.parse.quote("需求.docx")})
    data = json.loads(body)
    assert status == 200 and data["ok"] is True, data
    assert data["kind"] == "docx" and "合同台账需求" in data["text"], data
    assert "合同名称 | 文本" in data["text"], data
    status, headers, body = req(
        "/api/extract", _pdf("Budget Test"),
        {"Content-Type": "application/octet-stream",
         "X-Filename": urllib.parse.quote("需求.pdf")})
    data = json.loads(body)
    assert status == 200 and data["ok"] is True, data
    assert data["kind"] == "pdf" and "Budget Test" in data["text"], data
    status, headers, body = req(
        "/api/extract", b"x",
        {"Content-Type": "application/octet-stream",
         "X-Filename": urllib.parse.quote("旧文档.doc")})
    data = json.loads(body)
    assert data["ok"] is False and ".docx" in data["error"], data


@case("WEB22", "webui", "流程模式按钮/普通工作区是真实 DOM，不能被 HTML 注释吞掉", "P0",
      ["flowModeBar / btnFlowXml / btnPack / aiResult / result 都是 start tag",
       "HTML 注释必须闭合（上一次 */ 写成注释结尾，整块 UI 被吞）"])
def _():
    from html.parser import HTMLParser
    from pathlib import Path

    class _Ids(HTMLParser):
        def __init__(self):
            super().__init__()
            self.ids = set()
            self.comments = []

        def handle_starttag(self, tag, attrs):
            for k, v in attrs:
                if k == "id" and v:
                    self.ids.add(v)

        def handle_comment(self, data):
            self.comments.append(data)

    p = _Ids()
    p.feed((Path(__file__).resolve().parents[1] / "pp6kit" / "static" / "index.html")
           .read_text(encoding="utf-8"))
    for wanted in ("flowModeBar", "btnFlowXml", "btnPack", "aiResult", "result",
                   "entryPanels", "entryAi", "entryExcel"):
        assert wanted in p.ids, f"#{wanted} 不是真实 DOM 元素（可能在注释里）"
    # 2026-11-21：入口二选一（#entryChoice）已拆掉，首屏直接给输入框；
    # 这里改钉「新入口必须存在」，避免以后又把首屏挡在一张选择页后面。
    assert "btnPackTop" in p.ids, "结果页缺少页头下载按钮"
    assert not any("btnFlowXml" in c for c in p.comments), "流程 XML 按钮还在注释里"


@case("WEB26", "webui", "文件生成：不是标准模板的 Excel 自动转 AI 识别", "P0",
      ["入口卡承诺「AI 识别字段后生成」，标准模板才直接解析",
       "自家 Excel 以前解析成 0 个对象停在结果页，看着像「不支持 AI 分析文件」",
       "AI 回填的 Excel（fromAi）和流程页 quiet 上传不能被这条兜底劫持",
       "扫描版 PDF 提取 0 字要明确报错，不能把空文本填进输入框"])
def _():
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1]
          / "pp6kit" / "static" / "app.js").read_text(encoding="utf-8")

    assert "function looksLikeTemplate(" in js, "缺少「是不是标准模板」的判定"
    start = js.index("async function handleFile(")
    body = js[start:js.index("async function loadDemo(")]
    assert "!looksLikeTemplate(data)" in body, "handleFile 没判断解析结果是不是模板"
    assert "await analyzeSheet(file)" in body, "不是模板时没把文件交给 AI 识别"
    assert "!fromAi && !quiet" in body, "AI 回填 / 流程页上传不该走这条兜底"
    assert "这份文档没有可提取的文字" in js, "扫描版 PDF 提取 0 字还当成功"


@case("WEB27", "webui", "解析结果带 looksTemplate：空模板 vs 陌生表格分得开", "P1",
      ["空的标准模板也是模板：该提示去 1_业务对象 填，不能丢给 AI",
       "陌生表格（没有标准页签）才交给 AI 识别",
       "两个都是 0 个对象，只能靠页签名区分"])
def _():
    import io

    from openpyxl import Workbook

    from pp6kit import webui
    from pp6kit.model import Project
    from pp6kit.template import build_workbook

    buf = io.BytesIO()
    build_workbook(Project()).save(buf)
    empty = webui.parse_excel(buf.getvalue())
    assert empty["looksTemplate"] is True, "空白标准模板被当成陌生文件"
    assert not empty["objects"], "空白模板不该解析出对象"

    wb = Workbook()
    ws = wb.active
    ws.title = "采购申请"
    ws.append(["申请单号", "申请人", "金额"])
    ws.append(["CG-001", "张三", 12000])
    buf = io.BytesIO()
    wb.save(buf)
    plain = webui.parse_excel(buf.getvalue())
    assert plain["looksTemplate"] is False, "陌生表格被当成标准模板"
    assert not plain["objects"]


@case("WEB28", "webui", "业务对象预览的选项值：下拉查看 + 逐个删除", "P1",
      ["选项值以前是一行逗号分隔文本，选项一多没法看、也不好删中间一个",
       "改成「N 个选项」按钮 + 浮层，浮层挂 body，避开 .tbl-wrap 的 overflow 裁切",
       "每个值带 × 删除，回车可继续添加；删完计数同步，导出 Excel 用的是改后的 choices"])
def _():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    js = (root / "pp6kit" / "static" / "app.js").read_text(encoding="utf-8")
    css = (root / "pp6kit" / "static" / "style.css").read_text(encoding="utf-8")

    assert 'data-ctoggle="${i}"' in js, "选项列没换成下拉按钮"
    assert "ai-choice-btn" in js and ".ai-choice-btn" in css
    assert 'document.body.appendChild(pop)' in js, \
        "浮层必须挂 body，放单元格里会被 .tbl-wrap 的 overflow 裁掉"
    assert 'data-cdel="${j}"' in js and "f.choices.splice(j, 1)" in js, \
        "选项值没有删除入口"
    assert "renderAiChoicePop()" in js and "updateAiChoiceCount" in js, \
        "删/加之后没刷新标签和计数"
    assert "closeAiChoicePop()" in js, "点外面/滚动/Esc 没关闭浮层"
    assert "选项值，逗号分隔" not in js, "旧的逗号分隔输入还在"
    assert ".ai-choice-pop" in css and ".ai-choice-tag" in css


@case("WEB29", "webui", "未连库时关联项目：zip 入口与快照 token 仍在，但点关联不再弹窗", "P0",
      ["没配数据库时读不到 pp_project 字段，关联项目等于空转",
       "弹窗要能选 zip、上传到 /api/project-snapshot，拿到 token",
       "token 要进 X-Project-Link.snapshot，parse/build 用同一份快照",
       "顶栏切「关联」只切档（弹窗会把人锁死在项目这条路）；单对象开关仍先确保目标"])
def _():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    js = (root / "pp6kit" / "static" / "app.js").read_text(encoding="utf-8")
    html = (root / "pp6kit" / "static" / "index.html").read_text(encoding="utf-8")

    for mark in ('id="psMask"', 'id="psFile"', 'id="psPick"', 'id="psCancel"'):
        assert mark in html, f"弹窗缺 {mark}"
    # 弹窗文案是 JS 按目标对象/是否连库改写的（2026-11-18 起）：
    # 没连库说「没有配置数据库连接」，连了但查不到这个对象要说「库里没有名称完全一致的」。
    assert "当前 App 没有配置数据库连接" in js
    assert "数据库里没有名称完全一致的" in js
    for mark in ('id="psTitle"', 'id="psSub"', 'id="psLead1"', 'id="psLead2"'):
        assert mark in html, f"弹窗文案没有可改写的锚点 {mark}"
    assert "function ensureProjectSnapshot(" in js
    assert "function askProjectZip(" in js
    assert '"/api/project-snapshot"' in js
    # 决策头必须带 snapshot token
    assert "payload.snapshot = state.plSnapshotToken" in js
    # 两个入口都要先走 ensure：导入前两档 + 对象卡片开关。
    # 2026-11-18 起两个入口统一走 ensureLinkTarget()：项目分派给
    # ensureProjectSnapshot()，其它业务对象走名称核对 / 上传该对象的 zip。
    assert "function ensureLinkTarget(" in js
    assert 'if (state.plTargetKind === "project") return await ensureProjectSnapshot();' in js
    assert '/api/link-target' in js, "其它业务对象没接口可调"
    # 2026-11-24：顶栏切「关联」只切档，不再用 ensureLinkTarget 弹窗拦人
    #（用户点关联就被项目 zip 挡住，连「其他业务对象」都选不了）。
    start = js.index("function bindPlPre()")
    seg = js[start:js.index("function renderAll()", start)]
    assert "await ensureLinkTarget()" not in seg, "顶栏切档又去做目标校验了"
    # 对象卡片上的单对象开关仍先确保目标就绪：那时文件已解析，弹窗是有上下文的
    assert "checked && !(await ensureLinkTarget())" in js
    assert "syncPlPreInputs();" in js


@case("WEB30", "webui", "列表预览的显示列可自己勾选（不再全量写死）", "P0",
      ["列表视图页签给每个对象字段一个复选框，勾选写回 state.aiData.list.columns",
       "至少保留 1 列；清空只留第一列，避免出包退回全字段",
       "级联详情下拉跟着显示列走，删列时清掉失效的 detailField"])
def _():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    js = (root / "pp6kit" / "static" / "app.js").read_text(encoding="utf-8")
    css = (root / "pp6kit" / "static" / "style.css").read_text(encoding="utf-8")

    assert 'id="aiListCols"' in js, "列表预览没有勾选容器"
    assert 'data-col="${esc(f.code)}"' in js, "字段行缺复选框"
    assert "function bindListColumnPicker(" in js
    assert "bindListColumnPicker(d);" in js, "renderAiActions 没绑定勾选"
    assert "列表至少要保留 1 列" in js, "没有拦住「清空到 0 列」"
    assert "list.columns = fields.slice(0, 1)" in js, "清空按钮没兜底留第一列"
    assert ".ai-col-grid" in css and ".ai-col-item" in css


@case("WEB31", "webui", "AI 表单预览可切换「字段分组 / 拖拽布局V1.0」", "P0",
      ["预览头部有布局样式下拉，选择写回 state.aiData.form.layoutStyle",
       "flat 时不再画分区标题，控件平铺",
       "出包时 layoutStyle 原样带走（ai_apply_bo → Form.layout_style）"])
def _():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    js = (root / "pp6kit" / "static" / "app.js").read_text(encoding="utf-8")
    css = (root / "pp6kit" / "static" / "style.css").read_text(encoding="utf-8")
    webui_src = (root / "pp6kit" / "webui.py").read_text(encoding="utf-8")

    assert 'id="aiFormStyle"' in js
    assert "bindAiFormStyle(" in js
    assert 'layoutStyle === "flat"' in js
    assert ".ai-formstyle" in css
    assert 'layoutStyle' in webui_src and 'layout_style=' in webui_src


@case("WEB32", "webui", "查数据页：结果在前、SQL 在后，进页自动读结构", "P0",
      ["结果卡片必须排在 SQL 卡片之前（否则长 SQL 把结果挤出视野）",
       "查询理解显示在结果上方",
       "进页面自动 /api/nlq/test，示例问题不用点设置才出现",
       "结果不对可一键 AI 重答"])
def _():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    html = (root / "pp6kit" / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "pp6kit" / "static" / "query.js").read_text(encoding="utf-8")
    css = (root / "pp6kit" / "static" / "style.css").read_text(encoding="utf-8")

    assert html.index('id="qResultCard"') < html.index('id="qSqlCard"')
    assert 'id="qRetryAi"' in html
    assert 'id="qOrigin"' in html
    assert "ensureQueryReady" in js and "qApi(\"/api/nlq/test\")" in js
    assert "q-understood" in js and ".q-understood" in css
    assert 'ask("llm")' in js


@case("WEB33", "webui", "查数据页：语义口径可见、缺条件先澄清、结果可入库验证", "P0",
      ["侧栏显示「口径 N 条 / 已验证 N/M 条」",
       "缺时间范围渲染澄清卡片，选项写回问题再查",
       "结果头有「标记为已验证」，点击 POST /api/nlq/verify"])
def _():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    html = (root / "pp6kit" / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "pp6kit" / "static" / "query.js").read_text(encoding="utf-8")
    css = (root / "pp6kit" / "static" / "style.css").read_text(encoding="utf-8")
    py = (root / "pp6kit" / "webui.py").read_text(encoding="utf-8")

    assert 'id="qVerify"' in html
    assert "/api/nlq/verify" in js and "qVerify" in js
    assert "renderClarify" in js and "q-clarify" in js
    assert "口径" in js and "已验证" in js
    assert ".q-clarify" in css and ".src-verified" in css
    assert '"/api/nlq/verify"' in py


@case("WEB34", "webui", "空白填写模板可下载：表头齐全、一条数据都没有", "P1",
      ["手填的用户不该只能从「载入示例」起步（示例会直接进预览，本地拿不到文件）",
       "页签与列顺序必须和带示例的模板完全一致，只差有没有数据",
       "下载的空表要能被自己的解析器认成「标准模板」"])
def _():
    import io

    from openpyxl import load_workbook

    from pp6kit import webui

    status, headers, body = req("/api/template?blank=1")
    assert status == 200, status
    assert body[:2] == b"PK", body[:20]
    assert "spreadsheet" in headers.get("content-type", "")
    name = urllib.parse.unquote(headers.get("x-filename", ""))
    assert "空白" in name, f"文件名没标明是空白模板：{headers.get('x-filename')}"

    wb = load_workbook(io.BytesIO(body))
    assert wb.sheetnames == S.SHEET_ORDER, wb.sheetnames
    # 表头 + 紧跟的说明行 = 2 行；再往下有任何一行都说明预填了示例数据
    for sheet in (S.S_BO, S.S_FIELD, S.S_FORM, S.S_FORMFIELD):
        assert wb[sheet].max_row == 2, f"{sheet} 不是空白模板（max_row={wb[sheet].max_row}）"
    assert wb[S.S_BO]["A1"].value == "对象编码", wb[S.S_BO]["A1"].value

    # 空表也是标准模板：页签名在，但解析出 0 个对象（不能当成陌生表格丢给 AI）
    parsed = webui.parse_excel(body)
    assert parsed["looksTemplate"] is True, "下载的空白模板被当成陌生表格"
    assert not parsed["objects"], "空白模板里解析出了对象"


@case("WEB35", "webui", "入口：空白模板按钮真下载，反解入口默认下线", "P1",
      ["模板按钮必须下载文件，不能退化成「载入示例」",
       "反解下线只关入口：inspectZip 与 /api/inspect 保留，开关注释写明怎么恢复"])
def _():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    html = (root / "pp6kit" / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "pp6kit" / "static" / "app.js").read_text(encoding="utf-8")
    qjs = (root / "pp6kit" / "static" / "query.js").read_text(encoding="utf-8")
    py = (root / "pp6kit" / "webui.py").read_text(encoding="utf-8")

    assert 'id="btnBlankTpl"' in html, "文件生成入口没有「下载空白模板」"
    assert "function downloadBlankTemplate(" in js
    assert "/api/template?blank=1" in js, "按钮没请求空白模板"
    assert "blankTplBtn.onclick = downloadBlankTemplate" in js, "按钮没绑上下载"

    # 反解：HTML 直接带 hidden（无 JS 也不露），query.js 开关默认 false
    assert 'id="btnInspect" data-feature="inspect" hidden' in html
    assert "inspect: false" in qjs
    assert "PP6_FEATURES.inspect !== true" in qjs
    # 只关入口，能力不删
    assert "async function inspectZip(" in js
    assert '"/api/inspect"' in py


# ---------------------------------------------------------------- WEB36–WEB39
# 「值来源」面板：用户在 app 上自己选过滤条件 / 返回结果。
#
# 为什么补这一组（2026-11-19）：需求第 4 条「过滤条件与返回结果能在 app 上自己选」
# 之前完全没有自动化覆盖——面板→sourceConfig→6_数据集→包里的
# filterConditions/resultColumns 这条链断了没有任何用例会红，而表现是
# 「下拉能点、筛选不生效」，只能靠线上排查。这里钉三个接口契约：
#   · /api/objects/fields 的主键那条必须排第一且 key=true（面板据此置灰必选）
#   · _apply_source_configs 同目标同配置只建一份，不同配置另起 <key>_2
#   · 界面配置 → Excel 6_数据集 行 → 出包后 linkdata/联动/过滤，一路不丢


@case("WEB36", "webui", "值来源面板的字段清单：主键排第一且 key=true（面板据此置灰必选）",
      "P0", ["object_field_options", "主键必选", "需求 5"])
def _():
    from pp6kit import webui

    got = webui.object_field_options("project")
    assert got["ok"] is True, got
    fields = got["fields"]
    assert fields, got
    # 主键必须排第一：面板是按顺序渲染的，藏到中间用户会以为「可以不选」
    first = fields[0]
    assert first["key"] is True, first
    assert first["column"] == "id_", first
    assert sum(1 for f in fields if f["key"]) == 1, [f["code"] for f in fields if f["key"]]
    # 子表不能当返回列（选进去值来源会取不到值）
    assert all(f["type"] != "table" for f in fields), \
        [f["code"] for f in fields if f["type"] == "table"]


@case("WEB37", "webui", "GET /api/objects/fields?code=project：200 且 fields[0].key 为 true",
      "P0", ["面板真正调的那个接口", "不是内部函数"])
def _():
    status, headers, body = req("/api/objects/fields?code=project")
    assert status == 200, (status, body[:200])
    assert "application/json" in headers.get("content-type", "")
    data = json.loads(body)
    assert data["ok"] is True, data
    assert data["fields"], data
    assert data["fields"][0]["key"] is True, data["fields"][0]
    # 缺参数要明确报错，不能给一份看起来正常的清单
    status2, _, body2 = req("/api/objects/fields")
    data2 = json.loads(body2)
    assert status2 == 200 and data2["ok"] is False and data2["fields"] == [], data2


@case("WEB38", "webui", "值来源配置落成数据集：同目标同配置复用一份，不同配置另起 _2",
      "P1", ["_apply_source_configs", "两个控件过滤不同不能共用一份"])
def _():
    from pp6kit import webui
    from pp6kit.model import Bo, BoField, Project

    proj = Project()
    bo = Bo(code="qacfg", name="配置用例", tbl_name="pp_qacfg")
    bo.fields = [
        BoField(bo_code="qacfg", code="suoShuXiangMu", name="所属项目",
                type_new="object", object_type="project", sn=1),
        BoField(bo_code="qacfg", code="beiYongXiangMu", name="备用项目",
                type_new="object", object_type="project", sn=2),
    ]
    proj.bos.append(bo)
    # custom=true = 用户动过面板（新版前端每次勾选/选过滤都会置位）。
    # 缺这个标记时后端**不写** resultFields：面板默认把「全部业务字段」显示成
    # 勾上，那是生成器的默认口径，不能当用户配置写进 6_数据集。
    cfg = {"custom": True, "filterField": "xiangMuLeiXing",
           "resultFields": ["id", "name", "zhuanYe"],
           "linkage": [{"name": "zhuan_ye_", "field": "zhuanYe", "label": "专业"}]}

    webui._apply_source_configs(proj, bo, [
        {"code": "suoShuXiangMu", "sourceConfig": dict(cfg)},
        {"code": "beiYongXiangMu", "sourceConfig": dict(cfg)},      # 同配置
    ])
    assert [d.key for d in proj.datasets] == ["project_value"], [d.key for d in proj.datasets]
    ds = proj.datasets[0]
    assert ds.filter_field == "xiangMuLeiXing", ds.filter_field
    assert ds.result_fields == ["id", "name", "zhuanYe"], ds.result_fields

    # 换一套返回列 = 另一套配置 → 必须另起一份，不能覆盖上面那份
    webui._apply_source_configs(proj, bo, [{"code": "beiYongXiangMu", "sourceConfig": {
        "custom": True, "filterField": "xiangMuLeiXing",
        "resultFields": ["id", "name"], "linkage": []}}])
    keys = [d.key for d in proj.datasets]
    assert keys == ["project_value", "project_value_2"], keys
    assert proj.datasets[0].result_fields == ["id", "name", "zhuanYe"], proj.datasets[0].result_fields

    # 面板上填了数据集 key 就按它来（反解/复用老包时用）
    proj2 = Project()
    bo2 = Bo(code="qacfg2", name="配置用例2", tbl_name="pp_qacfg2")
    bo2.fields = [BoField(bo_code="qacfg2", code="suoShuXiangMu", name="所属项目",
                          type_new="object", object_type="project", sn=1)]
    proj2.bos.append(bo2)
    # 手填数据集 key 也是「用户动过面板」的一种（反解/复用老包时面板会带上它）
    webui._apply_source_configs(proj2, bo2, [{"code": "suoShuXiangMu", "sourceConfig": {
        "custom": True, "datasetKey": "my_value",
        "resultFields": ["id", "name"], "linkage": []}}])
    assert [d.key for d in proj2.datasets] == ["my_value"], [d.key for d in proj2.datasets]

    # 面板没填 key 时，词干取**目标对象名**的英文（不是对象编码）：
    # 目标对象编码是拼音（gongSi），名字「公司」→ company → company_value。
    proj3 = Project()
    src_bo = Bo(code="gongSi", name="公司", tbl_name="pp_gong_si")
    src_bo.fields = [BoField(bo_code="gongSi", code="name", name="名称",
                            type_new="varchar", sn=1)]
    owner = Bo(code="qacfg3", name="配置用例3", tbl_name="pp_qacfg3")
    owner.fields = [BoField(bo_code="qacfg3", code="gongSiId", name="所属公司",
                            type_new="object", object_type="gongSi", sn=1)]
    proj3.bos = [src_bo, owner]
    webui._apply_source_configs(proj3, owner, [{"code": "gongSiId", "sourceConfig": {
        "custom": True, "resultFields": ["id", "name"], "linkage": []}}])
    assert [d.key for d in proj3.datasets] == ["company_value"], \
        [d.key for d in proj3.datasets]


@case("WEB39", "webui", "界面选的返回/过滤/联动真进产物：6_数据集 行 → linkdata + 联动 + 过滤",
      "P0", ["预览=产物", "需求 3/4", "sourceConfig 往返"])
def _():
    from pp6kit import webui
    from tests._harness import read_xlsx_rows

    cfg = {"custom": True, "filterField": "xiangMuLeiXing",
           "resultFields": ["id", "name", "zhuanYe"],
           "linkage": [{"name": "zhuan_ye_", "field": "zhuanYe", "label": "专业"}]}
    payload = {
        "bo": {"code": "qavs", "name": "面板用例", "fields": [
            {"code": "suoShuXiangMu", "name": "所属项目", "type": "object",
             "objectType": "project", "sourceConfig": dict(cfg)},
            {"code": "xiangMuLeiXing", "name": "项目类型", "type": "object",
             "objectType": "classification"},
            {"code": "zhuanYe", "name": "专业", "type": "varchar"},
        ]},
        "form": {"key": "create_qavs", "name": "新建面板用例", "layout": 2, "fields": [
            {"name": "suoShuXiangMu", "label": "所属项目", "type": "linkdata"},
            {"name": "xiangMuLeiXing", "label": "项目类型", "type": "select"},
            {"name": "zhuanYe", "label": "专业", "type": "text"},
        ]},
    }
    excel, _ = webui.ai_apply_bo(payload)

    # ① Excel 里能看到用户选的那一行（不是只活在内存里）。
    # 2026-11-19 起值来源单独一张 6.1_值来源（不再写 6_数据集），
    # 列口径也跟着变：目标对象写「对象编码」而不是「主表名」，
    # 也没有「类型」列（表名本身就说清了是值来源）。
    rows = [r for r in read_xlsx_rows(excel, S.S_VALUE)
            if str(r.get("值来源key") or "").strip()
            and not str(r.get("值来源key")).strip().startswith("#")]
    assert len(rows) == 1, rows
    assert rows[0]["对象编码"] == "project", rows[0]
    assert rows[0]["自动"] == "否", rows[0]
    assert rows[0]["过滤字段"] == "xiangMuLeiXing", rows[0]
    assert rows[0]["返回字段"] == "id,name,zhuanYe", rows[0]
    # ② 联动映射也跟着 Excel 走（否则换台机器就丢）
    # 只取新建表单那一段：详情表单里同一个字段是 selectObject，混在一起会被覆盖
    ctrl = {r["字段编码"]: r for r in read_xlsx_rows(excel, S.S_FORMFIELD)
            if str(r.get("字段编码") or "").strip()
            and not str(r.get("字段编码")).startswith("#")
            and r.get("表单key") == "create_qavs"}
    assert ctrl["suoShuXiangMu"]["控件类型"] == "linkdata", ctrl["suoShuXiangMu"]
    assert "zhuan_ye_" in str(ctrl["suoShuXiangMu"]["联动字段"]), ctrl["suoShuXiangMu"]

    # ③ 再出包：这些配置要真的落到 XML（预览=产物）
    import io
    import re
    import zipfile

    body, _ = webui.build_zip(excel)
    zf = zipfile.ZipFile(io.BytesIO(body))
    xmls = {n: zf.read(n).decode() for n in zf.namelist() if n.endswith(".xml")}
    form = next(t for n, t in xmls.items()
                if n.startswith("form/") and 'key="create_qavs"' in t)
    frag = next(f for f in re.findall(r"<formField\b[^>]*>", form)
                if 'name="suoShuXiangMu"' in f)
    opts = json.loads(re.search(r'fieldOptions="([^"]*)"', frag).group(1)
                      .replace("&quot;", '"').replace("&amp;", "&"))
    assert opts["linkdata"] == "project_value", opts.get("linkdata")
    by_name = {it["name"]: it["field"] for it in opts["link_linkage"]}
    assert by_name.get("zhuan_ye_") == "zhuanYe", by_name
    assert by_name.get("id_") == "" and by_name.get("name_") == "", by_name
    cond = (opts.get("link_condition") or [None])[0]
    assert cond and cond["value"] == "xiangMuLeiXing", opts.get("link_condition")

    ds = next(t for n, t in xmls.items()
              if n.startswith("data_template/") and 'key="project_value"' in t)
    jesc = lambda s: s.replace("&quot;", '"')      # noqa: E731
    cols = [c["name"] for c in json.loads(
        jesc(re.search(r'resultColumns="([^"]*)"', ds).group(1)))]
    assert "id_" in cols and "zhuan_ye_" in cols, cols
    rules = json.loads(jesc(re.search(r'filterConditions="([^"]*)"', ds).group(1)))
    rule = rules[0]["filter"]["rules"][0]
    assert rule["field"] == "classification_id_", rule
    assert rule["rule_id"] == cond["rule_id"], (rule, cond)


@case("WEB40", "webui", "面板默认态不当用户配置；custom+空返回列按「只返回主键/名称」兜底",
      "P0", ["预览=产物", "默认返回全部业务字段", "sourceConfig.custom"])
def _():
    """面板在用户没动过时把「全部业务字段」显示成勾上——那是生成器的默认口径。

    如果后端把这批默认勾选当成用户配置写进 6_数据集，产物就只剩屏幕上那几列：
    用户什么也没改，返回列却从「全部业务字段」缩成他看到的清单（而且没人会想到
    是面板默认值干的事）。所以 ``custom !== true`` 时 resultFields 一律忽略。
    """
    from pp6kit import webui

    default_cfg = {"resultFields": ["id", "name", "zhuanYe"], "linkage": [],
                   "filterField": ""}
    assert webui._source_configs(
        [{"code": "suoShuXiangMu", "sourceConfig": dict(default_cfg)}]) == {}, \
        "默认勾选被当成了用户配置"

    # 只选了过滤条件（没有 custom 也是用户动过的证据之一）→ 要落进数据集
    got = webui._source_configs([{"code": "suoShuXiangMu", "sourceConfig": {
        "filterField": "xiangMuLeiXing", "resultFields": ["id", "name"]}}])
    assert got["suoShuXiangMu"]["filterField"] == "xiangMuLeiXing", got
    assert got["suoShuXiangMu"]["resultFields"] == [], got

    # custom=true（用户真动过）→ 返回列照写
    got2 = webui._source_configs([{"code": "suoShuXiangMu", "sourceConfig": {
        "custom": True, "resultFields": ["id", "zhuanYe"]}}])
    assert got2["suoShuXiangMu"]["resultFields"] == ["id", "zhuanYe"], got2

    # custom=true 但返回列**全取消**了（只剩锁定的主键）→ 按「只返回主键 + 名称」
    # 处理。空列表不能当「没配过」：那样产物会倒回默认全字段，面板说 1 列、
    # 包里 38 列，同一个坑换个入口再踩一次。
    got3 = webui._source_configs([{"code": "suoShuXiangMu", "sourceConfig": {
        "custom": True, "resultFields": [], "linkage": [], "filterField": ""}}])
    assert got3["suoShuXiangMu"]["resultFields"] == ["id"], got3
    assert got3["suoShuXiangMu"]["linkage"] == [], got3      # 没有返回列就谈不上联动
    assert got3["suoShuXiangMu"]["resultFallback"] is True, got3

    # 端到端：AI 路径落成的值来源写「返回字段=id」，出包后 resultColumns
    # 就是「主键 + 名称」两列（不是 38 列），并且有一条能被人看见的提示
    from tests._harness import read_xlsx_rows

    notes: list[str] = []
    excel, _ = webui.ai_apply_bo({
        "bo": {"code": "qakeep", "name": "空返回列", "fields": [
            {"code": "suoShuXiangMu", "name": "所属项目", "type": "object",
             "objectType": "project",
             "sourceConfig": {"custom": True, "resultFields": [],
                              "linkage": [], "filterField": ""}},
            {"code": "beiZhu", "name": "备注", "type": "varchar"},
        ]},
        "form": {"key": "create_qakeep", "name": "新建", "layout": 2, "fields": [
            {"name": "suoShuXiangMu", "label": "所属项目", "type": "linkdata"},
            {"name": "beiZhu", "label": "备注", "type": "text"},
        ]},
    }, None, notes)
    rows = [r for r in read_xlsx_rows(excel, S.S_VALUE)
            if str(r.get("值来源key") or "").strip()
            and not str(r.get("值来源key")).startswith("#")]
    assert rows[0]["返回字段"] == "id", rows[0]
    assert any("只返回主键/名称" in n for n in notes), notes

    import io
    import re
    import zipfile

    body, _ = webui.build_zip(excel)
    zf = zipfile.ZipFile(io.BytesIO(body))
    ds = next(zf.read(n).decode() for n in zf.namelist()
              if n.startswith("data_template/") and 'key="project_value"' in zf.read(n).decode())
    cols = [c["name"] for c in json.loads(
        re.search(r'resultColumns="([^"]*)"', ds).group(1)
        .replace("&quot;", '"').replace("&amp;", "&"))]
    assert cols == ["id_", "name_"], cols


@case("WEB41", "webui", "AI 三个入口都认 X-Project-Link：生成预览 / 上传表格 / 导出 Excel",
      "P0", ["路由真把 header 传进去了（不是只有内部函数支持）",
             "带头的预览里有 projectId 且置顶 + isLink",
             "导出的 Excel 里 2_字段 有「项目选择」、1_业务对象 关联项目=是",
             "不带头时与改动前一致（一个字段都不多）"])
def _():
    """2026-11-21 用户报的 bug：勾了「关联项目」，AI 生成的字段里没有项目选择。

    这里必须走 HTTP（而不是只调内部函数）：缺的正是**路由层没读这个头**——
    内部函数支持、路由不传，界面上的开关照样不生效。
    """
    import io
    import zipfile

    from pp6kit import webui
    from pp6kit.reader import read_workbook

    from tests.test_projlink import _AI_FIELDS, _restore_ai, _stub_ai, pl_header

    orig = _stub_ai(_AI_FIELDS, {"groups": [{"title": "基本信息",
                                            "fields": ["申请单号"]}]})
    try:
        head = {"Content-Type": "application/json",
                "X-Project-Link": pl_header({})}
        status, _h, body = req("/api/ai/bo", json.dumps(
            {"description": "做一个采购申请单"}).encode("utf-8"), head)
        data = json.loads(body)
        assert status == 200 and data["ok"] is True, (status, body[:300])
        codes = [f["code"] for f in data["bo"]["fields"]]
        assert codes[0] == "projectId", codes
        assert data["bo"]["fields"][0]["isLink"] is True, data["bo"]["fields"][0]
        assert data["form"]["fields"][0]["name"] == "projectId", \
            [c["name"] for c in data["form"]["fields"]]

        # ② /api/ai/sheet：文件走二进制 body，头照样要认
        status, _h, body = req(
            "/api/ai/sheet?form=0&list=0", xlsx({
                S.S_BO: [{"对象名称": "采购申请"}],
                S.S_FIELD: [field("采购申请", "申请单号", "字符串")],
            }), {"Content-Type": "application/octet-stream",
                 "X-Project-Link": pl_header({})})
        sheet = json.loads(body)
        assert status == 200 and sheet["ok"] is True, (status, body[:300])
        assert "projectId" in {f["code"] for f in sheet["bo"]["fields"]}, \
            [f["code"] for f in sheet["bo"]["fields"]]

        # ③ /api/ai/bo/apply：导出的 Excel 必须自带链接字段与「关联项目=是」
        status, hdrs, body = req(
            "/api/ai/bo/apply",
            json.dumps({"bo": data["bo"], "form": data["form"],
                        "list": data["list"]}).encode("utf-8"),
            {"Content-Type": "application/json",
             "X-Project-Link": pl_header({})})
        assert status == 200, (status, body[:300])
        assert "spreadsheetml" in hdrs.get("content-type", ""), hdrs
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as fp:
            fp.write(body)
            path = fp.name
        try:
            proj, errors, _stats = read_workbook(path)
        finally:
            os.unlink(path)
        assert not errors, errors
        bo = proj.bos[0]
        assert bo.project_link is True, "1_业务对象 的「关联项目」不是「是」"
        link = bo.field_by_code("projectId")
        assert link is not None, [f.code for f in bo.fields]
        assert link.name == "项目选择", link.name

        # ④ 不带头（第 1 步选「不关联」）：与改造前一致，一个字段都不多
        status, _h, body = req("/api/ai/bo", json.dumps(
            {"description": "做一个采购申请单"}).encode("utf-8"),
            {"Content-Type": "application/json"})
        plain = json.loads(body)
        assert status == 200 and plain["ok"] is True, (status, body[:300])
        assert [f["code"] for f in plain["bo"]["fields"]] == [
            f["code"] for f in _AI_FIELDS], [f["code"] for f in plain["bo"]["fields"]]

        # ⑤ 头非法 → 400（与 /api/parse、/api/build 同口径，不静默当成没勾）
        status, _h, body = req("/api/ai/bo", json.dumps(
            {"description": "做一个采购申请单"}).encode("utf-8"),
            {"Content-Type": "application/json", "X-Project-Link": "{not json"})
        assert status == 400, (status, body[:300])
        bad = json.loads(body)
        assert bad["ok"] is False and "X-Project-Link" in bad["error"], bad
    finally:
        _restore_ai(orig)


@case("WEB42", "webui", "前端真把 X-Project-Link 发给 AI 三个入口（node 探针跑 app.js）",
      "P0", ["aiLinkHeader() 在 AI 路径可用（对象还没编码）",
             "「不关联」时一个头都不发", "合同目标带 target + snapshot",
             "apiPost/apiPostBlob 都没把 Content-Type 弄丢"])
def _():
    """界面侧的守卫：服务端支持 + 路由转发都对了，前端不发也是白搭。

    用 node + 最小 DOM stub 把真实 app.js 放进 vm 跑一遍 aiLinkHeader() /
    apiPost()，断言「请求里真的出现了这个头」。没装 node 就 skip。
    """
    import json as _json
    import re
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        skip("没装 node：AI 决策头的前端探针跳过（WEB41 的服务端断言仍然生效）")
    root = Path(__file__).resolve().parents[1]
    app = root / "pp6kit" / "static" / "app.js"
    probe = Path(__file__).resolve().parent / "fe_ai_pl_probe.js"
    assert probe.is_file(), f"缺少前端探针 {probe}"
    proc = subprocess.run([node, str(probe), str(app)],
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, f"node 探针失败：\n{proc.stderr[-1200:]}"
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    assert lines, "node 探针没有输出"
    out = _json.loads(lines[-1])

    # ① 第 1 步选「不关联」：不造头（等于改造前的行为）
    assert out["off_header"] is None, out
    assert out["off_headers"] == "{}", out
    # ② 选「关联」：头必须存在，objects 空（AI 生成前还没有对象编码）
    assert out["proj_is_null"] is False, \
        "AI 路径生成了 null 头：服务端会当成「没勾关联」，正是用户报的 bug"
    assert out["proj_version"] == 1, out
    assert out["proj_objects_empty"] is True, out
    assert out["proj_has_target"] is False, out        # 关联项目不带 target（服务端默认）
    assert out["proj_has_snapshot"] is False, out
    # ③ 关联「合同」+ 没连库：target/snapshot 都要带，服务端才认得出对象
    assert _json.loads(out["con_target"]) == {"name": "合同", "code": "contract"}, out
    assert out["con_snapshot"] == "0123456789abcdef0123", out
    # ④ 三个请求真的发了这个头；Content-Type 没被挤掉
    assert out["bo_has_header"] is True, out
    assert out["refine_has_header"] is True, out
    assert out["apply_has_header"] is True, out
    assert out["bo_content_type"] == "application/json", out
    # ⑤ 不关联时三个请求都不带
    assert out["off_bo_no_header"] is True, out
    assert out["off_apply_no_header"] is True, out

    # ⑥ /api/ai/sheet 走的是原生 fetch（不是 apiPost），探针里不好构造 File：
    #    在源码层钉住「这个 fetch 的 headers 是 aiHeaders()」，别被改回去。
    src = app.read_text(encoding="utf-8")
    seg = src[src.index("/api/ai/sheet?"):]
    seg = seg[:seg.index("});") + 3]
    assert re.search(r"headers:\s*aiHeaders\(\)", seg), \
        f"/api/ai/sheet 的 fetch 没带决策头：\n{seg}"


@case("WEB43", "webui", "AI 微调（/api/ai/refine）也认 X-Project-Link：链接字段不降级",
      "P0", ["路由把 header 传进 refine", "模型没回传 projectId 也照样保住 isLink",
             "表单第一行仍是 linkdata", "不带头时不加字段", "坏头 400"])
def _():
    """微调是第四条链路，原来一条用例都没覆盖。

    「再改一版」是最常点的一个按钮：模型只回业务字段，`isLink` 抄不回来，
    必须在服务端重新注入——不然用户改一句话之后，「项目选择」就降级成普通关联
    字段（不置顶、值来源不再是全字段），看起来像我们偷偷改了规则。
    """
    from pp6kit import ai_bo, ai_form, ai_refine, projlink

    from tests.test_projlink import pl_header

    # ① 当前结果：预览里已经补过「项目选择」
    bo = ai_bo.build_bo({"name": "采购申请", "code": "caigou", "fields": [
        {"code": "shenQingDanHao", "name": "申请单号", "type": "varchar",
         "length": 64}]}, [])[0]
    projlink.ensure_link_field(bo, None)
    form, _notes = ai_form.build_form(
        {"groups": [{"title": "基本信息", "fields": ["申请单号"]}]},
        bo, "create_caigou", name="新建采购申请")
    current = {"instruction": "把申请单号长度改成 100",
               "bo": ai_bo.to_dict(bo), "form": ai_form.to_dict(form)}

    # ② 模型只回业务字段（故意不带 projectId——它本来也抄不回 isLink）
    spec = {"bo": {"name": "采购申请", "code": "caigou", "fields": [
        {"code": "shenQingDanHao", "name": "申请单号", "type": "varchar",
         "length": 100}]},
        "form": {"fields": [{"name": "shenQingDanHao", "label": "申请单号",
                             "group": "基本信息"}]},
        "changes": ["申请单号长度 64 → 100"]}
    orig = ai_refine.llm.chat_any
    ai_refine.llm.chat_any = lambda cfg, msgs, timeout=90, max_tokens=0: (
        json.dumps(spec, ensure_ascii=False), {"model": "stub"})
    try:
        status, _h, body = req(
            "/api/ai/refine", json.dumps(current).encode("utf-8"),
            {"Content-Type": "application/json", "X-Project-Link": pl_header({})})
        first = json.loads(body)
        assert status == 200 and first["ok"] is True, (status, body[:300])
        codes = [f["code"] for f in first["bo"]["fields"]]
        assert codes == ["projectId", "shenQingDanHao"], codes
        assert first["bo"]["fields"][0]["isLink"] is True, first["bo"]["fields"][0]
        assert first["bo"]["projectLink"] is True, first["bo"].get("projectLink")
        assert first["form"]["fields"][0]["name"] == "projectId", \
            [c["name"] for c in first["form"]["fields"]]
        assert first["form"]["fields"][0]["type"] == "linkdata", \
            first["form"]["fields"][0]
        assert first["linkTarget"]["code"] == "project", first.get("linkTarget")
        assert any("projectId" in n for n in first["notices"]), first["notices"]

        # ③ 不带头（第 1 步选「不关联」）：与改造前一致，一个字段都不多
        status, _h, body = req("/api/ai/refine",
                               json.dumps(current).encode("utf-8"),
                               {"Content-Type": "application/json"})
        plain = json.loads(body)
        assert status == 200 and plain["ok"] is True, (status, body[:300])
        assert [f["code"] for f in plain["bo"]["fields"]] == ["shenQingDanHao"], \
            [f["code"] for f in plain["bo"]["fields"]]
        assert plain["bo"]["projectLink"] is False

        # ④ 坏头 → 400（不静默当成「没勾关联」，也不 500）
        status, _h, body = req("/api/ai/refine",
                               json.dumps(current).encode("utf-8"),
                               {"Content-Type": "application/json",
                                "X-Project-Link": "{not json"})
        assert status == 400, (status, body[:300])
        bad = json.loads(body)
        assert bad["ok"] is False and "X-Project-Link" in bad["error"], bad
    finally:
        ai_refine.llm.chat_any = orig


@case("WEB44", "webui", "值来源页签：清单可关、自动/全量锁死、不再有新增/删除（node 探针）",
      "P0", ["renderAiValues 在真实 app.js 里跑",
             "没有「+ 新增值来源」按钮，也没有删除按钮（自定义那条已移除）",
             "自动那份的开关 disabled",
             "过滤那份可开关、显示规则条数与引用字段，关闭行有 off 标记"])
def _():
    """界面侧的守卫：后端清单对了，前端把开关画错也白搭。没装 node 就 skip。"""
    import json as _json
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        skip("没装 node：值来源页签的前端探针跳过（BO46/BO47 的服务端断言仍然生效）")
    root = Path(__file__).resolve().parents[1]
    app = root / "pp6kit" / "static" / "app.js"
    probe = Path(__file__).resolve().parent / "fe_values_probe.js"
    assert probe.is_file(), f"缺少前端探针 {probe}"
    proc = subprocess.run([node, str(probe), str(app)],
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, f"node 探针失败：\n{proc.stderr[-1200:]}"
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    assert lines, "node 探针没有输出"
    out = _json.loads(lines[-1])
    assert out["has_add_button"] is False, "自定义新增入口已移除，不该再出现按钮"
    assert out["no_delete_button"] is True, out
    assert out["auto_locked"] is True, "自动那份不能被关（关了下拉就取不到数）"
    assert out["filtered_toggleable"] is True, "过滤那份必须能关"
    assert out["filtered_rule_count"] is True, out
    assert out["used_by_shown"] is True, out
    assert out["off_row_marked"] is True, out
    assert out["chips"] is True, out


@case("WEB45", "webui", "交互收尾：失败能直接跳到错处、出包后给出下一步、模块只留一个动作",
      "P1", ["错误横幅只有一句「去校验结果修」，用户得自己数 8 个页签找",
             "下载成功后只有 toast，闪完就没了，用户不知道下一步去哪上传",
             "模块行「导入 / 导出」两个按钮都只是下载 zip，且「导入」容易被误解为代导",
             "AI 生成失败只能重新组织语言，输入框里的描述明明还在"])
def _():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    html = (root / "pp6kit" / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "pp6kit" / "static" / "app.js").read_text(encoding="utf-8")

    # ① 失败即定位：横幅上的按钮 + 跳到校验结果并滚到第一条错误
    assert 'id="bannerFix"' not in html  # 按钮是脚本渲染的，别写死在模板里漏掉绑定
    assert 'id="bannerFix"' in js and "function bindBannerFix(" in js
    assert "function gotoIssues(" in js and "issue.error" in js
    assert 'data-panel="issues"' in js, "跳转目标页签不在"

    # ② 出包收尾：整包与单模块两条路径都要挂上
    assert 'id="nextStepBar"' in html, "结果页缺少出包收尾条"
    assert "function showNextStep(" in js and "function hideNextStep(" in js
    build = js[js.index("async function buildZip("):js.index("async function importOne(")]
    assert "showNextStep(name)" in build, "整包下载成功后没给下一步"
    one = js[js.index("async function importOne("):js.index("async function inspectZip(")]
    assert "showNextStep(name)" in one, "单模块下载成功后没给下一步"
    render_all = js[js.index("function renderAll("):js.index("function renderAll(") + 1500]
    assert "hideNextStep()" in render_all, "换了新数据还留着上一份的「已保存」"

    # ③ 模块行只留一个动作
    assert 'data-act="one"' in js, "模块行缺少「单独出包」"
    assert 'data-act="import"' not in js and 'data-act="export"' not in js, \
        "「导入 / 导出」两个按钮都只是下载 zip，语义重复，别再回来"

    # ④ AI 失败重试
    assert 'id="aiRetry"' in js and "generatePreview()" in js


@case("WEB46", "webui", "数据查询：没结果时不留白，出结果就把空状态撤掉",
      "P2", ["右半屏在第一次打开时是一大片空白，不知道能问什么、能不能改库",
             "空状态写死成常驻，查完 SQL 还盖在结果上面"])
def _():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    html = (root / "pp6kit" / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "pp6kit" / "static" / "query.js").read_text(encoding="utf-8")
    css = (root / "pp6kit" / "static" / "style.css").read_text(encoding="utf-8")

    assert 'id="qEmpty"' in html, "查询页缺少空状态"
    assert "还没有查询" in html
    # 空状态要顺带交代只读，别让人以为能在这里改数据
    assert "只读执行" in html, "空状态没说明只读"

    assert "function clearEmpty(" in js
    show = js[js.index("function showSql("):js.index("function renderResult(")]
    assert "clearEmpty()" in show, "生成出 SQL 后空状态还留着"
    render = js[js.index("function renderResult("):]
    assert render[:400].find("clearEmpty()") >= 0, "出结果后空状态还留着"

    assert ".q-empty" in css and "text-align: center" in css


@case("WEB47", "webui", "设置页：已保存的 Key / 密码显示成黑点，不再是空白输入框",
      "P2", ["Key 保存后输入框留空、只在 placeholder 写「留空则沿用」，看着像没配",
             "改成掩码后如果直接把黑点当值提交，会把库里的 Key 覆盖成「•••」"])
def _():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    html = (root / "pp6kit" / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "pp6kit" / "static" / "query.js").read_text(encoding="utf-8")
    css = (root / "pp6kit" / "static" / "style.css").read_text(encoding="utf-8")

    # ① 两个输入框都得是密码类型，否则黑点根本不会出现
    assert 'id="qKey" type="password"' in html
    assert 'id="qPass" type="password"' in html

    # ② 掩码是一串黑点，不是留空
    assert 'const SECRET_DOTS = "•".repeat(' in js
    assert "function maskSecret(" in js
    mask = js[js.index("function maskSecret("):js.index("function bindSecretFocus(")]
    assert "SECRET_DOTS" in mask and 'dataset.masked = "1"' in mask

    # ③ 已保存时走掩码，别退回「只改 placeholder」
    assert "if (ch.api_key_set) {" in js
    key_branch = js[js.index("if (ch.api_key_set) {"):js.index("if (ch.api_key_set) {") + 220]
    assert "maskSecret(Q.get(\"qKey\"), true)" in key_branch
    assert "maskSecret(Q.get(\"qPass\"), true)" in js
    assert 'placeholder = "已保存（留空则沿用）"' not in js, "退回留空写法了"
    assert 'placeholder = "已保存，留空则不修改"' not in js, "退回留空写法了"

    # ④ 掩码只是展示：提交必须当空值，否则会把已存的 Key 覆盖成黑点串
    assert "function secretValue(" in js
    assert 'const key = secretValue(Q.get("qKey")).trim();' in js
    assert "password: secretValue(Q.get(\"qPass\"))," in js

    # ⑤ 聚焦清空、失焦还原；切页面不覆盖用户手输的未保存值
    assert "function bindSecretFocus(" in js
    assert '"focus"' in js and '"blur"' in js
    assert "function canMask(" in js
    assert "if (db.password_set && canMask(Q.get(\"qPass\")))" in js

    # ⑥ 字号放大让黑点看得清
    assert '.set-form input[type="password"]' in css


@case("WEB48", "webui", "界面 v3：页签合并为「关联 / 校验」，结果页右栏常驻校验闸",
      "P1", ["8 个页签里「所属关系 / 项目对齐」语义重复、「校验结果 / 字段体检」要来回切",
             "校验问题是下载前的闸，必须在首屏可见，而不是沉在页面底部",
             "侧栏一层导航：分组即含义，设置沉底；不要再回到编号 + 快捷键说明"])
def _():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    html = (root / "pp6kit" / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "pp6kit" / "static" / "app.js").read_text(encoding="utf-8")
    css = (root / "pp6kit" / "static" / "style.css").read_text(encoding="utf-8")

    # ① 结果页 7 个页签：关联 / 校验各带一组面板
    start = html.index('id="tabs"')
    tabs = html[start:html.index("</nav>", start)]
    assert tabs.count('class="tab') == 7, "结果页签不是 7 个（导出模块 + 6 个核对页签）"
    for panel in ("mods", "objects", "forms", "lists", "link", "check", "package"):
        assert f'data-panel="{panel}"' in tabs, f"缺少页签 {panel}"
    for merged in ("relations", "projlink", "issues", "fields"):
        assert f'data-panel="{merged}"' not in tabs, f"{merged} 应该并进关联/校验，不再单独占页签"

    # ② 合并由一张映射表收口，一个页签可带多块面板
    assert "const RESULT_TAB_PANELS" in js
    assert '"relations", "projlink"' in js, "关联页签没有合并所属关系与项目对齐"
    assert '"check-extra", "issues", "fields"' in js, "校验页签没有合并校验结果与字段体检"

    # ③ 右栏校验闸：问题 + 结论常驻在下载按钮旁边
    for mark in ('id="checkCard"', 'id="railIssues"', 'id="railGate"', 'id="railCheckTag"'):
        assert mark in html, f"右栏校验卡缺 {mark}"
    assert "function renderCheckRail(" in js and "function showResultTab(" in js
    assert "railGate" in js and "可以下载导入包" in js

    # ④ 侧栏：一层导航 + 分组 + 设置沉底，不用编号和快捷键区
    assert ">生成<" in html and ">验证<" in html, "侧栏没有 生成 / 验证 分组"
    assert html.index(">生成<") < html.index(">验证<") < html.index('data-mode="settings"')
    assert 'class="side-keys"' not in html, "侧栏还留着快捷键说明区"
    assert 'id="keysModes"' in html, "快捷键说明应进顶栏浮层"

    # ⑤ 存量包：改名 / 新增合在一张卡的两个页签
    assert 'id="patchEditTabs"' in html
    assert 'data-tab="rename"' in html and 'data-tab="add"' in html
    assert "function pApplyEditTabs(" in (root / "pp6kit" / "static" / "patch.js").read_text(encoding="utf-8")

    # ⑥ 样式层可回退：body.v3 + 末尾的 v3 段落
    assert '<body class="v3">' in html
    assert "界面 v3 · 落地层" in css and "body.v3" in css


@case("WEB49", "webui", "主按钮用品牌蓝同族色；右栏任务记录与环境卡结构整洁",
      "P1", ["主按钮不发闷、白字对比度达 AA", "刷新在标题行、对比区在卡片底栏",
             "环境卡：切换常驻，编辑与高级各自折叠"])
def _():
    import re
    from pathlib import Path

    static = Path(__file__).resolve().parents[1] / "pp6kit" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    css = (static / "style.css").read_text(encoding="utf-8")
    tasks = (static / "tasks.js").read_text(encoding="utf-8")
    env = (static / "environment.js").read_text(encoding="utf-8")

    # ① 主按钮：白字对比度 ≥ 4.5（AA），且不能离品牌色太远。
    #    2026-11-24 用户反馈「按钮颜色有点淡」：当时为了达 AA 用了 #0d5fd6，
    #    它偏暗偏灰，和旁边的 #1677ff 放在一起显得发闷；#126efa 两头都满足。
    def _rgb(h):
        return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))

    def _lum(h):
        c = [v / 255 for v in _rgb(h)]
        c = [v / 12.92 if v <= .03928 else ((v + .055) / 1.055) ** 2.4 for v in c]
        return .2126 * c[0] + .7152 * c[1] + .0722 * c[2]

    m = re.search(r"body\.v3 \.btn\.primary \{ background: (#[0-9a-f]{6})", css)
    assert m, "v3 层没有 .btn.primary 的底色规则"
    primary = m.group(1)
    contrast = 1.05 / (_lum(primary) + .05)
    assert contrast >= 4.5, f"主按钮 {primary} 上白字对比度 {contrast:.2f}，不到 AA"
    dist = sum((a - b) ** 2 for a, b in zip(_rgb(primary), _rgb("#1677ff"))) ** .5
    assert dist <= 20, f"主按钮 {primary} 离品牌色 #1677ff 太远（色差 {dist:.0f}），会显得发闷"
    assert "#0d5fd6" not in css, "主按钮又回到了发闷的 #0d5fd6"

    # ② 任务记录：刷新在标题行且阻止折叠；对比按钮与结果框在卡片底栏。
    at = html.index('id="taskHistory"')
    summary = html[at:html.index("</summary>", at)]
    assert 'id="refreshHistory"' in summary, "刷新按钮不在任务记录的标题行里"
    assert "e.preventDefault()" in tasks, "刷新按钮在 <summary> 里，不阻止默认行为会折叠卡片"
    assert 'class="hist-foot"' in html and 'id="historyCompare"' in html
    assert 'id="historyCompareOut"' in html
    assert ".append(" not in tasks, "任务记录又在往卡片上 append 没有内边距的元素"
    assert "hist-empty" in tasks and ".hist-empty" in css, "空状态没有自己的样式类（会贴着卡片边缘）"
    assert "histWhen(" in tasks, "记录时间没有格式化（会显示成 2026-11-24T11:56:54+08:00）"
    assert "~/.pp6kit/history" not in html, "任务记录卡又放回了长说明"

    # ③ 环境卡：切换常驻，编辑与高级各自折叠；表单字段与按钮一个不少
    assert 'class="env-fold env-edit"' in env and 'class="env-fold env-adv"' in env
    assert env.index('id="environmentSelect"') < env.index('id="environmentForm"'), "切换下拉应排在编辑表单之前"
    assert "环境档案不改变数据库连接" in env and "实施档案不切换数据库连接" not in env, "环境卡的长说明回来了"
    for field in ("name", "platformVersion", "notes", "mappings", "rebind"):
        assert f'name="{field}"' in env, f"环境表单丢了字段 {field}"
    for button in ("environmentNew", "environmentDelete"):
        assert f'id="{button}"' in env, f"环境卡丢了按钮 {button}"


@case("WEB50", "webui", "首页与静态资源都不留缓存；打开浏览器带构建指纹", "P1",
      ["index.html 固定文件名，缓存住就会一直加载旧脚本",
       "static 仍 no-cache", "启动地址带 ?v=<提交>，旧标签不会被复用成旧页面"])
def _():
    status, headers, _ = req("/")
    assert status == 200, status
    assert "no-cache" in (headers.get("cache-control") or ""), headers.get("cache-control")

    status, headers, _ = req("/static/style.css")
    assert status == 200, status
    assert "no-cache" in (headers.get("cache-control") or ""), headers.get("cache-control")

    from pathlib import Path

    app = (Path(__file__).resolve().parents[1] / "pp6kit" / "app.py").read_text(encoding="utf-8")
    assert "def _browser_url(" in app
    assert "v={quote(str(tag))}" in app, "浏览器地址没有带上构建指纹"
    assert app.count("_browser_url(") >= 3, "启动与复用两条路径都要带指纹"


@case("WEB51", "webui", "输入卡的「用文件生成」是能认出来的按钮；整张卡都能拖入", "P1",
      ["按钮 44px 高、描边白底、图标 + 两行文字（不再是灰字说明）",
       "「也可拖入」常驻提示与 #dzInline 隐藏，#aiSheetInput 仍在文档里",
       "拖拽绑在 #aiChatLaunch 上（整卡接收），用计数器防止子元素抖动",
       "读取中只改 .ft-title，不冲掉图标与副标题"])
def _():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    html = (root / "pp6kit" / "static" / "index.html").read_text(encoding="utf-8")
    css = (root / "pp6kit" / "static" / "style.css").read_text(encoding="utf-8")
    js = (root / "pp6kit" / "static" / "app.js").read_text(encoding="utf-8")

    assert 'class="btn file-tile" id="btnPickSheet"' in html
    assert 'class="ft-title"' in html and 'class="ft-sub"' in html
    assert 'class="drop-veil"' in html
    assert 'id="dzInline"' in html and 'id="aiSheetInput"' in html
    assert 'class="hint drag-hint"' not in html, "「也可拖入」常驻提示没去掉"

    start = css.index("body.v3 .file-tile {")
    assert "height: 44px" in css[start:start + 240], "按钮高度不是 44"
    assert "body.v3 #dzInline { display: none; }" in css
    assert "body.v3 .ai-chat-foot .drag-hint { display: none; }" in css
    assert "body.v3 .drop-veil" in css

    assert "const setTileBusy = (on, text) =>" in js
    assert 'btn.querySelector(".ft-title")' in js
    for gone in ('btn.textContent = "读文档中…"', 'btn.textContent = "读表中…"',
                 'btn.textContent = old || "选择文件"', 'btn.textContent = "选择文件"'):
        assert gone not in js, f"旧的 textContent 改写还在：{gone}"

    assert 'const dz = $("#aiChatLaunch");' in js
    assert 'const veil = $("#aiChatLaunch .drop-veil");' in js
    assert "if (++dzDepth === 1) veil.hidden = false;" in js


@case("WEB52", "webui", "环境档案兼容旧的 GBK 文件（中文 Windows 升级不丢配置）", "P1",
      ["新文件写 UTF-8", "旧文件是 GBK 时退回本地编码读，不能直接崩",
       "升级后读出来内容一致"])
def _():
    import json
    from unittest.mock import patch

    from pp6kit import environments

    with tempfile.TemporaryDirectory() as tmp:
        target = __import__("pathlib").Path(tmp) / "environments.json"
        payload = {"activeId": "e1", "items": [
            {"id": "e1", "name": "华东实施", "platformVersion": "V6.2",
             "notes": "", "mappings": {}, "boundSnippetsSha256": ""}]}
        # 旧版本在中文 Windows 上就是这样写的（GBK）
        target.write_bytes(json.dumps(payload, ensure_ascii=False).encode("gbk"))
        with patch.object(environments, "path", return_value=target):
            got = environments.read()
        assert got["items"][0]["name"] == "华东实施", got
        assert got["activeId"] == "e1", got
