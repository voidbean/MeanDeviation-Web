"""Explicit one-shot/opt-in loop collector; never started by web or MCP startup."""
import argparse
import json
import time
from pathlib import Path

from core import config
from services.etf_kline import normalize
from services.etf_kline_feed import collect_etf, OfficialQuery


def main():
    parser = argparse.ArgumentParser(description="采集ETF日线/60分钟/15分钟，只写独立K线缓存，不开启策略监控")
    parser.add_argument("--code", required=True, help="ETF代码，如588170.SH")
    parser.add_argument("--db", default=config.DB_PATH, help="目标SQLite路径；默认与主服务同库，不修改旧行情/持仓表")
    parser.add_argument("--loop", action="store_true", help="显式开启轮询；默认只采集一次")
    parser.add_argument("--interval", type=int, default=90, help="每轮完成后等待秒数，至少90秒")
    args = parser.parse_args()
    try:
        code = normalize(args.code)
    except ValueError as exc:
        parser.error(str(exc))
    if args.interval < 90:
        parser.error("interval须至少90秒")
    if not config.TS_TOKEN:
        parser.error("根.env未配置TUSHARE_TOKEN；未调用网络或初始化缓存")
    query = OfficialQuery()  # permission denials remain blocked throughout this opt-in loop
    try:
        while True:
            result = collect_etf(code, str(Path(args.db).resolve()), query=query)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if not args.loop:
                return 0 if result["status"] == "collected" else 1
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
