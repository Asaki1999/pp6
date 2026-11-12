"""自然语言生成 SQL（`pp6kit/nlq/`）：安全闸、答案质量判定、表关系、选表、规则引擎。

这组用例守的是一条**直连客户生产库**的链路：用户用中文提问 → 规则或大模型
生成 SQL → 在客户库上执行。整条链路上只有一个组件在兜底，就是 `guard.validate`。
它漏一条 `UPDATE`，就是一次数据事故；`hybrid.judge` 判错一次，规则引擎的
错答案就会被当成"答对了"直接返回给用户。

覆盖的四个风险面：
  1. `guard.py` —— 只读、限表、限行。**这是最后一道闸，优先级最高。**
  2. `hybrid.py` —— 规则答案够不够格，要不要转 AI。
  3. `graph.py` / `select.py` —— 跨表 join 与主表选择，选错了答案就是错的。
  4. `local.py` / `engine.py` —— 规则引擎出的 SQL 本身是否可执行、是否安全。

三条约定：
  - **不连库、不联网、不调大模型**。所有样本用下面 `_pack()` 手工构造的
    `SchemaPack`；`guard` 与 `hybrid` 全是纯函数。
  - 拒绝类断言必须**同时断言拒绝理由**，否则"因为别的原因被拒"会被当成
    "正确拒绝"。
  - 放行类断言看**真实产物**：返回的 SQL 文本里到底有没有 `LIMIT 500`。

原先标了「【已知缺陷】」的 6 条用例断言的是**正确行为**，源码缺陷修复后已经
全部转为正式用例（NLQ19 / NLQ35 / NLQ41 / NLQ81 / NLQ82 / NLQ83），不再 skip。
源码再改坏，它们会直接变红。
"""
from __future__ import annotations

from pp6kit.nlq import engine, graph, guard, hybrid, local, select
from pp6kit.nlq.introspect import Column, SchemaPack, Table
from tests._harness import case


# ---------------------------------------------------------------- 样本构造

def _col(name: str, label: str, dtype: str = "varchar", **kw) -> Column:
    return Column(name=name, label=label, dtype=dtype, **kw)


def _table(name: str, label: str, rows: int, columns: list[Column]) -> Table:
    return Table(name=name, label=label, rows=rows, columns=columns)


def _pack() -> SchemaPack:
    """最小但够用的语义层样本：项目 / 需求 / 客户 / 合同 / 人员 / 组织 + 一张孤立表。

    关系是照着真实 PowerProject 库的形状搭的：
        需求 --projectId--> 项目 --contractId--> 合同 --customerId--> 客户
        项目 --manager_id_--> 人员 --group_id_--> 组织
    客户到项目没有直接外键，必须经合同两跳 —— `graph.shortest_path` 就测这个。
    """
    emp = _table("iform_party_employee", "人员记录", 30, [
        _col("id_", "主键", "char", is_pk=True),
        _col("name_", "姓名"),
        _col("group_id_", "所属部门", "char", ref_table="iform_party_org"),
    ])
    org = _table("iform_party_org", "组织", 10, [
        _col("id_", "主键", "char", is_pk=True),
        _col("name_", "组织名称"),
    ])
    cust = _table("pp_customer", "客户", 50, [
        _col("id_", "主键", "char", is_pk=True),
        _col("name_", "客户名称"),
        _col("create_time_", "创建时间", "datetime"),
    ])
    con = _table("pp_contract", "合同", 80, [
        _col("id_", "主键", "char", is_pk=True),
        _col("name_", "合同名称"),
        _col("customerId", "所属客户", "char", ref_table="pp_customer"),
        _col("amount_", "合同金额", "decimal", filled=70, distinct=30),
        _col("create_time_", "创建时间", "datetime"),
    ])
    proj = _table("pp_project", "项目", 200, [
        _col("id_", "主键", "char", is_pk=True),
        _col("name_", "项目名称"),
        _col("create_time_", "创建时间", "datetime"),
        _col("deleted_", "已删除", "char"),
        # 库里存的是英文码，中文标签在 enums 里（中文 -> 原始码）
        _col("state_", "项目状态", "varchar", dic_type="project_state",
             enums={"进行中": "inprogress", "已完成": "finished"},
             filled=190, distinct=3),
        _col("progress_", "进度", "decimal", filled=180, distinct=40),
        _col("plan_amount_", "计划金额", "decimal", filled=150, distinct=60),
        _col("actual_amount_", "实际金额", "decimal", filled=140, distinct=55),
        _col("manager_id_", "项目经理", "char", ref_table="iform_party_employee"),
        _col("contractId", "所属合同", "char", ref_table="pp_contract"),
    ])
    req = _table("pp_requirement", "需求", 500, [
        _col("id_", "主键", "char", is_pk=True),
        _col("name_", "需求名称"),
        _col("create_time_", "创建时间", "datetime"),
        _col("deleted_", "已删除", "char"),
        _col("projectId", "所属项目", "char", ref_table="pp_project"),
        _col("state_", "需求状态", "varchar", enums={"已完成": "done"},
             filled=480, distinct=2),
    ])
    # 孤立表：没有任何关系指向它，也没有外键 —— 用来测「不可达」
    iso = _table("pp_risk_config", "风险等级配置", 0, [
        _col("id_", "主键", "char", is_pk=True),
        _col("name_", "配置名称"),
    ])
    return SchemaPack(
        database="pp", server="demo",
        tables={t.name: t for t in (proj, req, cust, con, emp, org, iso)},
        dictionaries={"project_state": {"inprogress": "进行中", "finished": "已完成"}},
        relations={
            "pp_project": [{"dst": "iform_party_employee", "col": "manager_id_"},
                           {"dst": "pp_contract", "col": "contractId"}],
            "pp_requirement": [{"dst": "pp_project", "col": "projectId"}],
            "pp_contract": [{"dst": "pp_customer", "col": "customerId"}],
            "iform_party_employee": [{"dst": "iform_party_org", "col": "group_id_"}],
        },
    )


def _tiny(pack_tables, relations=None, dictionaries=None) -> SchemaPack:
    """临时拼一个小 pack，用于关系图的边角情况。"""
    return SchemaPack(database="d", server="s",
                      tables={t.name: t for t in pack_tables},
                      relations=relations or {},
                      dictionaries=dictionaries or {})


def _allowed(pack: SchemaPack | None = None) -> set[str]:
    """`engine.ask` 传给安全闸的白名单：业务表 + 三张平台表。"""
    return set((pack or _pack()).tables) | guard.PARTY_TABLES


def _rejects(sql: str, *needles: str, allowed: set[str] | None = None) -> str:
    """断言 SQL 被安全闸拒绝，且拒绝理由命中任一关键词。返回错误文案。"""
    try:
        out = guard.validate(sql, allowed if allowed is not None else _allowed())
    except guard.SqlRejected as exc:
        msg = str(exc)
        assert any(n in msg for n in needles), \
            f"拒绝理由对不上：期望含 {needles} 之一，实际「{msg}」"
        return msg
    raise AssertionError(f"应当拒绝却放行了，实际返回：{out}")


def _passes(sql: str, allowed: set[str] | None = None, **kw) -> str:
    """断言 SQL 放行，返回改写后的 SQL。"""
    return guard.validate(sql, allowed if allowed is not None else _allowed(), **kw)


# ================================================================ 一、安全闸
# guard.validate 是「模型生成的 SQL」与「客户生产库」之间的唯一一道闸。

@case("NLQ01", "nlq", "模型把 SQL 包在 markdown 代码块里也能剥出来", "P0",
      ["模型最爱把 SQL 包在 markdown 代码块里，这是它最常见的输出格式",
       "不测会怎样：带反引号的 SQL 直接进 parse，模型答得对也执行不了"])
