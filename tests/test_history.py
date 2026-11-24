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
