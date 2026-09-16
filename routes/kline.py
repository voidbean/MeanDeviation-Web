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
