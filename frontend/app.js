const state = {
  token: localStorage.getItem("pivot_platform_token") || "",
  user: JSON.parse(localStorage.getItem("pivot_platform_user") || "null"),
  offers: [],
  orders: [],
  selectedOfferId: null,
};

function backendBase() {
  const raw = document.getElementById("backend_base").value.trim();
  return raw || "";
}

function apiUrl(path) {
  return `${backendBase()}${path}`;
}

function authHeaders(json = true) {
  const headers = {};
  if (json) headers["Content-Type"] = "application/json";
  if (state.token) headers["Authorization"] = `Bearer ${state.token}`;
  return headers;
}

function setDebug(title, payload) {
  document.getElementById("debug_output").textContent = `=== ${title} ===\n${JSON.stringify(payload, null, 2)}`;
}

function setAuthFeedback(message, level = "") {
  const node = document.getElementById("auth_feedback");
  node.textContent = message;
  node.className = level ? `feedback ${level}` : "feedback";
}

async function fetchJson(path, options = {}) {
  const response = await fetch(apiUrl(path), options);
  const data = await response.json();
  if (!response.ok) {
    throw new Error(JSON.stringify(data));
  }
  return data;
}

function renderAuth() {
  const node = document.getElementById("auth_status");
  if (!state.token || !state.user) {
    node.textContent = "未登录";
    return;
  }
  node.textContent = `已登录：${state.user.email}`;
}

function badgeClass(status) {
  return status === "active" || status === "issued" || status === "redeemed" ? "ok" : "warn";
}

function renderOffers() {
  const root = document.getElementById("offer_list");
  if (!state.offers.length) {
    root.className = "list empty";
    root.textContent = state.token ? "暂无可租用容器商品" : "登录后加载";
    return;
  }
  root.className = "list";
  root.innerHTML = state.offers.map((offer) => `
    <div class="item">
      <div class="item-row">
        <strong>${offer.repository}:${offer.tag}</strong>
        <span class="badge ${badgeClass(offer.offer_status)}">${offer.offer_status}</span>
      </div>
      <div>节点：${offer.seller_node_key}</div>
      <div>时价：${offer.current_billable_price_cny_per_hour?.toFixed?.(2) ?? offer.current_billable_price_cny_per_hour ?? "-"} CNY/h</div>
      <div class="button-row">
        <button class="secondary" data-offer-id="${offer.offer_id}">查看详情</button>
      </div>
    </div>
  `).join("");
  root.querySelectorAll("button[data-offer-id]").forEach((button) => {
    button.addEventListener("click", () => selectOffer(Number(button.dataset.offerId)));
  });
}

function renderOfferDetail(offer) {
  const root = document.getElementById("offer_detail");
  if (!offer) {
    root.className = "detail empty";
    root.textContent = "请选择一个容器商品";
    return;
  }
  root.className = "detail";
  root.innerHTML = `
    <div class="item">
      <div class="item-row">
        <strong>${offer.repository}:${offer.tag}</strong>
        <span class="badge ${badgeClass(offer.offer_status)}">${offer.offer_status}</span>
      </div>
      <div>Seller Node：${offer.seller_node_key}</div>
      <div>Runtime Ref：${offer.runtime_image_ref}</div>
      <div>时价：${offer.current_billable_price_cny_per_hour} CNY/h</div>
      <div>Probe：${offer.probe_status}</div>
      <pre>${JSON.stringify(offer.probe_measured_capabilities || {}, null, 2)}</pre>
    </div>
  `;
}

function renderOrders() {
  const root = document.getElementById("order_list");
  if (!state.orders.length) {
    root.className = "list empty";
    root.textContent = state.token ? "暂无订单" : "登录后加载";
    return;
  }
  root.className = "list";
  root.innerHTML = state.orders.map((order) => `
    <div class="item">
      <div class="item-row">
        <strong>订单 #${order.id}</strong>
        <span class="badge ${badgeClass(order.order_status)}">${order.order_status}</span>
      </div>
      <div>${order.repository}:${order.tag}</div>
      <div>${order.requested_duration_minutes} 分钟 · ${order.issued_hourly_price_cny} CNY/h</div>
      <div class="button-row">
        <button class="secondary" data-order-id="${order.id}">查看</button>
        <button class="secondary" data-license="${order.license_token}">填入许可证</button>
      </div>
    </div>
  `).join("");
  root.querySelectorAll("button[data-order-id]").forEach((button) => {
    button.addEventListener("click", async () => {
      const payload = await fetchJson(`/api/v1/buyer/orders/${button.dataset.orderId}`, {
        headers: authHeaders(false),
      });
      document.getElementById("order_result").className = "detail";
      document.getElementById("order_result").innerHTML = `<pre>${JSON.stringify(payload, null, 2)}</pre>`;
      setDebug("order detail", payload);
    });
  });
  root.querySelectorAll("button[data-license]").forEach((button) => {
    button.addEventListener("click", () => {
      document.getElementById("license_token").value = button.dataset.license;
    });
  });
}

function renderWallet(wallet) {
  document.getElementById("wallet_balance").textContent = wallet ? `${wallet.balance_cny_credits.toFixed(2)} CNY` : "-";
}

function renderLedger(entries) {
  const root = document.getElementById("ledger_list");
  if (!entries.length) {
    root.className = "list empty";
    root.textContent = "暂无流水";
    return;
  }
  root.className = "list";
  root.innerHTML = entries.map((entry) => `
    <div class="item">
      <div class="item-row">
        <strong>${entry.entry_type}</strong>
        <span>${entry.amount_delta_cny}</span>
      </div>
      <div>余额后：${entry.balance_after}</div>
      <div>${entry.created_at}</div>
    </div>
  `).join("");
}

