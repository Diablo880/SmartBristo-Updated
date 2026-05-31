const state = {
  token: localStorage.getItem("smartbistroToken") || "",
  role: localStorage.getItem("smartbistroRole") || "",
  tableId: Number(new URLSearchParams(location.search).get("table") || 1),
  menu: [],
  cart: [],
  adminMenu: []
};

const money = value => `$${Number(value).toFixed(2)}`;
const authHeaders = () => state.token ? { Authorization: `Bearer ${state.token}` } : {};

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...authHeaders(), ...(options.headers || {}) }
  });
  const type = response.headers.get("Content-Type") || "";
  const payload = type.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) throw new Error(payload.error || response.statusText);
  return payload;
}

function setLoginState() {
  document.querySelector("#loginState").textContent = state.token
    ? `Signed in as ${state.role}`
    : "Guest mode";
}

document.querySelector("#loginForm").addEventListener("submit", async event => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  try {
    const result = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ email: form.get("email"), password: form.get("password") })
    });
    state.token = result.token;
    state.role = result.user.role;
    localStorage.setItem("smartbistroToken", state.token);
    localStorage.setItem("smartbistroRole", state.role);
    setLoginState();
    await refreshCurrentView();
  } catch (error) {
    alert(error.message);
  }
});

document.querySelectorAll(".tabs button").forEach(button => {
  button.addEventListener("click", async () => {
    activateView(button.dataset.view);
    await refreshCurrentView();
  });
});

document.querySelectorAll("[data-jump]").forEach(button => {
  button.addEventListener("click", async () => {
    activateView(button.dataset.jump);
    await refreshCurrentView();
  });
});

function activateView(view) {
  document.querySelectorAll(".tabs button").forEach(item => item.classList.toggle("active", item.dataset.view === view));
  document.querySelectorAll(".view").forEach(item => item.classList.toggle("active", item.id === view));
}

async function refreshCurrentView() {
  const view = document.querySelector(".view.active").id;
  if (view === "home") return;
  if (view === "guest") return loadMenu();
  if (view === "floor") return loadTables();
  if (view === "kds") return loadKds();
  if (view === "orders") return loadOrders();
  if (view === "menuAdmin") return loadMenuAdmin();
  if (view === "inventory") return loadInventory();
  if (view === "analytics") return loadAnalytics();
}

async function loadMenu() {
  const tableSelect = document.querySelector("#tableSelect");
  if (!tableSelect.children.length) {
    for (let i = 1; i <= 12; i += 1) {
      const option = document.createElement("option");
      option.value = i;
      option.textContent = `Table ${i}`;
      tableSelect.append(option);
    }
    tableSelect.value = state.tableId;
    tableSelect.addEventListener("change", () => {
      state.tableId = Number(tableSelect.value);
      loadMenu();
    });
  }
  const data = await api(`/api/menu/${state.tableId}`);
  state.menu = data.items;
  document.querySelector("#menuTitle").textContent = `${data.table.label} digital menu`;
  document.querySelector("#menuGrid").innerHTML = data.items.map(item => `
    <article class="menu-card">
      <img class="food-photo" src="${item.image_url}" alt="${item.name}">
      <div>
        <h3>${item.name}</h3>
        <p>${item.description}</p>
      </div>
      <div class="badges">
        ${item.allergens.map(a => `<span class="badge alert">${a}</span>`).join("")}
        ${item.dietary.map(d => `<span class="badge">${d}</span>`).join("")}
      </div>
      <div class="menu-card-footer">
        <span class="price">${money(item.price)}</span>
        <button data-add="${item.id}">Add</button>
      </div>
    </article>
  `).join("");
  document.querySelectorAll("[data-add]").forEach(button => {
    button.addEventListener("click", () => addToCart(Number(button.dataset.add)));
  });
  renderCart();
  await refreshLoyaltyBalance(false);
}

function addToCart(id) {
  const existing = state.cart.find(item => item.menu_item_id === id);
  if (existing) existing.qty += 1;
  else state.cart.push({ menu_item_id: id, qty: 1 });
  renderCart();
}

function renderCart() {
  const holder = document.querySelector("#cartItems");
  if (!state.cart.length) {
    holder.className = "cart-empty";
    holder.textContent = "No items yet.";
    document.querySelector("#cartTotal").textContent = "$0.00";
    return;
  }
  holder.className = "";
  holder.innerHTML = state.cart.map(line => {
    const item = state.menu.find(menuItem => menuItem.id === line.menu_item_id);
    return `<div class="cart-line"><span>${line.qty} x ${item.name}</span><button data-remove="${item.id}">Remove</button></div>`;
  }).join("");
  document.querySelectorAll("[data-remove]").forEach(button => {
    button.addEventListener("click", () => {
      state.cart = state.cart.filter(item => item.menu_item_id !== Number(button.dataset.remove));
      renderCart();
    });
  });
  const total = state.cart.reduce((sum, line) => {
    const item = state.menu.find(menuItem => menuItem.id === line.menu_item_id);
    return sum + item.price * line.qty;
  }, 0);
  document.querySelector("#cartTotal").textContent = money(total);
}

