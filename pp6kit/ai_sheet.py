"""项目经理自己整理的（非标准）Excel → 标准对象模型。

为什么要单独一条路
------------------
「一句话生成」拿到的信息太少，字段全靠猜。
而项目经理手上往往已经有一份自己整理的表格——列名、示例数据、枚举值都在里面，
这是**最贴近真实需求**的输入，比任何描述都准。

做法：把表格压成一份紧凑概要（表头 + 几行样例 + 每列的取值分布），
交给 AI 映射成业务对象；生成逻辑仍复用 ai_bo.build_bo，
保证和「一句话生成」走同一条校验/出包链路。

不把整张表塞给模型：几十列 × 几百行会撑爆上下文，也没必要——
判断字段类型只需要看列名和 3~6 个样例值。
"""
from __future__ import annotations

import io
import json
import re

from openpyxl import load_workbook

from . import ai_bo, objects
from .nlq import llm

MAX_SHEETS = 4
MAX_COLS = 24
MAX_ROWS = 6
HEADER_SCAN = 8        # 前 8 行里找真表头（跳过标题行/空行）
CELL_CHARS = 48

SHEET_EXTRA = """

============ 本次输入不是一句话，而是一份「项目经理自己整理的 Excel 」 ============

下面会给你每张表的表头、样例数据和每列的取值情况。请把它映射成一个标准业务对象：

1. **先判断这张表想记录什么业务**，起一个准确的对象名（如「采购申请」「设备台账」）；
   如果一张表里明显混了主表 + 明细行（如订单头 + 多行物料），只做主表，明细字段名里带「明细」。
2. **字段名沿用表头的中文**，不要自己改叫法；表头是英文的翻成中文名，编码按规范生成。
3. 每个字段给出 `sourceColumn`，格式 `"sheet名!表头"`，便于用户核对来源。
4. **类型判断**（这条最重要，宁可保守）：
   - 表头含「日期/时间/截止/开始/完成」→ datetime
   - 含「金额/数量/单价/比例/进度/工时/分数」→ number
   - 含「负责人/经办人/审批人/创建人」→ user
   - 含「部门/组织」→ department
   - 含「编号/单号/流水」且值形如 NO2026001 → autoNumber
   - 含「附件/文件/图纸」→ attachment
   - **某列在样例里只有少数几个重复值（≤5 种），且是中文字词**（如 草稿/进行中/已完成）→ dic，
     并在 `values` 里列出这些值
   - 关联到本系统其它对象的（如「所属项目」「所属客户」）→ object，
     `objectType` 必须填**清单里给出的对象编码**（如 project / customer）；
     不要填中文名（「项目」）、不要自己拼（pp_project），填错导入后是空下拉框；
     指向「人」的用 user，不要用 object 指员工
   - **表头里出现候选对象的名字（客户、项目、合同、产品…）就给 object，不要因为
     样例值是人能看懂的汉字（「江城晶圆制造基地」）就判成 varchar**——
     这类对象在系统里是单独维护的，填文本会导致后面统计、关联全断
   - 长段落描述 → clob；其余 → varchar
5. 忽略：纯空列、只有一两个值的序号列、明显是 Excel 备注的行。
6. 不要编造表里没有的字段。表里只写了 8 列，就只输出 8 个字段。

字段额外支持两个键：
  "values": ["草稿", "进行中", "已完成"]     // 仅 dic 类型，取自表里实际出现的值
  "sourceColumn": "Sheet1!状态"             // 来源列，便于核对

============ 另一种输入：表单规格（【单行文本】这种标记） ============

如果概要里给的是「【表单 xxx】」加上一串 `字段｜【控件类型】说明`，说明这是
业务方填的表单搜集表，不是数据表。你要**自己读说明文字，判断每个字段该怎么生成**：

1. 字段类型照说明判断（**输出只能用字段层那 14 种类型代码：varchar / number /
   datetime / clob / user / department / attachment / i18n / dic / object /
   userGroupCate / table / autoNumber / formula**，不要输出「单行文本」「下拉框」
   「数据字典」这种控件层叫法）：
   - 【单行文本】【文本】→ varchar（字符串）；【多行文本】→ clob（大文本）；
     【日期/日期选择】→ datetime（日期时间）
   - **只要说明里出现「候选值」，一律判 dic**（选项），不管前面标记是【下拉选择】
     【单选】【自动带出】还是别的，取值写进 `values`
   - **判断顺序（从硬到软，前一条命中就停）**：
     1. 说明里写了「部门选择 / 组织选择」→ department；「人员选择 / 成员选择 / 用户」→ user
     2. 说明里写了「项目选择 / 关联项目 / 关联合同」`且` 名字能在对象清单里找到 → object
     3. **其他情况一律 dic**——尤其出现「候选值」时，「研发中心、华润三九（雅安）药业…」
        这种看着像公司/部门名的，也照样是 dic（选项），不是 department、不是 object
   - **反例（这些是错的，别学）**：「项目需求单位｜【下拉选择】候选值：研发中心、
     创新药物研究院…」不是 department、也不是 object，是 **dic**，values 填那些单位名。
     理由：文档给的是一串**固定取值**，系统里没有对应的组织对象/部门树，
     判成 department 或 object 导入后就是一个空下拉，用户啥也选不到。
   - 【附件上传】【附件】→ attachment（附件列表）；【自动编号】→ autoNumber；
     【人员】→ user（用户）；【部门】→ department（部门）；金额/数量 → number（数字）
   - 【只读文本】【自动带出】【自动识别】→ 类型照常，`readonly` 设 true
2. `readonly` 默认 false。**只有说明里明确出现「只读」「自动带出」「自动识别」「自动生成」
   「不可编辑/不可修改」时才设 true**；【多行文本】【日期选择】【下拉选择】这类
   要人填写/选择的，不要设只读。
3. `values` 只要真正的取值：说明里混着「候选值：」「半自动？可编辑」这类
   解释文字时，把它们剔掉，只留「立项中、待启动、在研」这样的值。
   **候选值是分组的（说明里出现「随"XX"字段联动」+ 每行「父值：子1、子2」）时，
   必须按分组原样保留**：一条 value 写「父值：子1、子2」（例如
   `"values": ["中药：OTC、RX、双跨、其他", "化药：OTC、RX、双跨、其他", "大健康：大健康"]`），
   同时给这个字段填 `cascadeParent` = 父字段编码（「随"品种类别"联动」→ 品种类别字段的编码）。
   不要把分组拍平成不相关的候选值——拍平后级联联动就没了。
   **概要里如果直接给了「· 级联父字段：…」或「· 分组候选值（父：子）：…」，
   原样照它填 `cascadeParent` 和 `values`**，不要自己再拆一遍；
   父值只属于父字段，不要写进子字段的 `values`。
4. 对象名用表单标题去掉公司简称和「表单」二字（「三九项目立项信息表单」→「项目立项信息」）。
5. 分组〔提报信息〕〔品种基本信息〕保留：同组字段排相邻，顺序按文档。
6. **文档里出现的每个字段都要输出，一个都不能少**；字段多时宁可写得紧凑
   （不写 note、不加解释），也不要截断。
"""