def _():
    raw = "```sql\nSELECT `name_` FROM `pp_project`\n```"
    out = guard.clean(raw)
    assert out == "SELECT `name_` FROM `pp_project`", f"实际 {out!r}"
    assert "```" not in out
    # 大写语言标记、带分号、代码块紧贴文本都要认得
    assert guard.clean("```SQL SELECT 1; ```") == "SELECT 1"
    assert guard.clean("```\nSELECT 1;\n```") == "SELECT 1"


@case("NLQ02", "nlq", "末尾分号、多余空白、空值都被清掉", "P0",
      ["模型常在结尾多打一个分号，多语句判断会误伤",
       "不测会怎样：`SELECT 1;;` 被当成两条语句拒掉，用户看到无意义的报错"])
def _():
    assert guard.clean("  SELECT 1 ;  ") == "SELECT 1"
    assert guard.clean("SELECT 1;;") == "SELECT 1"
    assert guard.clean("") == ""
    assert guard.clean(None) == ""


@case("NLQ03", "nlq", "普通 SELECT 放行且自动补 LIMIT 500", "P0",
      ["生产库表动辄几十万行，没有 LIMIT 会把内存和带宽打满",
       "不测会怎样：一个漏写 LIMIT 的查询把客户库拖慢"])
def _():
    out = _passes("SELECT * FROM pp_project")
    assert "LIMIT 500" in out.upper(), f"没补行数上限：{out}"
    assert guard.MAX_ROWS == 500


@case("NLQ04", "nlq", "模型自己写了较小的 LIMIT 时保留原值", "P0",
      ["「前 10 个项目」必须真的只返回 10 行",
       "不测会怎样：统一改写成 500，排名类结果全错"])
def _():
    out = _passes("SELECT `t`.`name_` FROM `pp_project` `t` LIMIT 100")
    assert "LIMIT 100" in out.upper(), f"实际 {out}"
    assert "LIMIT 500" not in out.upper()


@case("NLQ05", "nlq", "超大 LIMIT 被压到 500", "P0",
      ["模型经常写 LIMIT 10000 甚至 99999",
       "不测会怎样：一条「列出全部项目」把整张表拉到前端"])
def _():
    out = _passes("SELECT * FROM pp_project LIMIT 99999")
    assert "LIMIT 500" in out.upper(), f"实际 {out}"
    assert "99999" not in out


@case("NLQ06", "nlq", "行数上限可由调用方下调", "P1",
      ["导出/预览场景要更小的上限",
       "不测会怎样：max_rows 参数形同虚设，只能出 500 行"])
def _():
    out = _passes("SELECT * FROM pp_project", max_rows=10)
    assert "LIMIT 10" in out.upper(), f"实际 {out}"


@case("NLQ07", "nlq", "UPDATE 被拒且理由写明是写操作", "P0",
      ["一条 UPDATE 就是一次生产数据事故",
       "不测会怎样：模型幻觉出一条 UPDATE 就落库了",
       "拒绝理由必须命中，否则「因为语法错误被拒」会掩盖漏洞"])
def _():
    msg = _rejects("UPDATE pp_project SET name_ = 'x' WHERE id_ = 1",
                   "只允许 SELECT", "Update")
    assert "Update" in msg, msg


@case("NLQ08", "nlq", "DELETE 被拒", "P0",
      ["删数据比改数据更难挽回", "不测会怎样：一条 DELETE 清空项目表"])
def _():
    _rejects("DELETE FROM pp_project WHERE id_ = 1", "只允许 SELECT", "Delete")


@case("NLQ09", "nlq", "INSERT ... SELECT 被拒", "P0",
      ["写操作套在 SELECT 的外壳里，最容易被漏掉",
       "不测会怎样：往生产库灌脏数据"])
def _():
    _rejects("INSERT INTO pp_project (name_) SELECT name_ FROM pp_project",
             "只允许 SELECT", "Insert")


@case("NLQ10", "nlq", "建表/改表/清表/授权全部被拒", "P0",
      ["DDL 与 GRANT 是不可逆的",
       "不测会怎样：DROP TABLE 或 GRANT 把权限放开给所有人"])
def _():
    for sql, needle in (
            ("DROP TABLE pp_project", "Drop"),
            ("CREATE TABLE hack (a int)", "Create"),
            ("ALTER TABLE pp_project ADD COLUMN x int", "Alter"),
            ("TRUNCATE pp_project", "TruncateTable"),
            ("GRANT ALL ON *.* TO 'x'@'%'", "Command"),
    ):
        _rejects(sql, "只允许 SELECT", needle)


@case("NLQ11", "nlq", "多语句（SELECT 1; DROP TABLE）被拒", "P0",
      ["这是最经典的注入形态：第一句看起来无害",
       "不测会怎样：只看第一句就放行，后一句照常执行"])
def _():
    _rejects("SELECT 1; DROP TABLE pp_project", "只允许单条语句")


@case("NLQ12", "nlq", "访问白名单外的表被拒并点名是哪张表", "P0",
      ["客户库里除了 pp_ 业务表还有一堆敏感表",
       "不测会怎样：模型自由发挥 join 到工资、账号表",
       "必须报出表名，不然排查时不知道是哪张"])
def _():
    msg = _rejects("SELECT * FROM pp_salary", "未授权的表", "pp_salary")
    assert "pp_salary" in msg
    _rejects("SELECT * FROM pp_project, pp_salary", "未授权的表", "pp_salary")


@case("NLQ13", "nlq", "四个系统库一律拒绝", "P0",
      ["information_schema/mysql/performance_schema/sys 里是账号、权限、执行计划",
       "不测会怎样：一句 information_schema.user 就把客户所有账号读出来"])
def _():
    for schema in ("information_schema", "mysql", "performance_schema", "sys"):
        _rejects(f"SELECT * FROM {schema}.tables", "系统库", schema)


@case("NLQ14", "nlq", "INTO OUTFILE / DUMPFILE 写文件被拒", "P0",
      ["这两个语法能把查询结果写成服务器上的文件，等于写权限",
       "不测会怎样：客户库目录被写进文件，甚至被用来提权"])
def _():
    _rejects("SELECT * FROM pp_project INTO OUTFILE '/tmp/a.txt'",
             "OUTFILE", "语法错误", "导出")
    _rejects("SELECT * FROM pp_project INTO DUMPFILE '/tmp/a.bin'",
             "DUMPFILE", "OUTFILE", "语法错误", "导出")


@case("NLQ15", "nlq", "SLEEP / BENCHMARK 这类拖库函数被拒", "P0",
      ["一条 SLEEP(100) 就能把连接池占死",
       "不测会怎样：一个问句让客户整个系统卡住 100 秒"])
def _():
    _rejects("SELECT SLEEP(100)", "禁止的函数", "SLEEP")
    _rejects("SELECT * FROM pp_project WHERE BENCHMARK(1000000, MD5('x'))",
             "禁止的函数", "BENCHMARK")


@case("NLQ16", "nlq", "LOAD_FILE / GET_LOCK 被拒", "P0",
      ["LOAD_FILE 读服务器任意文件，GET_LOCK 能卡住业务锁",
       "不测会怎样：模型一句「读一下配置」就把 /etc/passwd 读出来"])
def _():
    _rejects("SELECT LOAD_FILE('/etc/passwd')", "禁止的函数", "LOAD_FILE")
    _rejects("SELECT GET_LOCK('pp6', 10)", "禁止的函数", "GET_LOCK")


@case("NLQ17", "nlq", "空 SQL 与纯空白被拒", "P0",
      ["模型偶尔只回一句「无法回答」，取出来是空串",
       "不测会怎样：空串进了 parse 抛的异常类型和拒绝路径不一致"])
