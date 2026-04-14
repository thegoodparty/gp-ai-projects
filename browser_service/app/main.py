import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from browser_service.app.browser_pool import BrowserPool
from browser_service.app.models import ErrorResponse, RenderRequest, RenderResponse

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = BrowserPool()
    await pool.start()
    app.state.browser_pool = pool
    yield
    await pool.stop()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    pool: BrowserPool = app.state.browser_pool
    return {
        "status": "ok",
        "browser_connected": pool.browser is not None and pool.browser.is_connected(),
        "active_contexts": pool.active_contexts,
    }


@app.post("/render", response_model=RenderResponse)
async def render(request: RenderRequest):
    pool: BrowserPool = app.state.browser_pool
    try:
        result = await pool.render(
            url=request.url,
            timeout_ms=request.timeout_ms,
            wait_until=request.wait_until,
            wait_after_load_ms=request.wait_after_load_ms,
        )
        return result
    except ValueError as e:
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                error="invalid_url", detail=str(e)
            ).model_dump(),
        )
    except TimeoutError:
        logger.error("Render timed out for url=%s timeout_ms=%d", request.url, request.timeout_ms)
        return JSONResponse(
            status_code=504,
            content=ErrorResponse(
                error="timeout", detail="Request timed out"
            ).model_dump(),
        )
    except ConnectionError as e:
        logger.error("Browser connection failed for url=%s: %s", request.url, e)
        return JSONResponse(
            status_code=502,
            content=ErrorResponse(
                error="connection_error", detail="Failed to connect"
            ).model_dump(),
        )
    except Exception:
        logger.exception("Unexpected error rendering url=%s", request.url)
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error="internal_error", detail="An internal error occurred"
            ).model_dump(),
        )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