# 候选值可能很长（「项目研发部门」列了 37 个公司名，900 多字），
# 截断会让 AI 只看到前几个，生成出来的选项就残缺了。给个宽上限。
OPTION_CAP = 1200


def _cell_text(v) -> str:
    if v is None:
        return ""
    # 换行保留成「、」而不是空格：这类表用换行分隔候选值（级别/选项各占一行），
    # 换成空格后 AI 会当成一句话，拆不出候选值。
    s = str(v).strip().replace("\n", " ")
    if "候选值" in s or len(s) > CELL_CHARS:
        # 带候选值的说明整段保留（上限放宽到 OPTION_CAP）
        return s[:OPTION_CAP]
    return s[:CELL_CHARS]


def _read_grid(ws, max_rows: int = 60) -> list[list[str]]:
    """读一张表的前若干行。

    坑：WPS / 部分三方工具导出的 xlsx 会把工作表尺寸写成 `A1:A1`，
    openpyxl 的 read_only 模式**信任这个声明**，于是整张表只读出 1 行。
    「三九蝶变项目表单及流程图搜集表.xlsx」26 张表全是这样，
    表现就是「一张有效表格都没读到」。reset_dimensions() 让它重新扫描。
    """
    try:
        ws.reset_dimensions()
    except Exception:
        pass
    grid: list[list[str]] = []
    for row in ws.iter_rows(values_only=True):
        grid.append([_cell_text(v) for v in (row or [])[:MAX_COLS]])
        if len(grid) >= max_rows:
            break
    while grid and not any(grid[-1]):
        grid.pop()
    return grid


def _form_score(grid: list[list[str]]) -> int:
    """有多少个格子以【开头。≥3 就当表单规格，而不是数据表。"""
    return sum(1 for row in grid for c in row if c.startswith("【"))