def _():
    for sql in ("", "   ", "\n\t ", "```sql\n```"):
        _rejects(sql, "SQL 为空")


@case("NLQ18", "nlq", "语法错误的 SQL 被拒且提示语法问题", "P0",
      ["模型会吐出半截 SQL（`SELECT FROM WHERE`）",
       "不测会怎样：坏 SQL 原样发到生产库，报错信息丢给用户"])
def _():
    _rejects("SELECT FROM WHERE", "语法错误")
    _rejects("SELECT * FROM", "语法错误")


@case("NLQ19", "nlq", "锁定读（FOR UPDATE / LOCK IN SHARE MODE）必须被拒", "P0",
      ["SELECT ... FOR UPDATE 是只读语句，但会在客户库上持有行锁到事务结束",
       "不测会怎样：一条「查一下」把生产库的写事务全部阻塞"])
def _():
    for sql in ("SELECT * FROM pp_project FOR UPDATE",
                "SELECT * FROM pp_project LOCK IN SHARE MODE",
                # guard 自己的 docstring 就拿这个变体举例，说明本意是要拦的
                "SELECT * FROM pp_project WHERE id_ IN "
                "(SELECT id_ FROM pp_task FOR UPDATE)"):
        _rejects(sql, "锁定读", "FOR UPDATE", "FOR SHARE")


@case("NLQ20", "nlq", "WITH ... SELECT（CTE）放行，CTE 别名不算未授权表", "P0",
      ["CTE 是复杂查询的标准写法，模型很爱用",
       "不测会怎样：把 CTE 别名当真实表判「未授权」，多表统计全体失效"])
def _():
    out = _passes("WITH project_agg AS (SELECT * FROM pp_project) "
                  "SELECT * FROM project_agg")
    assert "LIMIT 500" in out.upper(), out
    # 别名可以随便叫（哪怕叫 secret），只要底层真表在白名单里
    out2 = _passes("WITH secret AS (SELECT COUNT(*) AS c FROM pp_requirement) "
                   "SELECT * FROM secret")
    assert "LIMIT 500" in out2.upper(), out2


@case("NLQ21", "nlq", "UNION 两段都被检查且放行合法写法", "P0",
      ["UNION 的第二段常被正则漏掉",
       "不测会怎样：`SELECT ... UNION SELECT ... FROM 敏感表` 只查了前半段"])
def _():
    out = _passes("SELECT * FROM pp_project UNION SELECT * FROM pp_requirement")
    assert "LIMIT 500" in out.upper(), out
    # 第二段里的越权表同样要被拦
    _rejects("SELECT * FROM pp_project UNION SELECT * FROM pp_salary",
             "未授权的表", "pp_salary")


@case("NLQ22", "nlq", "三张平台字典表（人员/组织/角色）放行", "P0",
      ["问「每个负责人有几个项目」必须 join 人员表拿姓名",
       "不测会怎样：所有带姓名的查询都被判越权，功能直接不可用"])
def _():
    assert guard.PARTY_TABLES == {"iform_party_employee", "iform_party_org",
                                  "iform_object_role"}
    out = _passes("SELECT e.name_ FROM pp_project p "
                  "JOIN iform_party_employee e ON e.id_ = p.manager_id_")
    assert "iform_party_employee" in out, out


@case("NLQ23", "nlq", "白名单漏传平台表时平台表被拒（调用方必须并集）", "P0",
      ["engine.ask 里写的是「业务表集合 ∪ 平台表」（set(pack.tables) 并 guard.PARTY_TABLES）",
       "不测会怎样：有人改成只传业务表，join 人员的查询全部失败且原因难查"])
def _():
    # 只给业务表，不给平台表 —— 模拟有人漏了并集
    only_business = {t for t in _pack().tables if t.startswith("pp_")}
    _rejects("SELECT e.name_ FROM pp_project p "
             "JOIN iform_party_employee e ON e.id_ = p.manager_id_",
             "未授权的表", "iform_party_employee", allowed=only_business)


@case("NLQ24", "nlq", "带库名前缀的合法表照常放行", "P1",
      ["模型经常写成 db.pp_project 这种全限定名",
       "不测会怎样：明明合法的查询被判越权，用户反复重试"])
def _():
    out = _passes("SELECT * FROM pp.pp_project")
    assert "pp_project" in out and "LIMIT 500" in out.upper(), out


@case("NLQ25", "nlq", "大写表名按未授权拒（fail-closed），注释里的 DROP 不算语句", "P1",
      ["白名单是 lowercase，大小写不匹配宁可拒绝也别放过",
       "注释里的 DROP 只是文本，不能被当成写操作（否则模型写注释就报错）",
       "不测会怎样：两边都判错时，要么误放行要么误拒绝，都难排查"])
def _():
    _rejects("SELECT * FROM PP_PROJECT", "未授权的表", "PP_PROJECT")
    out = _passes("SELECT * FROM pp_project -- 之前有人用 DROP TABLE 改过")
    assert "LIMIT 500" in out.upper(), out


@case("NLQ26", "nlq", "referenced_tables 能从多表 join 取全表名并去重", "P1",
      ["诊断与白名单复核都靠它列举涉及的表",
       "不测会怎样：join 进来的第二、三张表在报告里看不见"])
def _():
    got = guard.referenced_tables(
        "SELECT * FROM pp_project p "
        "JOIN pp_requirement r ON r.projectId = p.id_ "
        "JOIN pp_project q ON q.id_ = p.id_")
    assert got == ["pp_project", "pp_requirement"], f"实际 {got}"


@case("NLQ27", "nlq", "referenced_tables 排除 CTE 别名", "P0",
      ["CTE 别名不是库里的表", "不测会怎样：把别名当表报给用户，诊断结论全错"])
def _():
    got = guard.referenced_tables("WITH project_agg AS (SELECT * FROM pp_project) "
                                 "SELECT * FROM project_agg")
    assert got == ["pp_project"], f"实际 {got}"


@case("NLQ28", "nlq", "referenced_tables 遇到语法错误返回空列表而不是抛异常", "P1",
      ["这个函数在诊断路径上调用，抛异常会把原始报错盖掉",
       "不测会怎样：解析失败时用户看到的是 AttributeError 而不是「SQL 有问题」"])
def _():
    assert guard.referenced_tables("SELECT FROM WHERE (") == []
    assert guard.referenced_tables("") == []
    assert guard.referenced_tables("SELECT 1") == []


@case("NLQ29", "nlq", "安全闸输出再过一遍闸，LIMIT 不会叠加", "P1",
      ["重试路径会把上一次的输出再送进 validate",
       "不测会怎样：出现 LIMIT 500 LIMIT 500 这种坏 SQL"])
def _():
    once = _passes("SELECT * FROM pp_project")
    twice = _passes(once)
    assert once == twice, f"再处理一次就变了：{once!r} -> {twice!r}"
    assert twice.upper().count("LIMIT 500") == 1, twice


@case("NLQ30", "nlq", "中文列名别名与中文值不影响校验", "P1",
      ["库里的中文注释、AS 中文别名很常见",
       "不测会怎样：带中文的合法 SQL 被判语法错误，中文库没法用"])
def _():
    out = _passes("SELECT `t`.`name_` AS `项目名称` FROM `pp_project` `t` "
                  "WHERE `t`.`name_` = '测试项目'")
    assert "项目名称" in out and "测试项目" in out, out


# ================================================================ 二、答案质量判定
# hybrid.judge 决定「规则答的这条要不要转 AI 重答」。判松了错答案直接返回，
# 判严了每个问题都白花 2-5 秒和一次额度。

