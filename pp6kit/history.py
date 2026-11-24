"""本机出包档案；原输入、实际参数与产物独立保存，不改导入包。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from . import __version__
from .resources import snippets_path


def root() -> Path:
    return Path.home() / '.pp6kit' / 'history'


def save(source: bytes, output: bytes, name: str, params: dict, report: dict, environment: dict | None = None) -> str:
    base = root()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = uuid.uuid4().hex
    pending = base / ('.' + key)
    pending.mkdir(mode=0o700)
    try:
        metadata = {
            'schemaVersion': 2, 'environment': environment, 'id': key, 'createdAt': datetime.now().astimezone().isoformat(timespec='seconds'),
            'packageName': Path(name).name, 'toolVersion': __version__,
            'sourceSha256': hashlib.sha256(source).hexdigest(),
            'outputSha256': hashlib.sha256(output).hexdigest(),
            'snippetsSha256': hashlib.sha256(snippets_path().read_bytes()).hexdigest(),
            'params': params, 'report': report,
        }
        for filename, data in [('input.xlsx', source), ('output.zip', output),
                               ('record.json', json.dumps(metadata, ensure_ascii=False, indent=2).encode())]:
            target = pending / filename
            with target.open('xb') as stream:
                os.chmod(target, 0o600)
                stream.write(data)
        pending.rename(base / key)
    except Exception:
        shutil.rmtree(pending, ignore_errors=True)
        raise
    return key


def read(key: str, filename: str = 'record.json') -> bytes:
    if not re.fullmatch(r'[0-9a-f]{32}', key) or filename not in {'record.json', 'input.xlsx', 'output.zip'}:
        raise ValueError('无效的任务记录')
    return (root() / key / filename).read_bytes()


def recent() -> list[dict]:
    items = []
    for path in root().glob('*/record.json'):
        if not re.fullmatch(r'[0-9a-f]{32}', path.parent.name):
            continue
        try:
            row = json.loads(path.read_text())
            items.append({k: row[k] for k in ('id', 'createdAt', 'packageName', 'toolVersion')})
        except (OSError, ValueError, KeyError):
            continue
    return sorted(items, key=lambda x: x['createdAt'], reverse=True)[:100]


def compare(left: str, right: str) -> dict:
    import io
    import zipfile
    def entries(key):
        with zipfile.ZipFile(io.BytesIO(read(key, 'output.zip'))) as archive:
            return {info.filename: hashlib.sha256(archive.read(info)).hexdigest()
                    for info in archive.infolist() if not info.is_dir()}
    a, b = entries(left), entries(right)
    first, second = json.loads(read(left)), json.loads(read(right))
    return {'added': sorted(b.keys() - a.keys()), 'removed': sorted(a.keys() - b.keys()),
            'modified': sorted(k for k in a.keys() & b.keys() if a[k] != b[k]),
            'unchanged': sorted(k for k in a.keys() & b.keys() if a[k] == b[k]),
            'metadata': {k: {'before': first.get(k), 'after': second.get(k)}
                         for k in ('params', 'environment', 'sourceSha256', 'toolVersion', 'snippetsSha256')
                         if first.get(k) != second.get(k)}}
