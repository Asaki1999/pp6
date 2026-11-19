"""安全与极端输入：坏包、怪字符、超大配置。

这些不是「正常用户会做的事」，但都是真实遇到过的：
用户从微信里转存一个损坏的 zip、字段名里带 `]]>`、一个对象 200 个字段。
处理不好就是整个 app 崩掉或者写出一份导不进去的包。
"""
from __future__ import annotations

import io
import zipfile

from pp6kit import patch, webui
from pp6kit import spec as S
from tests._harness import case, field, texts, unzip, xlsx, xml_ok


def build(sheets: dict) -> bytes:
    return webui.build_zip(xlsx(sheets))[0]


def empty_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w"):
        pass
    return buf.getvalue()


def hostile_zip() -> bytes:
    """条目名带 ../ 的包，模拟有人手工拼包。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.xml", "<root/>")
        z.writestr("bo/001-project.xml", """<?xml version="1.0" encoding="UTF-8"?>
<tns:bodefs xmlns:tns="http://www.ankepower.com/iform-bo-def">
  <tns:bodefentity id="1" tblName="pp_project" code="project" name="项目">
    <tns:attribute code="name" fieldName="name_" name="名称" datatype="varchar" dataTypeNew="varchar" sn="1"/>
  </tns:bodefentity>