@case("NLQ31", "nlq", "问「多少」但 SQL 没做统计 → 判不合格", "P0",
      ["「今年有多少个项目」返回 200 行明细，等于没回答",
       "不测会怎样：规则引擎拿明细当数量，用户看到 200 行以为什么都对了"])
def _():
    v = hybrid.judge("今年有多少个项目", "SELECT * FROM pp_project", 200)
    assert not v.ok
    assert any("统计" in r for r in v.reasons), v.reasons


@case("NLQ32", "nlq", "问「平均/合计」但没有聚合函数 → 判不合格", "P0",
      ["问平均进度却列出每行进度，用户还得自己算",
       "不测会怎样：聚合问句永远由规则引擎给出明细"])
def _():
    assert not hybrid.judge("项目的平均进度", "SELECT * FROM pp_project", 200).ok
    v = hybrid.judge("合计工时", "SELECT COUNT(*) FROM pp_task", 1)
    assert not v.ok and any("聚合" in r for r in v.reasons), v.reasons
    # 有 AVG 的正常通过
    assert hybrid.judge("项目的平均进度", "SELECT AVG(progress_) FROM pp_project", 1).ok


@case("NLQ33", "nlq", "问「前 5」但 SQL 没有 ORDER BY → 判不合格", "P0",
      ["「前 5」没有排序等于随机取 5 条",
       "不测会怎样：用户以为看到的是金额最大的 5 个项目"])
def _():
    v = hybrid.judge("前5个项目", "SELECT * FROM pp_project", 5)
    assert not v.ok and any("排序" in r for r in v.reasons), v.reasons
    assert hybrid.judge("前5个项目",
                        "SELECT * FROM pp_project ORDER BY plan_amount_ DESC LIMIT 5",
                        5).ok


@case("NLQ34", "nlq", "问「超支/超期」但 SQL 没有数值比较 → 判不合格", "P0",
      ["「超支」必须真的比较实际金额和计划金额",
       "不测会怎样：把全部项目当成超支项目列出来，业务决策直接错"])
def _():
    v = hybrid.judge("哪些项目超支了", "SELECT name_ FROM pp_project", 200)
    assert not v.ok and any("数值比较" in r for r in v.reasons), v.reasons
    # 和数字比较也算
    assert hybrid.judge("哪些项目超支了",
                        "SELECT name_ FROM pp_project "
                        "WHERE actual_amount_ - plan_amount_ > 100", 12).ok
    # DATEDIFF 也算「做了时间比较」
    assert hybrid.judge("哪些项目超期了",
                        "SELECT name_ FROM pp_project "
                        "WHERE DATEDIFF(end_date_, CURDATE()) < 0", 3).ok


@case("NLQ35", "nlq", "列与列比较也算「做了数值比较」", "P1",
      ["「超支」的正确答案就是 实际金额 > 计划金额，两列相比，没有数字字面量",
       "不测会怎样：规则答对了超支问题却被判不合格，每次都白调一次 AI（2-5 秒 + 一次额度）"])
def _():
    v = hybrid.judge("哪些项目超支了",
                     "SELECT name_ FROM pp_project "
                     "WHERE actual_amount_ > plan_amount_", 12)
    assert v.ok, v.reasons
    # 字符串等值不算「数值比较」——否则 state_ = 'finished' 会把超支问题混过去
    v2 = hybrid.judge("哪些项目超支了",
                      "SELECT name_ FROM pp_project WHERE state_ = 'finished'", 12)
    assert not v2.ok and any("数值比较" in r for r in v2.reasons), v2.reasons


@case("NLQ36", "nlq", "问「负责人/项目经理」但没关联人员表 → 判不合格", "P0",
      ["角色名存在 iform_party_employee 里，不 join 就只有一堆 UUID",
       "不测会怎样：结果里全是 id，用户看不出是谁"])
def _():
    v = hybrid.judge("项目经理负责的项目", "SELECT name_ FROM pp_project", 200)
    assert not v.ok and any("人员表" in r for r in v.reasons), v.reasons
    assert hybrid.judge(
        "项目经理负责的项目",
        "SELECT p.name_, e.name_ FROM pp_project p "
        "JOIN iform_party_employee e ON e.id_ = p.manager_id_", 200).ok


@case("NLQ37", "nlq", "问句点名的对象一个都没用到 → 判主表选错", "P0",
      ["问「有多少客户」却查项目表，数字差几个数量级",
       "不测会怎样：选表错误被当成正常答案返回"])
def _():
    v = hybrid.judge("有多少客户", "SELECT * FROM pp_project", 200,
                     expected_tables=["pp_customer"])
    assert not v.ok and any("不是问的那个对象" in r for r in v.reasons), v.reasons
    # 主表不是点名的对象、但确实用到了它（经合同连客户）→ 放过
    via = hybrid.judge("每个客户有多少项目",
                       "SELECT c.name_, COUNT(*) FROM pp_project p "
                       "JOIN pp_contract ct ON ct.id_ = p.contractId "
                       "JOIN pp_customer c ON c.id_ = ct.customerId GROUP BY c.name_",
                       20, expected_tables=["pp_customer"])
    assert via.ok, via.reasons


@case("NLQ38", "nlq", "0 行结果 → 判不合格（条件多半写歪了）", "P0",
      ["SQL 不报错但返回空，比报错更难发现",
       "不测会怎样：一个写死枚举值的条件让所有查询静默返回空表"])
def _():
    v = hybrid.judge("项目A有多少任务", "SELECT COUNT(*) FROM pp_task", 0)
    assert not v.ok and any("空" in r for r in v.reasons), v.reasons


@case("NLQ39", "nlq", "问句与 SQL 对得上时放行，且不产生任何理由", "P0",
      ["这是主路径：没问题就必须放过，否则每条都白调一次 AI",
       "不测会怎样：判太严导致规则引擎永不生效，成本与延迟全落在 AI 上"])
def _():
    v = hybrid.judge("每个项目有多少需求",
                     "SELECT p.name_, COUNT(*) FROM pp_project p "
                     "JOIN pp_requirement r ON r.projectId = p.id_ GROUP BY p.name_",
                     20)
    assert v.ok and v.reasons == [], v.reasons
    assert v.note == "看起来正常", v.note


@case("NLQ40", "nlq", "拿不准的情况一律放过（设计意图：宁可让规则答）", "P1",
      ["judge 只做能确定的判定",
       "不测会怎样：把「本月新增的项目」这种没问题的话判成错，白烧 AI 额度"])
def _():
    cases = [
        ("项目", "SELECT name_ FROM pp_project LIMIT 5"),
        ("所有项目", "SELECT name_ FROM pp_project"),
        ("本月新增的项目", "SELECT * FROM pp_project "
                          "WHERE MONTH(create_time_) = MONTH(NOW())"),
        ("每个客户有多少项目", "SELECT COUNT(*) FROM pp_project GROUP BY customerId"),
    ]
    for q, sql in cases:
        v = hybrid.judge(q, sql, 10)
        assert v.ok, f"{q} 被误判：{v.reasons}"


@case("NLQ41", "nlq", "「最X的N个」没排序也要判不合格", "P1",
      ["中文里「进度最高的5个项目」比「前5个项目」更常用",
       "不测会怎样：这类问句的规则答案不排序直接返回，且永远不转 AI（local._rank_order 里修过同一个坑）"])
def _():
    v = hybrid.judge("进度最高的5个项目", "SELECT * FROM pp_project", 200)
    assert not v.ok and any("排序" in r for r in v.reasons), v.reasons
    # 排了序就放过
    assert hybrid.judge("进度最高的5个项目",
                        "SELECT * FROM pp_project ORDER BY progress_ DESC LIMIT 5",
                        5).ok


