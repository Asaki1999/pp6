"""编排：Project -> 一组可直接导入的 zip。"""
from __future__ import annotations

import copy
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import blueprint, gen_bo, gen_form, gen_tpl, gen_workflow, modules as mod_def
from .gen_form import Snippets
from .ids import new_id
from .model import (
    FORM_DETAIL,
    PROCESS_FIELDS,
    WF_STATE_OPTIONS,
    Bo,
    BoField,
    Form,
    FormField,
    Project,
    table_name_for,
)

# 系统必备字段（样例导出中始终存在；缺失会导致表结构不完整）
#
# 系统新建对象时自带 16 个默认字段（对象属性 › 默认字段页签，2026-11-13 用户截图）：
#   名称(name) + id/tenantId/ip/createBy/createTime/updateBy/updateTime/
#   deleted/version/baselineVersion/isDraft + 流程四件套(processNode/
#   processHandler/processDefId/processInstanceId，仅流程驱动对象落库)
# 这里补的是「对象名称」等基础 12 个；流程四件套在 ensure_process_fields
# （flow_driven 对象）里补。
#
# 顺序照「推送工作区」导出包里的 sn 排列（2026-11-12 用 4 个原生包核对）：
#   id → tenantId → ip → createBy → createTime → updateBy → updateTime
#     → deleted → version
# 注意 **updateBy 在 updateTime 前面**——之前写成 updateTime→updateBy，
# 字段顺序与系统产物不一致，界面上「更新人/更新时间」两列会反过来。
#
# ⚠ 规格以官方导出包为准，不照抄默认字段页签的显示值：
#   createBy/updateBy 页签显示 24，官方包实际 64；isDraft 页签显示
#   草稿/字符串10，官方包实际「是否草稿」varchar(1)；processNode 页签
#   显示 300，官方流程对象实际 1000。长度不一致会导致导入后列宽与
#   系统建的对象对不上。
SYSTEM_FIELDS: list[dict] = [
    # 名称：系统新建对象的第一个默认字段（截图实测 varchar(10)）。
    # 用户字段表里自己定义了「名称」时（编码自动生成 name）会自动跳过，
    # 以用户的定义为准（如 project_create 的「项目名称」是 varchar 200）。
    {"code": "name", "name": "名称", "type_new": "varchar", "length": 10},
    {"code": "id", "name": "主键", "type_new": "varchar", "length": 64, "is_index": True},
    {"code": "tenantId", "name": "租户ID", "type_new": "varchar", "length": 64, "is_index": True},
    {"code": "ip", "name": "IP地址", "type_new": "varchar", "length": 15},
    {"code": "createBy", "name": "创建人", "type_new": "user", "length": 64, "is_index": True},
    {"code": "createTime", "name": "创建时间", "type_new": "datetime"},
    {"code": "updateBy", "name": "更新人", "type_new": "user", "length": 64, "is_index": True},
    {"code": "updateTime", "name": "更新时间", "type_new": "datetime"},
    # deleted 线上是 varchar(1)，存 Y/N。之前写 20，建出来的列比系统宽 19 倍，
    # 已有表结构对不上时导入端会按「字段已存在」跳过，不报错但列宽是错的。
    {"code": "deleted", "name": "数据删除状态", "type_new": "varchar", "length": 1, "is_index": True},
    {"code": "version", "name": "版本", "type_new": "number", "length": 20},
    # 下面两个是 2026-10 对照线上系统补的：实测 project/test_plan 等对象的
    # 字段里都有，缺了导入后基线/草稿功能不可用。
    # 名称沿用官方包（是否草稿 varchar(1)）；页签显示的「草稿/10」是模板展示值。
    {"code": "baselineVersion", "name": "基线版本", "type_new": "number", "length": 20},
    {"code": "isDraft", "name": "是否草稿", "type_new": "varchar", "length": 1},
]


@dataclass
class Artifact:
    kind: str          # 业务对象 / 表单 / 列表 / 数据集 / 筛选器
    key: str
    zip_path: Path
    entry: str
    bytes: int
    module: str = ""   # modules.py 里的模块编码（bo/form/list/dataset/filter）
    xml: str | None = None   # 单条 XML 原文，打模块包时用（避免 zip 套 zip）


