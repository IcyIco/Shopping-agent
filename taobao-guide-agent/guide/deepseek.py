from typing import Literal

import httpx
from pydantic import BaseModel, Field


MODEL = "deepseek-flash"
ENDPOINT = "https://api.deepseek.com/chat/completions"
MISSING_KEY = "尚未配置 DeepSeek API 密钥。请重新打开 start.cmd，在启动窗口输入密钥。"
SYSTEM_PROMPT = """你是寻物，一个中文购物助手。商城的商品、优惠和订单来自本地模拟数据。
用户询问具体商品时，先调用搜索工具，即使你觉得商城可能没有这种商品。按用户原话分别填写商品名或品牌、类目、卖点和价格条件。
用户更换商品时使用新条件。只调整预算或排序时，沿用上次搜索的商品。用户按第几款追问时，按上一份展示清单的实际顺序定位商品。
商品详情、优惠和订单物流使用相应工具。只依据工具返回的数据说明商城事实，区分商品未收录、价格不匹配和服务出错。没有提供的信息如实说明。
用自然中文回答。通用购物知识可以直接解答。确实缺少必要信息时，询问缺少的那一项。"""


class ModelError(Exception):
    pass


class Function(BaseModel):
    name: str = Field(min_length=1)
    arguments: str


class ToolCall(BaseModel):
    id: str = Field(min_length=1)
    type: Literal["function"]
    function: Function


class AssistantMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str | None = None
    tool_calls: list[ToolCall] | None = None


class DeepSeek:
    def __init__(self, client: httpx.AsyncClient, key: str, tools: list[dict]):
        self.client = client
        self.key = key
        self.tools = tools

    async def complete(self, messages: list[dict], *, allow_tools: bool) -> AssistantMessage:
        if not self.key:
            raise ModelError(MISSING_KEY)
        try:
            response = await self.client.post(
                ENDPOINT,
                headers={"Authorization": f"Bearer {self.key}"},
                json={
                    "model": MODEL,
                    "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages],
                    "tools": self.tools,
                    "tool_choice": "auto" if allow_tools else "none",
                    "thinking": {"type": "disabled"},
                },
            )
            response.raise_for_status()
        except httpx.TimeoutException as error:
            raise ModelError("连接 DeepSeek 超时，请稍后重试。") from error
        except httpx.HTTPStatusError as error:
            code = error.response.status_code
            reasons = {
                401: "API 密钥无效，请检查启动时填写的密钥。",
                402: "账户余额不足。",
                403: "账户或密钥没有访问权限。",
                404: "模型接口或模型名称不可用。",
                429: "请求过于频繁或已达到配额。",
            }
            reason = reasons.get(code, "模型服务暂时无法处理请求。")
            raise ModelError(f"DeepSeek 返回 HTTP {code}。{reason}") from error
        except httpx.RequestError as error:
            if "CERTIFICATE_VERIFY_FAILED" in str(error):
                raise ModelError("无法验证 DeepSeek 的 HTTPS 证书，请检查本机网络或代理的证书配置。") from error
            raise ModelError("连接 DeepSeek 失败，请检查本机网络或代理。") from error

        try:
            choice = response.json()["choices"][0]
            message = AssistantMessage.model_validate(choice["message"])
        except (KeyError, IndexError, ValueError, TypeError) as error:
            raise ModelError("DeepSeek 返回了无法解析的消息，请重试。") from error
        if choice.get("finish_reason") == "length":
            raise ModelError("DeepSeek 的回答达到长度上限，本次回答未完成。")
        if message.tool_calls and len({call.id for call in message.tool_calls}) != len(message.tool_calls):
            raise ModelError("DeepSeek 返回了重复的工具调用编号，请重试。")
        if not message.tool_calls and not (message.content or "").strip():
            raise ModelError("DeepSeek 返回了空消息，请重试。")
        return message
