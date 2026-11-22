import json
import os
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

import httpx

from app import ROOT, create_app
from guide.catalog import Catalog, ShoppingTools


@asynccontextmanager
async def asgi_client(application):
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            yield client


class CatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = ShoppingTools(Catalog.load(ROOT / "data"))

    def lookup(self, tool, **arguments):
        return self.tools.execute(tool, json.dumps(arguments)).result

    def test_combined_filters_and_sort(self):
        result = self.lookup("search_products", category="耳机", keyword="蓝牙", max_price=200)
        self.assertEqual([card["price"] for card in result.cards], [129, 199, 200])
        combined = self.lookup("search_products", keyword="蓝牙耳机", max_price=200)
        self.assertEqual(combined.cards, result.cards)
        by_brand = self.lookup("search_products", keyword="北栖蓝牙耳机")
        self.assertEqual([card["id"] for card in by_brand.cards], ["P102"])
        result = self.lookup("search_products", category="键盘", tags=["机械"], min_price=150, max_price=250)
        self.assertEqual([card["id"] for card in result.cards], ["P201", "P202"])
        result = self.lookup("search_products", category="键盘", sort="sales_desc")
        sales = [card["sales"] for card in result.cards]
        self.assertEqual(sales, sorted(sales, reverse=True))

    def test_empty_price_miss_and_invalid_range(self):
        missing = self.lookup("search_products", keyword="牙膏", max_price=20)
        self.assertEqual(missing.status, "empty")
        self.assertIn("牙膏", missing.reply)
        budget = self.lookup("search_products", keyword="微岚", max_price=100)
        self.assertIn("当前价格范围", budget.reply)
        self.assertEqual(self.lookup("search_products", min_price=300, max_price=100).status, "invalid")

    def test_product_coupons_and_orders(self):
        detail = self.lookup("get_product_detail", product_id="P102")
        self.assertEqual(detail.cards[0]["stock"], 0)
        self.assertEqual(self.lookup("get_product_detail", product_id="P999").status, "empty")
        coupons = self.lookup("list_coupons", category="耳机", amount=129)
        self.assertEqual([(card["id"], card["eligible"], card["shortfall"]) for card in coupons.cards],
                         [("C002", False, 21), ("C001", False, 71)])
        eligible = self.lookup("list_coupons", category="耳机", amount=150, eligible_only=True)
        self.assertEqual([card["id"] for card in eligible.cards], ["C002"])
        self.assertEqual(eligible.cards[0]["shortfall"], 0)
        order = self.lookup("query_order", order_id="TB1001").cards[0]
        self.assertEqual(order["status"], "运输中")
        self.assertEqual(order["tracking"][-1]["detail"], "到达华东分拨中心")
        self.assertEqual(self.lookup("query_order", order_id="TB9999").status, "empty")


class LiveProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_entrypoints_without_a_key(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
            async with asgi_client(create_app()) as client:
                self.assertIn("寻物导购", (await client.get("/")).text)
                self.assertEqual((await client.get("/static/chat.js")).status_code, 200)
                health = (await client.get("/health")).json()
                self.assertFalse(health["configured"])
                response = await client.post("/chat", json={"message": "看看耳机"})
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["detail"], health["detail"])
                self.assertEqual((await client.post("/chat", json={"message": "  "})).status_code, 422)

    @asynccontextmanager
    async def live_client(self, handler):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only"}):
            async with asgi_client(create_app(httpx.MockTransport(handler))) as client:
                yield client

    async def call(self, handler, message):
        async with self.live_client(handler) as client:
            response = await client.post("/chat", json={"message": message})
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()

    async def test_tool_round_trip_with_grounded_cards(self):
        events = []

        def handler(request):
            self.assertEqual(str(request.url), "https://api.deepseek.com/chat/completions")
            self.assertEqual(request.headers["Authorization"], "Bearer test-only")
            payload = json.loads(request.content)
            events.append(payload)
            if len(events) == 1:
                names = [entry["function"]["name"] for entry in payload["tools"]]
                self.assertIn("search_products", names)
                message = {"role": "assistant", "content": "查询前的估计是 ¥998。", "tool_calls": [{
                    "id": "call_123",
                    "type": "function",
                    "function": {"name": "search_products", "arguments": '{"category":"耳机","max_price":200,"keyword":"蓝牙"}'},
                }]}
            else:
                last = payload["messages"][-1]
                self.assertEqual(last["role"], "tool")
                self.assertEqual(last["tool_call_id"], "call_123")
                self.assertEqual(json.loads(last["content"])["results"][-1]["price"], 200)
                prices = [card["price"] for card in json.loads(last["content"])["results"]]
                message = {"role": "assistant", "content": f"符合条件的耳机从 {min(prices)} 元到 {max(prices)} 元。"}
            return httpx.Response(200, json={"choices": [{"message": message}]})

        result = await self.call(handler, "有没有 200 元以内的蓝牙耳机？")
        self.assertEqual(len(events), 2)
        self.assertEqual(result["tools"][0]["name"], "search_products")
        self.assertEqual([card["price"] for card in result["cards"]], [129, 199, 200])
        self.assertNotIn("998", result["reply"])
        self.assertEqual(result["reply"], "符合条件的耳机从 129 元到 200 元。")

    async def test_deepseek_flash_tool_round_trip_and_visible_model_reply(self):
        events = []

        def handler(request):
            self.assertEqual(str(request.url), "https://api.deepseek.com/chat/completions")
            self.assertEqual(request.headers["Authorization"], "Bearer test-only")
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], "deepseek-flash")
            self.assertEqual(payload["thinking"], {"type": "disabled"})
            events.append(payload)
            if len(events) == 1:
                self.assertEqual(payload["messages"][-1]["content"], "有没有素声的耳机")
                message = {"content": None, "tool_calls": [{
                    "id": "call_deepseek",
                    "type": "function",
                    "function": {"name": "search_products", "arguments": '{"category":"耳机","keyword":"素声"}'},
                }]}
            else:
                result = payload["messages"][-1]
                self.assertEqual(set(result), {"role", "tool_call_id", "content"})
                self.assertEqual(result["tool_call_id"], "call_deepseek")
                self.assertEqual([card["id"] for card in json.loads(result["content"])["results"]], ["P105"])
                card = json.loads(result["content"])["results"][0]
                message = {"content": f'{card["name"]}售价 {card["price"]} 元，目前有货，库存 {card["stock"]} 件。'}
            return httpx.Response(200, json={"choices": [{"message": message}]})

        result = await self.call(
            handler, "有没有素声的耳机",
        )
        self.assertEqual(len(events), 2)
        self.assertEqual([card["id"] for card in result["cards"]], ["P105"])
        self.assertEqual(result["reply"], "素声 Line 有线耳机售价 69 元，目前有货，库存 37 件。")

    async def test_deepseek_can_clarify_without_searching_rejected_category(self):
        def handler(request):
            return httpx.Response(200, json={"choices": [{"message": {"content": "那你想看哪类商品？"}}]})

        result = await self.call(
            handler, "我不想要耳机",
        )
        self.assertEqual(result["tools"], [])
        self.assertIn("想看哪类商品", result["reply"])

    async def test_model_timeout(self):
        def handler(request):
            raise httpx.ReadTimeout("simulated", request=request)

        async with self.live_client(handler) as client:
            response = await client.post("/chat", json={"message": "查订单 TB1001"})
        self.assertEqual(response.status_code, 502)
        self.assertIn("超时", response.json()["detail"])

    async def test_shopping_knowledge_answer_survives_without_catalog_search(self):
        explanation = "主动降噪用麦克风收集周围噪声，再产生相位相反的声波。"

        def handler(request):
            return httpx.Response(200, json={"choices": [{"message": {"content": explanation}}]})

        result = await self.call(handler, "降噪耳机的原理是什么？")
        self.assertEqual(result["reply"], explanation)
        self.assertEqual(result["tools"], [])

    async def test_local_result_survives_failed_model_followup(self):
        def handler(request):
            last = json.loads(request.content)["messages"][-1]
            if last["role"] == "tool":
                raise httpx.ReadTimeout("test timeout after tool", request=request)
            message = {"tool_calls": [{"type": "function", "id": "detail",
                       "function": {"name": "get_product_detail", "arguments": '{"product_id":"P105"}'}}]}
            return httpx.Response(200, json={"choices": [{"message": message}]})

        result = await self.call(handler, "P105 的库存呢？")
        self.assertIn("超时", result["reply"])
        self.assertEqual(result["cards"][0]["stock"], 37)
        self.assertEqual(result["tools"][0]["status"], "ok")

    async def test_two_searches_keep_the_displayed_order_for_a_followup(self):
        def handler(request):
            messages = json.loads(request.content)["messages"]
            last = messages[-1]
            if last["role"] == "tool":
                message = {"content": "查到了，见商品卡片。"}
            elif last["content"] == "第一款的详情":
                visible = [card for entry in messages if entry["role"] == "tool"
                           for card in json.loads(entry["content"])["results"]]
                self.assertEqual([card["id"] for card in visible], ["P105", "P302"])
                message = {"tool_calls": [{"type": "function", "id": "detail",
                           "function": {"name": "get_product_detail",
                                        "arguments": json.dumps({"product_id": visible[0]["id"]})}}]}
            else:
                message = {"tool_calls": [
                    {"type": "function", "id": call_id, "function": {
                        "name": "search_products", "arguments": json.dumps({"keyword": name})}}
                    for call_id, name in (("earphones", "素声"), ("mouse", "松果"))
                ]}
            return httpx.Response(200, json={"choices": [{"message": message}]})

        async with self.live_client(handler) as client:
            first = (await client.post("/chat", json={"message": "看看素声耳机和松果鼠标"})).json()
            self.assertEqual([row["id"] for row in first["cards"]], ["P105", "P302"])
            second = (await client.post("/chat", json={
                "message": "第一款的详情", "session_id": first["session_id"],
            })).json()
            self.assertEqual(second["cards"][0]["id"], "P105")

    async def test_new_product_and_budget_followup_share_a_session(self):
        requests = [
            ("200元以内的蓝牙耳机", {"category": "耳机", "keyword": "蓝牙", "max_price": 200}),
            ("有没有200元的西红柿", {"keyword": "西红柿", "min_price": 200, "max_price": 200}),
            ("那就不限制价格了呢？", {"keyword": "西红柿"}),
        ]

        def handler(request):
            messages = json.loads(request.content)["messages"]
            user_turns = [m["content"] for m in messages if m["role"] == "user"]
            index = len(user_turns) - 1
            self.assertEqual(user_turns, [text for text, _ in requests[:index + 1]])
            last = messages[-1]
            if last["role"] == "tool":
                self.assertEqual(last["tool_call_id"], f"search_{index}")
                result = json.loads(last["content"])
                message = {"content": result["message"]}
            else:
                if index == 2:
                    prior_result = [m for m in messages if m["role"] == "tool"][-1]
                    self.assertEqual(json.loads(prior_result["content"])["status"], "empty")
                message = {"tool_calls": [{"type": "function", "id": f"search_{index}",
                           "function": {"name": "search_products", "arguments": json.dumps(requests[index][1])}}]}
            return httpx.Response(200, json={"choices": [{"message": message}]})

        async with self.live_client(handler) as client:
            session = None
            for index, (query, filters) in enumerate(requests):
                response = await client.post("/chat", json={"session_id": session, "message": query})
                result = response.json()
                if session:
                    self.assertEqual(result["session_id"], session)
                session = result["session_id"]
                search = result["tools"][0]
                self.assertEqual(search["arguments"], {**filters, "sort": "price_asc", "tags": []})
                if index == 0:
                    self.assertEqual([card["price"] for card in result["cards"]], [129, 199, 200])
                else:
                    self.assertEqual(search["status"], "empty")
                    self.assertIn("西红柿", result["reply"])
                    self.assertEqual(result["cards"], [])

    async def test_invalid_arguments_reach_guard(self):
        events = []

        def handler(request):
            events.append(json.loads(request.content))
            message = {"content": "请把模拟订单号发给我。"}
            if len(events) == 1:
                message = {"content": None, "tool_calls": [{
                    "id": "call_456",
                    "type": "function",
                    "function": {"name": "query_order", "arguments": "{}"},
                }]}
            else:
                self.assertEqual(json.loads(events[-1]["messages"][-1]["content"])["status"], "invalid")
            return httpx.Response(200, json={"choices": [{"message": message}]})

        result = await self.call(handler, "我的订单呢")
        self.assertIn("订单号", result["reply"])
        self.assertEqual(result["tools"][0]["status"], "invalid")
        self.assertEqual(result["cards"], [])