@case("NLQ42", "nlq", "问句里的数字条件没进 SQL → 判不合格", "P0",
      ["「进度低于 50」被写成低于 80，结果看起来对但筛错了人",
       "不测会怎样：数字条件丢失，用户拿到的是另一个问题的答案"])
def _():
    v = hybrid.judge("进度低于50的项目",
                     "SELECT * FROM pp_project WHERE progress_ < 80", 30)
    assert not v.ok and any("50" in r for r in v.reasons), v.reasons
    assert hybrid.judge("进度低于50的项目",
                        "SELECT * FROM pp_project WHERE progress_ < 50", 30).ok


@case("NLQ43", "nlq", "不同 mode 下的「要不要转 AI」决策", "P0",
      ["local 模式必须完全不调模型（离线/无 Key 场景），llm 模式必须全走模型",
       "不测会怎样：local 模式下偷偷发请求，或者 hybrid 模式下永远不升级到 AI"])
def _():
    good, bad = hybrid.Verdict(True), hybrid.Verdict(False, ["问数量但没做统计"])
    assert hybrid.should_try_ai(bad, "local") is False
    assert hybrid.should_try_ai(bad, "llm") is True
    assert hybrid.should_try_ai(good, "llm") is True
    assert hybrid.should_try_ai(bad, "auto") is True
    assert hybrid.should_try_ai(good, "auto") is False
    assert hybrid.should_try_ai(good, "hybrid") is False
    assert hybrid.should_try_ai(bad, "hybrid") is True


@case("NLQ44", "nlq", "转 AI 的提示文案带上判定理由", "P1",
      ["用户要知道为什么被降级到 AI，否则会怀疑规则引擎乱答",
       "不测会怎样：失败原因只留在日志里，界面上只有一句「已用 AI 重答」"])
def _():
    note = hybrid.fallback_note(hybrid.Verdict(False, ["问数量但没做统计",
                                                       "问了超支/超期，但没做数值比较"]))
    assert "AI" in note and "问数量但没做统计" in note, note
    assert hybrid.Verdict(False).note == "看起来正常"


@case("NLQ45", "nlq", "只有确实答出来的 AI 结果才值得沉淀成示例", "P0",
      ["沉淀错了会把错误答案固化成永久规则（历史上被带偏过）",
       "不测会怎样：一次错的 AI 答案被存起来，同类问题从此全错"])
def _():
    # 存：有行、非 SELECT *、无一次性数字、问题够长
    assert hybrid.learn_worthy("每个客户有多少项目",
                               "SELECT COUNT(*) FROM pp_project", 3)
    # 不存：SELECT *（没写清要什么）
    assert not hybrid.learn_worthy("每个客户有多少项目",
                                   "SELECT * FROM pp_project", 3)
    # 不存：0 行
    assert not hybrid.learn_worthy("每个客户有多少项目",
                                   "SELECT COUNT(*) FROM pp_project", 0)
    # 不存：带具体数字的一次性问法（下次问「前 10 个」会命中「前 5」的 SQL）
    assert not hybrid.learn_worthy("前5个项目", "SELECT name_ FROM pp_project", 5)
    # 不存：问题太短，泛化性差
    assert not hybrid.learn_worthy("项目", "SELECT name_ FROM pp_project", 5)


@case("NLQ46", "nlq", "AI 结论与规则结论明显冲突时不沉淀", "P0",
      ["规则有结果、AI 换成完全不同的表 → 大概率是 AI 答错了",
       "不测会怎样：把 AI 的错误答案当成正确答案存进示例库"])
def _():
    # 两边主表完全不同、互不引用 → 冲突
    assert hybrid.conflicts("SELECT * FROM pp_task", 5,
                            "SELECT * FROM pp_project") is True
    # 同一张表 → 不冲突
    assert hybrid.conflicts("SELECT * FROM pp_project", 5,
                            "SELECT name_ FROM pp_project") is False
    # 规则本来 0 行 → 以 AI 为准，不判冲突
    assert hybrid.conflicts("SELECT * FROM pp_task", 0,
                            "SELECT * FROM pp_project") is False
    # 缺一侧 SQL → 无从比较
    assert hybrid.conflicts("", 5, "SELECT 1") is False


# ================================================================ 三、表关系图
# 跨对象问题（「每个客户有多少项目」）靠 BFS 找 join 路径，路径错了答案就错。

@case("NLQ47", "nlq", "外键关系被识别成图的边", "P0",
      ["关系图是跨表 join 的唯一依据",
       "不测会怎样：客户→合同→项目这条两跳路径根本不存在，跨对象问题全废"])
def _():
    edges = {e.src: e for e in graph.build_edges(_pack())}
    assert edges["pp_requirement"].dst == "pp_project"
    assert edges["pp_requirement"].col == "projectId"
    assert edges["pp_contract"].dst == "pp_customer"
    assert edges["iform_party_employee"].dst == "iform_party_org"
    assert len(graph.build_edges(_pack())) == 5, graph.build_edges(_pack())


@case("NLQ48", "nlq", "元数据里有、物理列不存在的属性不拿去 join", "P0",
      ["客户改名/删列后元数据会残留旧属性名",
       "不测会怎样：生成的 JOIN 引用不存在的列，SQL 直接报错"])
def _():
    a = _table("pp_a", "甲", 1, [_col("id_", "主键", "char", is_pk=True),
                                _col("b_id", "乙", "char", ref_table="pp_b")])
    b = _table("pp_b", "乙", 1, [_col("id_", "主键", "char", is_pk=True)])
    pack = _tiny([a, b], {"pp_a": [
        {"dst": "pp_b", "col": "b_id"},          # 物理列在 → 要收
        {"dst": "pp_b", "col": "gone_col"},      # 物理列已改名 → 丢掉
    ]})
    edges = graph.build_edges(pack)
    assert [e.col for e in edges] == ["b_id"], edges


@case("NLQ49", "nlq", "目标表不存在或自引用的关系被跳过", "P1",
      ["元数据里会有指向已删表、或指向自己的记录",
       "不测会怎样：join 一段指向不存在表的路径，或者自己 join 自己"])
def _():
    a = _table("pp_a", "甲", 1, [_col("id_", "主键", "char", is_pk=True)])
    pack = _tiny([a], {"pp_a": [
        {"dst": "pp_missing", "col": "id_"},   # 目标表不在库里
        {"dst": "pp_a", "col": "id_"},         # 自引用
    ], "pp_ghost": [{"dst": "pp_a", "col": "x"}]})   # 源表不在库里
    assert graph.build_edges(pack) == [], graph.build_edges(pack)


@case("NLQ50", "nlq", "环形关系不会死循环，最短路径仍然正确", "P0",
      ["客户↔合同这类双向关系在元数据里很常见",
       "不测会怎样：BFS 不记已访问节点时整个页面卡死"])
def _():
    x = _table("pp_x", "X", 1, [_col("id_", "主键", "char", is_pk=True),
                                _col("y_id", "Y", "char")])
    y = _table("pp_y", "Y", 1, [_col("id_", "主键", "char", is_pk=True),
                                _col("x_id", "X", "char")])
    pack = _tiny([x, y], {"pp_x": [{"dst": "pp_y", "col": "y_id"}],
                          "pp_y": [{"dst": "pp_x", "col": "x_id"}]})
    path = graph.shortest_path(pack, "pp_x", "pp_y")
    assert path is not None and len(path) == 1, path
    # 反方向也立刻返回，不能沿环一直走
    assert len(graph.shortest_path(pack, "pp_y", "pp_x")) == 1


@case("NLQ51", "nlq", "两跳路径：客户 → 合同 → 项目", "P0",
      ["客户和项目之间没有直接外键，「每个客户有多少项目」全靠这一跳",
       "不测会怎样：跨对象问题要么无解，要么连错表放大行数"])