@dataclass
class Deliverable:
    """真正交到用户手上的一个文件（按模块打包 / 整包）。"""
    module: str
    label: str
    filename: str
    path: Path
    items: list[Artifact]
    menu: str = ""

    @property
    def bytes(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0


@dataclass
class BuildResult:
    artifacts: list[Artifact] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    deliverables: list[Deliverable] = field(default_factory=list)
    codes: list[str] = field(default_factory=list)
    # 包名用的业务标签（如「采购申请」「采购申请等 3 个对象」），
    # 由 build() 统一算一次，下载文件名也复用它
    biz_label: str = ""

    def by_kind(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in self.artifacts:
            out[a.kind] = out.get(a.kind, 0) + 1
        return out

    def of_module(self, code: str) -> list[Artifact]:
        return [a for a in self.artifacts if a.module == code]


def ensure_process_fields(bo: Bo) -> list[str]:
    """手册：流程驱动的对象必须冗余 4 个字段，流程流转时反写。"""
    if not bo.flow_driven:
        return []
    added = []
    have = {f.code for f in bo.fields}
    sn = max([f.sn for f in bo.fields], default=0)
    for code, name, length in PROCESS_FIELDS:
        if code in have:
            continue
        sn += 1
        bo.fields.append(BoField(
            bo_code=bo.code, code=code, name=name, type_new="varchar",
            length=length, sn=sn,
        ))
        added.append(code)
    return added


# 平台自建的表（pp_ 开头、非本项目生成的对象）不带基线/草稿字段。
# 给它们硬补 baselineVersion / isDraft，导入时字段对不上原对象结构。
_PLATFORM_CODE_HINT = ("pp_equipment", "test_case", "pp_directory", "iform_")


# 流程状态的 4 个选项（顺序即字典 sn，短码由它算出，**不能改序**）。
# 唯一来源是 model.WF_STATE_OPTIONS：gen_workflow.state_codes() 也用同一份，
# 否则「对象里的字典 key」与「流程里的 BusinessState」会是两套短码。
PROCESS_STATE_OPTIONS = list(WF_STATE_OPTIONS)
PROCESS_FIELD_CODES = {code for code, _name, _length in PROCESS_FIELDS} | {"processState"}


def ensure_default_workflow(project) -> list[str]:
    """流程驱动对象没配流程时，自动补一条「开始 → 审批 → 通过」。

    为什么要有这个：这个 app 的主业是生成**业务对象和表单**。流程是必须的
    （对象要有 processState、待办要有个入口），但真实流程的分支、会签、
    条件处理人应该由项目经理在系统「流程设计」里拖，不该逼人在 Excel 里
    配三张表。所以默认给最小三步，够跑通、够生成合规的 XML，剩下的在线上改。

    只补「流程驱动但一条流程都没有」的对象；用户自己填了 9_流程 的对象
    一律不动（哪怕只填了一个节点）。

    审批人不填：``_handler_chain`` 会出成「提交时人工选择下一处理人」，
    validate 对 ``auto`` 流程只警告，不拦出包。
    """
    from .model import WF_ONGOING, WF_PASSED, WF_START, WfEdge, WfNode, Workflow

    have = {w.bo_code for w in project.workflows}
    made: list[str] = []
    for bo in project.bos:
        if not bo.flow_driven or bo.code in have:
            continue
        wf = Workflow(
            name=f"{bo.name or bo.code}审批",
            bo_code=bo.code,
            module_id=bo.module_id or project.module_id,
            category_id=project.wf_category_id,
            auto=True,
            nodes=[
                WfNode(wf="", name="开始", kind=WF_START, id=1),
                WfNode(wf="", name="审核", kind=WF_ONGOING, id=2),
                WfNode(wf="", name="通过", kind=WF_PASSED, id=3),
            ],
            # 不写流转：生成器会按节点行序自动串成直线（开始→部门审批→通过），
            # 少一处要维护的默认值
            edges=[],
        )
        for n in wf.nodes:
            n.wf = wf.name
        project.workflows.append(wf)
        made.append(f"{bo.code} → 流程「{wf.name}」（开始 → 审核 → 通过）")
    return made


def ensure_workflow_fields(bo: Bo) -> list[str]:
    """流程驱动且本包里有流程时，再补第 5 个流程字段 processState（流程状态，dic）。

    为什么另开一个函数、而不改 ``ensure_process_fields`` 的默认：
    ``tests/test_reader.py::RDR22`` 直接调后者并断言 ``len(added) == 4``，
    把默认改成 5 会连别人的用例一起改；而「这个对象有没有流程」是 Project 级信息，
    Bo 自己不知道，所以判据留在调用方（builder.build）。

    必须是 **dic + choices**：``gen_bo._dict_blocks`` 只认 dic 字段，
    少了 choices 会出一个空字典，流程的 BusinessState 就换不出短码。
    """
    if not bo.flow_driven:
        return []
    if bo.field_by_code("processState") is not None:
        return []
    sn = max([f.sn for f in bo.fields], default=0) + 1
    bo.fields.append(BoField(
        bo_code=bo.code, code="processState", name="流程状态", type_new="dic",
        length=64, sn=sn, choices=list(PROCESS_STATE_OPTIONS),
    ))
    return ["processState"]


def ensure_system_fields(bo: Bo) -> list[str]:
    added = []
    have = {f.code for f in bo.fields}
    is_platform = any(bo.code.startswith(h) for h in _PLATFORM_CODE_HINT)
    sn = max([f.sn for f in bo.fields], default=0)
    for spec_ in SYSTEM_FIELDS:
        if spec_["code"] in have:
            continue
        if is_platform and spec_["code"] in ("baselineVersion", "isDraft", "ip", "name"):
            continue          # 平台自建对象没有这几个字段（实测 pp_equipment 就不带）
        sn += 1
        bo.fields.append(BoField(
            bo_code=bo.code,
            code=spec_["code"],
            name=spec_["name"],
            type_new=spec_["type_new"],
            length=spec_.get("length"),
            is_index=bool(spec_.get("is_index")),
            sn=sn,
            is_system=True,
        ))
        added.append(spec_["code"])
    return added


def _ensure_link_control(project: Project, bo: Bo, field: BoField) -> int:
    """让该对象的每张表单都有「所属项目」下拉控件，返回新增条数。

    两条路径共用这里（新增 projectId / 复用用户已有的 proj 字段）：
      - 只补字段不补控件 → 用户在新建/详情表单里根本选不到项目（实测抓到的坑：
        已有 proj 字段的对象出包后表单里既没有 proj 也没有 projectId 控件）；
      - 去重只看控件 name == 字段编码 → 幂等：同一 Project 连续 build 两次
        （预览 + 出包，或确认版 Excel）不会 append 第二个同名控件。

    value_source 先填对象编码（project），随后 blueprint._ensure_value_sources
    会把它接到系统已有的 project 值来源数据集（没有才新建 project_value）。
    """
    added = 0
    for form in project.forms:
        if form.bo_code != bo.code:
            continue
        if any(x.name == field.code for x in form.fields):
            continue
        form.fields.append(FormField(
            form_key=form.key,
            name=field.code,
            label=field.name,
            field_type="select",
            value_source=field.object_type or "project",
        ))
        added += 1
    return added


def ensure_project_links(project: Project) -> list[str]:
    """给勾了「关联项目」的对象补「所属项目」链接字段（+ 表单控件）。

    为什么补字段放在 builder 而不是 projlink.apply（架构 D3）
    --------------------------------------------------------
    ① `sn` 必须排在**系统字段与流程字段之后**（PRD A2：链接字段不覆盖、不重号），
       而系统字段是 builder 补的，apply 阶段还看不到它们；
    ② `apply` 的边界是「对齐 + 改名 + 重写引用」，补字段会破坏这条边界；
    ③ 出包入口有 4 个（web 整包 / 单模块 / CLI / 确认版 Excel），都走 builder，
       钩子挂在这里不会漏。

    幂等：已有指向 project 的 object 字段时只记一条 notice（复用，不新增）。
    """
    from . import projlink

    notices: list[str] = []
    if not any(getattr(bo, "project_link", False) for bo in project.bos):
        return notices
    snap = projlink.load_snapshot()
    pool = snap.pool()
    for bo in project.bos:
        if not getattr(bo, "project_link", False):
            continue
        if getattr(bo, "skip_link_field", False):
            # 决策里明确说「只对齐、不补『所属项目』」——尊重它，别硬塞字段
            continue
        plan = projlink.plan_link_field(bo, pool)
        if plan.action == "reject_self":
            raise ValueError(f"业务对象 {bo.code}: 对象 project 不能关联自己（自关联不允许）")
        if plan.action == "conflict":
            raise ValueError(
                f"业务对象 {bo.code}: 物理列 {projlink.LINK_FIELD_NAME} 已被字段 "
                f"{plan.code}（{plan.name}）占用且未关联项目，请先改字段名或改用现有字段")
        if plan.action == "reuse":
            # 复用的字段也要有表单控件：已有 object 字段（如 proj）说明用户
            # 已经在用这个关联，但表单里可能压根没放控件——不补的话在建/详情
            # 表单里选不到项目（复验时抓到的坑）。
            existing = bo.field_by_code(plan.code)
            notices.append(f"业务对象 {bo.code}: {plan.note}")
            if existing is not None:
                if _ensure_link_control(project, bo, existing):
                    notices.append(
                        f"业务对象 {bo.code}: 已给复用的链接字段 {existing.code}"
                        f"（{existing.name}）补上表单下拉控件")
            continue
        if bo.field_by_code(projlink.LINK_CODE) is not None:
            # 幂等：parse 阶段已经补过一次（同一份 Excel 先预览再出包），
            # 这次只补缺失的表单控件，不再追加第二个同编码字段
            _ensure_link_control(project, bo, bo.field_by_code(projlink.LINK_CODE))
            continue
        sn = max([f.sn for f in bo.fields], default=0) + 1
        field = projlink.link_field(name=plan.name, sn=sn)
        field.bo_code = bo.code
        bo.fields.append(field)
        notices.append(
            f"业务对象 {bo.code}: 关联项目，已补链接字段 {field.code}"
            f"（{field.resolved_field_name()} varchar 64，sn={sn}）")
        # 已有表单也要看得见「所属项目」下拉，否则用户在新建/详情表单里选不到项目
        _ensure_link_control(project, bo, field)
    # 幂等：把 select 控件的取值来源接到 project_value（命中系统已有则复用）
    notices.extend(blueprint._ensure_value_sources(project))
    return notices


def _write_zip(out_dir: Path, folder: str, entry_name: str, content: str) -> tuple[Path, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    entry = f"{folder}/{entry_name}"
    zp = out_dir / f"{entry_name.replace('.xml', '')}.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(entry, content.encode("utf-8"))
    return zp, entry


def _write_plain(out_dir: Path, filename: str, content: str) -> Path:
    """把**裸文件**（流程定义 XML）落盘，返回它的路径。

    流程是裸 XML，不能套 zip：系统的流程导入入口直接吃这份 XML。
    与 ``_write_zip`` 分开放，是因为两者的产物类型不同——合在一起以后
    一定会有人给流程包上 zip，那就白做了。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    path.write_text(content, encoding="utf-8")
    return path


_BAD_NAME = re.compile(r'[\\/:*?"<>|\s]+')


def biz_label(project: Project) -> str:
    """包名里的业务标签：一个对象用对象名，多个用「首个对象等 N 个对象」。

    名字只用来认文件，不影响导入，所以取最直白的那一个：
    用户下载下来一眼能看出是「采购申请」还是「项目立项」。
    """
    names = [b.name.strip() for b in project.bos if (b.name or "").strip()]
    if not names:
        return "导入包"
    if len(names) == 1:
        return names[0]
    return f"{names[0]}等{len(names)}个对象"


def safe_filename(text: str) -> str:
    """去掉文件名里不能用的字符（对象名里可能有斜杠、空格）。"""
    return _BAD_NAME.sub("_", text).strip("_") or "导入包"


def package_name(label: str, part: str, stamp: str = "") -> str:
    """统一的包名规则：{业务标签}-{内容}-{日期}.zip。

    例：采购申请-导入包-20261110.zip / 采购申请-表单-20261110.zip
    """
    bits = [safe_filename(label), part]
    if stamp:
        bits.append(stamp)
    return "-".join(bits) + ".zip"


_ENTRY_SN = re.compile(r"^\d{3,}-")


def entry_slug(entry: str, prefix: str = "") -> str:
    """包内条目名 → 去掉序号先后的文件名（`bo/001-project.xml` → `project.xml`）。

    系统导出包的命名规则是「前缀-编码.xml」，序号在**一个 zip 内唯一**：
    `多个对象整体导出.zip` 里是 bo/001、bo/002、bo/003、form/004、data_template/005
    ——同目录不重复，还跨目录接着往下数。
    所以重排序号时只能取编码部分，否则会拼出 `form/001-create.xml`、
    `form/001-detail.xml` 这种重号（zip 允许同名条目，导入端只会认其中一个，
    等于静默丢文件）。

    prefix：中间产物实际用的前缀（来自 `0_项目设置 › 文件前缀`）。用户可能填
    `pp6` 这种非数字前缀，此时必须按它整段剥掉，否则会拼出
    `bo/001-pp6-project.xml` 这种双前缀；剥完再用 `\\d{3,}-` 兜底（前缀改过、
    或条目来自不认前缀的老包时仍能去掉序号）。
    """
    name = Path(entry).name
    head = (prefix or "").strip().rstrip("-")
    # 前缀和序号谁在前都可能（正常是 `pp6-project.xml`、老包是 `001-project.xml`），
    # 两轮剥：先剥前缀再剥序号，谁先命中都能收敛到编码本身。
    for _ in range(2):
        if head and name.startswith(f"{head}-"):
            name = name[len(head) + 1:]
        name = _ENTRY_SN.sub("", name)
    return name


def unique_entry(used: set[str], entry: str) -> str:
    """保证包内条目名不重复；真撞名（两条产物同一个编码）时补 `__2`/`__3` 后缀。

    zip 格式本身允许同名条目，写进去不会报错，导入时按名字取文件就会丢内容，
    所以这里宁可加后缀也要让它唯一。

    后缀**不能带连字符**：反解侧（`inspect`、`tools/compare.py`、
    `tools/capture_snippets.py`）用 `stem.split("-", 1)[-1]` 取编码，
    补 `-2` 会被读成 `code-2` 这种不存在的编码，`code__2` 至少不会伪装成编码。
    """
    if entry not in used:
        used.add(entry)
        return entry
    path = Path(entry)
    sn = 2
    candidate = f"{path.parent.as_posix()}/{path.stem}__{sn}{path.suffix}"
    while candidate in used:
        sn += 1
        candidate = f"{path.parent.as_posix()}/{path.stem}__{sn}{path.suffix}"
    used.add(candidate)
    return candidate


def _pack_module(out: Path, code: str, items: list[Artifact], prefix: str,
                 name: str | None = None) -> Path:
    """把一个模块的所有 XML 合成**一个** zip——系统导入是按模块来的，
    一次导一个包，省得用户对着上百个文件逐个点上传、还容易传错菜单。

    注意要写 XML 原文：单条产物本身已经是 data_template/xxx.xml 这样的一层 zip，
    直接把那个 zip 塞进去会变成 zip 套 zip，系统解不出来。

    条目名重排连续序号（001/002/003…），与系统自己导出的包一致：
    真实包 `项目列表.zip` → data_template/001-project_list_table.xml，
    勾多个同类内容导出时同样是 001、002、003 往下数，没有重号。
    """
    mod = mod_def.of(code)
    out.mkdir(parents=True, exist_ok=True)
    target = out / (name or f"{prefix}-{mod.label}.zip")
    used: set[str] = set()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for sn, a in enumerate(items, 1):
            folder = Path(a.entry).parent.as_posix()
            entry = unique_entry(used, f"{folder}/{sn:03d}-{entry_slug(a.entry, prefix)}")
            if a.xml is None:
                with zipfile.ZipFile(a.zip_path) as src:
                    z.writestr(entry, src.read(a.entry))
            else:
                z.writestr(entry, a.xml.encode("utf-8"))
    return target


def a_module(a: Artifact) -> str:
    """一条产物属于哪个模块（bo/form/list/dataset/filter/workflow）。"""
    if a.module:
        return a.module
    return mod_def.code_of_kind(a.kind)


def _pack_all(out: Path, res: BuildResult, prefix: str, name: str | None = None) -> Path | None:
    """整包：把各模块的 XML 合进**同一个** zip。

    平台支持一次导入多个模块（开发 2026-10-28 确认）：
    包里按目录区分 bo/ form/ data_template/，序号跨目录连续编号，
    和系统自己导出的 `多个对象整体导出.zip` 结构完全一致：

        bo/001-test_case_base_category.xml
        bo/002-sjzcscd.xml
        bo/003-pp_equipment.xml
        form/004-project_create_start.xml
        data_template/005-customer_list.xml

    流程是裸 XML，不进这个包（系统的流程导入是另一个入口）。
    """
    # 先算「真能进包的产物」再决定要不要建目录：
    # 只看 res.artifacts 非空是不够的——只选「流程」时 artifacts 里有流程产物，
    # 但循环里全被 continue 掉，结果会留下一个空 zip（已由下面的 written 兜住），
    # 顺带还留下一个空的 out/整包/ 目录。目录也不该留：用户看到空目录会以为漏了文件。
    packable = [a for a in res.artifacts if a_module(a) != "workflow"]
    if not packable:
        return None
    out.mkdir(parents=True, exist_ok=True)
    target = out / (name or f"{prefix}-整包（全部模块）.zip")
    sn = 0
    used: set[str] = set()
    # 实际写进去的条目数。**只看 res.artifacts 非空是不够的**：只选「流程」时
    # artifacts 里有流程产物，但下面的循环把它 continue 掉了，
    # 结果会产出一个 0 条目的空 zip（解压是空的，导入端只会说包有问题）。
    written = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        # 按真实导出包的顺序写：bo → form → data_template（业务对象必须最先，
        # 表单/列表按表名找它）。`多个对象整体导出.zip` 就是
        # bo/001-003、form/004、data_template/005 —— data_template 排在 form 之后，
        # 服务端是否依赖这个顺序未知，但既然真实包如此，跟着走更稳。
        order = {"bo": 10, "form": 20, "list": 30, "dataset": 30, "filter": 30}
        for a in sorted(res.artifacts, key=lambda x: order.get(a_module(x), 99)):
            mod = a_module(a)
            if mod == "workflow":
                continue
            folder = mod_def.of(mod).folder
            sn += 1
            written += 1
            entry = unique_entry(used, f"{folder}/{sn:03d}-{entry_slug(a.entry, prefix)}")
            if a.xml is not None:
                z.writestr(entry, a.xml.encode("utf-8"))
            else:
                with zipfile.ZipFile(a.zip_path) as src:
                    z.writestr(entry, src.read(a.entry))
    if not written:
        # 空 zip 比没有包更糟：用户会当成「生成成功了」拿去导入
        target.unlink(missing_ok=True)
        return None
    return target


def _deliver(res: BuildResult, out: Path, prefix: str, codes: list[str],
             label: str = "", stamp: str = "") -> None:
    """按模块出包；只选了一个模块时不再套一层文件夹说明，直接给单包。

    分流依据是 ``Module.zipped``（modules.py 已经把这个语义建模过了），
    **不要**写 ``if code == "workflow"``：以后再有裸 XML 模块，那一行就会漏。
    """
    for code in codes:
        items = res.of_module(code)
        if not items:
            continue
        mod = mod_def.of(code)
        if not mod.zipped:
            # 裸 XML：文件在生成阶段就已经写好了（out/workflow/{流程名}.xml），
            # 这里只包成 Deliverable，**不再写一遍**——
            # 两处各写一次文件名/编码规则迟早漂移（中文名、去字符、加后缀…）。
            for a in items:
                res.deliverables.append(Deliverable(
                    module=code, label=mod.label, filename=a.zip_path.name,
                    path=a.zip_path, items=[a], menu=mod.menu))
            continue
        name = package_name(label, mod.label, stamp) if label else None
        packed = _pack_module(out, code, items, prefix, name)
        res.deliverables.append(Deliverable(
            module=code, label=mod.label, filename=packed.name, path=packed,
            items=items, menu=mod.menu,
        ))


def slice_by_mode(project: Project, flow: bool) -> Project:
    """按对象「选择模式」切一刀：``flow=True`` 只留流程对象，``False`` 只留普通对象。

    为什么要切：流程对象在系统里是流程模式（bo XML 的 ``mode="workflow"``），
    要和流程定义配套导入，所以走独立的一包；同步包里只放普通对象。
    切完两边各自跑同一套 ``build()``，比在 build 内部到处加分支干净。

    连带规则（少带一样导入端就会报「找不到对象」）：
      - 表单、列表按 ``bo_code`` 跟着对象走；
      - 数据集没有 bo_code，按 ``from_table`` 对对象的编码或物理表名；
      - 流程定义只留在流程那一侧，普通对象不该带流程；
      - 表单脚本按表单 key 过滤。

    返回浅拷贝：Bo/Form 实例是共享的，build() 里对对象的修补（补 tbl_name、
    补 options）对原 Project 同样可见——同一份 Excel 连续出两个包时不用重读。
    """
    keep = [b for b in project.bos if bool(b.flow_driven) is bool(flow)]
    codes = {b.code for b in keep}
    tables = {(b.tbl_name or "").lower() for b in keep if b.tbl_name}

    out = copy.copy(project)
    out.bos = keep
    out.forms = [f for f in project.forms if f.bo_code in codes]
    form_keys = {f.key for f in out.forms}
    out.lists = [l for l in project.lists if l.bo_code in codes]
    out.datasets = [
        d for d in project.datasets
        # from_table 在 Excel 里可能填编码、在原生包里是物理表名，两种都认
        if (d.from_table or "").lower() in tables or (d.from_table or "") in codes
    ]
    out.workflows = [w for w in project.workflows if w.bo_code in codes] if flow else []
    out.form_scripts = {k: v for k, v in project.form_scripts.items() if k in form_keys}
    return out


# 反解旧包后只改了「对象编码」，表单 / 列表的 key 还带着旧编码，是这次
# 「列表报错」的根因（2026-11-15 对着 8112 真实只读库核对）：
#   新对象 project_test_1115 的包里仍是 create_project_create / project_create_list，
#   导入时系统按 key 更新，把原对象「项目立项信息列表」的 dataset_key_/bo_code_
#   改成了新对象；库里没有 project_test_1115_list，列表页按新对象取模板就抛
#   「未找到相关数据模板信息」。
# 判据做成纯函数，避免误伤：只有 key 符合平台标准派生模式、且其中取出的编码
#   既不等于当前对象编码、也不在本包任何对象编码里，才判定为骨架残留。
# 同一个对象做修订（编码不变）、自定义短 key（cs / fxcs）、多对象包里
#   指向别的在包对象的 key，一律保持原样（BO25 的「原地更新」语义不动）。
_STALE_KEY_PATTERNS = (
    re.compile(r"^(?:create|detail|import)_(.+)$"),
    re.compile(r"^fetch_basic_(.+)_information$"),
    re.compile(r"^(.+)_(?:list|details)$"),
)


def stale_code_in_key(key: str) -> str:
    """标准派生 key 里带的旧对象编码；不是标准模式返回空串。"""
    for pat in _STALE_KEY_PATTERNS:
        m = pat.match(key or "")
        if m:
            return m.group(1)
    return ""


def normalize_derived_keys(project: Project) -> list[str]:
    """对象编码改了之后，把残留旧编码的表单 / 列表 key 重算到当前对象。

    同时把所有以表单 key 为外键的引用（控件、隐藏规则、脚本、对象 options、
    流程节点）一起改掉，并清掉「按更新导入」用的 exist_key / exist_id：
    派生新对象时若还带着旧 id / 旧模板 key，导入会去覆盖线上旧模板。
    返回的是给用户看的提示，出包时进 notices。
    """
    codes = {bo.code for bo in project.bos}
    notices: list[str] = []
    form_map: dict[str, str] = {}
    affected: set[str] = set()

    for bo in project.bos:
        for f in project.forms:
            if f.bo_code != bo.code:
                continue
            old = stale_code_in_key(f.key)
            if not old or old == bo.code or old in codes:
                continue
            new_key = f.key.replace(old, bo.code, 1)
            tpl = blueprint._NAME_TEMPLATE.get(f.kind)
            form_map[f.key] = new_key
            f.key = new_key
            f.name = tpl.format(obj=bo.name) if tpl else f.name.replace(old, bo.code)
            f.exist_key = ""
            affected.add(bo.code)
            notices.append(f"表单 key 还带着旧对象编码 {old}，已改为 {new_key}"
                           "（新对象不该复用旧模板 key，否则导入会覆盖线上同名模板）")
        for lst in project.lists:
            if lst.bo_code != bo.code:
                continue
            old = stale_code_in_key(lst.key)
            if not old or old == bo.code or old in codes:
                continue
            new_key = lst.key.replace(old, bo.code, 1)
            lst.key = new_key
            lst.name = f"{bo.name}列表"
            lst.exist_key = ""
            affected.add(bo.code)
            notices.append(f"列表 key 还带着旧对象编码 {old}，已改为 {new_key}"
                           "（新对象不该复用旧模板 key，否则导入会覆盖线上同名模板）")

    if not form_map and not affected:
        return notices

    def fix(value: str) -> str:
        return form_map.get(value, value)

    for f in project.forms:
        for ff in f.fields:
            ff.form_key = fix(ff.form_key)
        for rule in f.hide_rules:
            rule.form_key = fix(rule.form_key)
    project.form_scripts = {fix(k): v for k, v in project.form_scripts.items()}
    for bo in project.bos:
        if bo.code in affected:
            bo.exist_id = ""
        # bo.options 里可能是一整段 JSON（如 struType=list 的 formKey），
        # 不能只做精确相等替换，得按子串换。
        for k, v in list(bo.options.items()):
            if not isinstance(v, str):
                continue
            nv = v
            for old_key, new_key in form_map.items():
                nv = nv.replace(old_key, new_key)
            if nv != v:
                bo.options[k] = nv
    for wf in project.workflows:
        for node in wf.nodes:
            node.form_key = fix(node.form_key)
    return notices


def build(project: Project, snippets: dict, out_dir: Path | None = None,
          codes: list[str] | None = None) -> BuildResult:
    """codes：要出包的模块（bo/form/list/dataset/filter）；None = 全部。"""
    res = BuildResult()
    snip = Snippets(snippets)
    out = Path(out_dir or project.out_dir or "out")
    prefix = project.file_prefix or "001"
    # 包名统一成「业务名-内容-日期」，一眼能看出是给哪个业务用的；
    # 日期取本地今天，方便区分同一业务反复导入的版本
    res.biz_label = biz_label(project)
    stamp = datetime.now().strftime("%Y%m%d")
    tenant = project.tenant_id or "-999"
    plan = mod_def.Plan(list(codes or [m.code for m in mod_def.MODULES]))
    res.codes = [m.code for m in plan.modules]
    # 流程：按 9_流程 / 10_流程节点 / 11_流程流转 生成 workflowDefinition（裸 XML）。
    # 「选了流程但表是空的」要说清为什么没产物——以前那句「流程定义请在流程设计里
    # 导出 XML 后原样上传」已经过时了（现在能真生成）。
    if plan.has("workflow") and not project.workflows:
        res.notices.append("选了「流程」但 9_流程 是空的，跳过流程生成")

    # 手册：物理表名 = PP_ + 编码，由系统生成，无需手填
    for bo in project.bos:
        if not bo.tbl_name:
            bo.tbl_name = table_name_for(bo.code)
            res.notices.append(f"业务对象 {bo.code}: 物理表名按规范取 {bo.tbl_name}")

    # 反解旧包改编码后，先把残留旧编码的表单 / 列表 key 归位，
    # 再展开「标准全套」——否则 standard bundle 会按新编码建一套，
    # 而反解来的旧 key 又留着，一个对象出两套互相覆盖的模板。
    res.notices.extend(normalize_derived_keys(project))

    # 先按规范展开「标准全套」表单
    res.notices.extend(blueprint.expand(project))

    # 业务对象 options.formKey 必须指向它的「条件专用表单」，
    # 自动化/条件选择时才能动态取到已配好显示类型的字段
    cond_form = {f.bo_code: f.key for f in project.forms if f.kind == "condition"}
    first_form: dict[str, str] = {}
    for f in project.forms:
        first_form.setdefault(f.bo_code, f.key)
    for bo in project.bos:
        # 反解已有包时保留原值，只补空缺，避免改动线上行为
        if not bo.options.get("formKey"):
            bo.options["formKey"] = cond_form.get(bo.code) or first_form.get(bo.code, "")
        if bo.parent_obj:
            parent_bo = project.bo(bo.parent_obj)
            if parent_bo:
                bo.options["parentName"] = parent_bo.name
            else:
                # 父对象是系统已有对象（不在本包内）时，用对象清单里的中文名，
                # 否则 XML 的 dstObjName 会写编码（project），和线上真实包的「项目」不一致
                try:
                    from . import objects as _objects
                    _lbl = _objects.label(bo.parent_obj)
                    if _lbl:
                        bo.options["parentName"] = _lbl
                except Exception:
                    pass

    # 只生成选中的模块：单独导表单时不必把上百个对象 XML 也跑一遍。
    # 表单靠对象拿物理列名、列表靠对象转字段编码，所以有这两种就顺带生成对象；
    # 对象本身不出包时，不补系统字段（那是给对象 XML 用的）。
    need_bo_rows = plan.has("bo") or plan.has("form") or plan.has("list")

    # 流程驱动但没配流程 → 补一条最小三步。必须放在 need_bo_rows 之外：
    # 只勾「流程」出包时不出对象 XML，但默认流程照样要生成。
    for line in ensure_default_workflow(project):
        res.notices.append(f"自动生成默认流程：{line}（审批人留空，导入后到 管理 › 流程设计 里指定）")

    if need_bo_rows:
        if not project.module_id:
            res.notices.append("未配置默认模块ID，导入前建议在 0_项目设置 里补上，否则对象可能挂不到模块下")
        # 本包里有流程的对象要补 processState（流程状态字典），流程的 BusinessState
        # 靠它换短码；必须在 gen_bo.render() 之前进 bo.fields，否则字典不进 bo XML。
        wf_bos = {w.bo_code for w in project.workflows}
        for bo in project.bos:
            bo.module_id = bo.module_id or project.module_id
            bo.type_id = bo.type_id or project.type_id
            if not bo.type_id:
                res.notices.append(f"业务对象 {bo.code}: 未指定分类ID")
            proc = ensure_process_fields(bo)
            if proc:
                res.notices.append(f"业务对象 {bo.code}: 流程驱动，已补齐冗余字段 {', '.join(proc)}")
            if bo.code in wf_bos:
                extra = ensure_workflow_fields(bo)
                if extra:
                    res.notices.append(
                        f"业务对象 {bo.code}: 有流程，已补流程状态字段 {', '.join(extra)}"
                        f"（选项 待发起/进行中/已完成/已失败，与流程里的 BusinessState 同源）")
            added = ensure_system_fields(bo)
            if added:
                res.notices.append(f"业务对象 {bo.code}: 已自动补齐系统字段 {', '.join(added)}")

        # 关联项目：补「所属项目」链接字段 + 表单控件 + 接 project_value。
        # 位置很关键：进程字段/系统字段都已补完 → 链接字段 sn 必然最大（PRD A2）；
        # 在 objects.resolve_object_fields 之前 → object_type 参与归一；
        # 在详情表单与 gen_bo 之前 → 字段与控件都会被生成。
        for line in ensure_project_links(project):
            res.notices.append(line)

    # 库里已有同名对象 → 用它的 id，导入才是「更新」；否则新 id = 「新增」。
    # 判断依据就是目标库 iform_bo_def.id，填错就会多出一份同名字段的对象。
    #
    # 先把 object 字段的「关联对象/字典」对成真实对象编码，再对表。
    # 这一步不能并到 bind() 里：bind() 开头 `if not catalog(): return`，
    # 没连库/没快照时直接退出，而「留空字段按字段名推断」用的是本包对象，
    # 那种环境下同样要生效。出错也不影响出包，只是字段可能是个空下拉。
    try:
        from . import objects as _objects
        # 读表时已经提示过的（reader 塞进 proj.auto_notes，CLI 会在「自动补齐」段打印），
        # 这里不再重复一遍同样的文案
        told = set(getattr(project, "auto_notes", None) or [])
        for msg in _objects.resolve_object_fields(project):
            if msg in told:
                continue
            res.notices.append(msg)
    except Exception:
        pass

    # 出包前再对表：名字对上了就沿用库里已有的 id / 模板 key
    try:
        from . import objects as _objects
        for msg in _objects.bind(project):
            res.notices.append(msg)
    except Exception:
        pass          # 没接库/查不到就按新建走，不影响出包

    bo_ids: dict[str, str] = {}
    for bo in project.bos:
        if bo.exist_id:
            bo_ids[bo.code] = bo.exist_id
            res.notices.append(
                f"业务对象 {bo.code}: 库里已存在同名对象，沿用原 id {bo.exist_id}（按更新处理）")
        else:
            bo_ids[bo.code] = new_id()

    # 每个对象取第一个表单作为「新建」入口与待办入口
    form_of_bo: dict[str, str] = {}
    for form in project.forms:
        form_of_bo.setdefault(form.bo_code, form.key)

    # ---- 详情表单：每个有表单的对象都补一张「所有字段只读」的详情表单。
    # 用户 2026-11-15 明确要求：普通模式 / 流程模式都生成「新建 + 详情」，
    # 不同场景用不同表单。之前只在列表开了「级联详情字段」时补，AI 生成的对象
    # 只有新建表单，详情场景没表单可用。
    # 已有详情表单（标准全套展开的、或用户手填的）不重复生成。
    detail_of_bo: dict[str, str] = {}
    for f in project.forms:
        if f.kind == FORM_DETAIL:
            detail_of_bo.setdefault(f.bo_code, f.key)
    for bo in project.bos:
        if bo.code in detail_of_bo:
            continue
        if not any(f.bo_code == bo.code for f in project.forms):
            continue
        dkey = f"{bo.code}_details"
        if any(x.key == dkey for x in project.forms):
            detail_of_bo[bo.code] = dkey
            continue
        dform = Form(
            key=dkey,
            name=f"{bo.name}详情",
            bo_code=bo.code,
            kind=FORM_DETAIL,
            form_type="form",
        )
        for x in sorted(bo.fields, key=lambda x: x.sn):
            if x.is_system or x.code in PROCESS_FIELD_CODES:
                continue
            dform.fields.append(FormField(
                form_key=dkey,
                name=x.code,
                label=x.name,
                field_type=blueprint.control_for(x),
                # 同一处真相：dic 的取值来源要分全局/自定义两支
                value_source=x.value_source(),
                placeholder="",
                # 详情表单：全部只读
                readonly=True,
                group="详情",
            ))
        project.forms.append(dform)
        detail_of_bo[bo.code] = dkey

    # ---- 业务对象
    for bo in project.bos if plan.has("bo") else []:
        xml = gen_bo.render(bo, bo_ids[bo.code], log_enable=bo.with_log,
                            todo_form_key=form_of_bo.get(bo.code, ""))
        zp, entry = _write_zip(out / "bo", "bo", f"{prefix}-{bo.code}.xml", xml)
        res.artifacts.append(Artifact("业务对象", bo.code, zp, entry,
                                      len(xml.encode("utf-8")), "bo", xml))

    # ---- 表单
    for form in project.forms if plan.has("form") else []:
        form.type_id = form.type_id or project.type_id
        bo = project.bo(form.bo_code)
        if bo is None:
            res.notices.append(f"表单 {form.key}: 对象 {form.bo_code} 不存在，已跳过")
            continue
        xml, notices = gen_form.render(
            form, bo, bo_ids.get(form.bo_code, ""), snip, tenant,
            # 宽度/样式统一由 model.form_style(kind) 按手册决定
            # （新建 45%、详情紧凑 45%、展开详情/流程详情 90%），这里不再覆盖。
            # form.width 只作为用户显式指定时的逃生口。
            extend_patch=({"width": form.width} if form.width_explicit else {}),
            script=project.form_scripts.get(form.key, ""),
        )
        res.notices.extend(notices)
        zp, entry = _write_zip(out / "form", "form", f"{prefix}-{form.key}.xml", xml)
        res.artifacts.append(Artifact("表单", form.key, zp, entry,
                                      len(xml.encode("utf-8")), "form", xml))

    # ---- 列表
    for lst in project.lists if plan.has("list") else []:
        bo = project.bo(lst.bo_code)
        if bo is None:
            res.notices.append(f"列表 {lst.key}: 对象 {lst.bo_code} 不存在，已跳过")
            continue
        xml, notices = gen_tpl.render_list(lst, bo, form_of_bo.get(bo.code, ""), snippets, tenant,
                                           detail_form_key=detail_of_bo.get(bo.code, ""))
        res.notices.extend(notices)
        zp, entry = _write_zip(out / "data_template", "data_template", f"{prefix}-{lst.key}.xml", xml)
        res.artifacts.append(Artifact("列表", lst.key, zp, entry,
                                      len(xml.encode("utf-8")), "list", xml))

    # ---- 数据集 / 值来源 / 筛选器
    for ds in project.datasets:
        code = mod_def.module_of_dataset(ds.ds_type)
        if not plan.has(code):
            continue
        bo = project.bo(ds.from_table) or next(
            (b for b in project.bos if b.tbl_name == ds.from_table), None
        )
        xml, notices = gen_tpl.render_dataset(ds, bo, snippets, tenant)
        res.notices.extend(notices)
        zp, entry = _write_zip(out / "data_template", "data_template", f"{prefix}-{ds.key}.xml", xml)
        res.artifacts.append(Artifact(mod_def.of(code).label, ds.key, zp, entry,
                                      len(xml.encode("utf-8")), code, xml))

    # ---- 流程（裸 XML，不进 zip：系统的流程导入是另一个入口）
    #
    # 放在这里而不是业务对象循环里：流程要按对象名/表单 key 推导 FormKey 与
    # 节点字段权限，这时 blueprint 已展开、form_of_bo / detail_of_bo 都已就绪。
    if plan.has("workflow"):
        wf_folder = out / mod_def.of("workflow").folder
        for wf in project.workflows:
            bo = project.bo(wf.bo_code)
            if bo is None:
                res.notices.append(f"流程 {wf.name}: 对象 {wf.bo_code} 不存在，已跳过")
                continue
            xml, notices = gen_workflow.render(
                wf, bo, project, snip, gen_workflow.wf_id(wf, bo))
            res.notices.extend(notices)
            if not xml:
                res.notices.append(f"流程 {wf.name}: 没有可生成的节点，已跳过")
                continue
            path = _write_plain(wf_folder, f"{safe_filename(wf.name)}.xml", xml)
            # 裸 XML 没有「包内条目」的概念：zip_path 填文件本身、entry 留空，
            # 这样 Deliverable.bytes（path.stat）、by_kind()、报告里的
            # 「· 流程 xxx」全部照旧可用，不必给 Artifact 加新字段。
            res.artifacts.append(Artifact("流程", wf.name, path, "",
                                          len(xml.encode("utf-8")), "workflow", xml))
            res.notices.append(
                f"流程 {wf.name}: 已生成 {len(wf.nodes)} 个节点，"
                f"文件 {path.name}（导入位置 {mod_def.of('workflow').menu}）")

    _deliver(res, out / "按模块", prefix, res.codes, res.biz_label, stamp)

    # 整包：开发确认平台支持一个 zip 里放多个模块，给一个「一把导入」的文件
    packed_all = _pack_all(out / "整包", res, prefix,
                           package_name(res.biz_label, "导入包", stamp))
    if packed_all:
        res.deliverables.append(Deliverable(
            module="all", label="整包（全部模块）", filename=packed_all.name,
            path=packed_all, items=list(res.artifacts),
            menu="管理 › 业务对象 › 导入（一次全部导入）",
        ))
    return res