def _cell(grid: list[list[str]], ri: int, ci: int) -> str:
    if 0 <= ri < len(grid):
        row = grid[ri]
        if 0 <= ci < len(row):
            return row[ci]
    return ""


def _find_label(grid: list[list[str]], ri: int, ci: int) -> str:
    """从一个【说明】格子反找它的标签。

    文档里三种排法都要认：
    - 同一行左边（项目名称 | 【自动带出】）
    - 上一行同列（横向表头，说明写在下一行）
    - 上一行左边（标签单独一行、说明挤在下一行开头）
    """
    for j in range(ci - 1, max(-1, ci - 4), -1):
        t = _cell(grid, ri, j)
        if t and not t.startswith("【"):
            return t
    if ri > 0:
        for j in range(ci, max(-1, ci - 4), -1):
            t = _cell(grid, ri - 1, j)
            if t and not t.startswith("【"):
                return t
    return ""


# 分组标题允许的长度。原来是 26，实测「预算计划（根据计划完成年度日期，
# 列出各年度所需费用概况）」28 字就被当成普通内容，整个分组丢了。
# 判断"是不是分组"靠的是「整行只有一个格子」这个结构特征，
# 长度只是防呆，放宽松一点更安全。
SECTION_MAX = 40


# 标签里夹带加载条件的特征词。命中就把第一个出现的位置之后全部当"条件说明"
_COND_WORDS = ("若", "仅", "需加载", "不需加载", "需要加载", "（", "(")


def _split_label_condition(raw: str) -> tuple[str, str]:
    """「MAH持有人 若战略类别为其他…」→ ("MAH持有人", "若战略类别为其他…")

    只按**第一个**条件词切，切出来的前半段必须是合理的字段名（≤20 字、非空）。
    否则原样返回——宁可留着，也不能把正常字段名切坏。
    """
    text = (raw or "").strip()
    if not text:
        return text, ""
    # 换行分隔是最明确的信号
    if "\n" in text:
        head, _, tail = text.partition("\n")
        head, tail = head.strip(), tail.strip()
        if head and len(head) <= 20:
            return head, tail
    # 「…项目来源为"外部引进"需加载」这种条件带主语，只有"需加载"三个字
    # 会切出「…项目来源为"外部引进"」当字段名。先把"条件句的起点"找出来。
    m = re.search(r"\s*(项目来源为|战略类别为|状态为|类型为|当|若|如果)", text)
    cut = m.start() if m and m.start() > 1 else len(text)

    for w in _COND_WORDS:
        i = text.find(w)
        # 条件词出现在开头（如"是否…"）不算，至少要在第 2 个字之后
        if not (1 < i < cut):
            continue
        # 括号里是单位的不算条件：「项目总预算（含税：万元）」的括号说明是单位口径
        if w in ("（", "("):
            tail_all = text[i:]
            if "元" in tail_all or "天" in tail_all or "%" in tail_all:
                continue
        cut = i
    if cut == len(text):
        return text, ""
    head, tail = text[:cut].strip(), text[cut:].strip()
    if head and len(head) <= 20:
        return head, tail
    return text, ""


# 这些名字表示"这里是一张子表"，同名出现多次是正常的（各属不同分组的子表）
_SUB_TABLE_LABELS = {"计划表格", "明细", "子表", "表格", "列表明细"}


def _group_above(grid: list[list[str]], ri: int, span: int = 6) -> str:
    """从第 ri 行往上找最近的一个「整行只有一格」的行，当分组标题。

    为什么要往上找而不是用循环里的 cur：
    「里程碑计划（…）」写在 R26，字段在 R27；但 cur 要等循环走到 R26 才更新，
    而 R27 的字段已经用上了 R26 的值——顺序上是对的。
    问题在于 R28「预算计划」标题和 R29 字段之间隔着一行表头说明，
    只看 cur 会把 R29 归到 R26 那组。往上扫最近的分组标题最稳。
    """
    for back in range(1, span + 1):
        r = ri - back
        if r < 0:
            break
        row = grid[r]
        solid = [c for c in row if c]
        if len(solid) == 1 and not solid[0].startswith("【") and len(solid[0]) <= SECTION_MAX:
            return solid[0]
    return ""


