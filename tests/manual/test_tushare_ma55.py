"""Manual, read-only API probe; never run network calls on import.

Run from any directory with the project's Python environment. Credentials are
read only from the root .env and sent only to Tushare over HTTPS. Raw responses,
exceptions and request bodies are deliberately never printed.
"""
import datetime as dt
import json
import subprocess
from pathlib import Path

import requests
from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[2]
URL = "https://api.tushare.pro"


def probe(session, token, api, params, transport="requests"):
    result = {"api": api, "freq": params.get("freq", "daily")}
    try:
        body = {"api_name": api, "token": token, "params": params, "fields": ""}
        if transport == "curl":
            # Credentials travel through stdin, never command-line arguments.
            completed = subprocess.run(
                ["curl", "--silent", "--fail", "--connect-timeout", "10",
                 "--max-time", "25", "--header", "Content-Type: application/json",
                 "--data-binary", "@-", URL],
                input=json.dumps(body), text=True, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=30,
            )
            if completed.returncode:
                return {**result, "status": "transport_error", "exit_code": completed.returncode}
            payload = json.loads(completed.stdout)
        else:
            response = session.post(URL, json=body, timeout=(10, 25), allow_redirects=False)
            if response.status_code != 200:
                return {**result, "status": "http_error", "http_status": response.status_code}
            payload = response.json()
        if payload.get("code") != 0:
            # Classify privately; never echo provider messages or arbitrary codes.
            message = str(payload.get("msg", "")).lower()
            status = "api_error"
            if any(s in message for s in ("权限", "permission", "积分")):
                status = "permission_denied"
            elif any(s in message for s in ("频次", "每分钟", "每小时", "每天", "rate limit")):
                status = "rate_limited"
            elif "token" in message:
                status = "authentication_error"
            return {**result, "status": status}
        data = payload.get("data") or {}
        fields, items = data.get("fields") or [], data.get("items") or []
        result.update(status="ok" if items else "empty", rows=len(items))
        time_field = next((f for f in ("trade_time", "time", "trade_date") if f in fields), None)
        if time_field and items:
            index = fields.index(time_field)
            # Only validated date/time strings may reach the output.
            fmt = "%Y%m%d" if time_field == "trade_date" else "%Y-%m-%d %H:%M:%S"
            timestamps = [dt.datetime.strptime(str(row[index]), fmt) for row in items]
            result.update(first=min(timestamps).isoformat(), last=max(timestamps).isoformat())
            if "close" in fields:
                close_index = fields.index("close")
                import math
                values = [float(row[close_index]) for _, row in sorted(zip(timestamps, items), key=lambda p: p[0])]
                valid = len(set(timestamps)) == len(timestamps) and all(math.isfinite(v) and v > 0 for v in values)
                result.update(unique_timestamps=len(set(timestamps)), valid_closes=valid)
                for n in (55, 233):
                    # Feasibility only; not a verified adjusted or closed-bar signal.
                    result[f"ma{n}_computable"] = valid and len(values) >= n
        return result
    except requests.exceptions.Timeout:
        return {**result, "status": "timeout"}
    except requests.exceptions.SSLError:
        return {**result, "status": "tls_error"}
    except requests.exceptions.ConnectionError:
        return {**result, "status": "connection_error"}
    except Exception:
        return {**result, "status": "response_validation_error"}


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=("requests", "curl"), default="requests")
    parser.add_argument("--only", choices=("daily", "history15", "history60", "live15", "live60"))
    args = parser.parse_args()
    token = dotenv_values(ROOT / ".env").get("TUSHARE_TOKEN")
    if not token:
        print(json.dumps({"status": "missing_root_env_token"}))
        return 1
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
    end = today - dt.timedelta(days=1)
    jobs = [("daily", {"ts_code": "600000.SH",
                       "start_date": (today - dt.timedelta(days=500)).strftime("%Y%m%d"),
                       "end_date": end.strftime("%Y%m%d")})]
    for freq in ("15min", "60min"):
        jobs.append(("stk_mins", {"ts_code": "600000.SH", "freq": freq,
                                 "start_date": f"{today - dt.timedelta(days=150)} 09:00:00",
                                 "end_date": f"{end} 16:00:00"}))
    for freq in ("15MIN", "60MIN"):
        jobs.append(("rt_min", {"ts_code": "600000.SH", "freq": freq}))
    with requests.Session() as session:
        for name, (api, params) in zip(("daily", "history15", "history60", "live15", "live60"), jobs):
            if args.only and args.only != name:
                continue
            print(json.dumps(probe(session, token, api, params, args.transport), ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
