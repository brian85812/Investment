"""只讀檢查富途行情權限；不建置、不通知、不輸出憑證。"""
from pathlib import Path
from datetime import date, timedelta
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from futu_source import FutuClient, SourceError


def main():
    parser = argparse.ArgumentParser(description='測試富途 QQQ 正常交易時段歷史日線權限。')
    parser.add_argument('--end', type=date.fromisoformat, required=True, help='已完成交易日，YYYY-MM-DD')
    args = parser.parse_args()
    try:
        client = FutuClient.from_environment()
        start = args.end - timedelta(days=500)
        raw = client.daily_bars(start, args.end, 0)
        adjusted = client.daily_bars(start, args.end, 1)
        if not raw.index.equals(adjusted.index):
            raise SourceError('adjustment_sessions_mismatch')
        print(json.dumps({'status': 'api_access_ok', 'symbol': 'US.QQQ', 'sessions': len(raw),
                          'first_session': raw.index[0].date().isoformat(),
                          'last_session': raw.index[-1].date().isoformat(),
                          'price_alignment': '尚須通過正式流程的Yahoo重疊還原價驗證'}, ensure_ascii=False))
        return 0
    except SourceError as error:
        print(json.dumps({'status': str(error), 'symbol': 'US.QQQ'}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