def _extract_form(grid: list[list[str]]) -> dict:
    """把「标签 | 【控件类型】说明」的表单规格拆成「标签 + 原始说明」。

    这里只做**结构识别**（哪个是字段名、哪个是它的说明），不做任何类型判断——
    类型、候选值、是否关联对象、是否只读，全部留给 AI 按说明文字去判。

    以【说明】格子为锚点反找标签，三种排版都能覆盖：
    - 同一行左边（项目名称 | 【自动带出】）
    - 上一行同列（横向表头，说明写在下一行）
    - 上一行左边（标签单独一行、说明挤在下一行开头）

    干扰自动跳过：
    - 右侧「序号|流程节点|责任角色」流程小表：没有【标记，不成为字段
    - 示例数据行（【项目1里程碑2】每完成一个…）：上一格也是说明，找不到标签
    """
    fields: list[dict] = []
    labels: dict[str, int] = {}
    sections: list[str] = []
    title = ""
    cur = ""
    dup = 0

    for ri, row in enumerate(grid):
        if ri == 0:
            for c in row:
                if c:
                    title = c
                    break

        solid = [(ci, c) for ci, c in enumerate(row) if c]
        if len(solid) == 1:
            _ci, text = solid[0]
            if ri > 0 and not text.startswith("【") and len(text) <= SECTION_MAX:
                cur = text
                sections.append(text)

        for ci, text in enumerate(row):
            if not text.startswith("【"):
                continue
            label = _find_label(grid, ri, ci).strip()
            if not label or label.startswith("【") or len(label) > SECTION_MAX:
                continue
            # 「计划表格」这类名字在搜集表里会重复出现三四次，每次是不同子表
            # （里程碑计划 / 预算计划 / 主要风险控制）。子表一律带上它上方的分组名，
            # 既避免被当成重复字段合并，也让"这是哪个子表"一眼可见。
            group = _group_above(grid, ri) or cur or title
            is_sub = label in _SUB_TABLE_LABELS
            if label in labels or (is_sub and group and group != label):
                merged = f"{group}·{label}" if group and group != label else label
                if merged in labels:
                    dup += 1
                    continue
                labels[merged] = len(fields)
                fields.append({"label": merged, "note": text, "section": cur,
                               "alias": label, "group": group})
                continue
            labels[label] = len(fields)
            fields.append({"label": label, "note": text, "section": cur})

    # 标签里常夹着加载条件（「MAH持有人 若战略类别为其他或N/A不需加载此字段」），
    # 直接拿去生成编码会得到 76 个字符的垃圾列名。这里把"说明部分"剥出来单独存。
    for f in fields:
        f["label"], f["cond"] = _split_label_condition(f["label"])

    _cascade_hints(fields)
    return {"title": title, "sections": sections, "fields": fields,
            "kind": "form", "dropped": dup}


# 「随“品种类别”字段联动」→ 父字段名
_LINK_RE = re.compile(r'随\s*[“"]([^”"]{1,24})[”"]\s*(?:字段)?\s*联动')
# 一行一个父值的分组写法：中药：OTC、RX、双跨、其他
_GROUP_LINE = re.compile(r"^([^：:>＞【】\s]{1,24})[：:>＞](.+)$")
# 单行形态：「中药：OTC、RX、双跨、其他 化药：OTC、RX…」——靠「空格 + 下一个父值：」断句
_INLINE_GROUP = re.compile(
    r"(?:^|\s)([^\s：:>＞【】]{1,24})[：:>＞]\s*([^：:>＞]+?)"
    r"(?=\s+[^\s：:>＞【】]{1,24}[：:>＞]|$)")


_NA_RE = re.compile(r"(?<![A-Za-z])N/A(?![A-Za-z])", re.I)


def _split_kids(text: str) -> list[str]:
    """子项分隔（、，,；;|/），N/A 保留完整。"""
    marks: list[str] = []

    def _hold(m):
        marks.append(m.group(0))
        return f"\x00{len(marks) - 1}\x00"

    held = _NA_RE.sub(_hold, str(text or ""))
    return [re.sub(r"\x00(\d+)\x00", lambda m: marks[int(m.group(1))], x).strip()
            for x in re.split(r"[,，;；|、/]+", held) if x.strip()]


def _add_group(groups: dict[str, list[str]], parent: str, kids_text: str) -> None:
    parent = str(parent).strip()
    kids = _split_kids(kids_text)
    if not parent or not kids:
        return
    bucket = groups.setdefault(parent, [])
    for k in kids:
        if k not in bucket:
            bucket.append(k)


