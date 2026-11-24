"""本轮实施档案、差异及口径的行为回归。"""
import io
import json
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch
from tests._harness import case
from pp6kit import environments as env, history
from pp6kit.nlq.explain import describe, decorate


def archive(entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        for name, content in entries.items():
            z.writestr(name, content)
    return out.getvalue()


@case('DEL01', 'delivery', '环境隔离与历史冻结，删除回默认', 'P0', ['快照不漂移', '映射白名单'])
def environments_roundtrip():
    with tempfile.TemporaryDirectory() as tmp, patch.object(env, 'path', return_value=Path(tmp)/'env.json'), patch.object(history, 'root', return_value=Path(tmp)/'history'):
        first = env.update('save', {'name':'A','mappings':{'moduleId':'123'}})['activeId']
        frozen = env.snapshot(first)
        key = history.save(b'in', archive({'a':'a'}), 'a.zip', {}, {}, frozen)
        second = env.update('save', {'name':'B'})['activeId']
        assert first != second
        env.update('save', {'id':first,'name':'changed'})
        assert json.loads(history.read(key))['environment']['name'] == 'A'
        env.update('delete', {'id':first})
        assert env.read()['activeId'] == 'default'
        try:
            env.update('save', {'name':'invalid','mappings':{'password':'secret'}})
        except ValueError:
            pass
        else:
            assert False, '敏感映射被接受'


@case('DEL02', 'delivery', 'ZIP条目增删改与相同记录', 'P0', ['真实解压内容比较'])
def compare_roundtrip():
    with tempfile.TemporaryDirectory() as tmp, patch.object(history,'root',return_value=Path(tmp)):
        a=history.save(b'in',archive({'same':'a','change':'b','remove':'c'}),'a.zip',{}, {})
        b=history.save(b'in',archive({'same':'a','change':'d','add':'e'}),'b.zip',{}, {})
        result=history.compare(a,b)
        assert result['added']==['add'] and result['removed']==['remove']
        assert result['modified']==['change'] and result['unchanged']==['same']
        assert history.compare(a,a)['modified']==[]


@case('DEL03', 'delivery', 'SQL口径聚合CTE子查询及澄清', 'P0', ['SQL证据', '不猜测口径'])
def sql_explanation():
    d=describe('WITH t AS (SELECT id FROM pp_task WHERE deleted_=0) SELECT COUNT(*) FROM t GROUP BY id HAVING COUNT(*)>1 LIMIT 5')
    assert d['filters'] and d['groups'] and d['having'] and d['aggregates'] and d['limits']
    assert 'pp_task' in d['tables']
    assert describe('SELECT * FROM (SELECT * FROM pp_project LIMIT 2) q LIMIT 1')['limits']
    assert 'explanation' not in decorate({'sql':'SELECT 1','clarify':True})
    assert decorate({'sql':'SELECT 1','rows':[]})['explanation']['status']=='已解析'
    assert describe('SELECT (')['status'].startswith('未识别')