def _():
    path = graph.shortest_path(_pack(), "pp_customer", "pp_project")
    assert path is not None, "客户到项目没有路径"
    assert len(path) == 2, [f"{e.src}.{e.col}->{e.dst}" for e in path]
    tables = {e.src for e in path} | {e.dst for e in path}
    assert tables == {"pp_customer", "pp_contract", "pp_project"}, tables


@case("NLQ52", "nlq", "同表返回空路径，不可达表返回空", "P1",
      ["孤立表（配置表）不该被硬连进来",
       "不测会怎样：为了连上孤立表乱造 JOIN，两表行数相乘出笛卡尔积"])
def _():
    assert graph.shortest_path(_pack(), "pp_project", "pp_project") == []
    assert not graph.shortest_path(_pack(), "pp_risk_config", "pp_project")
    assert not graph.shortest_path(_pack(), "pp_project", "pp_risk_config")


@case("NLQ53", "nlq", "join_clauses 生成带条件的 LEFT JOIN，方向正确", "P0",
      ["起点持有外键时，条件必须写在 id_ = 外键列 上",
       "不测会怎样：条件写反导致 join 不上，结果全空"])
def _():
    pack = _pack()
    path = graph.shortest_path(pack, "pp_project", "iform_party_employee")
    joins, aliases = graph.join_clauses(pack, "pp_project", path, "t")
    assert joins == ["LEFT JOIN `iform_party_employee` `j_party_employee` "
                     "ON `j_party_employee`.`id_` = `t`.`manager_id_`"], joins
    assert aliases["pp_project"] == "t"
    assert aliases["iform_party_employee"] == "j_party_employee"


@case("NLQ54", "nlq", "反向路径的条件方向也跟着反过来", "P1",
      ["从人员出发找项目时，外键在项目那侧",
       "不测会怎样：条件方向写死成一种，反查场景永远 join 不上"])
def _():
    pack = _pack()
    path = graph.shortest_path(pack, "iform_party_employee", "pp_project")
    joins, _ = graph.join_clauses(pack, "iform_party_employee", path, "t")
    assert joins == ["LEFT JOIN `pp_project` `j_project` "
                     "ON `t`.`id_` = `j_project`.`manager_id_`"], joins


@case("NLQ55", "nlq", "多值外键用 FIND_IN_SET 而不是等号", "P0",
      ["多值字段存的是逗号分隔的 id 串，`=` 永远匹配不上",
       "不测会怎样：多值外键（负责人、参与人）的 join 全部返回空"])
def _():
    audit = _table("pp_audit", "审计", 5, [
        _col("id_", "主键", "char", is_pk=True),
        _col("member_ids_", "成员", "char", multi=True, ref_table="iform_member")])
    member = _table("iform_member", "人员档案", 5, [
        _col("id_", "主键", "char", is_pk=True), _col("name_", "姓名")])
    pack = _tiny([audit, member], {"pp_audit": [
        {"dst": "iform_member", "col": "member_ids_", "multi": True}]})
    path = graph.shortest_path(pack, "pp_audit", "iform_member")
    joins, _ = graph.join_clauses(pack, "pp_audit", path, "t")
    assert "FIND_IN_SET(`j_member`.`id_`, `t`.`member_ids_`) > 0" in joins[0], joins
    # 反过来（人员出发找审计）同样走 FIND_IN_SET
    rev, _ = graph.join_clauses(pack, "iform_member",
                                graph.shortest_path(pack, "iform_member", "pp_audit"), "t")
    assert "FIND_IN_SET(`t`.`id_`, `j_audit`.`member_ids_`) > 0" in rev[0], rev


@case("NLQ56", "nlq", "别名撞车时自动加序号，不产生重复别名", "P1",
      ["pp_member 和 iform_member 去掉前缀后同名，都会变成 j_member",
       "不测会怎样：两个 JOIN 用同一个别名，MySQL 报「Not unique table/alias」"])
def _():
    audit = _table("pp_audit", "审计", 5, [
        _col("id_", "主键", "char", is_pk=True),
        _col("member_ids_", "成员", "char", multi=True, ref_table="iform_member")])
    a = _table("pp_member", "成员", 5, [_col("id_", "主键", "char", is_pk=True)])
    b = _table("iform_member", "人员档案", 5, [_col("id_", "主键", "char", is_pk=True)])
    pack = _tiny([audit, a], {"pp_audit": [
        {"dst": "iform_member", "col": "member_ids_", "multi": True}]})
    # 目标表不在 pack 里时先补齐（上面的 relations 只是为了让边成立）
    pack.tables[b.name] = b
    path = graph.shortest_path(pack, "pp_audit", "iform_member")
    _, first = graph.join_clauses(pack, "pp_audit", path, "t")
    _, second = graph.join_clauses(pack, "pp_audit", path, "t", set(first.values()))
    assert first["iform_member"] == "j_member", first
    assert second["iform_member"] == "j_member2", second
    assert len({*first.values()} & {*second.values()}) == 1, \
        f"两次调用应为第二张表换名，实际 {first} / {second}"


@case("NLQ57", "nlq", "多跳路径每一跳一个 JOIN，且用上一跳的别名", "P1",
      ["两跳路径的第二个 JOIN 必须连到第一个 JOIN 的别名上",
       "不测会怎样：两跳 join 全连到主表，结果变成笛卡尔积"])
def _():
    pack = _pack()
    path = graph.shortest_path(pack, "pp_project", "pp_customer")
    joins, aliases = graph.join_clauses(pack, "pp_project", path, "t")
    assert len(joins) == 2, joins
    assert "`j_contract`.`id_` = `t`.`contractId`" in joins[0], joins
    assert "`j_customer`.`id_` = `j_contract`.`customerId`" in joins[1], joins
    assert aliases == {"pp_project": "t", "pp_contract": "j_contract",
                       "pp_customer": "j_customer"}, aliases


@case("NLQ58", "nlq", "连接度最高的表排最前，且能截断", "P2",
      ["「哪些对象是中心」的判断依赖这个排名",
       "不测会怎样：把配置表当成中心对象，问句被导向错误的主表"])
def _():
    hubs = graph.reachable_hubs(_pack())
    assert hubs[0][1] == "pp_project", hubs          # 项目连了 3 条边，最多
    assert hubs[0][0] == 3, hubs
    degrees = [d for d, _, _ in hubs]
    assert degrees == sorted(degrees, reverse=True), hubs
    assert all(label for _, _, label in hubs), hubs   # 中文名要能取到
    assert len(graph.reachable_hubs(_pack(), top=2)) == 2


# ================================================================ 四、问句选表
# 主表选错 = 答案错，而且用户看不出来（表面上有数据）。

@case("NLQ59", "nlq", "「每个 X 有多少 Y」拆出分组对象与被统计对象", "P0",
      ["中文句子的核心在句尾，句首多是修饰",
       "不测会怎样：「每个项目有多少阶段」会落到项目表，数字差一个量级"])
def _():
    assert select.count_target("每个项目有多少阶段") == ("项目", "阶段")
    assert select.count_target("每个客户有多少条合同") == ("客户", "合同")


@case("NLQ60", "nlq", "「每个 X 的 Y 数」同样拆得出来", "P0",
      ["「任务数」这种省略「多少」的说法很常见",
       "不测会怎样：只认「有多少」，带「数」的问句全部退化成明细"])
def _():
    assert select.count_target("每个开发迭代的任务数") == ("开发迭代", "任务")