def _cascade_hints(fields: list[dict]) -> None:
    """从表单规格的说明文字里提前抽出「级联父字段」和「父→子分组」。

    模型经常把「中药：OTC、RX…」拍平，或者把父值「中药」也算进子字段。
    这里先在 Python 侧把结构化信息抽出来，作为概要的一部分喂给模型；
    即使模型还是拍平，ai_bo 的分组解析 + 父字段推断也能兜住。

    只认简单的「父：子1、子2」行（父值不含空格/【】）。像
    「【中药】新品获得：中药1类…」这种多级写法不在这里猜，交给模型。
    """
    for f in fields:
        text = f"{f.get('note') or ''} {f.get('cond') or ''}"
        m = _LINK_RE.search(text)
        if m:
            f["parentLabel"] = m.group(1).strip()
        groups: dict[str, list[str]] = {}
        note = f.get("note") or ""
        # 概要里的换行在 _cell_text 已经压成空格，所以先按行解析，
        # 再对「候选值： 中药：OTC… 化药：OTC…」这种单行形态做一次配对。
        for line in re.split(r"[\n。；;]+", note):
            mm = _GROUP_LINE.match(line.strip())
            if mm:
                _add_group(groups, mm.group(1), mm.group(2))
        m = re.search(r"候选值[：:]\s*(.+)", note)
        body = m.group(1) if m else note
        for mm in _INLINE_GROUP.finditer(" " + body):
            _add_group(groups, mm.group(1), mm.group(2))
        # 多级写法（【中药】新品获得：…）不在这里猜，避免抽出一堆错父值；
        # 只给 parentLabel，让模型按原文处理。
        if "【" in body:
            groups = {}
        # 只有一个分组不算「分组写法」（防把「L1：创新药」当级联）
        if len(groups) >= 2:
            f["groups"] = groups


def _table_from_grid(grid: list[list[str]]) -> dict | None:
    """数据表：前几行里非空格子最多的那行当表头（跳过标题行/空行）。"""
    best, best_n = 0, -1
    for i, cells in enumerate(grid[:HEADER_SCAN]):
        n = sum(1 for c in cells if c)
        if n > best_n:
            best, best_n = i, n
    if best_n < 2:
        return None

    header = grid[best]
    body = [r for r in grid[best + 1:] if any(r)][:MAX_ROWS]
    cols = []
    for i, h in enumerate(header):
        if not h:
            continue
        vals = [r[i] for r in body if i < len(r) and r[i]]
        cols.append({
            "header": h,
            "samples": list(dict.fromkeys(vals))[:3],
            "distinct": len(set(vals)),
            "nonEmpty": len(vals),
        })
    if not cols:
        return None
    return {"columns": cols, "headerRow": best + 1, "kind": "table",
            "rowCount": max(0, len(grid) - best - 1)}


def summarize(data: bytes, only: str | None = None) -> dict:
    """把 Excel 压成概要。

    两类表分开处理：
    - 数据表：表头 + 样例值（原来的路）
    - 表单规格：`标签 | 【控件类型】说明`，直接拆出字段（新增）

    传 only 只分析指定那张（26 张表单的表，一次只生成一个对象才合理）。
    """
    wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    pool = objects.catalog()
    inventory: list[dict] = []
    parsed: list[dict] = []

    for ws in wb.worksheets:
        grid = _read_grid(ws)
        if not grid:
            continue
        table = _table_from_grid(grid)
        if _form_score(grid) >= 3:
            got = _extract_form(grid)
            # 表单规格识别得太少、而这张表又有像样的表头（≥5 列）→ 它其实是数据表，
            # 「里程碑激励计算汇总表」就是：表头在第 4 行，下面几行的【】只是列说明
            if len(got["fields"]) < 5 and table and len(table["columns"]) >= 5:
                got = table
        else:
            got = table
        if not got:
            inventory.append({"name": ws.title, "kind": "empty", "fieldCount": 0, "title": ""})
            continue
        n = len(got["fields"]) if got["kind"] == "form" else len(got["columns"])
        inventory.append({"name": ws.title, "kind": got["kind"], "fieldCount": n,
                          "title": got.get("title") or ""})
        if n:
            parsed.append({"name": ws.title, **got})
    wb.close()

    if only:
        picked = [p for p in parsed if p["name"] == only] or parsed[:1]
    else:
        # 默认第一张表单（工作簿顺序），和人的直觉一致：先看第一页。
        # 以前按「字段最多」挑，26 张表的文件会默认落到 57 个字段的那张，又慢又不对。
        forms = [p for p in parsed if p["kind"] == "form"]
        picked = forms[:1] if forms else parsed[:MAX_SHEETS]

    return {"sheets": picked, "inventory": inventory, "sheetCount": len(inventory),
            # 跨 sheet 的疑似重复字段：一次只分析一张表时，靠这个提醒
            "crossDupes": cross_sheet_dupes(parsed)}


