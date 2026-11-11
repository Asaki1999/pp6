"""业务对象目录。

模板里 `object` 类型的字段，「关联对象/字典」必须填**目标系统里真实存在的对象编码**。
填错（比如把「项目」写成 `Project`、`pp_project`、`项目`）导入后那个下拉框是空的，
字段等于废掉，而且 check 原来查不出来——因为它只要求「非空」。

所以这里维护一份对象清单：
  1. 随包快照 assets/objects.json（PM 机器上没有库也能用）
  2. 能连上目标库时刷新 → ~/.pp6kit/objects.json（优先用这份）
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from .resources import resource_root

# 提示词里最多列多少个对象。太多会稀释注意力，按列名挑相关的就够。
PROMPT_LIMIT = 60

# 各业务域最常被引用的对象。不管列名有没有提到，都塞进候选，
# 因为「合同里挂个项目」这种关系，列名可能叫「关联号」之类的怪名字。
CORE = ("project", "customer", "contract", "product")

_cache: list[dict] | None = None
_cache_source = ""          # 目标库 / 本地缓存 / 随包快照
_refreshing = False

# 缓存多久算旧。对象不会天天变，一天一次足够，也避免每次提问都连库。
CACHE_TTL = 12 * 3600


def _data_home() -> Path:
    return Path.home() / ".pp6kit"


def cache_path() -> Path:
    return _data_home() / "objects.json"


def _read(path: Path) -> list[dict]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = raw.get("objects") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if not (isinstance(it, dict) and it.get("code")):
            continue
        row = {"code": str(it["code"]), "name": str(it.get("name") or it["code"])}
        # id / moduleId / forms / lists 是「导入按更新还是新增」的唯一依据，不能在这一步丢掉
        for k in ("id", "moduleId", "forms", "lists"):
            if it.get(k):
                row[k] = it[k]
        out.append(row)
    return out


def builtin() -> list[dict]:
    """随包快照。路径必须带 assets/——和 resources.snippets_path() / build/pp6.spec
    的 dest 一致，冻结后 resource_root() 就是 Resources 目录。
    """
    return _read(resource_root() / "assets" / "objects.json")


def catalog(force: bool = False) -> list[dict]:
    """当前可用的对象清单：本地缓存优先，其次随包快照。"""
    global _cache, _cache_source
    if _cache is not None and not force:
        return _cache
    cached = _read(cache_path())
    if cached:
        _cache, _cache_source = cached, "目标库"
    else:
        _cache, _cache_source = builtin(), "随包快照"
    return _cache


def status() -> dict:
    pool = catalog()
    age = None
    try:
        age = int((time.time() - cache_path().stat().st_mtime) / 60)
    except Exception:
        pass
    return {"count": len(pool), "source": _cache_source or "随包快照",
            "cache_age_minutes": age, "refreshing": _refreshing}


def _db_ready() -> dict | None:
    try:
        from .nlq import store
        db = store.load().get("db") or {}
    except Exception:
        return None
    return db if db.get("host") else None


def _cache_stale() -> bool:
    try:
        return (time.time() - cache_path().stat().st_mtime) > CACHE_TTL
    except Exception:
        return True


def ensure() -> list[dict]:
    """拿清单，顺手在后台刷一次（连得上库时）。

    不阻塞：第一次用的还是缓存/快照，刷新完下次就是最新的。
    接库超时 6 秒，放同步调用里会明显卡住「生成」按钮，所以走线程。
    """
    global _refreshing
    pool = catalog()
    if _refreshing or not _cache_stale() or not _db_ready():
        return pool

    def _work() -> None:
        global _refreshing
        try:
            refresh()
        except Exception:
            pass          # 连不上就用现有的，不打扰用户
        finally:
            _refreshing = False

    _refreshing = True
    threading.Thread(target=_work, name="pp6-objects-refresh", daemon=True).start()
    return pool


def save(items: list[dict]) -> int:
    global _cache, _cache_source
    clean, seen = [], set()
    for it in items or []:
        code = (it.get("code") or "").strip()
        name = (it.get("name") or code).strip()
        if not code or code in seen:
            continue
        seen.add(code)
        row = {"code": code, "name": name}
        for k in ("id", "moduleId", "forms", "lists"):
            if it.get(k):
                row[k] = it[k]
        clean.append(row)
    clean.sort(key=lambda x: x["code"])
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"objects": clean, "count": len(clean)},
                               ensure_ascii=False, indent=0), encoding="utf-8")
    _cache, _cache_source = clean, "目标库"
    return len(clean)


def refresh(cfg: dict | None = None) -> dict:
    """连目标库拉一遍「对象 + 表单/列表模板 key + 模块」的登记表。

    为什么必须查这些：导入 zip 时判断「更新还是新增」靠的是
    **iform_bo_def.id**；表单和列表却是靠**模板 key**（iform_form_def.key_ /
    iform_data_template_def.key_）。两套标识对不上就会出现
    「对象被更新了，表单却又建了一份新的」，系统里两份同名字段打架。

    所以这里不只存 code/name，还存 id 和已有的模板 key，
    出包时如果用户填的名字在库里存在，直接把 id / key 写进 zip。
    """
    from .nlq import introspect, store

    db = cfg or (store.load().get("db") or {})
    if not db.get("host"):
        raise RuntimeError("还没配置数据库连接，没法刷新对象清单")
    conn = introspect.connect(db["host"], db.get("port"), db.get("user"),
                              db.get("password"), db.get("database"))
    items: list[dict] = []
    try:
        cur = conn.cursor()
        cur.execute("SELECT id_, code_, name_, COALESCE(module_id_, '') "
                    "FROM iform_bo_def WHERE COALESCE(state_, '') <> 'deleted' ORDER BY code_")
        for bid, code, name, module_id in cur.fetchall():
            if not code:
                continue
            items.append({"code": code, "name": name or code,
                          "id": str(bid or ""), "moduleId": str(module_id or "")})

        # 表单模板：key → 模板 id
        forms: dict[str, list] = {}
        try:
            cur.execute("SELECT key_, name_, id_ FROM iform_form_def")
            for key, name, fid in cur.fetchall():
                if key:
                    forms.setdefault(key, []).append({"id": str(fid or ""), "name": name or ""})
        except Exception:
            pass

        # 列表模板：key → 模板 id + 它绑的 bo_code_
        lists: dict[str, list] = {}
        try:
            cur.execute("SELECT key_, name_, id_, COALESCE(bo_code_, '') FROM iform_data_template_def")
            for key, name, tid, bo_code in cur.fetchall():
                if key:
                    lists.setdefault(key, []).append(
                        {"id": str(tid or ""), "name": name or "", "boCode": bo_code or ""})
        except Exception:
            pass

        # 模块表是 module_config，主键就叫 id（不是 id_）
        try:
            cur.execute("SELECT id, module_name FROM module_config")
            modules = [{"id": str(r[0] or ""), "name": r[1] or ""} for r in cur.fetchall()]
        except Exception:
            modules = []          # 老版本没有模块表，不影响对象/模板

        dics = _query_dics(cur)

        # 对象关系（obj_relationship）：识别「新建对象该挂到哪个已有对象下」的证据。
        # 列名不带尾下划线，与 iform_* 表不同；方向是 src=子对象、dst=父对象。
        try:
            relationships = _query_relationships(cur)
        except Exception:
            relationships = None      # 老库没有这张表，降级用旧缓存/快照

        # 系统已有数据集 key 清单：自动创建值来源数据集前先查这里——
        # 真实系统自带 97 个 <对象>_value 数据集，撞 key 的重复导入会报错
        try:
            cur.execute("SELECT key_ FROM iform_data_dataset WHERE key_ IS NOT NULL")
            ds_keys = [r[0] for r in cur.fetchall() if r[0]]
        except Exception:
            ds_keys = []

        # 表单/列表按「它属于哪个对象」归到对象名下，出包时一眼能查到
        for it in items:
            prefix = it["code"]
            it["forms"] = {k: v for k, v in forms.items()
                           if k == prefix or k.startswith(prefix + "_")}
            it["lists"] = {k: v for k, v in lists.items()
                           if any(x.get("boCode") == prefix for x in v) or k.startswith(prefix + "_")}
    finally:
        conn.close()

    n = save(items)
    save_extra(forms=forms, lists=lists, modules=modules)
    save_extra(dics=dics)
    if ds_keys:
        save_extra(datasets=ds_keys)
    rel_count = save_relationships(relationships) if relationships is not None else -1
    return {"count": n, "forms": sum(len(v) for v in forms.values()),
            "lists": sum(len(v) for v in lists.values()), "modules": len(modules),
            "dics": len(dics), "datasets": len(ds_keys),
            "relationships": rel_count, "source": "目标库"}


def _relationships_cache_path() -> Path:
    return _data_home() / "relationships.json"


def _query_relationships(cur) -> list[dict]:
    """拉系统里已有的对象关系，作为「新对象该挂到哪」的参照。

    方向（已与真实导出包核对）：src_obj_code 是**子对象**（rel_field 属于它），
    dst_obj_code 是**父对象**。例如 project_create --project_id_--> project。
    """
    cur.execute("SELECT src_obj_code, src_obj_name, dst_obj_code, dst_obj_name, "
                "rel_field, rel_type FROM obj_relationship ORDER BY src_obj_code")
    return [{"src": r[0] or "", "srcName": r[1] or "",
             "dst": r[2] or "", "dstName": r[3] or "",
             "field": r[4] or "", "type": r[5] or "PARENT"}
            for r in cur.fetchall() if r[0] and r[2]]


def save_relationships(rows: list[dict]) -> int:
    """原子写入关系缓存。返回条数。"""
    import datetime
    payload = {"version": 1,
               "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "rows": rows}
    path = _relationships_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return len(rows)


def _query_dics(cur) -> dict[str, dict]:
    """拉系统里已有的数据字典及其候选值。

    数据库里的真实结构（2026-10-28 实测）：
        iform_cat_type  category_key_='DIC_TYPE'，type_key_ 是字典 key（如 sf / invoice_type）
        iform_cat_dic   候选值，type_id_ 指向 iform_cat_type.id_，
                        key_ 是值标识、name_ 是显示名、sn_ 是顺序

    dic 字段在 iform_bo_attr.object_type_ 里存的就是这个 type_key_，
    所以拿到这张表就能：① AI 分析时复用已有取值；② 出包时和系统里的字典对上，
    不会因为同名不同 key 出现两套字典。
    """
    out: dict[str, dict] = {}
    try:
        cur.execute("SELECT id_, type_key_, name_ FROM iform_cat_type "
                    "WHERE category_key_ = 'DIC_TYPE'")
        types = {str(tid): (key, name or key) for tid, key, name in cur.fetchall() if key}
        for tid, (key, name) in types.items():
            out[key] = {"key": key, "name": name, "values": []}
        cur.execute("SELECT type_id_, key_, name_, sn_ FROM iform_cat_dic ORDER BY sn_")
        for tid, val_key, label, _sn in cur.fetchall():
            hit = types.get(str(tid))
            if not hit:
                continue
            vals = out[hit[0]]["values"]
            text = (label or val_key or "").strip()
            if text and text not in vals:
                vals.append(text)
    except Exception:
        pass                    # 老库没有字典表，不影响出包
    return {k: v for k, v in out.items() if v["values"]}


# ---------------------------------------------------------------- 模板/模块登记

def _extra_path() -> Path:
    return _data_home() / "templates.json"


_extra: dict | None = None


def extra(force: bool = False) -> dict:
    """表单/列表模板与模块的登记表：{forms, lists, modules}。"""
    global _extra
    if _extra is not None and not force:
        return _extra
    try:
        _extra = json.loads(_extra_path().read_text(encoding="utf-8"))
    except Exception:
        _extra = {}
    _extra.setdefault("forms", {})
    _extra.setdefault("lists", {})
    _extra.setdefault("modules", [])
    _extra.setdefault("dics", {})
    return _extra


def save_extra(forms=None, lists=None, modules=None, dics=None, datasets=None) -> None:
    global _extra
    data = extra()
    if forms is not None:
        data["forms"] = forms
    if lists is not None:
        data["lists"] = lists
    if modules is not None:
        data["modules"] = modules
    if dics is not None:
        data["dics"] = dics
    if datasets is not None:
        data["datasets"] = datasets
    _extra_path().parent.mkdir(parents=True, exist_ok=True)
    _extra_path().write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
    _extra = data


def dics(force: bool = False) -> dict[str, dict]:
    """系统里已有的数据字典：{type_key: {key, name, values[]}}。

    有快照就用快照（PM 的机器不一定连得上库）；接了库并 refresh 过就是真实的。
    """
    return (extra(force=force).get("dics") or {})


def dict_values(name_or_key: str, limit: int = 40) -> list[str]:
    """按字典 key 或字典名称取候选值，命中就返回列表，否则空。

    用途：AI 分析非标准 Excel 时，如果某个选项字段的候选值和系统里已有字典
    对得上（名字一致或值高度重合），直接复用库里的值——
    避免同一套「状态/类别」在系统里存在两份，报表统计时对不上。
    """
    text = _norm_key(name_or_key)
    if not text:
        return []
    table = dics()
    # 先按 key 精确匹配，再按名称匹配
    for item in table.values():
        if _norm_key(item.get("key", "")) == text:
            return list(item.get("values") or [])[:limit]
    for item in table.values():
        if _norm_key(item.get("name", "")) == text:
            return list(item.get("values") or [])[:limit]
    return []


def _norm_key(text: str) -> str:
    """模板 key 归一：create_project / createProject / 新建通用项目 → 可比的形式。"""
    t = (text or "").strip()
    if not t:
        return ""
    if re.fullmatch(r"[A-Za-z0-9_]+", t):
        t = re.sub(r"(?<!^)(?=[A-Z])", "_", t).lower()
        return re.sub(r"[^a-z0-9]+", "_", t).strip("_")
    return t.lower()


def find_form(key_or_name: str, bo_code: str = "") -> dict | None:
    """按模板 key 或模板名称找已有表单。返回 {key, id, name} 或 None。"""
    raw = (key_or_name or "").strip()
    if not raw:
        return None
    data = extra()
    key = _norm_key(raw)
    if key in data["forms"]:
        hit = data["forms"][key][0]
        return {"key": key, "id": hit.get("id", ""), "name": hit.get("name", "")}
    # 按名称找（用户可能填「新建通用项目」）
    for k, rows in data["forms"].items():
        for r in rows:
            if (r.get("name") or "").strip() == raw:
                return {"key": k, "id": r.get("id", ""), "name": r.get("name", "")}
    return None


def find_list(key_or_name: str, bo_code: str = "") -> dict | None:
    """按模板 key 或名称找已有列表模板。"""
    raw = (key_or_name or "").strip()
    if not raw:
        return None
    data = extra()
    key = _norm_key(raw)
    if key in data["lists"]:
        hit = data["lists"][key][0]
        return {"key": key, "id": hit.get("id", ""), "name": hit.get("name", ""),
                "boCode": hit.get("boCode", "")}
    for k, rows in data["lists"].items():
        for r in rows:
            if (r.get("name") or "").strip() == raw:
                return {"key": k, "id": r.get("id", ""), "name": r.get("name", ""),
                        "boCode": r.get("boCode", "")}
    return None


def find_module(name: str) -> dict | None:
    raw = (name or "").strip()
    if not raw:
        return None
    for m in extra()["modules"]:
        if (m.get("name") or "").strip() == raw:
            return m
    return None


def lookup(name_or_code: str) -> dict:
    """用户填的名字/编码 → 库里已有的完整登记信息。

    返回：
      {exists, code, name, id, moduleId, forms{key:id}, lists{key:id}}
    exists=True 表示必须在 zip 里写回原 id，否则导入会新增一次。

    **只认精确命中**（编码相等 / 名称相等），不做「互相包含」的模糊匹配：
    「项目绩效看板」包含「项目」，模糊匹配会把它绑到 project 的 id 上，
    导入时直接把别人的项目对象覆盖掉——比多建一个对象严重得多。
    """
    raw = (name_or_code or "").strip()
    pool = catalog()
    if not raw:
        return {"exists": False}

    def _pack(it: dict) -> dict:
        return {
            "exists": True, "code": it["code"], "name": it.get("name", ""),
            "id": it.get("id", "") or "", "moduleId": it.get("moduleId", "") or "",
            "forms": {k: (v[0].get("id", "") if v else "")
                      for k, v in (it.get("forms") or {}).items()},
            "lists": {k: (v[0].get("id", "") if v else "")
                      for k, v in (it.get("lists") or {}).items()},
        }

    # ① 编码精确（含 pp_project → project 的归一）
    code = _norm(raw)
    for it in pool:
        if it["code"] == raw or _norm(it["code"]) == code:
            return _pack(it)
    # ② 名称精确
    for it in pool:
        if (it.get("name") or "").strip() == raw:
            return _pack(it)
    return {"exists": False, "code": code or raw}


def _norm(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"\s+", "", t)
    t = t.replace("-", "_")
    if t.startswith("pp_"):
        t = t[3:]
    return t


def resolve(text: str, items: list[dict] | None = None) -> str | None:
    """把用户/AI 写的各种说法归一到真实对象编码。

    `Project` / `pp_project` / `项目` / `项目表` → `project`
    认不出来返回 None。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    pool = items if items is not None else catalog()
    if not pool:
        return None
    key = _norm(raw)

    # 1) 编码精确匹配
    for it in pool:
        if _norm(it["code"]) == key:
            return it["code"]
    # 2) 名称精确匹配
    for it in pool:
        if (it["name"] or "").strip() == raw:
            return it["code"]
    # 3) 名称互相包含，两个方向必须分开判，因为「谁更长」代表的意思正好相反：
    #    - 原文更长（「所属项目」「关联客户」）：对象名是原文的一部分，
    #      名字越长限定越多、越贴近用户想说的那个对象 → 取**最长**
    #    - 原文更短（「项目阶段」→「项目阶段表」）：原文是对象名的前缀，
    #      名字越短说明这层包含关系越紧密 → 取**最短**
    #    以前两个方向混在一起统一取最长，「项目」配上候选里的「项目阶段」
    #    就会串到项目阶段上，把好好的关联项目字段改成错的对象。
    inside = None       # 对象名 ⊂ 原文
    prefix = None       # 原文 ⊂ 对象名
    for it in pool:
        name = (it.get("name") or "").strip()
        if len(name) < 2:
            continue
        if name in raw:
            if inside is None or len(name) > len(inside[1]):
                inside = (it["code"], name)
        elif raw in name:
            if prefix is None or len(name) < len(prefix[1]):
                prefix = (it["code"], name)
    # 原文正好是某个对象名的开头，比「原文里夹了个对象名」更明确，优先采信
    best = prefix or inside
    return best[0] if best else None