document.querySelector("#submitOrder").addEventListener("click", async () => {
  if (!state.cart.length) return;
  try {
    const order = await api("/api/orders", {
      method: "POST",
      body: JSON.stringify({
        table_id: state.tableId,
        items: state.cart,
        customer: {
          name: document.querySelector("#customerName").value,
          contact: document.querySelector("#customerContact").value
        },
        redeem_points: Number(document.querySelector("#redeemPoints").value || 0),
        payment_method: "table-card"
      })
    });
    state.cart = [];
    renderCart();
    if (order.loyalty) {
      document.querySelector("#loyaltyBalance").textContent = `${order.loyalty.loyalty_points} pts`;
    }
    const earnedText = order.loyalty ? ` Loyalty balance is now ${order.loyalty.loyalty_points} pts.` : "";
    document.querySelector("#orderResult").textContent = `Order #${order.id} paid and sent to KDS. Total ${money(order.total)}.${earnedText}`;
    renderReceipt(order);
  } catch (error) {
    document.querySelector("#orderResult").textContent = error.message;
  }
});

function renderReceipt(order) {
  const receipt = document.querySelector("#receiptPanel");
  receipt.classList.remove("hidden");
  receipt.innerHTML = `
    <h3>Receipt #${order.id}</h3>
    <p class="muted">Table ${order.table_id} - ${order.payment_status}</p>
    ${order.items.map(item => `<div class="cart-line"><span>${item.qty} x ${item.name}</span><strong>${money(item.price * item.qty)}</strong></div>`).join("")}
    <div class="cart-line"><span>Subtotal</span><strong>${money(order.subtotal)}</strong></div>
    <div class="cart-line"><span>Loyalty discount</span><strong>${money(order.discount)}</strong></div>
    <div class="total-row"><span>Total paid</span><strong>${money(order.total)}</strong></div>
    <p class="muted">Receipt target: ${order.receipt_contact || "not provided"}</p>
    ${order.loyalty ? `<p class="badge">Loyalty balance ${order.loyalty.loyalty_points} pts</p>` : ""}
  `;
}

async function refreshLoyaltyBalance(showErrors = true) {
  const contact = document.querySelector("#customerContact").value.trim();
  if (!contact) {
    document.querySelector("#loyaltyBalance").textContent = "0 pts";
    return;
  }
  try {
    const customer = await api(`/api/customers/loyalty?contact=${encodeURIComponent(contact)}`);
    document.querySelector("#loyaltyBalance").textContent = `${customer.loyalty_points} pts`;
    document.querySelector("#redeemPoints").max = customer.loyalty_points;
  } catch (error) {
    if (showErrors) alert(error.message);
  }
}

document.querySelector("#checkLoyalty").addEventListener("click", () => refreshLoyaltyBalance(true));
document.querySelector("#customerContact").addEventListener("change", () => refreshLoyaltyBalance(false));

async function loadTables() {
  const data = await api("/api/tables");
  document.querySelector("#floorGrid").innerHTML = data.tables.map(table => `
    <article class="table-card">
      <strong>${table.label}</strong>
      <p>${table.seats} seats</p>
      <p class="status ${table.status}">${table.status}</p>
      <select data-table="${table.id}">
        ${["available", "occupied", "dirty", "reserved"].map(s => `<option ${s === table.status ? "selected" : ""}>${s}</option>`).join("")}
      </select>
      <p><a href="/api/tables/${table.id}/qr" target="_blank">Open table QR</a></p>
    </article>
  `).join("");
  document.querySelectorAll("[data-table]").forEach(select => {
    select.addEventListener("change", async () => {
      await api(`/api/tables/${select.dataset.table}`, {
        method: "PATCH",
        body: JSON.stringify({ status: select.value })
      });
      loadTables();
    });
  });
}