def render_summary(summary: dict) -> str:
    """概要 → 给模型看的紧凑文本。"""
    lines = []
    for sh in summary["sheets"]:
        if sh.get("kind") == "form":
            lines.append(f"【表单 {sh['name']}】标题：{sh['title'] or sh['name']}，"
                         f"{len(sh['fields'])} 个字段")
            for f in sh["fields"]:
                sec = f"〔{f['section']}〕" if f.get("section") else ""
                lines.append(f"  - {f['label']}｜{f['note']}{sec}")
                if f.get("parentLabel"):
                    lines.append(f"      · 级联父字段：{f['parentLabel']}"
                                 f"（cascadeParent 必须填它的编码，值不要混进子字段）")
                if f.get("groups"):
                    pairs = "；".join(f"{p}→{'、'.join(k)}"
                                     for p, k in f["groups"].items())
                    lines.append(f"      · 分组候选值（父：子）：{pairs}")
            lines.append("")
            continue

        lines.append(f"【表 {sh['name']}】约 {sh['rowCount']} 行，{len(sh['columns'])} 列")
        for c in sh["columns"]:
            sample = "、".join(c["samples"]) or "（空）"
            shape = f"{c['distinct']} 种取值" if c["distinct"] > 1 else "单一取值"
            lines.append(f"  - {c['header']}｜样例：{sample}｜{shape}")
        lines.append("")
    return "\n".join(lines).strip()


def _avoid_dict_key_clash(bo) -> list[str]:
    """选项字段的字典 key 不能和系统里已有的撞车。

    为什么**不复用**库里的字典（2026-10-29 用户纠正）：
      1. iform_cat_type.type_key_ 是**全局唯一**的（实测 194 个字典，零重复）。
         复用一个已有 key，等于新字段和原有对象**共用同一份字典**——
         以后改这份字典，两边一起变，互相牵连。
      2. 名字碰巧一样不代表含义一样。库里 `sf` 叫「是否签收」，
         新表单里的「是否签收」可能指别的东西，绑过去就是错的。
      3. 新表单是**新建**的业务对象，它需要的字典也应该是新的、独立的一份。

    所以这里只做一件事：如果按字段编码生成的 key 恰好和库里已有字典重名，
    给 key 加对象编码做前缀，保证是独立的新字典。
    """
    from . import objects as _objects

    existing = _objects.dics()
    if not existing:
        return []

    notices: list[str] = []
    used: set[str] = set()
    for f in bo.fields:
        if f.type_new != "dic" or not f.choices:
            continue
        key = f.dict_key()
        if key in existing or key in used:
            new_key = f"{bo.code}_{key}"
            n = 2
            while new_key in existing or new_key in used:
                new_key = f"{bo.code}_{key}{n}"
                n += 1
            f.object_type = new_key
            notices.append(
                f"字段「{f.name}」字典 key {key} 系统里已存在，改用 {new_key} 另建一份"
                f"（避免和已有字典共用）")
            key = new_key
        used.add(key)
    return notices


def _apply_cascade_hints(spec: dict, summary: dict, notices: list[str]) -> None:
    """把概要里抽出的级联信息盖到模型输出的 spec 上（不覆盖模型已给的）。"""
    raw_fields = spec.get("fields") or (spec.get("bo") or {}).get("fields") or []
    by_name: dict[str, dict] = {}
    for raw in raw_fields:
        if not isinstance(raw, dict):
            continue
        nm = str(raw.get("name") or "").strip()
        if nm:
            by_name.setdefault(nm, raw)
    for sh in summary.get("sheets") or []:
        for f in sh.get("fields") or []:
            label = str(f.get("label") or "").strip()
            target = by_name.get(label)
            if target is None:
                continue
            parent_label = str(f.get("parentLabel") or "").strip()
            if parent_label and not target.get("cascadeParent"):
                parent = by_name.get(parent_label)
                if parent and parent.get("code"):
                    target["cascadeParent"] = parent["code"]
            groups = f.get("groups")
            if groups and not target.get("choiceGroups"):
                target["choiceGroups"] = groups
                notices.append(
                    f"字段「{label}」的候选值按说明里的「父：子」分组还原"
                    f"（{'、'.join(list(groups)[:4])}→…），父值不会出现在子字段里")


