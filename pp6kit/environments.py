"""本机实施档案，与数据库连接独立。"""
import copy
import hashlib
import json
import threading
import uuid
from pathlib import Path
from datetime import datetime
from .resources import snippets_path

_LOCK = threading.RLock()

def path():
    return Path.home() / '.pp6kit' / 'environments.json'

def fingerprint():
    return hashlib.sha256(snippets_path().read_bytes()).hexdigest()

def read():
    if path().exists():
        return json.loads(path().read_text())
    return {'activeId': 'default', 'items': [{'id': 'default', 'name': '默认环境', 'platformVersion': '', 'notes': '', 'mappings': {}, 'boundSnippetsSha256': fingerprint()}]}

def snapshot(key=None):
    data = read()
    item = next((x for x in data['items'] if x['id'] == (key or data['activeId'])), None)
    if item is None:
        raise ValueError('环境档案不存在，请重新选择')
    return {**copy.deepcopy(item), 'currentSnippetsSha256': fingerprint(), 'metadataSource': '未核验', 'metadataCollectedAt': None, 'verified': False}

def update(action, payload):
    with _LOCK:
        data = read()
        key = payload.get('id')
        if action == 'save':
            item = next((x for x in data['items'] if x['id'] == key), None)
            if item is None:
                item = {'id': uuid.uuid4().hex}
                data['items'].append(item)
            for field in ('name', 'platformVersion', 'notes'):
                value = payload.get(field, '')
                if not isinstance(value, str) or len(value) > 2000:
                    raise ValueError('环境字段无效')
                item[field] = value.strip()
            if not item['name']:
                raise ValueError('请填写环境名称')
            mappings = payload.get('mappings', {})
            if not isinstance(mappings, dict) or any(k not in {'moduleId', 'typeId', 'objectCode'} for k in mappings):
                raise ValueError('映射仅支持 moduleId、typeId、objectCode')
            if any(not isinstance(v, str) or len(v) > 200 for v in mappings.values()):
                raise ValueError('映射值无效')
            item['mappings'] = mappings
            bound = payload.get('boundSnippetsSha256')
            if bound is not None and (not isinstance(bound, str) or len(bound) != 64 or any(c not in '0123456789abcdef' for c in bound)):
                raise ValueError('骨架指纹无效')
            item.setdefault('boundSnippetsSha256', bound or fingerprint())
            if payload.get('rebind') is True:
                item['boundSnippetsSha256'] = fingerprint()
            item['updatedAt'] = datetime.now().astimezone().isoformat()
            data['activeId'] = item['id']
        elif action in ('select', 'delete'):
            if key not in [x['id'] for x in data['items']]:
                raise ValueError('环境不存在')
            if action == 'delete':
                if key == 'default':
                    raise ValueError('默认环境不能删除')
                data['items'] = [x for x in data['items'] if x['id'] != key]
                if data['activeId'] == key:
                    data['activeId'] = 'default'
            else:
                data['activeId'] = key
        else:
            raise ValueError('未知环境操作')
        target = path()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        temporary.chmod(0o600)
        temporary.replace(target)
        return data