async function register() {
  const email = document.getElementById("email").value.trim();
  const password = document.getElementById("password").value;
  const displayName = document.getElementById("display_name").value.trim() || null;

  if (!email) {
    setAuthFeedback("请输入邮箱。", "error");
    setDebug("register validation failed", { ok: false, error: "missing_email" });
    return;
  }
  if (password.length < 8) {
    setAuthFeedback("注册失败：密码至少 8 位。", "error");
    setDebug("register validation failed", { ok: false, error: "password_too_short", min_length: 8 });
    return;
  }

  const payload = await fetchJson("/api/v1/auth/register", {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify({
      email,
      password,
      display_name: displayName,
    }),
  });
  setAuthFeedback("注册成功，现在可以直接登录。", "info");
  setDebug("register", payload);
}

async function login() {
  const payload = await fetchJson("/api/v1/auth/login", {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify({
      email: document.getElementById("email").value.trim(),
      password: document.getElementById("password").value,
    }),
  });
  state.token = payload.access_token;
  state.user = payload.user;
  localStorage.setItem("pivot_platform_token", state.token);
  localStorage.setItem("pivot_platform_user", JSON.stringify(state.user));
  renderAuth();
  setAuthFeedback("登录成功。", "info");
  setDebug("login", payload);
  await refreshAll();
}

function logout() {
  state.token = "";
  state.user = null;
  state.offers = [];
  state.orders = [];
  state.selectedOfferId = null;
  localStorage.removeItem("pivot_platform_token");
  localStorage.removeItem("pivot_platform_user");
  renderAuth();
  renderOffers();
  renderOfferDetail(null);
  renderOrders();
  renderWallet(null);
  renderLedger([]);
  document.getElementById("order_result").className = "detail empty";
  document.getElementById("order_result").textContent = "订单和许可证结果会显示在这里";
  setAuthFeedback("注册时密码至少 8 位。");
}

async function refreshAll() {
  if (!state.token) {
    renderAuth();
    renderOffers();
    renderOfferDetail(null);
    renderOrders();
    renderWallet(null);
    renderLedger([]);
    return;
  }
  const [wallet, ledger, offers, orders] = await Promise.all([
    fetchJson("/api/v1/buyer/wallet", { headers: authHeaders(false) }),
    fetchJson("/api/v1/buyer/wallet/ledger", { headers: authHeaders(false) }),
    fetchJson("/api/v1/buyer/catalog/offers", { headers: authHeaders(false) }),
    fetchJson("/api/v1/buyer/orders", { headers: authHeaders(false) }),
  ]);
  state.offers = offers;
  state.orders = orders;
  renderAuth();
  renderWallet(wallet);
  renderLedger(ledger);
  renderOffers();
  renderOrders();
  const active = state.selectedOfferId ? state.offers.find((item) => item.offer_id === state.selectedOfferId) : state.offers[0];
  if (active) {
    await selectOffer(active.offer_id, false);
  } else {
    renderOfferDetail(null);
  }
}

async function selectOffer(offerId, updateDebug = true) {
  state.selectedOfferId = offerId;
  const payload = await fetchJson(`/api/v1/buyer/catalog/offers/${offerId}`, {
    headers: authHeaders(false),
  });
  renderOfferDetail(payload);
  if (updateDebug) setDebug("offer detail", payload);
}

async function createOrder() {
  if (!state.selectedOfferId) {
    setDebug("order", { ok: false, error: "no_offer_selected" });
    return;
  }
  const payload = await fetchJson("/api/v1/buyer/orders", {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify({
      offer_id: state.selectedOfferId,
      requested_duration_minutes: Number.parseInt(document.getElementById("order_minutes").value || "60", 10),
    }),
  });
  document.getElementById("order_result").className = "detail";
  document.getElementById("order_result").innerHTML = `<pre>${JSON.stringify(payload, null, 2)}</pre>`;
  document.getElementById("license_token").value = payload.license_token;
  setDebug("create order", payload);
  await refreshAll();
}

async function redeemLicense() {
  const token = document.getElementById("license_token").value.trim();
  if (!token) {
    setDebug("redeem license", { ok: false, error: "missing_license_token" });
    return;
  }
  const payload = await fetchJson("/api/v1/buyer/orders/redeem", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ license_token: token }),
  });
  document.getElementById("license_result").className = "detail";
  document.getElementById("license_result").innerHTML = `<pre>${JSON.stringify(payload, null, 2)}</pre>`;
  setDebug("redeem license", payload);
  if (state.token) await refreshAll();
}

document.getElementById("register_btn").addEventListener("click", () => register().catch((error) => setDebug("register failed", String(error))));
document.getElementById("login_btn").addEventListener("click", () => login().catch((error) => setDebug("login failed", String(error))));
document.getElementById("refresh_btn").addEventListener("click", () => refreshAll().catch((error) => setDebug("refresh failed", String(error))));
document.getElementById("order_btn").addEventListener("click", () => createOrder().catch((error) => setDebug("order failed", String(error))));
document.getElementById("redeem_btn").addEventListener("click", () => redeemLicense().catch((error) => setDebug("redeem failed", String(error))));
document.getElementById("logout_btn").addEventListener("click", logout);

renderAuth();
refreshAll().catch((error) => setDebug("init failed", String(error)));
