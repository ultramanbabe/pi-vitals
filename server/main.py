import asyncio
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from stats import collect

STATIC = Path(__file__).parent / "static"
app = FastAPI()
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def root():
    return FileResponse(STATIC / "index.html")


@app.get("/api/stats")
async def stats_once():
    return collect()


@app.websocket("/ws")
async def ws(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            await ws.send_json(collect())
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        return
    except Exception:
        return
