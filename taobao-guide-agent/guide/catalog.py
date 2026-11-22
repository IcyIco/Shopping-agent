import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

logger = logging.getLogger("uvicorn.error")
Category = Literal["耳机", "键盘", "鼠标", "背包", "水杯", "充电宝"]
SortOrder = Literal["price_asc", "price_desc", "sales_desc", "rating_desc"]
Card = dict[str, object]


class Product(BaseModel):
    id: str
    name: str
    category: Category
    price: int = Field(ge=0)
    sales: int = Field(ge=0)
    rating: int = Field(ge=0, le=100)
    stock: int = Field(ge=0)
    tags: list[str]
    colors: list[str]

    def card(self) -> Card:
        return {"kind": "product", **self.model_dump()}


class Coupon(BaseModel):
    id: str
    name: str
    category: Category | None
    min_spend: int = Field(gt=0)
    discount: int = Field(gt=0)


class TrackingEvent(BaseModel):
    time: str
    detail: str


class Order(BaseModel):
    id: str
    product_id: str
    status: str
    tracking: list[TrackingEvent]


@dataclass
class Catalog:
    products: dict[str, Product]
    coupons: list[Coupon]
    orders: dict[str, Order]

    @classmethod
    def load(cls, directory: Path) -> "Catalog":
        products = TypeAdapter(list[Product]).validate_json(
            (directory / "products.json").read_text(encoding="utf-8")
        )
        coupons = TypeAdapter(list[Coupon]).validate_json(
            (directory / "coupons.json").read_text(encoding="utf-8")
        )
        orders = TypeAdapter(list[Order]).validate_json(
            (directory / "orders.json").read_text(encoding="utf-8")
        )
        for records in (products, coupons, orders):
            if len({record.id for record in records}) != len(records):
                raise ValueError("数据文件里有重复编号，请检查 data 目录。")
        products_by_id = {product.id: product for product in products}
        if any(order.product_id not in products_by_id for order in orders):
            raise ValueError("订单引用了不存在的商品，请检查 data/orders.json。")
        return cls(products_by_id, coupons, {order.id: order for order in orders})


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Search(Arguments):
    category: str | None = Field(
        None, min_length=1, max_length=70,
        description="按类目名称精确筛选。用户明确指定的类目均可查询，包括目录可能尚未收录的类目。",
    )
    keyword: str | None = Field(
        None, min_length=1, max_length=70,
        description="商品名称、品牌、型号或商品编号。组合词中出现的已有类目和卖点也会分别匹配。",
    )
    min_price: float | None = Field(None, ge=0, le=1000000)
    max_price: float | None = Field(None, ge=0, le=1000000)
    tags: list[str] = Field(default_factory=list, max_length=6, description="用户要求的功能或卖点，所有条件都要满足。")
    sort: SortOrder = "price_asc"

    @model_validator(mode="after")
    def check_price_range(self) -> "Search":
        if self.min_price is not None and self.max_price is not None:
            if self.min_price > self.max_price:
                raise ValueError("最低价应小于或等于最高价。")
        return self


class ProductDetail(Arguments):
    product_id: str = Field(pattern=r"^P[0-9]{3,8}$")


class CouponLookup(Arguments):
    category: Category | None = None
    amount: float | None = Field(None, ge=0, le=1000000, multiple_of=0.01,
                                 description="要判断门槛的实际消费金额。泛问某类商品的优惠时可省略。")
    eligible_only: bool = Field(False, description="仅当用户只想看该金额已达门槛的券时设为 true，并填写 amount。")


class OrderLookup(Arguments):
    order_id: str = Field(pattern=r"^TB[0-9]{4,12}$")


@dataclass
class ToolResult:
    status: Literal["ok", "empty", "invalid", "error"]
    reply: str
    cards: list[Card] = field(default_factory=list)


@dataclass
class ToolExecution:
    name: str
    arguments: dict[str, object]
    took_ms: float
    result: ToolResult

    def model_content(self) -> str:
        return json.dumps({
            "status": self.result.status,
            "message": self.result.reply,
            "results": self.result.cards,
        }, ensure_ascii=False)


@dataclass(frozen=True)
class Tool:
    args: type[Arguments]
    description: str
    handler: Callable[[Arguments], ToolResult]


