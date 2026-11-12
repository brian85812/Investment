"""唯讀驗收報表不能洩漏憑證或把未授權當成功。"""
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch
import contextlib
import importlib.util
import io
import json
import os
import tempfile

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('futu_live_check', REPO / 'tools/verify_futu_live.py')
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


class LiveReportTests(TestCase):
    def test_report_redacts_messages_unknown_fields_and_non_numeric_price_strings(self):
        payload = {'code': -12006, 'message': 'SECRET_VALUE', 'api_key': 'SECRET_VALUE',
                   'data': {'private_key': 'SECRET_VALUE', 'kline_list': [
                       {'date': 20261111, 'time_key': 1794430800000, 'close': 'SECRET_VALUE', 'high': float('nan')}]}}
        result = live.public_response_sample(json.dumps(payload).encode())
        encoded = json.dumps(result, allow_nan=False)
        self.assertNotIn('SECRET_VALUE', encoded)
        self.assertEqual(result['code'], -12006)
        self.assertIn('[unexpected_type]', encoded)
        self.assertIn('[non_finite]', encoded)

    def test_missing_credentials_writes_failure_report_without_network(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('STOCK_TEST_TMPDIR')) as folder:
            report = Path(folder) / 'result.json'
            with patch.dict(os.environ, {}, clear=True), patch('sys.argv', ['verify_futu_live.py', '--report', str(report)]), \
                 patch.object(live.dashboard, 'fetch_robust_market_data') as network, \
                 contextlib.redirect_stdout(io.StringIO()):
                code = live.main()
            value = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(code, 2)
            self.assertEqual(value['status'], 'authorization_required')
            self.assertFalse(value['all_passed'])
            self.assertFalse(value['telegram_sent'])
            network.assert_not_called()


if __name__ == '__main__':
    main()
