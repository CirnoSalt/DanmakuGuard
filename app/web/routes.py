"""REST + SSE 路由。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from app.core import Event, bus
from app.paths import resource_path


class TaskCreate(BaseModel):
    url: str
    loop_mode: bool = False


def create_router() -> APIRouter:
    r = APIRouter()

    @r.get("/api/tasks")
    async def list_tasks(request: Request):
        tm = request.app.state.task_manager
        return [t.to_dict() for t in tm.list_tasks()]

    @r.post("/api/tasks")
    async def create_task(req: TaskCreate, request: Request):
        tm = request.app.state.task_manager
        if not req.url or not req.url.strip():
            raise HTTPException(status_code=400, detail="url 不能为空")
        task = await tm.submit(req.url.strip(), loop_mode=req.loop_mode)
        return task.to_dict()

    @r.post("/api/tasks/{task_id}/stop")
    async def stop_task(task_id: str, request: Request):
        tm = request.app.state.task_manager
        ok = tm.request_stop(task_id)
        if not ok:
            raise HTTPException(status_code=404, detail="任务不存在")
        return {"ok": True}

    @r.get("/api/stats")
    async def stats(request: Request):
        tm = request.app.state.task_manager
        return tm.session_stats()

    @r.get("/api/status")
    async def status(request: Request):
        st = request.app.state
        return {
            "mode": getattr(st, "mode", "ai"),
            "ai_available": bool(getattr(st, "ai_available", True)),
            "dictionary_enabled": bool(getattr(st, "dictionary_enabled", False)),
            "dictionary_count": getattr(st, "dictionary_count", 0),
        }

    @r.get("/api/accounts")
    async def accounts(request: Request):
        am = getattr(request.app.state, "account_manager", None)
        if am is None:
            return {"enabled": False, "accounts": []}
        return {"enabled": True, "accounts": am.status()}

    @r.get("/api/tasks/{task_id}/stream")
    async def stream(task_id: str, request: Request):
        tm = request.app.state.task_manager
        if task_id not in tm.tasks:
            raise HTTPException(status_code=404, detail="任务不存在")
        q = await bus.subscribe()

        # 若任务已结束（done/done 事件已发布过），直接补发终态
        terminal = tm.tasks[task_id]

        async def gen():
            try:
                # 任务已处于终态：立即推送一次 stats + done，让前端能正常收尾
                if terminal.status.value in {"done", "stopped", "failed"}:
                    yield Event(
                        task_id=task_id, type="stats",
                        data=terminal.to_dict().get("stats", {}),
                    ).to_sse()
                    yield Event(
                        task_id=task_id, type="done", data=terminal.to_dict(),
                    ).to_sse()
                    return
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        # 缩短超时到 5s，更及时感知客户端断开
                        event = await asyncio.wait_for(q.get(), timeout=5.0)
                    except asyncio.TimeoutError:
                        # 心跳，保持连接
                        yield ": keepalive\n\n"
                        continue
                    if event.task_id != task_id:
                        continue
                    yield event.to_sse()
                    if event.type == "done":
                        break
            finally:
                await bus.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @r.get("/")
    async def index():
        return FileResponse(resource_path("app/web/static/index.html"))

    return r
