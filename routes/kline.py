"""On-demand research K-lines; GET never consumes upstream API quota."""
import asyncio
from fastapi.responses import JSONResponse
from services.kline55 import get_snapshot, refresh_snapshot


def register(app, templates):
    async def respond(fn, code, period):
        try:
            return await asyncio.to_thread(fn, code, period)
        except ValueError as exc:
            return JSONResponse({"status": "invalid", "message": str(exc)}, status_code=400)
        except Exception:
            return JSONResponse({"status": "error", "message": "K线缓存暂不可用，请稍后重试。"}, status_code=503)

    @app.get("/api/kline55")
    async def read_kline55(code: str, period: str = "D"):
        return await respond(get_snapshot, code, period)

    @app.post("/api/kline55/refresh")
    async def refresh_kline55(code: str, period: str = "D"):
        return await respond(refresh_snapshot, code, period)

    @app.get("/api/kline55/monitor")
    async def read_monitor55(code: str):
        from core import db
        from services.kline55 import normalize
        try:
            code, _ = normalize(code, '60min')
        except ValueError as exc:
            return JSONResponse({"message": str(exc)}, status_code=400)
        rows = await asyncio.to_thread(db.live55_subscription, code)
        row = rows[0] if rows else dict(code=code, enabled=0, status='未开启')
        return {k: row.get(k) for k in ('code', 'enabled', 'status', 'checked_at', 'watermark')}

    @app.post("/api/kline55/monitor")
    async def set_monitor55(code: str, enabled: bool):
        from core import db
        from services.kline55 import normalize
        try:
            code, _ = normalize(code, '60min')
        except ValueError as exc:
            return JSONResponse({"message": str(exc)}, status_code=400)
        await asyncio.to_thread(db.live55_subscription, code, enabled)
        return await read_monitor55(code)