async function loadKds() {
  try {
    const data = await api("/api/kds/orders");
    document.querySelector("#kdsGrid").innerHTML = data.orders.length ? data.orders.map(order => `
      <article class="kds-card">
        <h3>Order #${order.id} - Table ${order.table_id}</h3>
        <p class="muted">${order.status} - target ${order.max_prep_minutes} min</p>
        <ul class="kds-items">
          ${order.items.map(item => `<li>${item.qty} x ${item.name} ${item.allergens.length ? `<span class="badge alert">${item.allergens.join(", ")}</span>` : ""}</li>`).join("")}
        </ul>
        <div class="kds-actions">
          ${["received", "prepping", "ready", "served"].map(status => `<button data-order="${order.id}" data-status="${status}">${status}</button>`).join("")}
        </div>
      </article>
    `).join("") : `<p class="muted">No active kitchen orders.</p>`;
    document.querySelectorAll("[data-order]").forEach(button => {
      button.addEventListener("click", async () => {
        await api(`/api/orders/${button.dataset.order}/status`, {
          method: "PATCH",
          body: JSON.stringify({ status: button.dataset.status })
        });
        loadKds();
      });
    });
  } catch (error) {
    document.querySelector("#kdsGrid").innerHTML = `<p class="muted">${error.message}. Sign in as kitchen, staff, or manager.</p>`;
  }
}

async function loadInventory() {
  try {
    const data = await api("/api/inventory");
    document.querySelector("#inventoryList").innerHTML = data.ingredients.map(item => {
      const pct = Math.min(100, Math.round((item.stock / Math.max(item.opening_stock, item.par)) * 100));
      return `<article class="inventory-row ${item.low ? "low" : ""}">
        <div>
          <h3>${item.name}</h3>
          <p class="muted">${item.stock}${item.unit} on hand - par ${item.par}${item.unit}</p>
          <div class="meter"><span style="width:${pct}%"></span></div>
        </div>
        <input data-stock="${item.id}" type="number" value="${item.stock}">
        <input data-par="${item.id}" type="number" value="${item.par}">
      </article>`;
    }).join("");
    document.querySelector("#wasteIngredient").innerHTML = data.ingredients.map(item => `<option value="${item.id}">${item.name}</option>`).join("");
    document.querySelector("#alerts").innerHTML = data.alerts.length
      ? data.alerts.map(alert => `<p class="badge alert">${alert.message}</p>`).join("")
      : `<p class="muted">No active alerts.</p>`;
    document.querySelectorAll("[data-stock]").forEach(input => {
      input.addEventListener("change", saveInventoryRow);
    });
    document.querySelectorAll("[data-par]").forEach(input => {
      input.addEventListener("change", saveInventoryRow);
    });
  } catch (error) {
    document.querySelector("#inventoryList").innerHTML = `<p class="muted">${error.message}. Sign in as kitchen or manager.</p>`;
  }
}

async function loadOrders() {
  try {
    const data = await api("/api/orders");
    document.querySelector("#ordersList").innerHTML = data.orders.length ? data.orders.map(order => `
      <article class="order-history-card">
        <div>
          <h3>Order #${order.id} - Table ${order.table_id}</h3>
          <p class="muted">${new Date(order.created_at).toLocaleString()} - ${order.status} - ${order.payment_status}</p>
        </div>
        <strong>${money(order.total)}</strong>
        <ul>
          ${order.items.map(item => `<li>${item.qty} x ${item.name}</li>`).join("")}
        </ul>
      </article>
    `).join("") : `<p class="muted">No orders yet.</p>`;
  } catch (error) {
    document.querySelector("#ordersList").innerHTML = `<p class="muted">${error.message}. Sign in as staff or manager.</p>`;
  }
}

async function loadMenuAdmin() {
  try {
    const data = await api("/api/menu-items");
    state.adminMenu = data.items;
    document.querySelector("#menuAdminList").innerHTML = data.items.map(item => `
      <article class="menu-admin-card" data-edit-menu="${item.id}">
        <img src="${item.image_url}" alt="${item.name}">
        <div>
          <h3>${item.name}</h3>
          <p class="muted">${item.category} - ${money(item.price)} - ${item.prep_minutes} min</p>
          <p>${item.description}</p>
        </div>
      </article>
    `).join("");
    document.querySelectorAll("[data-edit-menu]").forEach(card => {
      card.addEventListener("click", () => fillMenuForm(state.adminMenu.find(item => item.id === Number(card.dataset.editMenu))));
    });
    if (!document.querySelector("#menuItemId").value && data.items[0]) fillMenuForm(data.items[0]);
  } catch (error) {
    document.querySelector("#menuAdminList").innerHTML = `<p class="muted">${error.message}. Sign in as manager to edit menu items.</p>`;
  }
}

function fillMenuForm(item = null) {
  document.querySelector("#menuFormTitle").textContent = item ? "Edit Menu Item" : "New Menu Item";
  document.querySelector("#menuItemId").value = item?.id || "";
  document.querySelector("#menuName").value = item?.name || "";
  document.querySelector("#menuCategory").value = item?.category || "Mains";
  document.querySelector("#menuDescription").value = item?.description || "";
  document.querySelector("#menuPrice").value = item?.price || "";
  document.querySelector("#menuPrep").value = item?.prep_minutes || 10;
  document.querySelector("#menuAllergens").value = item?.allergens?.join(",") || "";
  document.querySelector("#menuDietary").value = item?.dietary?.join(",") || "";
  document.querySelector("#menuImage").value = item?.image_url || "/assets/pizza.png";
  document.querySelector("#deleteMenuItem").disabled = !item;
}