class ShoppingTools:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        products = catalog.products.values()
        self.categories = {product.category.casefold() for product in products}
        labels = self.categories | {tag.casefold() for product in products for tag in product.tags if tag}
        # Longest labels first so a shorter catalog term cannot split a more specific one.
        alternatives = "|".join(re.escape(label) for label in sorted(labels, key=lambda label: (-len(label), label)))
        self.keyword_splitter = re.compile(f"({alternatives})" if alternatives else r"(?!)")
        self.exact_names = {value.casefold() for product in products for value in (product.name, product.id)}
        self.registry = {
            "search_products": Tool(
                Search,
                "查询用户指定的商品。将类目放在 category，功能卖点放在 tags，具体名称、品牌和型号放在 keyword。"
                "价格区间包含端点，tags 需全部满足。sort 可按价格、销量、好评率排序。",
                self.search,
            ),
            "get_product_detail": Tool(ProductDetail, "用明确的模拟商品编号查询价格、销量、好评率、颜色、库存和卖点。商品指代不明时先询问。", self.detail),
            "list_coupons": Tool(
                CouponLookup,
                "查询类目券和全品类券。泛问优惠时说明有哪些券和各自门槛；若有明确金额，再说明该金额是否达标及差额。"
                "仅查询当前能用的券时使用 eligible_only。没有券叠加规则的数据。",
                self.coupons,
            ),
            "query_order": Tool(OrderLookup, "用用户提供的 TB 开头订单编号查模拟物流。缺少订单号时询问用户。", self.order),
        }

    def definitions(self) -> list[dict]:
        return [
            {"type": "function", "function": {
                "name": name,
                "description": tool.description,
                "parameters": tool.args.model_json_schema(),
            }}
            for name, tool in self.registry.items()
        ]

    def execute(self, name: str, arguments_json: str) -> ToolExecution:
        started = perf_counter()
        arguments = {}
        tool = self.registry.get(name)
        if tool is None:
            result = ToolResult("invalid", "这个查询暂时不支持。你可以问商品、优惠或模拟订单。")
        else:
            try:
                parsed = tool.args.model_validate_json(arguments_json)
            except ValidationError:
                prompts = {
                    "get_product_detail": "请告诉我商品编号，或说清楚你想看搜索结果里的第几款。",
                    "query_order": "请提供 TB 开头的模拟订单号，例如 TB1001。",
                    "search_products": "请确认商品名称和筛选条件。价格应为非负数，最低价应小于或等于最高价。",
                    "list_coupons": "请确认优惠券类目和消费金额。",
                }
                result = ToolResult("invalid", prompts.get(name, "请确认查询条件后再试。"))
            else:
                arguments = parsed.model_dump(exclude_none=True)
                try:
                    result = tool.handler(parsed)
                except Exception:
                    logger.exception("购物工具运行失败 name=%s", name)
                    result = ToolResult("error", "这次查询暂时失败，请稍后重试。")
        elapsed = round((perf_counter() - started) * 1000, 2)
        logger.info("工具=%s 参数=%s 结果=%s 耗时=%.2fms", name, arguments, result.status, elapsed)
        return ToolExecution(name, arguments, elapsed, result)

    def search(self, args: Search) -> ToolResult:
        keyword = (args.keyword or "").casefold()
        exact = keyword in self.exact_names
        terms = [] if exact else [
            word for part in self.keyword_splitter.split(keyword) for word in part.split()
        ]
        categories = self.categories.intersection(terms)
        if args.category:
            categories.add(args.category.casefold())
        terms = [term for term in terms if term not in self.categories]
        terms.extend(tag.strip().casefold() for tag in args.tags if tag.strip())

        # Match every requested term. Keep Latin model names and numbers on token boundaries.
        patterns = []
        for term in terms:
            left = r"(?<![a-z0-9])" if term[0].isascii() and term[0].isalnum() else ""
            right = r"(?![a-z0-9])" if term[-1].isascii() and term[-1].isalnum() else ""
            patterns.append(re.compile(left + re.escape(term) + right))

        candidates = []
        for product in self.catalog.products.values():
            if categories and categories != {product.category.casefold()}:
                continue
            if exact and keyword not in (product.name.casefold(), product.id.casefold()):
                continue
            searchable = " ".join([product.id, product.name, *product.tags]).casefold()
            if not all(pattern.search(searchable) for pattern in patterns):
                continue
            candidates.append(product)

        if not candidates:
            if args.keyword and not (args.category or args.tags):
                return ToolResult("empty", f"模拟商品库里没有找到「{args.keyword}」。你可以核对商品名，或换个关键词。")
            conditions = []
            if args.category:
                conditions.append(f"类目「{args.category}」")
            if args.keyword:
                conditions.append(f"关键词「{args.keyword}」")
            if args.tags:
                conditions.append(f"卖点「{'、'.join(args.tags)}」")
            if conditions:
                label = "、".join(conditions)
                return ToolResult("empty", f"模拟商品库里没有找到符合{label}的商品。")
            return ToolResult("empty", "模拟商品库目前没有商品记录。")

        matches = [
            product for product in candidates
            if (args.min_price is None or product.price >= args.min_price)
            and (args.max_price is None or product.price <= args.max_price)
        ]
        if not matches:
            return ToolResult("empty", "模拟商品库里有符合商品条件的记录。当前价格范围内没有找到匹配商品，可以调整预算。")

        sort_keys = {
            "price_asc": lambda p: (p.price, p.id),
            "price_desc": lambda p: (-p.price, p.id),
            "sales_desc": lambda p: (-p.sales, p.price, p.id),
            "rating_desc": lambda p: (-p.rating, p.price, p.id),
        }
        matches.sort(key=sort_keys[args.sort])

        shown = [product.card() for product in matches[:6]]
        context = f"找到 {len(matches)} 款符合条件的商品。"
        if len(matches) > len(shown):
            context += f"这里展示前 {len(shown)} 款。"
        return ToolResult("ok", context, shown)

    def detail(self, args: ProductDetail) -> ToolResult:
        product = self.catalog.products.get(args.product_id)
        if product is None:
            return ToolResult("empty", "商品库里没有这个编号。你可以先搜一次商品，再选一款查看。")
        if product.stock:
            inventory = f"库存 {product.stock} 件"
        else:
            inventory = "当前缺货"
        colors = "、".join(product.colors)
        summary = (
            f"{product.name}售价 ¥{product.price}，{inventory}。"
            f"销量 {product.sales} 件，好评率 {product.rating}%。"
            f"可选颜色有{colors}。"
        )
        return ToolResult("ok", summary, [product.card()])

    def coupons(self, args: CouponLookup) -> ToolResult:
        if args.eligible_only and args.amount is None:
            return ToolResult("invalid", "要判断哪些券已经达到门槛，请提供消费金额。")
        matches = [
            coupon for coupon in self.catalog.coupons
            if (args.category is None or coupon.category in (None, args.category))
        ]
        if not matches:
            return ToolResult("empty", "当前模拟券库没有该范围的优惠券。")
        cards = []
        for coupon in sorted(matches, key=lambda item: item.min_spend):
            card = {"kind": "coupon", **coupon.model_dump()}
            if args.amount is not None:
                eligible = args.amount >= coupon.min_spend
                if args.eligible_only and not eligible:
                    continue
                shortfall = round(max(0, coupon.min_spend - args.amount), 2)
                card.update(amount=args.amount, eligible=eligible, shortfall=shortfall)
            cards.append(card)
        if not cards:
            return ToolResult("empty", f"有相关优惠券，消费 {args.amount:g} 元尚未达到任何一张的门槛。")
        summary = f"查到 {len(cards)} 张相关优惠券。"
        if args.amount is not None:
            count = sum(card["eligible"] for card in cards)
            summary += f"按消费 {args.amount:g} 元计算，{count} 张达到金额门槛。"
        return ToolResult("ok", summary, cards)

    def order(self, args: OrderLookup) -> ToolResult:
        order = self.catalog.orders.get(args.order_id)
        if order is None:
            return ToolResult("empty", "模拟订单库里没有这个订单号。请核对后再试。")
        card = {"kind": "order", **order.model_dump(), "product_name": self.catalog.products[order.product_id].name}
        summary = f"订单 {order.id} 当前状态为「{order.status}」。"
        if not order.tracking:
            summary += "暂时没有物流节点。"
        return ToolResult("ok", summary, [card])
