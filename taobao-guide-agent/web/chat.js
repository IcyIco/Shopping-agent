const refs = {
  conversation: document.querySelector("#conversation"),
  welcome: document.querySelector("#welcome"),
  messages: document.querySelector("#messages"),
  form: document.querySelector("#composer"),
  input: document.querySelector("#message"),
  mode: document.querySelector("#mode"),
  newChat: document.querySelector("#new-chat"),
  send: document.querySelector("#send"),
};

const state = { sessionId: null, pending: false };
const toolLabels = {
  search_products: "商品搜索",
  get_product_detail: "商品详情",
  list_coupons: "优惠查询",
  query_order: "订单查询",
};
const statuses = { ok: "成功", empty: "无结果", invalid: "参数待确认", error: "失败" };

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function action(label, prompt, className) {
  const button = element("button", className, label);
  button.type = "button";
  button.dataset.message = prompt;
  return button;
}

function productCard(card, includeAction) {
  const node = element("article", "result-card");
  node.append(
    element("p", "card-type", card.category + " · " + card.id),
    element("h3", "", card.name),
    element("p", "card-value", "¥" + card.price),
    element("p", "card-meta", card.stock ? "库存 " + card.stock + " 件" : "当前缺货"),
    element("p", "card-meta", "销量 " + card.sales + " · 好评率 " + card.rating + "%"),
    element("p", "card-meta", card.tags.join(" · ")),
  );
  if (includeAction) {
    node.append(action("看详情", "查看 " + card.id + " 的详情", "card-action"));
  }
  return node;
}

function couponCard(card) {
  const node = element("article", "result-card");
  node.append(
    element("p", "card-type", (card.category || "全品类") + " · " + card.id),
    element("h3", "", card.name),
    element("p", "card-value", "减 ¥" + card.discount),
    element("p", "card-meta", "满 ¥" + card.min_spend + " 可用"),
  );
  if (card.amount != null) {
    const status = card.eligible ? "已达门槛" : "还差 ¥" + card.shortfall;
    node.append(element("p", "card-meta", "按 ¥" + card.amount + " 计算，" + status));
  }
  return node;
}

function orderCard(card) {
  const node = element("article", "result-card order");
  node.append(
    element("p", "card-type", "模拟订单 · " + card.id),
    element("h3", "", card.product_name),
    element("p", "card-value", card.status),
  );
  if (card.tracking.length) {
    const timeline = element("ol", "tracking");
    for (const event of card.tracking) {
      timeline.append(element("li", "", event.time + " · " + event.detail));
    }
    node.append(timeline);
  }
  return node;
}

function resultCards(data) {
  const grid = element("div", "cards");
  const includeProductAction = data.tools.some(tool => tool.name === "search_products");
  for (const card of data.cards) {
    if (card.kind === "product") grid.append(productCard(card, includeProductAction));
    if (card.kind === "coupon") grid.append(couponCard(card));
    if (card.kind === "order") grid.append(orderCard(card));
  }
  return grid;
}

function trace(tools) {
  const box = element("details", "trace");
  const lines = element("ul", "");
  for (const call of tools) {
    const name = toolLabels[call.name] || call.name;
    const row = element("li", "");
    row.append(
      element("span", "", name + " · " + statuses[call.status] + " · " + call.ms + " ms "),
      element("code", "", call.name + "(" + JSON.stringify(call.arguments) + ")"),
    );
    lines.append(row);
  }
  box.append(element("summary", "", "查看工具调用"), lines);
  return box;
}

function appendMessage(role, message, extraClass = "") {
  const article = element("article", "message " + role + " " + extraClass);
  article.append(
    element("p", "message-role", role === "user" ? "你" : "寻物导购"),
    element("p", "message-text", message),
  );
  refs.messages.append(article);
  return article;
}

function setPending(value) {
  state.pending = value;
  refs.newChat.disabled = value;
  refs.send.disabled = value;
  refs.form.setAttribute("aria-busy", String(value));
  document.querySelectorAll("[data-message]").forEach(button => {
    button.disabled = value;
  });
}

function scrollToLatest() {
  refs.conversation.scrollTop = refs.conversation.scrollHeight;
}

async function sendMessage(message) {
  if (!message.trim() || state.pending) return;
  refs.welcome.hidden = true;
  refs.input.value = "";
  appendMessage("user", message);
  const pending = appendMessage("assistant", "正在处理你的查询…", "pending");
  setPending(true);
  scrollToLatest();

  try {
    const response = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, session_id: state.sessionId }),
      signal: AbortSignal.timeout(85000),
    });
    const data = await response.json();
    if (!response.ok) {
      const detail = typeof data.detail === "string" ? data.detail : "请求格式有误，请检查输入后重试。";
      pending.remove();
      appendMessage("assistant", detail, "error");
      return;
    }
    state.sessionId = data.session_id;
    pending.remove();
    const article = appendMessage("assistant", data.reply);
    if (data.cards.length) article.append(resultCards(data));
    if (data.tools.length) article.append(trace(data.tools));
  } catch (error) {
    pending.remove();
    const detail = error.name === "TimeoutError"
      ? "查询等待超时，请稍后重试。"
      : "暂时连不上本地服务。请确认启动窗口仍在运行，刷新后再试。";
    appendMessage("assistant", detail, "error");
  } finally {
    setPending(false);
    scrollToLatest();
  }
}

refs.form.addEventListener("submit", event => {
  event.preventDefault();
  sendMessage(refs.input.value);
});

refs.input.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    refs.form.requestSubmit();
  }
});

document.addEventListener("click", event => {
  const button = event.target.closest("button[data-message]");
  if (button) sendMessage(button.dataset.message);
});

refs.newChat.addEventListener("click", () => {
  state.sessionId = null;
  refs.messages.replaceChildren();
  refs.welcome.hidden = false;
  refs.input.value = "";
  refs.input.focus();
});

async function showModelStatus() {
  try {
    const response = await fetch("/health");
    if (!response.ok) throw new Error("health");
    const data = await response.json();
    refs.mode.textContent = data.configured ? "DeepSeek Flash" : "缺少 DeepSeek 密钥";
    refs.mode.title = data.detail || "DeepSeek Flash";
    if (!data.configured) refs.mode.dataset.state = "error";
  } catch {
    refs.mode.textContent = "本地服务连接失败";
    refs.mode.dataset.state = "error";
  }
}

showModelStatus();