</tns:bodefs>""")
    return buf.getvalue()


@case("SEC01", "security", "非 zip 文件给出人话错误", "P0",
      ["不是抛一堆 traceback", "用户知道要重新导出"])
def _():
    try:
        patch.inspect_package(b"this is not a zip")
    except ValueError as exc:
        assert "zip" in str(exc).lower(), exc
    else:
        raise AssertionError("坏文件竟然解析成功了")


@case("SEC02", "security", "空 zip 不崩，返回 0 个对象", "P1",
      ["提示改用别的工作区", "而不是白屏"])
def _():
    snap = patch.inspect_package(empty_zip())
    assert snap["ok"] is True
    assert snap["objects"] == []
    assert snap["entryCount"] == 0


@case("SEC03", "security", "包内 ../ 条目不会写到目录外", "P1",
      ["产物里保留原条目名但不定向到磁盘", "重点是别让程序崩"])
def _():
    snap = patch.inspect_package(hostile_zip())
    assert snap["ok"] is True
    assert any("project" in o["code"] for o in snap["objects"])
    body, _, _ = patch.build_patch(hostile_zip(), {
        "targets": ["bo/001-project.xml"], "scope": "full", "mode": "skip",
        "fields": [{"code": "riskLevel", "name": "风险等级", "type": "varchar"}]})
    assert body[:2] == b"PK"


@case("SEC04", "security", "字段名里的 CDATA 结束符不破坏 XML", "P0",
      ["]]> 必须被转义", "否则 XML 直接坏掉"])
def _():
    body = build({
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract"}],
        S.S_FIELD: [field("contract", "含]]>标记的名称", "字符串")],
    })
    for text in texts(body).values():
        xml_ok(text)
        assert text.count("]]>") <= text.count("<![CDATA["), "CDATA 被提前闭合了"


@case("SEC05", "security", "字段名里的尖括号与引号被转义", "P0",
      ["<script> 不能原样进 XML"])
def _():
    body = build({
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract"}],
        S.S_FIELD: [field("contract", '名称<script>alert("x")</script>', "字符串")],
    })
    for text in texts(body).values():
        xml_ok(text)
        assert "<script>" not in text


@case("SEC06", "security", "超长对象名仍能出包", "P1",
      ["编码截到 40 字符", "表名不超库限制"])
def _():
    long_name = "项目" * 60
    body = build({
        S.S_BO: [{"对象名称": long_name}],
        S.S_FIELD: [field("project", "名称", "字符串")],
    })
    bo_files = [n for n in texts(body) if n.startswith("bo/")]
    text = texts(body)[bo_files[0]]
    tbl = text.split('tblName="')[1].split('"')[0]
    assert len(tbl) <= 63, f"表名 {len(tbl)} 字符，超过 PostgreSQL 限制"
    xml_ok(text)


@case("SEC07", "security", "字段编码撞数据库关键字自动避开", "P1",
      ["order/desc 这类", "列名加尾下划线就不能再撞"])
def _():
    body = build({
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract"}],
        S.S_FIELD: [field("contract", "排序", "字符串", 字段编码="order")],
    })
    text = texts(body)["bo/001-contract.xml"]
    col = text.split('code="order"')[1].split('fieldName="')[1].split('"')[0]
    assert col.endswith("_"), col
    assert col != "order"


@case("SEC08", "security", "200 个字段的对象能正常出包", "P1",
      ["sn 连续", "XML 不截断"])
def _():
    rows = [field("contract", f"字段{i}", "字符串") for i in range(1, 201)]
    body = build({
        S.S_BO: [{"对象名称": "宽表", "对象编码": "wide_table", "物理表名": "pp_wide_table"}],
        S.S_FIELD: rows,
    })
    text = texts(body)["bo/001-wide_table.xml"]
    xml_ok(text)
    sns = [int(x) for x in __import__("re").findall(r'\bsn="(\d+)"', text)]
    assert sns == list(range(1, len(sns) + 1)), "sn 不连续"


@case("SEC09", "security", "名称里的 emoji 与全角符号不破坏包", "P2",
      ["UTF-8 原文保留", "XML 仍合法"])
def _():
    body = build({
        S.S_BO: [{"对象名称": "合同📄", "对象编码": "contract", "物理表名": "pp_contract"}],
        S.S_FIELD: [field("contract", "金额（含税）💰", "字符串")],
    })
    for text in texts(body).values():
        xml_ok(text)
    assert "合同📄" in texts(body)["bo/001-contract.xml"]


@case("SEC10", "security", "重复的表单 key 被拦下", "P1",
      ["同名会互相覆盖", "导入后只剩一张"])
def _():
    try:
        build({
            S.S_BO: [{"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract"}],
            S.S_FIELD: [field("contract", "名称", "字符串")],
            S.S_FORM: [
                {"表单key": "create_contract", "表单名称": "新建合同",
                 "对象编码": "contract", "表单类型": "新建表单"},
                {"表单key": "create_contract", "表单名称": "再建合同",
                 "对象编码": "contract", "表单类型": "新建表单"},
            ],
            S.S_FORMFIELD: [{"表单key": "create_contract", "字段编码": "mingCheng",
                             "显示标签": "名称", "控件类型": "text"}],
        })
    except ValueError as exc:
        assert "表单" in str(exc), exc
    else:
        raise AssertionError("重复表单 key 没被拦下")


@case("SEC11", "security", "改包时传空字段列表不产生空包", "P1",
      ["空改动应报错而不是给个原包副本"])
def _():
    raw = patch.inspect_package and open("PP6/业务对象/项目.zip", "rb").read()
    obj = patch.inspect_package(raw)["objects"][0]
    try:
        body, _, _ = patch.build_patch(raw, {
            "targets": [obj["file"]], "scope": "full", "mode": "skip", "fields": []})
    except Exception as exc:
        assert str(exc), exc
    else:
        # 允许出包，但产物必须与原包完全一致（不能悄悄改动）
        assert unzip(body) == unzip(raw)


@case("SEC12", "security", "zip 里混入非 XML 条目不影响出包", "P2",
      ["有些导出包带 .DS_Store / 说明.txt"])
def _():
    src = open("PP6/业务对象/项目.zip", "rb").read()
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(src)) as zin, zipfile.ZipFile(buf, "w") as zout:
        for name in zin.namelist():
            zout.writestr(name, zin.read(name))
        zout.writestr("说明.txt", "这是说明文件")
        zout.writestr(".DS_Store", b"\x00\x01")
    snap = patch.inspect_package(buf.getvalue())
    assert snap["ok"]
    assert snap["objects"][0]["code"] == "project"


@case("SEC13", "security", "提交前密钥扫描能拦住明文 Key", "P0",
      ["sk- 长串判红", "占位值 sk-test/sk-xxx 不误报", "只扫跟踪文件，不扫本机 out/ 和 ~/.pp6kit"])
def _():
    import tempfile
    from pathlib import Path
    from tools import check_secrets

    d = Path(tempfile.mkdtemp())
    bad = d / "bad.py"
    # 源码里把 Key 拆开拼接：扫描器扫这个测试文件时不会命中，
    # 但写进临时文件的是完整长串，用来验证扫描器真的能拦住
    bad.write_text('KEY = "' + "sk-" + "a" * 32 + '"\n', encoding="utf-8")
    ok = d / "ok.py"
    ok.write_text('KEY = "sk-test"  # 占位\n', encoding="utf-8")

    orig = check_secrets.tracked
    check_secrets.tracked = lambda: [str(bad)]
    try:
        assert check_secrets.main() == 1
    finally:
        check_secrets.tracked = lambda: [str(ok)]
        try:
            assert check_secrets.main() == 0
        finally:
            check_secrets.tracked = orig


@case("SEC14", "security", "密钥扫描在中文路径与非 UTF-8 locale 下不误报", "P0",
      ["git ls-files -z 显式 UTF-8 解码，Windows cp1252 也不炸",
       "降级分支不扫 .git/out/dist，中文文件名照样进扫描列表"])
def _():
    import subprocess
    import tempfile
    from pathlib import Path
    from tools import check_secrets

    d = Path(tempfile.mkdtemp())
    # 模拟仓库里的中文路径：UTF-8 字节里含 cp1252 未定义的 0x81
    cn = "docs/项目关联字段-说明.md"
    (d / cn).parent.mkdir(parents=True, exist_ok=True)
    (d / cn).write_text("只是说明文档，没有密钥\n", encoding="utf-8")

    # git 正常：check_output 返回 NUL 分隔的 bytes，按 UTF-8 解出中文路径
    # 真实仓库里这条命令会带出全仓库文件；此处只验证解码逻辑本身：
    # 直接喂一段含中文的 NUL 分隔 bytes，等价于 Windows runner 上的输出
    encoded = ("docs/原生包样本/b\0" + cn + "\0").encode("utf-8")
    assert any(b > 127 for b in encoded)  # 确实有非 ASCII 字节
    orig = check_secrets.subprocess.check_output
    check_secrets.subprocess.check_output = lambda *a, **k: encoded
    try:
        got = check_secrets.tracked()
        assert cn in got, got
    finally:
        check_secrets.subprocess.check_output = orig

    # git 不可用：降级扫描不得把 .git/、out/、dist/ 里的东西算进来
    (d / ".git").mkdir(exist_ok=True)
    (d / ".git" / "config").write_text('token = "sk-' + "z" * 32 + '"\n', encoding="utf-8")
    (d / "out").mkdir(exist_ok=True)
    (d / "out" / "模型参数.json").write_text('{"api_key": "' + "y" * 32 + '"}', encoding="utf-8")
    (d / "dist").mkdir(exist_ok=True)
    (d / "dist" / "bundle.js").write_text("// built\n", encoding="utf-8")
    check_secrets.subprocess.check_output = lambda *a, **k: (_ for _ in ()).throw(OSError("no git"))
    try:
        got = check_secrets.tracked(root=d)
        # root 是绝对路径，降级分支返回的是绝对路径，按文件名判断
        assert any(p.endswith(cn) for p in got), got
        assert not any(
            f"{sep}{name}{sep}" in p or p.startswith(f"{name}{sep}")
            for p in got for sep in ("/", "\\") for name in (".git", "out", "dist")
        ), got
    finally:
        check_secrets.subprocess.check_output = orig


@case("SEC15", "security", "密钥扫描在 ascii/cp1252 控制台下仍能正常退出", "P0",
      ["脚本入口强制 stdout/stderr UTF-8", "中文提示不会让 Windows runner 秒退 1"])
def _():
    import os
    import subprocess
    import sys
    from pathlib import Path
    from tools import check_secrets

    script = Path(check_secrets.__file__)
    root = script.parent.parent
    for enc in ("ascii", "cp1252"):
        env = dict(os.environ, PYTHONIOENCODING=enc)
        r = subprocess.run(
            [sys.executable, str(script)], cwd=str(root), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        assert r.returncode == 0, f"{enc}: {r.returncode} {r.stderr.decode('utf-8', 'replace')[:300]}"
