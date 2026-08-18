import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from core.config import logger
from core.db import init_db
from services.indicators import _intraday_bg_loop
from services.calibration import calibration_bg_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    t = threading.Thread(target=_intraday_bg_loop, daemon=True, name="intraday-fetcher")
    t.start()
    calibration_thread = threading.Thread(target=calibration_bg_loop, daemon=True, name="watch-calibrator")
    calibration_thread.start()
    logger.info("lifespan: 后台分时快照线程已启动")
    logger.info("lifespan: 盘中校准线程已启动")
    yield


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")

import routes.main
import routes.sector
import routes.review

routes.main.register(app, templates)
routes.sector.register(app, templates)
routes.review.register(app, templates)
