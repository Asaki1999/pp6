"""出包记录：原始字节、参数可追溯，非法路径不能读取。"""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from pp6kit import history
from tests._harness import case


@case('HIST01', 'history', '任务记录保留输入输出与参数，拒绝非法路径', 'P0', ['原始字节一致', '参数保留', '非法路径拒绝', '失败清理'])
def history_roundtrip():
    with tempfile.TemporaryDirectory() as tmp, patch.object(history, 'root', return_value=Path(tmp)):
        key = history.save(b'input', b'output', '测试.zip', {'X-Modules': '["bo"]'}, {'valid': True})
        assert history.read(key, 'input.xlsx') == b'input'
        assert history.read(key, 'output.zip') == b'output'
        record = json.loads(history.read(key))
        assert record['params']['X-Modules'] == '["bo"]'
        assert history.recent()[0]['id'] == key
        for bad in ('../outside', '', 'x' * 32):
            try:
                history.read(bad)
            except ValueError:
                pass
            else:
                raise AssertionError('非法路径被接受')
        try:
            history.read(key, '../record.json')
        except ValueError:
            pass
        else:
            raise AssertionError('非法文件名被接受')
        with patch.object(history, 'snippets_path', side_effect=OSError('test')):
            try:
                history.save(b'a', b'b', 'x.zip', {}, {})
            except OSError:
                pass
        assert len(list(Path(tmp).iterdir())) == 1


@case('HIST02', 'history', '中文 Windows（默认编码不是 UTF-8）也能列出任务记录', 'P0',
      ['record.json 是 UTF-8 写的，读的时候必须显式 utf-8',
       '不写 encoding 时 cp936 解码失败会被当成坏记录静默跳过（用户看到「记录为空」）'])
def history_reads_utf8_on_windows():
    with tempfile.TemporaryDirectory() as tmp, patch.object(history, 'root', return_value=Path(tmp)):
        key = history.save(b'in', b'out', '采购申请-导入包-20261124.zip', {}, {})
        real = Path.read_text

        def strict(self, encoding=None, errors=None):
            if encoding is None:
                # 模拟中文 Windows：open() 默认 cp936，UTF-8 的中文会炸
                raise UnicodeDecodeError('utf-8', b'\xd6\xd0', 0, 1, 'illegal multibyte sequence')
            return real(self, encoding=encoding, errors=errors)

        with patch.object(Path, 'read_text', strict):
            items = history.recent()
        assert items and items[0]['id'] == key, items
        assert items[0]['packageName'] == '采购申请-导入包-20261124.zip', items


@case('HIST03', 'history', '本机 JSON 配置的读写都显式 UTF-8', 'P1',
      ['read_text / write_text 不带 encoding 依赖系统 locale',
       '中文 Windows 上会写出 GBK、读到乱码或被静默跳过'])
def local_files_use_explicit_encoding():
    import ast
    root = Path(__file__).resolve().parents[1] / 'pp6kit'
    bad = []
    for path in root.rglob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ('read_text', 'write_text')
                    and not any(k.arg == 'encoding' for k in node.keywords)):
                bad.append(f'{path.relative_to(root)}:{node.lineno}')
    assert not bad, bad
