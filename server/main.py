import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from stats import collect

STATIC = Path(__file__).parent / "static"

SAMPLE_INTERVAL = 0.5
_latest: dict | None = None
_subscribers: set[asyncio.Queue] = set()


async def _sampler():
    global _latest
    while True:
        # collect() does sync sysfs reads + occasional subprocess calls; offload so
        # the event loop stays responsive when smartctl runs.
        _latest = await asyncio.to_thread(collect)
        for q in list(_subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(_latest)
        await asyncio.sleep(SAMPLE_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_sampler())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def root():
    return FileResponse(STATIC / "index.html")


@app.get("/api/stats")
async def stats_once():
    if _latest is not None:
        return _latest
    return await asyncio.to_thread(collect)


@app.websocket("/ws")
async def ws(ws: WebSocket):
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    _subscribers.add(q)
    try:
        if _latest is not None:
            await ws.send_json(_latest)
        while True:
            snap = await q.get()
            await ws.send_json(snap)
    except WebSocketDisconnect:
        return
    except Exception:
        return
    finally:
        _subscribers.discard(q)
