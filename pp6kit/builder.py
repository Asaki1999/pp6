"""编排：Project -> 一组可直接导入的 zip。"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import blueprint, gen_bo, gen_form, gen_tpl, modules as mod_def
from .gen_form import Snippets
from .ids import new_id
from .model import PROCESS_FIELDS, Bo, BoField, Project, table_name_for

# 系统必备字段（样例导出中始终存在；缺失会导致表结构不完整）
SYSTEM_FIELDS: list[dict] = [
    {"code": "id", "name": "主键", "type_new": "varchar", "length": 64, "is_index": True},
    {"code": "tenantId", "name": "租户ID", "type_new": "varchar", "length": 64, "is_index": True},
    {"code": "ip", "name": "IP地址", "type_new": "varchar", "length": 15},
    {"code": "deleted", "name": "数据删除状态", "type_new": "varchar", "length": 20, "is_index": True},
    {"code": "createBy", "name": "创建人", "type_new": "user", "length": 64, "is_index": True},
    {"code": "createTime", "name": "创建时间", "type_new": "datetime"},
    {"code": "updateTime", "name": "更新时间", "type_new": "datetime"},
    {"code": "version", "name": "版本", "type_new": "number", "length": 20},
    {"code": "updateBy", "name": "更新人", "type_new": "user", "length": 64, "is_index": True},
    # 下面两个是 2026-10 对照线上系统补的：
    # 实测 project/test_plan 等对象的字段里都有，缺了导入后基线/草稿功能不可用
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


def ensure_system_fields(bo: Bo) -> list[str]:
    added = []
    have = {f.code for f in bo.fields}
    is_platform = any(bo.code.startswith(h) for h in _PLATFORM_CODE_HINT)
    sn = max([f.sn for f in bo.fields], default=0)
    for spec_ in SYSTEM_FIELDS:
        if spec_["code"] in have:
            continue
        if is_platform and spec_["code"] in ("baselineVersion", "isDraft", "ip"):
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


def _write_zip(out_dir: Path, folder: str, entry_name: str, content: str) -> tuple[Path, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    entry = f"{folder}/{entry_name}"
    zp = out_dir / f"{entry_name.replace('.xml', '')}.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(entry, content.encode("utf-8"))
    return zp, entry


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
    if not res.artifacts:
        return None
    out.mkdir(parents=True, exist_ok=True)
    target = out / (name or f"{prefix}-整包（全部模块）.zip")
    sn = 0
    used: set[str] = set()
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
            entry = unique_entry(used, f"{folder}/{sn:03d}-{entry_slug(a.entry, prefix)}")
            if a.xml is not None:
                z.writestr(entry, a.xml.encode("utf-8"))
            else:
                with zipfile.ZipFile(a.zip_path) as src:
                    z.writestr(entry, src.read(a.entry))
    return target


def _deliver(res: BuildResult, out: Path, prefix: str, codes: list[str],
             label: str = "", stamp: str = "") -> None:
    """按模块出包；只选了一个模块时不再套一层文件夹说明，直接给单包。"""
    for code in codes:
        items = res.of_module(code)
        if not items:
            continue
        mod = mod_def.of(code)
        name = package_name(label, mod.label, stamp) if label else None
        packed = _pack_module(out, code, items, prefix, name)
        res.deliverables.append(Deliverable(
            module=code, label=mod.label, filename=packed.name, path=packed,
            items=items, menu=mod.menu,
        ))


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
    # 流程不在生成器里做（需要流程设计器导出的定义），只提示
    if plan.has("workflow"):
        res.notices.append("流程定义请在「流程设计」里导出 XML 后原样上传，生成器不做流程")

    # 手册：物理表名 = PP_ + 编码，由系统生成，无需手填
    for bo in project.bos:
        if not bo.tbl_name:
            bo.tbl_name = table_name_for(bo.code)
            res.notices.append(f"业务对象 {bo.code}: 物理表名按规范取 {bo.tbl_name}")

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

    if need_bo_rows:
        if not project.module_id:
            res.notices.append("未配置默认模块ID，导入前建议在 0_项目设置 里补上，否则对象可能挂不到模块下")
        for bo in project.bos:
            bo.module_id = bo.module_id or project.module_id
            bo.type_id = bo.type_id or project.type_id
            if not bo.type_id:
                res.notices.append(f"业务对象 {bo.code}: 未指定分类ID")
            proc = ensure_process_fields(bo)
            if proc:
                res.notices.append(f"业务对象 {bo.code}: 流程驱动，已补齐冗余字段 {', '.join(proc)}")
            added = ensure_system_fields(bo)
            if added:
                res.notices.append(f"业务对象 {bo.code}: 已自动补齐系统字段 {', '.join(added)}")

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
        xml, notices = gen_tpl.render_list(lst, bo, form_of_bo.get(bo.code, ""), snippets, tenant)
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
