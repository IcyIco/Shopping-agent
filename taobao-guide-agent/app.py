import os
import sys
from contextlib import asynccontextmanager
from getpass import getpass
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from guide.agent import SessionBusy, Sessions, ShoppingAgent
from guide.catalog import Catalog, ShoppingTools
from guide.deepseek import DeepSeek, MISSING_KEY, MODEL, ModelError

ROOT = Path(__file__).resolve().parent


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=800, pattern=r"\S")
    session_id: UUID | None = None


class ChatResponse(BaseModel):
    session_id: UUID
    reply: str
    cards: list[dict]
    tools: list[dict]


def create_app(transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        tools = ShoppingTools(Catalog.load(ROOT / "data"))
        async with httpx.AsyncClient(timeout=httpx.Timeout(18.0, connect=6.0), transport=transport) as http:
            application.state.configured = bool(key)
            application.state.sessions = Sessions()
            application.state.agent = ShoppingAgent(DeepSeek(http, key, tools.definitions()), tools)
            yield

    application = FastAPI(title="寻物导购", version="2.0.0", lifespan=lifespan)
    application.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")

    @application.get("/", include_in_schema=False)
    async def homepage():
        return FileResponse(ROOT / "web" / "index.html", media_type="text/html")

    @application.get("/health")
    async def health(request: Request):
        configured = request.app.state.configured
        return {"model": MODEL, "configured": configured, "detail": None if configured else MISSING_KEY}

    @application.post("/chat", response_model=ChatResponse)
    async def chat(data: ChatRequest, request: Request):
        state = request.app.state
        if not state.configured:
            raise HTTPException(status_code=503, detail=MISSING_KEY)
        try:
            session = state.sessions.get(data.session_id)
            answer = await state.agent.reply(session, data.message)
        except SessionBusy as error:
            raise HTTPException(status_code=429, detail="当前会话较忙，请稍后重试。") from error
        except ModelError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return ChatResponse(session_id=session.id, reply=answer.reply, cards=answer.cards, tools=answer.tools)

    return application


app = create_app()

if __name__ == "__main__":
    import uvicorn

    if not os.environ.get("DEEPSEEK_API_KEY", "").strip() and sys.stdin.isatty():
        os.environ["DEEPSEEK_API_KEY"] = getpass("请输入 DeepSeek API 密钥（输入隐藏，回车继续）：").strip()
    uvicorn.run(app, host="127.0.0.1", port=18766)