def analyze(summary: dict, cfg: dict, known_objects: list | None = None,
            hint: str = "", timeout: int = 120) -> dict:
    """概要 + 可选补充说明 → 业务对象。

    返回 {ok, bo, notices, model, seconds}，与 ai_bo.generate 同构。
    """
    import time

    if not summary.get("sheets"):
        raise ValueError("这个 Excel 里没读到有效表格（前几行都是空的）")
    if not ai_bo._pick_provider(cfg):
        raise llm.LlmError("还没配置大模型 API Key，无法分析表格")

    pool = ai_bo._as_pool(known_objects) or objects.ensure()
    headers: list[str] = []
    for sh in summary["sheets"]:
        if sh.get("kind") == "form":
            headers += [f["label"] for f in sh["fields"]]
        else:
            headers += [c["header"] for c in sh.get("columns") or []]
    known = objects.prompt_block(headers, pool)
    known = ("\n\n" + known) if known else ""

    user = "===== 表格概要 =====\n" + render_summary(summary)
    if hint.strip():
        user += "\n\n===== 项目经理补充说明 =====\n" + hint.strip()[:600]

    # 期望字段数：表格概要里能数出来的所有字段/列
    def _expect() -> int:
        n = 0
        for sh in summary["sheets"]:
            n += len(sh.get("fields") or sh.get("columns") or [])
        return n
    n_expect = _expect()

    from .ai_bo import _fast_cfg
    t0 = time.time()
    # max_tokens 必须按字段数算，不能用固定值：表单规格 48 个字段时
    # 6000 token 在第 16 个字段中间就断了（2026-11-12 尹姣定表实测，
    # 结尾停在 "values": ["自研），_extract_json 只能从残骸里抢出十几个字段，
    # 用户看到的就是「一张表只生成了 18 个字段」。
    # 每个字段约 120~200 token（编码+名称+类型+候选值），按 240 留余量；
    # 另加 2000 的推理余量 —— deepseek-flash 一次推理就能烧 1800+ token。
    budget = max(8000, min(32000, 4000 + n_expect * 240))
    msgs = [
        {"role": "system", "content": ai_bo.SYSTEM + SHEET_EXTRA + known},
        {"role": "user", "content": user},
    ]
    text, used = llm.chat_any(_fast_cfg(cfg), msgs, timeout=timeout,
                              max_tokens=budget)

    try:
        spec = ai_bo._extract_json(text)
    except json.JSONDecodeError:
        # 截断/残缺时重试一次：预算翻倍，并明确要求输出完整 JSON。
        # 只有这一轮还失败才把「不是合法 JSON」抛给用户。
        text2, used2 = llm.chat_any(_fast_cfg(cfg), [
            *msgs,
            {"role": "assistant", "content": text[-2000:]},
            {"role": "user", "content":
                f"你上一次的输出在第 {len(text)} 个字符处被截断了，不是合法 JSON。"
                f"请重新输出**完整**的 JSON（共 {n_expect} 个字段，一个都不能少）；"
                f"字段多就写得紧凑些（note 留空），不要省略任何字段。"},
        ], timeout=timeout, max_tokens=min(32000, budget * 2))
        text, used = text2, used2
        spec = ai_bo._extract_json(text)

    # 字段数核对 + 缺失补齐：截断修复后仍可能少字段（模型偷懒）。
    # 缺了就再发一轮，只要缺失字段的 JSON，合并进 spec。
    def _spec_names(sp: dict) -> set[str]:
        return {(f.get("name") or "").strip()
                for f in (sp.get("fields") or (sp.get("bo") or {}).get("fields") or [])}

    _got = _spec_names(spec)
    if n_expect and len(_got) < n_expect:
        _labels: list[str] = []
        for sh in summary["sheets"]:
            for f in sh.get("fields") or []:
                _labels.append(f.get("label") or "")
            for c in sh.get("columns") or []:
                _labels.append(c.get("header") or "")
        _missing = [x for x in _labels if x and x not in _got]
        if _missing:
            text3, _ = llm.chat_any(_fast_cfg(cfg), [
                {"role": "system", "content": ai_bo.SYSTEM + SHEET_EXTRA + known},
                {"role": "user", "content":
                    user + "\n\n===== 补充任务 =====\n"
                    f"你之前只输出了 {len(_got)} 个字段，下面这些字段漏掉了：\n"
                    + "、".join(_missing)
                    + "\n\n请**只**输出这些缺失字段，格式为 {\"fields\": [...]}，"
                      "结构与之前一致（含 code/name/type/values/sourceColumn），"
                      "不要重复已输出的字段。"},
            ], timeout=timeout, max_tokens=min(16000, 2000 + len(_missing) * 240))
            try:
                extra = ai_bo._extract_json(text3)
                extra_fields = extra.get("fields") or (extra.get("bo") or {}).get("fields") or []
                have = _spec_names(spec)
                added = [f for f in extra_fields
                         if (f.get("name") or "").strip() not in have]
                if added:
                    (spec.setdefault("fields", []) or spec["fields"]).extend(added)
            except json.JSONDecodeError:
                pass

    # 概要里已经抽出的「级联父字段 / 父→子分组」直接盖到 spec 上：
    # 模型拍平或漏写 cascadeParent 时，这一步仍然能把级联关系补回来。
    _cascade_notes: list[str] = []
    _apply_cascade_hints(spec, summary, _cascade_notes)

    # values（候选值）在 build_bo 里统一落到 choices，这里不再单独处理
    bo, notices = ai_bo.build_bo(spec, pool, strict_objects=True)
    notices = _cascade_notes + notices
    notices.extend(_avoid_dict_key_clash(bo))
    # 级联字段的分组候选值若被模型拍平，单独补一轮归属映射
    notices.extend(ai_bo.fix_cascade_groups(bo, cfg))
    # 表格字段只判了类型、没给列：按需求再问一次，把列名补出来
    # （用户 2026-11-17：「表格里面的列名你不会帮我添加吗」）
    notices.extend(ai_bo.fill_table_columns(bo, cfg, hint=hint))

    # 把「来源列」还给前端，预览时能对照原表
    src = {}
    for raw in (spec.get("fields") or []):
        sc = (raw.get("sourceColumn") or "").strip()
        if sc:
            src[(raw.get("code") or "").strip()] = sc[:60]
            src[(raw.get("name") or "").strip()] = sc[:60]

    dic_missing = [f.name for f in bo.fields if f.type_new == "dic" and not f.choices]
    if dic_missing:
        notices.append("这些字段判成了选项但没取到候选值，预览里可手动补：" + "、".join(dic_missing[:5]))

    return {
        "ok": True,
        "bo": bo,
        "notices": notices,
        "model": used.get("model", ""),
        "seconds": round(time.time() - t0, 2),
        "sourceColumns": src,
    }


