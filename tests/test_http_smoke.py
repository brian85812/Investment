"""實際 Flask 模組/路由煙霧測試；固定時鐘與快取，不抓行情。"""
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest import TestCase, main
from unittest.mock import patch
import copy
import socket
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as dashboard


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        fixed = datetime(2026, 11, 4, 17, tzinfo=ZoneInfo('America/New_York'))
        return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)


class DashboardSmokeTests(TestCase):
    def setUp(self):
        self.original = copy.deepcopy(dashboard._cache)
        self.clock = patch.object(dashboard, 'datetime', FixedClock)
        self.clock.start()
        self.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('煙霧測試禁止外部連線'))
        self.network.start()
        self.client = dashboard.app.test_client()

    def tearDown(self):
        dashboard._cache.clear()
        dashboard._cache.update(self.original)
        self.network.stop()
        self.clock.stop()

    def test_template_and_static_assets(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('最近目標變更', response.get_data(as_text=True))
        self.assertIn('最近模擬超標修剪', response.get_data(as_text=True))
        response = self.client.get('/static/script.js')
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_health_missing_data_is_unavailable(self):
        dashboard._cache.update(data=None, degraded=False, last_updated=None)
        response = self.client.get('/health')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()['status'], 'error')

    def test_seeded_api_and_health_use_both_markets(self):
        signals = [dict(ticker=ticker, date='2026-11-04', is_stale=False, data_status='ok',
                        target_today=target, target_yesterday=target,
                        last_signal_change=None, last_excess_trim=None)
                   for ticker, target in [('QQQ', 0.8), ('006208.TW', 2.8)]]
        dashboard._cache.update(data=signals, degraded=False, last_updated=FixedClock.now(),
                                last_success_at=FixedClock.now())
        response = self.client.get('/api/data')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), signals)
        response = self.client.get('/health')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['status'], 'ok')


if __name__ == '__main__':
    main()
