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
                   "entryChoice", "entryPanels", "entryAi", "entryExcel"):
        assert wanted in p.ids, f"#{wanted} 不是真实 DOM 元素（可能在注释里）"
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


@case("WEB29", "webui", "未连库时选「关联项目」：弹窗导入项目 zip，token 随决策头发回", "P0",
      ["没配数据库时读不到 pp_project 字段，关联项目等于空转",
       "弹窗要能选 zip、上传到 /api/project-snapshot，拿到 token",
       "token 要进 X-Project-Link.snapshot，parse/build 用同一份快照",
       "用户取消要回退开关，不能留下勾着但没快照的状态"])
def _():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    js = (root / "pp6kit" / "static" / "app.js").read_text(encoding="utf-8")
    html = (root / "pp6kit" / "static" / "index.html").read_text(encoding="utf-8")

    for mark in ('id="psMask"', 'id="psFile"', 'id="psPick"', 'id="psCancel"'):
        assert mark in html, f"弹窗缺 {mark}"
    assert "未检测到已连接的数据库" in html
    assert "function ensureProjectSnapshot(" in js
    assert "function askProjectZip(" in js
    assert '"/api/project-snapshot"' in js
    # 决策头必须带 snapshot token
    assert "payload.snapshot = state.plSnapshotToken" in js
    # 两个入口都要先走 ensure：导入前两档 + 对象卡片开关
    assert "value === \"on\" && !(await ensureProjectSnapshot())" in js
    assert "checked && !(await ensureProjectSnapshot())" in js
    # 取消要回退勾选
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
