"""把正式 cron 展開為真實時區時間，檢查收盤後窗口與 DST 邊界。"""
from datetime import date, datetime, time, timedelta
from pathlib import Path
import re
import unittest
from zoneinfo import ZoneInfo

WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/update_dashboard.yml'


def schedules():
    entries = []
    for line in WORKFLOW.read_text(encoding='utf-8').splitlines():
        cron = re.fullmatch(r"\s*- cron: ['\"]([^'\"]+)['\"]\s*", line)
        tz = re.fullmatch(r"\s*timezone: ['\"]([^'\"]+)['\"]\s*", line)
        if cron:
            entries.append({'cron': cron.group(1), 'timezone': 'UTC'})
        elif tz:
            entries[-1]['timezone'] = tz.group(1)
    return entries


def matches(value, field):
    for item in field.split(','):
        if item == '*':
            return True
        if '-' in item:
            first, last = map(int, item.split('-'))
            if first <= value <= last:
                return True
        elif int(item) == value:
            return True
    return False


def local_triggers(day, timezone):
    result = []
    for entry in schedules():
        if entry['timezone'] != timezone:
            continue
        minute, hour, dom, month, dow = entry['cron'].split()
        if not all([matches(day.day, dom), matches(day.month, month), matches(day.isoweekday() % 7, dow)]):
            continue
        for hh in range(24):
            for mm in range(60):
                if matches(hh, hour) and matches(mm, minute):
                    result.append(datetime.combine(day, time(hh, mm), ZoneInfo(timezone)))
    return sorted(set(result))


class CloseScheduleTests(unittest.TestCase):
    def assert_window(self, day, timezone, close, expected_offsets):
        closing = datetime.combine(day, close, ZoneInfo(timezone))
        offsets = [int((stamp - closing).total_seconds() / 60)
                   for stamp in local_triggers(day, timezone) if closing < stamp <= closing + timedelta(minutes=30)]
        self.assertEqual(offsets, expected_offsets)
        self.assertGreaterEqual(min(offsets), 5)  # 不早於 EOD 確認緩衝。
        self.assertLessEqual(max(offsets), 25)    # 預留至少5分鐘供建置與部署。

    def test_tw_three_attempts_before_fourteen(self):
        self.assert_window(date(2026, 11, 13), 'Asia/Taipei', time(13, 30), [7, 17, 25])

    def test_us_three_attempts_across_both_dst_transitions(self):
        fixtures = [(date(2026, 3, 6), 5), (date(2026, 3, 9), 4),
                    (date(2026, 10, 30), 4), (date(2026, 11, 2), 5),
                    (date(2026, 7, 8), 4), (date(2026, 11, 12), 5)]
        for day, taipei_hour in fixtures:
            with self.subTest(day=day):
                self.assert_window(day, 'America/New_York', time(16), [7, 17, 25])
                first = next(t for t in local_triggers(day, 'America/New_York') if t.hour == 16)
                taipei = first.astimezone(ZoneInfo('Asia/Taipei'))
                self.assertEqual((taipei.hour, taipei.minute), (taipei_hour, 7))
                self.assertEqual(taipei.date(), day + timedelta(days=1))

    def test_2026_early_closes_have_a_within_window_attempt(self):
        for day in [date(2026, 11, 27), date(2026, 12, 24)]:
            self.assert_window(day, 'America/New_York', time(13), [17])

    def test_friday_us_session_is_not_lost_on_taipei_saturday(self):
        regular = [t for t in local_triggers(date(2026, 11, 13), 'America/New_York') if t.hour == 16]
        self.assertEqual(len(regular), 3)
        self.assertTrue(all(t.astimezone(ZoneInfo('Asia/Taipei')).weekday() == 5 for t in regular))

    def test_weekends_do_not_trigger_daily_quote_jobs(self):
        for timezone in ['America/New_York', 'Asia/Taipei']:
            self.assertEqual(local_triggers(date(2026, 11, 14), timezone), [])
            self.assertEqual(local_triggers(date(2026, 11, 15), timezone), [])


if __name__ == '__main__':
    unittest.main()
