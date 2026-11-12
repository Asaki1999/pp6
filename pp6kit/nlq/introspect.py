"""从 PowerProject 库中读取语义元数据，构建 NL2SQL 用的 schema 包。

关键：不靠猜。库里有平台自己的元数据表：
    iform_bo_def          业务对象（code_ / name_ 中文）
    iform_bo_table        物理表（def_code_ = pp_xxx, comment_ 中文）
    iform_bo_attr         对象属性（name_ 中文 / code_ 驼峰 / data_type_new_ / object_type_）
    iform_bo_attr_column  属性 → 物理列（field_name_）
    iform_bo_column       物理列（含 comment_，补齐 attr 里没有的系统列）
    iform_cat_type/dic    选项字典（type_key_ → key_/name_）

这套映射是「客户自己扩展字段」也能自动跟上的根本原因：
每个客户的 pp_ 表列不一样，但元数据表里一定有对应记录。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import pymysql


@dataclass
class Column:
    name: str              # 物理列名
    label: str             # 中文名
    dtype: str             # 物理类型
    ref_table: str = ""    # object 字段指向的表
    dic_type: str = ""     # dic 字段对应的字典类型 key
    is_pk: bool = False
    multi: bool = False    # 多值外键（逗号分隔多个 id），join 要用 FIND_IN_SET
    # ---- 列画像（由 annotate_columns 填充）
    distinct: int = -1     # 去重值个数，-1 = 未采样
    filled: int = -1       # 非空行数
    samples: list[str] = field(default_factory=list)
    enums: dict[str, str] = field(default_factory=dict)  # 中文 -> 原始码

    @property
    def has_cn_enums(self) -> bool:
        """有中文标签的枚举（纯数字的「枚举」等于没有，别当状态列展示）。"""
        return any(any("\u4e00" <= ch <= "\u9fa5" for ch in k) for k in self.enums)

    @property
    def usable(self) -> bool:
        """整列为空、或整列只有一个值的列，筛它等于全表或空，没有查询价值。"""
        if self.filled == 0:
            return False
        if self.distinct == 1 and self.filled != -1:
            return False
        return True


# 中文对象名里常见的冗余前后缀：说「需求」而不是「系统需求」，
# 说「预算条目」而不是「项目预算条目」
CN_PREFIX = ("项目", "系统", "研发", "业务", "产品", "工程", "合同")
CN_SUFFIX = ("表", "记录", "信息", "明细", "条目", "清单", "配置")
# 业务口语同义词（少量、可扩展；换客户也能用，因为都是通用 PM 术语）
CN_SYNONYM = {
    # key 必须与去前缀/去后缀后的名字**完全一致**才生效（见 object_aliases）
    "人员记录": ("成员", "人员", "团队"),
    "系统需求": ("需求",),
    "预算条目": ("预算", "预算项"),
    "开发迭代": ("迭代", "sprint"),
    "预期文档": ("文档", "交付文档"),

    "待办": ("事项", "待办事项", "待办项"),
    "问题": ("bug", "缺陷单"),
    "风险": ("风险项",),
    "文档": ("附件", "资料"),
    "文档目录": ("目录", "文件夹"),
    "工时信息": ("工时", "工时数据", "工时记录", "工时明细", "工时填报"),
    "项目": ("工程", "单子"),
    "客户": ("甲方", "业主"),
    "合同": ("订单", "单子"),
    "成员": ("人员", "团队"),
    "产品": ("产品线",),
}
# 列级口语：用户说「钱」，库里叫「金额」
COL_SYNONYM = {
    "金额": ("钱", "费用", "花销"),
    "预算": ("经费", "钱"),
    "负责人": ("主管", "牵头人"),
    "责任人": ("主管", "负责人"),
    "名称": ("名字",),
}


def _n(text: str) -> str:
    import re as _re
    return _re.sub(r"[的地得\s]", "", text or "")


def object_aliases(label: str) -> set[str]:
    """一个对象可以被怎么称呼：全名、去前缀、去后缀、业务口语。"""
    base = _n(label).replace("表", "")
    if not base:
        return set()
    out = {base}
    for p in CN_PREFIX:
        if base.startswith(p) and len(base) - len(p) >= 2:
            out.add(base[len(p):])
    for suf in CN_SUFFIX:
        if base.endswith(suf) and len(base) - len(suf) >= 2:
            out.add(base[: -len(suf)])
    for stem, words in CN_SYNONYM.items():
        if _n(stem) == base:      # 只对完全同名的别名生效，别让「项目人员记录」也吃掉「成员」
            out.update(words)
    return {x for x in out if len(x) >= 2}


@dataclass
class Table:
    name: str              # pp_xxx
    label: str             # 中文名
    obj_code: str = ""
    rows: int = 0
    columns: list[Column] = field(default_factory=list)

    @property
    def has_cn_enums(self) -> bool:
        """有中文标签的枚举（纯数字的「枚举」等于没有，别当状态列展示）。"""
        return any(any("\u4e00" <= ch <= "\u9fa5" for ch in k) for k in self.enums)

    @property
    def usable(self) -> bool:
        return self.rows > 0

    @property
    def aliases(self) -> set[str]:
        """可被用户怎么称呼（含中文省略与口语）。"""
        return object_aliases(self.label)

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)

    def enum_of(self, name: str) -> dict[str, str]:
        c = self.column(name)
        return c.enums if c else {}


@dataclass
class SchemaPack:
    database: str
    server: str
    tables: dict[str, Table] = field(default_factory=dict)
    dictionaries: dict[str, dict[str, str]] = field(default_factory=dict)  # type_key -> {key: name}
    ref_values: dict[str, list[str]] = field(default_factory=dict)  # 关联表 -> 实际取值样例
    relations: dict[str, list[dict]] = field(default_factory=dict)  # 源表 -> 关系列表
    code_labels: dict[str, str] = field(default_factory=dict)       # 原始码 -> 中文

    def to_prompt(self, max_tables: int = 40) -> str:
        """压成给 LLM 看的紧凑 schema 描述。"""
        lines: list[str] = []
        ranked = sorted(self.tables.values(), key=lambda t: -t.rows)[:max_tables]
        for t in ranked:
            lines.append(f"表 {t.name}（{t.label}，约 {t.rows} 行）")
            for c in t.columns:
                extra = []
                if c.ref_table:
                    vals = "、".join(self.ref_values.get(c.ref_table, [])[:8])
                    extra.append(f"值来自 {c.ref_table}" + (f"，实际值：{vals}" if vals else ""))
                if c.dic_type and c.dic_type in self.dictionaries:
                    vals = "、".join(list(self.dictionaries[c.dic_type].values())[:8])
                    extra.append(f"字典值：{vals}")
                suffix = f"  ← {'；'.join(extra)}" if extra else ""
                lines.append(f"    {c.label} = {c.name} ({c.dtype}){suffix}")
        return "\n".join(lines)

    def to_json(self) -> dict:
        return {
            "database": self.database,
            "server": self.server,
            "tables": {
                name: {
                    "label": t.label, "obj": t.obj_code, "rows": t.rows,
                    "columns": [
                        {"name": c.name, "label": c.label, "dtype": c.dtype,
                         "ref": c.ref_table, "dic": c.dic_type, "pk": c.is_pk}
                        for c in t.columns
                    ],
                }
                for name, t in self.tables.items()
            },
            "dictionaries": self.dictionaries,
            "ref_values": self.ref_values,
        }


# 平台字典表里没有、但库里实际在用的码（全库枚举扫描得出）
SEED_CODE_LABELS = {
    "inprogress": "进行中", "in_progress": "进行中", "notstarted": "未开始",
    "not_started": "未开始", "finished": "已完成", "completed": "已完成",
    "closed": "已关闭", "passed": "已通过", "pass": "已通过", "rejected": "已拒绝",
    "paused": "已暂停", "archived": "已归档", "developing": "开发中",
    "released": "已发布", "tobeannounced": "待公布", "high": "高", "hig": "高",
    "medium": "中", "mid": "中", "low": "低", "urgent": "紧急",
    "signed": "已签约", "failed": "已失败", "unpaid": "未付款",
    "partiallypaid": "部分付款", "undernegotiation": "谈判中",
    "before_review": "评审前", "pending_review": "待评审", "reviewed": "已评审",
    "under_tracking": "跟踪中", "start": "开始", "uts": "未测试",
    "not_upload": "未上传", "need_upload": "需上传", "upload_archive": "已上传归档",
    "need_archive": "需归档", "review_archive": "评审归档", "not_need_upload": "无需上传",
    "unfinished": "未完成", "testing": "测试中",
}

# 需要采样的列：枚举类 + 外键 + 标题 + 时间
ENUM_HINTS = ("status", "state", "type", "level", "priority", "rating", "category",
              "source", "flag", "stage", "mode", "result", "urgency", "severity",
              "light", "progress", "percent")


def _needs_profile(c: Column) -> bool:
    """这列要不要做画像（去重数、非空数、采样值）。

    原来只画像少数几类（name_/枚举/datetime），结果工时表的
    normal_hours_、work_content_ 这些关键业务列永远是 filled=-1（未知），
    默认列选择时全被当成"没数据"跳过，最后只剩一个 create_time_。

    现在除纯系统列外全部画像 —— 画像成本是一次 GROUP BY，可以接受，
    换来的是「这列有没有数据、有哪些取值」都能确定。
    """
    low = c.name.lower()
    if c.is_pk:
        return False
    if low in ("deleted_", "version_", "tenant_id_", "ip_", "baseline_version_",
               "is_draft_", "cut_flow_inst_id_", "cut_flow_bo_id_"):
        return False
    return True


NUMERIC_TYPES = {"int", "bigint", "smallint", "tinyint", "decimal", "float",
                 "double", "numeric", "date", "datetime", "timestamp", "time"}


def _filled_expr(c: Column) -> str:
    """datetime/数字列不能和 '' 比较（MySQL 严格模式会直接报 1525）。"""
    if (c.dtype or "").lower() in NUMERIC_TYPES:
        return f"SUM(`{c.name}` IS NOT NULL)"
    return f"SUM(`{c.name}` IS NOT NULL AND `{c.name}` <> '')"


def _sample_where(c: Column) -> str:
    if (c.dtype or "").lower() in NUMERIC_TYPES:
        return f"`{c.name}` IS NOT NULL"
    return f"`{c.name}` IS NOT NULL AND `{c.name}` <> ''"


def annotate_columns(conn, pack, tables: list[str] | None = None) -> None:
    """列画像：一条 SQL 拿到每列的去重数/非空数，低基数列再采样取值。

    有了它才能回答三件事：
      1. 这列是不是整列为空/恒定值（筛了等于白筛）
      2. 用户说的中文能不能翻成库里的码（进行中 -> inProgress）
      3. 结果里该给哪几列（跳过 id_/tenant_/ip_ 这类噪音）
    """
    cur = conn.cursor()
    targets_all = tables if tables is not None else list(pack.tables)
    for name in targets_all:
        t = pack.tables.get(name)
        if not t or t.rows <= 0:
            continue
        targets = [c for c in t.columns if _needs_profile(c)]
        if not targets:
            continue
        parts: list[str] = []
        for c in targets:
            parts.append(f"COUNT(DISTINCT `{c.name}`)")
            parts.append(_filled_expr(c))
        try:
            cur.execute(f"SELECT COUNT(*), {', '.join(parts)} FROM `{name}`")
            row = cur.fetchone()
        except Exception:
            continue
        total = int(row[0] or 0)
        for i, c in enumerate(targets):
            c.distinct = int(row[1 + i * 2] or 0)
            c.filled = int(row[2 + i * 2] or 0)
            if 2 <= c.distinct <= 12 and c.filled:
                try:
                    cur.execute(
                        f"SELECT `{c.name}`, COUNT(*) FROM `{name}` "
                        f"WHERE {_sample_where(c)} "
                        f"GROUP BY 1 ORDER BY 2 DESC LIMIT 12"
                    )
                    vals = [str(r[0]) for r in cur.fetchall()]
                except Exception:
                    vals = []
                c.samples = vals
                c.enums = _build_enums(c, vals, pack)
    cur.close()


def _build_enums(c: Column, vals: list[str], pack: SchemaPack) -> dict[str, str]:
    """构造「中文 -> 原始码」。字典优先，其次用全局码表推断。"""
    out: dict[str, str] = {}
    dic = pack.dictionaries.get(c.dic_type, {}) if c.dic_type else {}
    for v in vals:
        if v in dic:                      # 字典 key 就是原值
            out[dic[v]] = v
        label = pack.code_labels.get(v.lower())
        if label:
            out.setdefault(label, v)
        out.setdefault(v, v)              # 原样也能查
    return out


def connect(host: str, port: int, user: str, password: str, database: str,
            timeout: int = 6, readonly: bool = True):
    """连库，默认**把会话设成只读事务**。

    readonly=True 时连上就跑一次 `SET SESSION transaction_read_only = 1`：
    这条连接从此写不了任何数据。为什么在连接这一层再做一次？
    上面已经有问句闸、SQL 闸、执行闸三道，但那三道都在判「生成的 SQL 对不对」；
    这一道是「就算判错了、或者将来有人绕过去，也写不进」。它由数据库执行，代码绕不过。
    """
    conn = pymysql.connect(
        host=host, port=int(port), user=user, password=password,
        database=database, charset="utf8mb4", connect_timeout=timeout,
        read_timeout=30, write_timeout=30, autocommit=True,
    )
    if readonly:
        set_session_readonly(conn)
    return conn


def set_session_readonly(conn) -> bool:
    """把当前会话设成只读事务，成功返回 True。

    两种写法都试：`transaction_read_only` 是会话变量（影响后续所有事务），
    `SET SESSION TRANSACTION READ ONLY` 只影响下一个事务但老版本更认。
    都失败就静默——老版本 MySQL、或者账号没有 SET 权限时，
    不能因为「设不上只读」就让整个工具连库都连不通。
    """
    for stmt in ("SET SESSION transaction_read_only = 1",
                 "SET SESSION TRANSACTION READ ONLY"):
        try:
            cur = conn.cursor()
            cur.execute(stmt)
            cur.close()
            return True
        except Exception:
            continue
    return False


def _rows(cur, sql: str, args=None) -> list[tuple]:
    cur.execute(sql, args)
    return cur.fetchall()


def build_schema(conn, database: str) -> SchemaPack:
    cur = conn.cursor()
    pack = SchemaPack(database=database, server=f"{database}")

    # ---- 对象：属性中文名 + 物理列
    cur.execute("""
        SELECT d.code_ AS obj_code, d.name_ AS obj_name,
               a.name_ AS attr_name, a.code_ AS attr_code,
               a.data_type_new_ AS dtype, a.object_type_ AS obj_type,
               COALESCE(ac.field_name_, '') AS col
        FROM iform_bo_def d
        JOIN iform_bo_attr a ON a.def_id_ = d.id_
        LEFT JOIN iform_bo_attr_column ac
               ON ac.def_id_ = d.id_ AND ac.attr_code_ = a.code_
    """)
    attr_rows = cur.fetchall()

    # ---- 物理表中文名
    cur.execute("SELECT def_code_, comment_ FROM iform_bo_table WHERE def_code_ LIKE 'pp\\_%'")
    table_label = {r[0]: (r[1] or r[0]) for r in cur.fetchall()}

    # ---- 真实行数
    cur.execute("""
        SELECT table_name, table_rows FROM information_schema.tables
        WHERE table_schema = %s""", (database,))
    row_count = {r[0]: int(r[1] or 0) for r in cur.fetchall()}

    # ---- 真实列类型（以库里为准，元数据可能滞后）
    cur.execute("""
        SELECT table_name, column_name, data_type FROM information_schema.columns
        WHERE table_schema = %s""", (database,))
    real_cols: dict[str, dict[str, str]] = {}
    for t, c, dt in cur.fetchall():
        real_cols.setdefault(t, {})[c] = dt

    # ---- 列中文注释（补齐 attr 里没有的系统列）
    cur.execute("SELECT table_name_, name_, comment_ FROM iform_bo_column WHERE name_ IS NOT NULL")
    col_comment: dict[str, dict[str, str]] = {}
    for t, c, cm in cur.fetchall():
        if t and c:
            col_comment.setdefault(t, {})[c] = cm or c

    # ---- 字典
    cur.execute("""
        SELECT t.type_key_, d.key_, d.name_ FROM iform_cat_dic d
        JOIN iform_cat_type t ON t.id_ = d.type_id_
    """)
    for tk, k, n in cur.fetchall():
        pack.dictionaries.setdefault(tk, {})[k] = n
        if k and n:
            pack.code_labels.setdefault(str(k).lower(), n)
    # 平台默认词典覆盖不到的常用码（实测库里就是这些写法）
    pack.code_labels.update({k.lower(): v for k, v in SEED_CODE_LABELS.items()
                             if k.lower() not in pack.code_labels})

    obj_to_table = {code: f"pp_{code}" for code in {r[0] for r in attr_rows}}
    # 只保留真实存在的表
    existing = set(real_cols)

    def true_rows(table: str) -> int:
        """information_schema.table_rows 是估算值（InnoDB 常差很多），选表排序必须用真实值。"""
        try:
            cur.execute(f"SELECT COUNT(*) FROM `{table}`")
            return int(cur.fetchone()[0] or 0)
        except Exception:
            return row_count.get(table, 0)

    for obj_code, obj_name, attr_name, attr_code, dtype, obj_type, colname in attr_rows:
        table = obj_to_table.get(obj_code)
        if not table or table not in existing:
            continue
        if not colname:
            continue
        # 元数据里的属性可能映射到已改名/已删除的物理列。
        # 不校验就会生成 Unknown column 的 SQL（实测 4 张表踩到）。
        if colname not in real_cols.get(table, {}):
            continue
        t = pack.tables.get(table)
        if t is None:
            t = Table(name=table, label=table_label.get(table, obj_name), obj_code=obj_code,
                      rows=true_rows(table))
            pack.tables[table] = t
        ref = ""
        dic = ""
        if dtype == "object" and obj_type:
            ref = obj_to_table.get(obj_type, "")
            if ref not in existing:
                ref = ""
        elif dtype == "user":
            # 人员字段存的是账号 id，姓名在 iform_party_employee
            ref = "iform_party_employee"
        elif dtype == "department":
            ref = "iform_party_org"
        elif dtype == "dic" and obj_type:
            dic = obj_type
        t.columns.append(Column(
            name=colname, label=attr_name or colname,
            dtype=real_cols.get(table, {}).get(colname, dtype or ""),
            ref_table=ref, dic_type=dic,
            is_pk=(colname == "id_"),
        ))
        if ref:
            pack.relations.setdefault(table, []).append({
                "col": colname, "label": attr_name or colname,
                "dst": ref, "dtype": dtype, "live": -1, "multi": False,
            })

    # ---- 补系统列（attr 里没有、但实际存在的）
    system_labels = {
        "id_": "主键", "tenant_id_": "租户", "create_by_": "创建人", "create_time_": "创建时间",
        "update_by_": "更新人", "update_time_": "更新时间", "deleted_": "删除标记",
        "version_": "版本", "ip_": "IP",
    }
    for t in pack.tables.values():
        have = {c.name for c in t.columns}
        for col, dtype in real_cols.get(t.name, {}).items():
            if col in have:
                continue
            label = system_labels.get(col) or col_comment.get(t.name, {}).get(col) or col
            t.columns.append(Column(name=col, label=label, dtype=dtype,
                                    is_pk=(col == "id_")))
    # ---- 平台内置的人员表 / 组织表：不进 tables（避免被当成业务表乱查），只记关系
    pack.dictionaries.setdefault("_party", {})
    pack.ref_values.setdefault("iform_party_employee", [])
    pack.ref_values.setdefault("iform_party_org", [])

    # ---- 关联表的真实取值（让模型知道"执行中/已立项"这种具体说法）
    ref_tables = {c.ref_table for t in pack.tables.values() for c in t.columns if c.ref_table}
    ref_tables |= {"iform_party_employee", "iform_party_org"}
    for ref in sorted(ref_tables):
        if ref not in existing:
            continue
        cols = real_cols.get(ref, {})
        label_col = "name_" if "name_" in cols else ("title_" if "title_" in cols else None)
        if not label_col:
            continue
        try:
            cur.execute(f"SELECT DISTINCT `{label_col}` FROM `{ref}` WHERE `{label_col}` IS NOT NULL LIMIT 12")
            vals = [str(r[0])[:24] for r in cur.fetchall() if r[0] not in (None, "")]
            if vals:
                pack.ref_values[ref] = vals
        except Exception:
            continue

    # ---- 补漏：有物理表但没有业务对象定义（或对象没建属性映射）的表。
    # 这类表在平台里可能是系统内置/历史表，用 iform_bo_column 的中文列注释兜底，
    # 否则「列出所有文档」这种问法会因为 pp_document 不在包里而查不到。
    covered = set(pack.tables)
    extra_sql = """
        SELECT c.table_name_, c.name_, c.comment_
        FROM iform_bo_column c
        JOIN information_schema.tables t
          ON t.table_schema = %s AND t.table_name = c.table_name_
        WHERE c.table_name_ LIKE 'pp\\_%%'
    """
    try:
        cur.execute(extra_sql, (database,))
        col_rows = cur.fetchall()
    except Exception:
        col_rows = []

    grouped: dict[str, list[tuple[str, str]]] = {}
    for tname, cname, comment in col_rows:
        if not tname or not cname or tname in covered:
            continue
        grouped.setdefault(tname, []).append((cname, comment or cname))

    for tname, cols in grouped.items():
        if tname not in existing or len(cols) < 3:
            continue
        label = table_label.get(tname) or tname
        n = true_rows(tname)
        if n <= 0:
            continue
        t = Table(name=tname, label=label, obj_code="", rows=n)
        have = real_cols.get(tname, {})
        for cname, comment in cols:
            if cname not in have:
                continue
            t.columns.append(Column(
                name=cname, label=comment, dtype=have[cname],
                is_pk=(cname == "id_"),
            ))
        if len(t.columns) >= 3:
            pack.tables[tname] = t

    # 元数据没登记的外键，用物理列名补一层（related_phase_id_ → pp_project_phase）
    try:
        infer_relations(pack)
    except Exception:
        pass

    cur.close()
    return pack


SMALL_TABLES_ONLY = """
SELECT table_name, table_rows FROM information_schema.tables
WHERE table_schema=%s AND table_name LIKE 'pp\\_%%' ORDER BY table_rows DESC
"""


SYS_COL_SUFFIX = ("create_by_", "update_by_", "tenant_id_", "id_", "deleted_")


def infer_relations(pack: SchemaPack) -> int:
    """元数据没登记、但物理列名 `xxx_id_` 能推断出的外键。

    实例：pp_task.related_phase_id_ 指向 pp_project_phase，但 iform_bo_attr 里
    object_type_ 是空的，语义层就断了。这类列不少，补上才能连图。
    """
    names = list(pack.tables)
    added = 0

    # 人员/部门字段：元数据里 object_type_ 常为空，ref_table 就丢了，
    # 结果「填报人」显示成一串 UUID。按列名 + 中文标签补回来。
    PERSON_COL = ("user_id_", "person_id_", "employee_id_", "assignee_id_",
                  "owner_id_", "handler_id_", "reporter_id_", "create_by_",
                  "update_by_", "manager_id_", "filler_id_")
    PERSON_LABEL = ("人", "员", "者")
    # 部门字段单独处理，别被人员规则吃掉
    ORG_COL = ("org_id_", "dept_id_", "department_id_", "group_id_")
    for t in pack.tables.values():
        for c in t.columns:
            if not c.ref_table and c.name in ORG_COL:
                c.ref_table = "iform_party_org"
                added += 1

    for t in pack.tables.values():
        for c in t.columns:
            if c.ref_table:
                continue
            if c.name in PERSON_COL or (
                    c.name.endswith(("_id_", "_ids_"))
                    and any(k in c.label for k in ("填报人", "责任人", "负责人",
                                                   "处理人", "执行人", "待办人", "创建人"))):
                c.ref_table = "iform_party_employee"
                c.multi = c.name.endswith("_ids_")
                added += 1

    for t in pack.tables.values():
        for c in t.columns:
            if c.ref_table or not c.name.endswith("_id_") or c.name in SYS_COL_SUFFIX:
                continue
            base = c.name[:-4]                    # 去掉 _id_
            if len(base) < 4:
                continue
            # 先试完全一致，再试「以某段结尾」（related_phase → pp_project_phase）
            cand = next((n for n in names if n == f"pp_{base}"), None)
            if cand is None:
                tails = [n for n in names if n != t.name and n.endswith(base)]
                tails.sort(key=len)
                cand = tails[0] if tails else None
            if cand is None and "_" in base:
                # related_phase_id_ → phase → pp_project_phase
                word = base.split("_")[-1]
                if len(word) >= 4:
                    tails = [n for n in names if n != t.name and n.endswith(word)]
                    tails.sort(key=len)
                    cand = tails[0] if tails else None
            if cand is None or cand == t.name:
                continue
            # 目标表必须真的有 id_ 列，否则 join 会拼出 Unknown column。
            # 实测 pp_i18n_resource 主键叫 id（没下划线），
            # source_id_ 被推断到它上面，一 join 就崩。
            target = pack.tables.get(cand)
            if target is None or not any(x.name == "id_" for x in target.columns):
                continue
            c.ref_table = cand
            pack.relations.setdefault(t.name, []).append({
                "col": c.name, "label": c.label, "dst": cand,
                "dtype": "object", "live": -1, "multi": False,
            })
            added += 1
    return added


def annotate_liveness(conn, pack: SchemaPack, tables: list[str]) -> None:
    """给指定表的关系列标上真实非空行数；空列 join 出来全是 NULL，提示里要标出来。"""
    cur = conn.cursor()
    for name in tables:
        rels = pack.relations.get(name)
        if not rels:
            continue
        cur.execute(f"SHOW COLUMNS FROM `{name}`")
        have = {r[0] for r in cur.fetchall()}
        cols = [r["col"] for r in rels if r["col"] in have]
        if not cols:
            continue
        expr = ", ".join(f"SUM(`{c}` IS NOT NULL AND `{c}`<>'')" for c in cols)
        multi_expr = ", ".join(f"SUM(`{c}` LIKE '%,%')" for c in cols)
        try:
            cur.execute(f"SELECT COUNT(*), {expr}, {multi_expr} FROM `{name}`")
            row = cur.fetchone()
        except Exception:
            continue
        n = len(cols)
        counts = {c: int(v or 0) for c, v in zip(cols, row[1:1 + n])}
        multis = {c: int(v or 0) for c, v in zip(cols, row[1 + n:1 + 2 * n])}
        for r in rels:
            if r["col"] in counts:
                r["live"] = counts[r["col"]]
                r["multi"] = multis.get(r["col"], 0) > 0
                for c in pack.tables[name].columns:
                    if c.name == r["col"]:
                        c.multi = r["multi"]
    cur.close()
