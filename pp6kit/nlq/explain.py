"""仅展示最终 SQL 可证实的技术口径，不猜业务含义。"""
import sqlglot
from sqlglot import exp

def describe(sql):
    result = {'sql': sql, 'status': '未识别'}
    try:
        tree = sqlglot.parse_one(sql, read='mysql')
        result.update(status='已解析', tables=sorted({x.name for x in tree.find_all(exp.Table)}),
                      filters=[x.sql(dialect='mysql') for x in tree.find_all(exp.Where)],
                      having=[x.sql(dialect='mysql') for x in tree.find_all(exp.Having)],
                      groups=[x.sql(dialect='mysql') for x in tree.find_all(exp.Group)],
                      aggregates=sorted({x.sql(dialect='mysql') for x in tree.find_all(exp.AggFunc)}),
                      limits=[x.sql(dialect='mysql') for x in tree.find_all(exp.Limit)])
    except Exception:
        result['status'] = '未识别，请核对原 SQL'
    return result

def decorate(result):
    if result.get('sql') and not result.get('clarify'):
        result['explanation'] = describe(result['sql'])
    return result