def cross_sheet_dupes(parsed: list[dict]) -> list[dict]:
    """同一个 Excel 里，不同 sheet 之间的疑似重复字段。

    要解决的是：一份需求汇总表几十个 sheet，同一个东西在 A 表写「立项申请单位」、
    在 B 表写「立项申报单位」。app 一次只分析一张 sheet，这个重复永远看不出来，
    所以在读表阶段（还没调模型、零成本）先比一遍。

    标准模板那几个配置表（1_业务对象 / 2_字段 …）列名天然重复，直接跳过。
    """
    from . import spec
    from .fieldcheck import near_level

    skip = set(spec.SHEET_ORDER) | {"说明"}
    per_sheet: list[tuple[str, list[str]]] = []
    for item in parsed:
        name = item.get("name") or ""
        if name in skip:
            continue
        if item.get("kind") == "form":
            names = [str(f.get("label") or "").strip() for f in item.get("fields") or []]
        else:
            names = [str(c).strip() for c in item.get("columns") or []]
        names = [n for n in names if n]
        # 字段太少的表（两三个列）比对意义不大，反而容易撞词
        if len(names) >= 3:
            per_sheet.append((name, names))

    out: list[dict] = []
    for i, (na, fa) in enumerate(per_sheet):
        for nb, fb in per_sheet[i + 1:]:
            for a in fa:
                for b in fb:
                    # 长度差太多的一定不是同一个，先挡掉，省掉大部分相似度计算
                    if abs(len(a) - len(b)) > 3:
                        continue
                    hit = near_level(a, b)
                    if not hit:
                        continue
                    level, sim = hit
                    out.append({
                        "level": level,
                        "sim": round(sim, 3),
                        "a": a, "sheetA": na,
                        "b": b, "sheetB": nb,
                    })
    out.sort(key=lambda d: -d["sim"])
    return out[:20]
