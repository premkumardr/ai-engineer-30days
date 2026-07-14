// AussieMart storefront — search, category filter, sort, and cart state.
// Cart persists to localStorage so a refresh keeps the trolley.

const state = {
  search: "",
  category: "All",
  sort: "featured",
  cart: loadCart(), // { [productId]: quantity }
};

const grid = document.getElementById("grid");
const chipsEl = document.getElementById("chips");
const searchEl = document.getElementById("search");
const sortEl = document.getElementById("sort");
const resultCountEl = document.getElementById("result-count");
const cartCountEl = document.getElementById("cart-count");
const cartItemsEl = document.getElementById("cart-items");
const cartTotalEl = document.getElementById("cart-total");
const gstEl = document.getElementById("gst-amount");
const checkoutBtn = document.getElementById("checkout-btn");

const AUD = new Intl.NumberFormat("en-AU", { style: "currency", currency: "AUD" });

function loadCart() {
  try {
    return JSON.parse(localStorage.getItem("aussiemart-cart")) || {};
  } catch {
    return {};
  }
}

function saveCart() {
  localStorage.setItem("aussiemart-cart", JSON.stringify(state.cart));
}

function getFiltered() {
  let items = PRODUCTS.filter((p) => {
    const matchesCategory = state.category === "All" || p.category === state.category;
    const matchesSearch = p.name.toLowerCase().includes(state.search) ||
      p.category.toLowerCase().includes(state.search);
    return matchesCategory && matchesSearch;
  });
  if (state.sort === "price-asc") items = [...items].sort((a, b) => a.price - b.price);
  if (state.sort === "price-desc") items = [...items].sort((a, b) => b.price - a.price);
  if (state.sort === "name") items = [...items].sort((a, b) => a.name.localeCompare(b.name));
  return items;
}

function renderChips() {
  const categories = ["All", ...new Set(PRODUCTS.map((p) => p.category))];
  chipsEl.innerHTML = categories
    .map((c) => `<button class="chip${c === state.category ? " active" : ""}" data-cat="${c}">${c}</button>`)
    .join("");
}

function cardActions(p) {
  const qty = state.cart[p.id] || 0;
  if (qty === 0) {
    return `<button class="add-btn" data-add="${p.id}">Add to Trolley</button>`;
  }
  return `<div class="qty-controls">
    <button data-dec="${p.id}" aria-label="Decrease quantity">−</button>
    <span class="qty">${qty}</span>
    <button data-inc="${p.id}" aria-label="Increase quantity">+</button>
  </div>`;
}

function renderGrid() {
  const items = getFiltered();
  resultCountEl.textContent = `${items.length} of ${PRODUCTS.length} products`;
  if (items.length === 0) {
    grid.innerHTML = `<div class="empty">No products match your search. Try something else.</div>`;
    return;
  }
  grid.innerHTML = items
    .map((p) => `
      <div class="card">
        <div class="emoji">${p.emoji}</div>
        <div class="cat">${p.category}</div>
        <div class="name">${p.name}</div>
        <div class="price-row">
          <span class="price">${AUD.format(p.price)}</span>
          <span class="unit">${p.unit}</span>
        </div>
        ${cardActions(p)}
      </div>`)
    .join("");
}

function renderCart() {
  const entries = Object.entries(state.cart).filter(([, qty]) => qty > 0);
  const totalItems = entries.reduce((sum, [, qty]) => sum + qty, 0);
  cartCountEl.textContent = totalItems;
  cartCountEl.style.display = totalItems > 0 ? "flex" : "none";

  if (entries.length === 0) {
    cartItemsEl.innerHTML = `<div class="cart-empty">Your trolley is empty.<br>Add some groceries! 🛒</div>`;
    cartTotalEl.textContent = AUD.format(0);
    gstEl.textContent = AUD.format(0);
    checkoutBtn.disabled = true;
    return;
  }

  let total = 0;
  cartItemsEl.innerHTML = entries
    .map(([id, qty]) => {
      const p = PRODUCTS.find((x) => x.id === Number(id));
      const line = p.price * qty;
      total += line;
      return `<div class="cart-item">
        <span class="emoji">${p.emoji}</span>
        <div class="info">
          <div class="name">${p.name}</div>
          <div class="line-price">${AUD.format(p.price)} × ${qty} = ${AUD.format(line)}</div>
        </div>
        <div class="qty-controls">
          <button data-dec="${p.id}" aria-label="Decrease quantity">−</button>
          <span class="qty">${qty}</span>
          <button data-inc="${p.id}" aria-label="Increase quantity">+</button>
        </div>
      </div>`;
    })
    .join("");

  cartTotalEl.textContent = AUD.format(total);
  gstEl.textContent = AUD.format(total / 11); // GST is 1/11 of a GST-inclusive price
  checkoutBtn.disabled = false;
}

function changeQty(id, delta) {
  const next = (state.cart[id] || 0) + delta;
  if (next <= 0) delete state.cart[id];
  else state.cart[id] = next;
  saveCart();
  renderGrid();
  renderCart();
}

// Event wiring — one delegated listener covers grid and cart drawer.
document.addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  if (btn.dataset.add) changeQty(Number(btn.dataset.add), 1);
  if (btn.dataset.inc) changeQty(Number(btn.dataset.inc), 1);
  if (btn.dataset.dec) changeQty(Number(btn.dataset.dec), -1);
  if (btn.dataset.cat) {
    state.category = btn.dataset.cat;
    renderChips();
    renderGrid();
  }
});

searchEl.addEventListener("input", () => {
  state.search = searchEl.value.trim().toLowerCase();
  renderGrid();
});

sortEl.addEventListener("change", () => {
  state.sort = sortEl.value;
  renderGrid();
});

document.getElementById("cart-btn").addEventListener("click", () => {
  document.body.classList.add("cart-open");
});
document.getElementById("close-cart").addEventListener("click", () => {
  document.body.classList.remove("cart-open");
});
document.getElementById("drawer-overlay").addEventListener("click", () => {
  document.body.classList.remove("cart-open");
});

checkoutBtn.addEventListener("click", () => {
  const totalItems = Object.values(state.cart).reduce((a, b) => a + b, 0);
  alert(`Thanks! Order placed for ${totalItems} item(s), total ${cartTotalEl.textContent}. (Demo only — no payment taken.)`);
  state.cart = {};
  saveCart();
  renderGrid();
  renderCart();
  document.body.classList.remove("cart-open");
});

renderChips();
renderGrid();
renderCart();