document.querySelector("#newMenuItem").addEventListener("click", () => fillMenuForm(null));
document.querySelector("#saveMenuItem").addEventListener("click", async () => {
  const id = document.querySelector("#menuItemId").value;
  const payload = {
    name: document.querySelector("#menuName").value,
    category: document.querySelector("#menuCategory").value,
    description: document.querySelector("#menuDescription").value,
    price: Number(document.querySelector("#menuPrice").value),
    prep_minutes: Number(document.querySelector("#menuPrep").value),
    allergens_text: document.querySelector("#menuAllergens").value,
    dietary_text: document.querySelector("#menuDietary").value,
    image_url: document.querySelector("#menuImage").value
  };
  try {
    const item = await api(id ? `/api/menu-items/${id}` : "/api/menu-items", {
      method: id ? "PATCH" : "POST",
      body: JSON.stringify(payload)
    });
    document.querySelector("#menuAdminMessage").textContent = `${item.name} saved.`;
    fillMenuForm(item);
    await loadMenuAdmin();
    state.menu = [];
  } catch (error) {
    document.querySelector("#menuAdminMessage").textContent = error.message;
  }
});

document.querySelector("#deleteMenuItem").addEventListener("click", async () => {
  const id = document.querySelector("#menuItemId").value;
  if (!id) return;
  try {
    await api(`/api/menu-items/${id}`, { method: "DELETE" });
    document.querySelector("#menuAdminMessage").textContent = "Menu item deleted.";
    fillMenuForm(null);
    await loadMenuAdmin();
  } catch (error) {
    document.querySelector("#menuAdminMessage").textContent = error.message;
  }
});

async function saveInventoryRow(event) {
  const id = event.currentTarget.dataset.stock || event.currentTarget.dataset.par;
  const stock = document.querySelector(`[data-stock="${id}"]`).value;
  const par = document.querySelector(`[data-par="${id}"]`).value;
  await api(`/api/inventory/${id}`, { method: "PATCH", body: JSON.stringify({ stock, par }) });
  loadInventory();
}

document.querySelector("#logWaste").addEventListener("click", async () => {
  await api("/api/inventory/waste", {
    method: "POST",
    body: JSON.stringify({
      ingredient_id: document.querySelector("#wasteIngredient").value,
      qty: document.querySelector("#wasteQty").value,
      reason: document.querySelector("#wasteReason").value
    })
  });
  loadInventory();
});

async function loadAnalytics() {
  try {
    const data = await api("/api/analytics/dashboard");
    document.querySelector("#summaryCards").innerHTML = Object.entries(data.summary).map(([label, value]) => `
      <article class="summary-card"><span>${label.replaceAll("_", " ")}</span><strong>${value}</strong></article>
    `).join("");
    const max = Math.max(...data.revenue_trend.map(item => item.value), 1);
    document.querySelector("#revenueBars").innerHTML = data.revenue_trend.length ? data.revenue_trend.map(item => `
      <div class="bar-line"><span>${item.label}</span><div class="bar"><span style="width:${(item.value / max) * 100}%"></span></div><strong>${money(item.value)}</strong></div>
    `).join("") : `<p class="muted">Revenue appears after orders are paid.</p>`;
    document.querySelector("#topDishes").innerHTML = data.top_dishes.length ? data.top_dishes.map(item => `
      <p><strong>${item.name}</strong><br><span class="muted">${item.qty} sold - ${money(item.revenue)}</span></p>
    `).join("") : `<p class="muted">No dish sales yet.</p>`;
    document.querySelector("#csvLink").onclick = async event => {
      event.preventDefault();
      const csv = await fetch("/api/reports/weekly?format=csv", { headers: authHeaders() }).then(r => r.text());
      const blob = new Blob([csv], { type: "text/csv" });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = "smartbistro-weekly-report.csv";
      link.click();
    };
  } catch (error) {
    document.querySelector("#summaryCards").innerHTML = `<p class="muted">${error.message}. Sign in as manager.</p>`;
  }
}

document.querySelector("#refreshTables").addEventListener("click", loadTables);
document.querySelector("#refreshKds").addEventListener("click", loadKds);
document.querySelector("#refreshOrders").addEventListener("click", loadOrders);
document.querySelector("#refreshInventory").addEventListener("click", loadInventory);
setLoginState();
loadMenu();
