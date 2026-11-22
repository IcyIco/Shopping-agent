import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from .catalog import ShoppingTools, ToolExecution
from .deepseek import DeepSeek, ModelError

MAX_TOOL_ROUNDS = 3


@dataclass
class Session:
    id: UUID = field(default_factory=uuid4)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turns: deque[list[dict]] = field(default_factory=lambda: deque(maxlen=6))


class SessionBusy(Exception):
    pass


class Sessions:
    def __init__(self, capacity: int = 128):
        self.capacity = capacity
        self.sessions: OrderedDict[UUID, Session] = OrderedDict()

    def get(self, session_id: UUID | None) -> Session:
        if session_id in self.sessions:
            self.sessions.move_to_end(session_id)
            return self.sessions[session_id]
        if len(self.sessions) >= self.capacity:
            oldest_idle = next((key for key, session in self.sessions.items() if not session.lock.locked()), None)
            if oldest_idle is None:
                raise SessionBusy
            del self.sessions[oldest_idle]
        session = Session()
        self.sessions[session.id] = session
        return session


@dataclass
class Answer:
    reply: str
    cards: list[dict]
    tools: list[dict]


class ShoppingAgent:
    def __init__(self, model: DeepSeek, tools: ShoppingTools):
        self.model = model
        self.tools = tools

    async def reply(self, session: Session, message: str) -> Answer:
        async with session.lock:
            history = [entry for turn in session.turns for entry in turn]
            turn = [{"role": "user", "content": message}]
            executions: list[ToolExecution] = []
            try:
                for round_index in range(MAX_TOOL_ROUNDS + 1):
                    allow_tools = round_index < MAX_TOOL_ROUNDS
                    decision = await self.model.complete(history + turn, allow_tools=allow_tools)
                    if not decision.tool_calls:
                        reply = decision.content.strip()
                        break
                    if not allow_tools:
                        raise ModelError("已达到本轮查询上限，DeepSeek 未返回最终回答。")

                    turn.append(decision.model_dump(exclude_none=True))
                    for call in decision.tool_calls:
                        execution = self.tools.execute(call.function.name, call.function.arguments)
                        executions.append(execution)
                        turn.append({
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": execution.model_content(),
                        })
            except ModelError as error:
                if not executions:
                    raise
                facts = "\n".join(item.result.reply for item in executions)
                reply = f"DeepSeek 未完成本轮回答。{error}\n已执行的查询结果如下。\n{facts}"

            turn.append({"role": "assistant", "content": reply})
            session.turns.append(turn)
            return Answer(
                reply=reply,
                cards=[card for item in executions for card in item.result.cards],
                tools=[{
                    "name": item.name,
                    "arguments": item.arguments,
                    "ms": item.took_ms,
                    "status": item.result.status,
                } for item in executions],
            )