@case("NLQ61", "nlq", "「X 最多的 Y」这种倒装语序", "P0",
      ["「成员最多的项目」和「每个项目有多少成员」意思一样，语序相反",
       "不测会怎样：倒装句落到被统计对象上，问项目答成成员"])
def _():
    assert select.count_target("成员最多的项目") == ("项目", "成员")


@case("NLQ62", "nlq", "「每个 X 的 Y 合计」与「X 都有哪些 Y」", "P1",
      ["合计类问句的主表该落在被统计的量上",
       "不测会怎样：「每个项目的工时合计」跑到项目表去 count 项目数"])
def _():
    assert select.count_target("每个项目的工时合计") == ("项目", "工时")
    assert select.count_target("项目都有哪些里程碑") == ("", "里程碑")


@case("NLQ63", "nlq", "没有分组句式时返回 None，不硬编", "P1",
      ["普通明细问句不该被当成统计",
       "不测会怎样：所有问句都被加上 GROUP BY，明细查询全废"])
def _():
    assert select.count_target("项目有几个") is None
    assert select.count_target("今年有多少个项目超支了") is None


@case("NLQ64", "nlq", "问句点名的对象优先当主表", "P0",
      ["这是选表的主路径", "不测会怎样：问需求查到项目表，数量对不上"])
def _():
    pack = _pack()
    assert select.choose(pack, "今年有多少个项目超支了",
                         local._label_variants).table.name == "pp_project"
    assert select.choose(pack, "有多少个需求",
                         local._label_variants).table.name == "pp_requirement"
    assert select.choose(pack, "有多少个合同",
                         local._label_variants).table.name == "pp_contract"


@case("NLQ65", "nlq", "问句点名的字段给所属表加权", "P0",
      ["「合同金额」只有合同表有，光靠对象名会选到项目表",
       "不测会怎样：问合同金额答项目金额，数字差得离谱但看着合理"])
def _():
    ch = select.choose(_pack(), "合同金额", local._label_variants)
    assert ch.table.name == "pp_contract", (ch.table.name, ch.reasons)
    assert any("字段" in r for r in ch.reasons), ch.reasons


@case("NLQ66", "nlq", "语序纠正：被统计对象压过句首的分组对象", "P0",
      ["「每个项目有多少需求」主表必须是需求表",
       "不测会怎样：按项目数回答「有多少需求」，两个数不是一个量级"])
def _():
    ch = select.choose(_pack(), "每个项目有多少需求", local._label_variants)
    assert ch.table.name == "pp_requirement", (ch.table.name, ch.reasons)
    assert any("被查询对象" in r for r in ch.reasons), ch.reasons


@case("NLQ67", "nlq", "一个对象都认不出来时明确报错，而不是乱选一张", "P0",
      ["选错表比报错更糟：用户会相信那个数字",
       "不测会怎样：问「今天天气」也返回一张业务表的全量数据"])
def _():
    pack = _tiny([_table("pp_risk_config", "风险等级配置", 0,
                         [_col("id_", "主键", "char", is_pk=True)])])
    try:
        select.choose(pack, "今天天气怎么样", local._label_variants)
    except KeyError as exc:
        assert "没认出" in str(exc), exc
    else:
        raise AssertionError("认不出来却选出了主表")


@case("NLQ68", "nlq", "schema linking 带上外键目标表、去重且不超上限", "P1",
      ["大模型要写子查询就必须看到关联表，但全表塞进 prompt 会爆 token",
       "不测会怎样：模型看不到 pp_customer 就编一个不存在的列名"])
def _():
    pack = _pack()
    picked = select.link_tables(pack, "客户有多少合同", local._label_variants)
    assert "pp_contract" in picked and "pp_customer" in picked, picked
    assert len(picked) == len(set(picked)), f"有重复：{picked}"
    assert len(picked) <= 10 + 4, picked          # 默认 limit=10，允许 +4 缓冲
    # 两跳中间表要被补进来（客户→合同→项目）
    two = select.link_tables(pack, "每个客户有多少项目", local._label_variants)
    assert "pp_contract" in two, two


# ================================================================ 五、规则引擎
# local.build 不经过大模型直接出 SQL —— 它出的 SQL 也必须能过安全闸。

@case("NLQ69", "nlq", "排除/不包含/去掉这类否定都识别得出来", "P1",
      ["否定识别错方向，条件就从「排除」变成「只看」",
       "不测会怎样：「排除状态是已关闭的」返回的全是已关闭的项目"])
def _():
    for text in ("排除已关闭的项目", "不包含测试的项目", "去掉已归档的项目",
                 "不看已完成的项目", "没有超支的项目", "去除掉任务名称为根任务的"):
        assert local.has_negation(text), f"没识别出否定：{text}"


@case("NLQ70", "nlq", "普通问句不会被误判成否定", "P0",
      ["误判否定会把正常条件翻成 NOT LIKE，结果反过来",
       "不测会怎样：「有风险的项目」返回「没有风险的项目」"])
def _():
    for text in ("所有项目", "有风险的项目", "超过预算的项目", "进行中的项目",
                 "项目经理负责的项目"):
        assert not local.has_negation(text), f"误判成否定：{text}"


@case("NLQ71", "nlq", "「未完成」不算子句否定（值级否定另有出口）", "P1",
      ["「未完成」是对**值**的否定，不是对子句的否定",
       "has_negation 只判子句级（排除/不包含）；值级由 NEG_PAIRS 翻成「已…」",
       "不测会怎样：把「状态未完成」写成 NOT LIKE '%完工%' 反而查不到"])
def _():
    assert not local.has_negation("未完成")
    assert not local.has_negation("未关闭")
    # 但带明确否定动词的仍然要认
    assert local.has_negation("排除未完成的项目")


@case("NLQ72", "nlq", "否定词的作用域只看条件前一小段", "P1",
      ["否定词离得太远就不该影响这个条件",
       "不测会怎样：一句话里前一个「没有」把后面所有条件全翻成否定"])
def _():
    q = "没有超支的项目"
    assert local.is_negated(q, q.find("超支"), q.find("超支")) is True
    q2 = "有风险的项目"
    assert local.is_negated(q2, q2.find("风险"), q2.find("风险")) is False
    q3 = "项目状态不是已完成"
    assert local.is_negated(q3, q3.find("是"), q3.find("状态")) is True


@case("NLQ73", "nlq", "归一化去掉空格与「的/地/得」", "P1",
      ["问句和字段标签之间差一个「的」就匹配不上",
       "不测会怎样：「项目的名称」认不出「项目名称」这个字段"])
def _():
    assert local._norm("  项 目 的 名 称 ") == "项目名称"
    assert local._norm("实际地进度") == "实际进度"
    assert local._norm(None) == ""


@case("NLQ74", "nlq", "空问题明确报「无法理解」而不是编一条 SQL", "P0",
      ["用户误点回车时不该去查生产库",
       "不测会怎样：空问题产出一条全表查询，白扫 20 万行"])
def _():
    for q in ("", "   ", "\n"):
        try:
            local.build(_pack(), q)
        except local.NotUnderstood as exc:
            assert "空" in str(exc), exc
        else:
            raise AssertionError(f"空问题没被拦下：{q!r}")


@case("NLQ75", "nlq", "规则引擎产出的 SQL 全部能过安全闸", "P0",
      ["规则引擎是另一条生成路径，同样不能绕过 guard",
       "不测会怎样：规则引擎拼出的 SQL 带写操作或多表，直接打到生产库"])
def _():
    pack = _pack()
    for q in ("项目名称和创建时间", "每个项目有多少需求", "进度最高的5个项目",
              "项目状态是进行中", "项目名称不能包含测试"):
        sql, note = local.build(pack, q)
        assert isinstance(note, str) and note, f"{q} 没有说明文案"
        safe = guard.validate(sql, _allowed(pack))     # 必须过闸
        assert safe.upper().startswith("SELECT"), safe