def match_exact(text: str, items: list[dict] | None = None) -> str | None:
    """表头/字段名**正好是某个对象名**时返回它的编码。

    只认「客户」=「客户」这种，不做包含匹配——
    否则「合同名称」会被判成关联「合同」、「项目进度」会被判成关联「项目」，
    把一个普通文本列变成下拉框，错得更隐蔽。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    pool = items if items is not None else catalog()
    for it in pool:
        if (it.get("name") or "").strip() == raw:
            return it["code"]
    # 「所属客户」「关联项目」「选择产品」这类，去掉前缀再比一次
    for pre in ("所属", "关联", "对应", "挂靠", "选择", "指定"):
        if raw.startswith(pre) and len(raw) > len(pre) + 1:
            return match_exact(raw[len(pre):], pool)
    return None


def label(code: str, items: list[dict] | None = None) -> str:
    pool = items if items is not None else catalog()
    for it in pool:
        if it["code"] == code:
            return it["name"]
    return ""


def prompt_block(headers: list[str] | None = None,
                 items: list[dict] | None = None) -> str:
    """给模型的「可用对象」段落。

    有两种写法，取决于知不知道列名：
    - 知道列名（读表分析）→ 先列相关候选（带中文名），剩下的只报编码，省 token
    - 不知道（一句话生成）→ 全量列 code｜name，让它自己找
    """
    pool = items if items is not None else catalog()
    if not pool:
        return ""
    head = ("【本系统已有的业务对象】\n"
            "object 类型的 objectType 只能填下面出现过的**编码**，"
            "不要自造编码、不要填中文名（填「项目」而不是 project 会导入后下拉为空）：")
    if not headers:
        lines = [head] + [f"- {it['code']}｜{it['name']}" for it in pool]
        return "\n".join(lines)

    picked = rank(headers, PROMPT_LIMIT, pool)
    lines = [head]
    if picked:
        lines.append("和本表列名相关的候选：")
        lines += [f"- {it['code']}｜{it['name']}" for it in picked]
        taken = {p["code"] for p in picked}
        rest = [it for it in pool if it["code"] not in taken]
        if rest:
            lines.append("其余可用编码：" + " ".join(it["code"] for it in rest))
    else:
        lines += [f"- {it['code']}｜{it['name']}" for it in pool]
    return "\n".join(lines)


def rank(headers: list[str], limit: int = PROMPT_LIMIT,
         items: list[dict] | None = None) -> list[dict]:
    """按表头挑相关对象，给模型当候选。

    「所属项目」这类列名里含「项目」，自然把 `project` 顶上来；
    另外固定带上 CORE 里几个各业务域最常被引用的对象。
    """
    pool = items if items is not None else catalog()
    if not pool:
        return []

    texts = [(h or "").strip() for h in headers if (h or "").strip()]
    scored: list[tuple[float, dict]] = []
    for it in pool:
        name = (it.get("name") or "").strip()
        code = it["code"]
        score = 0.0
        for h in texts:
            # 「所属项目」里含「项目」→ 这才是「表头点名要这个对象」的强信号
            if len(name) >= 2 and name in h:
                score += len(name) * 3.0
            elif len(h) >= 3 and h in name:
                # 反过来只给弱分，否则「项目类型项目状态参数配置」会靠堆字排第一
                score += 0.5
            if len(_norm(code)) >= 3 and _norm(code) in _norm(h):
                score += len(code) * 2.0
        if code in CORE:
            score += 2.0
        if score >= 2.0:
            scored.append((score, it))

    scored.sort(key=lambda x: (-x[0], len(x[1]["name"]), x[1]["code"]))
    out, seen = [], set()
    for _, it in scored:
        if it["code"] in seen:
            continue
        seen.add(it["code"])
        out.append(it)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 出包前对表

# 归一结果记在 Project 上的属性名。归一要跑两次（读表一次、出包一次），
# 靠它避免重复生成提示，也避免第二次把第一次的结果再「归一」一遍。
_MEMO_ATTR = "_object_field_memo"


def field_pool(project) -> list[dict]:
    """object 字段归一用的候选池：本包对象在前，系统已有对象在后。

    本包必须排在前面：一个包里新建的对象 A 引用同包里新建的对象 B 时，
    系统里根本还没有 B，只有本包能提供 B 的编码和名称。
    """
    pool: list[dict] = []
    seen: set[str] = set()
    for bo in getattr(project, "bos", None) or []:
        code = (getattr(bo, "code", "") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        name = (getattr(bo, "name", "") or "").strip() or code
        pool.append({"code": code, "name": name})
    for it in catalog():
        code = (it.get("code") or "").strip()
        if code and code not in seen:
            seen.add(code)
            pool.append({"code": code, "name": (it.get("name") or code).strip()})
    return pool


def _field_signature(project) -> tuple:
    """object 字段的当前状态指纹，用来判断归一结果还能不能用。"""
    sig = []
    for bo in getattr(project, "bos", None) or []:
        for f in getattr(bo, "fields", None) or []:
            if getattr(f, "type_new", "") == "object":
                sig.append((bo.code, f.code, f.name or "", f.object_type or ""))
    return tuple(sig)


def resolve_object_fields(project) -> list[str]:
    """把 project 里所有 object 字段的「关联对象/字典」对到真实对象编码，返回提示文案。

    为什么需要这一步：Excel 里这一列用户会填「项目」（中文名）、
    「pp_project」（对象表名）、「关联客户」（带前缀的叫法），而系统只认
    iform_bo_def.code_（project / customer）——226 个 object 字段里 224 个的
    object_type_ 就等于对象编码。填错的字段导入后是一个**空下拉框**，
    而且不报错，业务上等于这个字段废了。

    规则（顺序即优先级）：
      1. 候选池 = 本包对象 + 系统对象清单，本包在前（同包内互相引用要能命中）
      2. 已填值 → resolve()，能对上就改写；对不上保持原值并提示
      3. 留空 → 只认「字段名/字段编码正好是对象名」（match_exact），
         **不用 resolve 做推断**：包含匹配会把「项目进度」判成 project，
         把普通文本字段变成指向错误对象的下拉框，比留空更难发现
      4. 都认不出 → 保持原值（留空就留空），提示导入后是空下拉

    幂等：结果缓存在 project 上，同一个 project 重复调用返回同样的文案，
    字段被改过（指纹变了）才会重跑。

    只改内存里的 BoField.object_type，不写库、不改 Excel。
    """
    sig = _field_signature(project)
    memo = getattr(project, _MEMO_ATTR, None)
    if isinstance(memo, dict) and memo.get("sig") == sig:
        return list(memo.get("notes") or [])

    notes: list[str] = []
    pool = field_pool(project)
    # 「有没有真实的系统对象清单」和「池子里有没有东西」是两件事：
    # 没有清单时 pool 里只剩本包对象，此时说「系统里找不到 project」会误导用户，
    # 正确的说法是「清单还没加载，压根没核对」。
    sys_known = bool(catalog())
    if pool:
        for bo in project.bos:
            for f in bo.fields:
                if f.type_new != "object":
                    continue
                label_text = f.name or f.code
                raw = (f.object_type or "").strip()
                if raw:
                    hit = resolve(raw, pool)
                    if hit and hit != raw:
                        f.object_type = hit
                        notes.append(
                            f"关联对象「{raw}」→ {hit}（{label(hit, pool)}）："
                            f"只有对象编码才能在导入后显示出下拉数据")
                    elif not hit:
                        if sys_known:
                            notes.append(
                                f"字段「{label_text}」的关联对象「{raw}」在系统对象清单里找不到，"
                                f"已保持原值；导入后这个下拉框会是空的，"
                                f"请确认填的是对象编码（如 project）还是对象中文名（如 项目）")
                        else:
                            notes.append(
                                f"字段「{label_text}」的关联对象「{raw}」无法校验：本地没有对象清单"
                                f"（未连库、也没有随包快照），已按原值生成；"
                                f"请确认填的是对象编码（如 project）")
                    continue
                hit = (match_exact(f.name or "", pool)
                       or match_exact(f.code or "", pool))
                if hit:
                    f.object_type = hit
                    notes.append(
                        f"字段「{label_text}」关联对象留空，已按字段名推断为 {hit}（{label(hit, pool)}）")
                elif sys_known:
                    notes.append(
                        f"字段「{label_text}」关联对象留空，且无法从字段名推断出关联对象；"
                        f"请在「关联对象/字典」里填对象编码（如 project），否则导入后是空下拉框")
                else:
                    notes.append(
                        f"字段「{label_text}」关联对象留空，且无法校验：本地没有对象清单"
                        f"（未连库、也没有随包快照），无法从字段名推断；"
                        f"请直接填对象编码（如 project）")

    setattr(project, _MEMO_ATTR, {"sig": _field_signature(project), "notes": list(notes)})
    return notes


def bind(project) -> list[str]:
    """把用户填的名字/编码，对到目标库里已有的对象 id 和模板 key 上。

    三条规则（都建立在「导入按 id 判断更新，表单/列表按 key 判断」之上）：

    1. 对象名称/编码在库里对得上 → **只提示，不再静默写 exist_id**（2026-11-11：
       新建同名对象曾被误判成更新、导入覆盖线上对象；要更新请在 Excel 的
       「已有对象ID」列显式填 id，或用反解链路——反解会自带 id）
    2. 表单 key 在库里已有 → 写回 exist_key，不新建一份同名表单
    3. 列表 key 在库里已有 → 同上
    4. 模块填的是名字（如「项目」）→ 换成 module_config.id

    连不上库就什么都不做，行为和以前一致（全部新建）。
    """
    notices: list[str] = []
    pool = catalog()
    # 只有「真从库里刷回来」的清单才做绑定：随包快照只为「认名字/编码」而生，
    # 里面没有 id / 模板 key，拿它绑会给出「系统里已有，按更新处理」的假提示，
    # 而实际什么都没绑上（exist_id 还是空）。离线时保持原来的「全部新建」。
    if not pool or _cache_source != "目标库":
        return notices

    for bo in project.bos:
        hit = lookup(bo.name) or {}
        if not hit.get("exists"):
            hit = lookup(bo.code) or {}
        if hit.get("exists"):
            if bo.exist_id:
                # 用户显式填了「已有对象ID」——这是明确决定，照它走
                notices.append(
                    f"对象「{bo.name}」按你填的已有对象ID {bo.exist_id} 导入，"
                    f"将更新系统里的「{hit['code']}」")
            else:
                # 不再静默绑 id（2026-11-11 修复）：以前名称对上就写 exist_id，
                # 新建一个叫「项目」的对象会被当成更新系统 project，导入直接
                # 覆盖线上对象。现在只提示，让用户显式决定。
                notices.append(
                    f"⚠ 对象「{bo.name}」与系统已有对象「{hit['code']}」同名"
                    f"（id {hit.get('id', '')}）。本次按【新建】导入（不会动系统对象）；"
                    f"若要更新它，请在 1_业务对象 的「已有对象ID」列填 {hit.get('id', '')}；"
                    f"若担心混淆，请给新对象换个名字")
            if not bo.module_id and hit.get("moduleId"):
                bo.module_id = hit["moduleId"]   # 模块归属补齐，无覆盖风险

    form_keys = {k for it in catalog() for k in (it.get("forms") or {})}
    for form in project.forms:
        if form.exist_key:
            continue
        if form.key in form_keys:
            form.exist_key = extra()["forms"].get(form.key, [{}])[0].get("id", "")
            if form.exist_key:
                notices.append(f"表单 {form.key} 系统里已有同名模板，沿用原模板（不新建）")

    list_keys = {k for it in catalog() for k in (it.get("lists") or {})}
    for lst in project.lists:
        if lst.exist_key:
            continue
        if lst.key in list_keys:
            lst.exist_key = extra()["lists"].get(lst.key, [{}])[0].get("id", "")
            if lst.exist_key:
                notices.append(f"列表 {lst.key} 系统里已有同名模板，沿用原模板（不新建）")

    # 模块：用户可能直接填「项目」这种名字
    mod_names = {m["name"]: m["id"] for m in extra()["modules"] if m.get("id")}
    for obj in [project, *project.bos]:
        cur = getattr(obj, "module_id", "") or ""
        if cur and cur in mod_names.values():
            continue
        if cur in mod_names:
            setattr(obj, "module_id", mod_names[cur])
            notices.append(f"模块「{cur}」→ 已替换为模块 id {mod_names[cur]}")
    return notices