@case("NLQ76", "nlq", "选列问句带软删除过滤且用 IFNULL 包裹", "P0",
      ["平台上 deleted_ 有三种写法并存（全 NULL / N-Y / Y-N）",
       "不测会怎样：`deleted_ <> 'Y'` 会把全 NULL 的表整表过滤光（pp_task 601 行变 0 行）"])
def _():
    sql, _ = local.build(_pack(), "项目名称和创建时间")
    assert "`t`.`name_`" in sql and "`t`.`create_time_`" in sql, sql
    assert "IFNULL(`t`.`deleted_`, 'N') <> 'Y'" in sql, sql
    assert "LIMIT" in sql.upper(), sql


@case("NLQ77", "nlq", "中文枚举值被翻译成库里存的码", "P0",
      ["state_ 里存的是 inprogress，不是「进行中」",
       "不测会怎样：用中文去 LIKE 一个英文码列，永远返回 0 行"])
def _():
    sql, _ = local.build(_pack(), "项目状态是进行中")
    assert "'inprogress'" in sql, sql
    assert "进行中" not in sql, f"中文标签被直接写进 WHERE：{sql}"


@case("NLQ78", "nlq", "否定条件写成 NOT LIKE", "P0",
      ["「不能包含测试」写错方向就等于「只看测试」",
       "不测会怎样：用户拿到完全相反的清单"])
def _():
    sql, _ = local.build(_pack(), "项目名称不能包含测试")
    assert "NOT LIKE '%测试%'" in sql, sql
    assert " LIKE '%测试%'" not in sql.replace("NOT LIKE", ""), sql


@case("NLQ79", "nlq", "分组统计问句生成 COUNT + GROUP BY + 关联表", "P0",
      ["「每个项目有多少需求」是最常问的一类",
       "不测会怎样：只 COUNT 不 GROUP，用户拿到一个总数却以为看到的是分布"])
def _():
    sql, note = local.build(_pack(), "每个项目有多少需求")
    assert "COUNT(*)" in sql, sql
    assert "GROUP BY" in sql, sql
    assert "LEFT JOIN `pp_project`" in sql, sql
    assert "`t`.`projectId`" in sql, sql
    assert "分组统计" in note, note


@case("NLQ80", "nlq", "排名问句生成 ORDER BY DESC + LIMIT N", "P0",
      ["「进度最高的5个项目」不排序就是随机 5 个",
       "不测会怎样：老板看到的「最高 5 个」其实是随便 5 个"])
def _():
    sql, note = local.build(_pack(), "进度最高的5个项目")
    assert "ORDER BY `t`.`progress_` DESC" in sql, sql
    assert "LIMIT 5" in sql.upper(), sql
    assert "前 5" in note, note


@case("NLQ81", "nlq", "平均/合计问句不能生成 AVG(*) 这种非法聚合", "P1",
      ["AVG(*) / SUM(*) 在 MySQL 里是语法错误，整条 SQL 直接执行失败",
       "而 sqlglot 能解析它、安全闸也看不出问题，坏 SQL 会直达客户库",
       "不测会怎样：所有「平均/合计」问句要么报错要么走 AI 重试，规则引擎白跑"])
def _():
    import re

    def _bad(sql: str) -> str:
        m = re.search(r"\b(AVG|SUM)\(\s*\*\s*\)", sql, re.I)
        return m.group(0) if m else ""

    problems = []
    # 1) 样本语义层里能算的，必须聚合到**真实数值列**上
    for q, want in (("项目的平均进度", "AVG(`t`.`progress_`)"),
                    ("合同的合计金额", "SUM(`t`.`amount_`)")):
        sql, _ = local.build(_pack(), q)
        bad = _bad(sql)
        if bad:
            problems.append(f"{q} → {bad}")
        elif want not in sql:
            problems.append(f"{q} → 没聚合到真实列：{sql.replace(chr(10), ' ')[:80]}")
    # 2) 主表上根本没有可统计的数值列（样本里「需求」没有金额/工时列）：
    #    「明确说解析不了」或「退回非聚合查询」都算正确答案，
    #    唯一不允许的是 AVG(*)/SUM(*) 这种执行必然失败的 SQL。
    for q in ("平均金额", "所有任务的合计工时"):
        try:
            sql, _ = local.build(_pack(), q)
        except local.NotUnderstood:
            continue
        bad = _bad(sql)
        if bad:
            problems.append(f"{q} → {bad}")
    assert not problems, problems


@case("NLQ82", "nlq", "完全没意义的问句要抛 NotUnderstood 而不是给全表结果", "P1",
      ["local.build 的 docstring 承诺「解析不了的会明确说解析不了」",
       "「有数据 +3 / 有名称列 +2」这类结构分只该用来排同分，不该让一张没被提到的表变成候选",
       "不测会怎样：用户输入乱码也返回一张业务表的全部数据，看起来像答对了"])
def _():
    leaked = []
    for q in ("今天天气怎么样", "你好", "随便问点什么"):
        try:
            sql, _ = local.build(_pack(), q)
        except local.NotUnderstood:
            continue
        leaked.append(f"{q} → {sql.replace(chr(10), ' ')[:100]}")
    assert not leaked, "无意义问句照样出了 SQL：" + "；".join(leaked)
    # 别修过头：只点了对象名的问句必须照常出 SQL
    for q in ("有哪些项目", "所有项目", "本月新增的项目", "项目名称和创建时间"):
        sql, _ = local.build(_pack(), q)
        assert sql.upper().startswith("SELECT"), (q, sql)


@case("NLQ83", "nlq", "数值列不会被写成 LIKE '%高%'", "P2",
      ["progress_ LIKE '%高%' 在 MySQL 里恒不匹配，静默返回 0 行",
       "不测会怎样：用户问「高进度的项目」，得到一张空表还以为真的没有"])
def _():
    import re
    sql, _ = local.build(_pack(), "高进度的项目")
    assert "LIKE '%高%'" not in sql, sql
    assert "`progress_` LIKE" not in sql, sql
    # 数值列上永远不该出现「拿中文去模糊匹配」的写法
    assert not re.search(r"`progress_`\s+(NOT\s+)?LIKE\s+'.*[\u4e00-\u9fa5]", sql), sql
    # 「进度低于50」这种真数值条件必须仍然生效
    sql2, _ = local.build(_pack(), "进度低于50的项目")
    assert "`t`.`progress_` < 50" in sql2, sql2


@case("NLQ84", "nlq", "普通问句不会被语义模板劫持", "P1",
      ["语义模板是硬编码 SQL，命中即优先于规则和历史示例",
       "不测会怎样：一句普通问句命中报表模板，用户拿到完全不相干的报表"])
def _():
    assert local.match_template(_pack(), "项目名称和创建时间") is None
    assert local.match_template(_pack(), "有多少个需求") is None


@case("NLQ85", "nlq", "0 行时判断「这个结果是不是可疑」", "P1",
      ["0 行不报错但结果错，比报错更难发现",
       "不测会怎样：条件写歪导致空表时不做一次自查，用户直接拿到空结果"])
def _():
    # 「列出/有哪些」问 0 行几乎都是条件写歪了 → 要自查
    assert engine._looks_wrongly_empty("本月创建的项目有哪些")
    assert engine._looks_wrongly_empty("列出所有项目")
    # 带具体数字条件时 0 行很正常 → 不猜
    assert not engine._looks_wrongly_empty("金额大于1000万的项目")
    # 没有「列出」类关键词不问
    assert not engine._looks_wrongly_empty("有多少个项目")
    assert not engine._looks_wrongly_empty("")
