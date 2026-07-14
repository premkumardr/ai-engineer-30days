// AussieMart AI Assistant widget — floating chat that talks to the FastAPI
// backend (/assistant/chat), sending the current trolley so the assistant
// can suggest recipes from purchased items.

const ASSISTANT_API = "http://localhost:8000/assistant/chat";

const widgetHtml = `
  <button id="ai-fab" class="ai-fab" aria-label="Open AI assistant">🤖</button>
  <div id="ai-panel" class="ai-panel" role="dialog" aria-label="AI shopping assistant">
    <div class="ai-header">
      <span>🤖 AussieMart Assistant</span>
      <button id="ai-close" class="ai-close" aria-label="Close assistant">✕</button>
    </div>
    <div id="ai-messages" class="ai-messages">
      <div class="ai-msg ai-bot">G'day! Ask me about products and prices, or ask
      "what can I cook with my trolley?" for a recipe from your cart. 🛒</div>
    </div>
    <div class="ai-suggestions">
      <button class="ai-chip" data-q="How much is full cream milk?">Milk price?</button>
      <button class="ai-chip" data-q="What snacks do you have under $4?">Snacks under $4</button>
      <button class="ai-chip" data-q="What can I cook with the items in my trolley?">Recipe from trolley</button>
    </div>
    <form id="ai-form" class="ai-form">
      <input id="ai-input" type="text" placeholder="Ask about products, prices, recipes…"
             maxlength="500" autocomplete="off">
      <button type="submit" id="ai-send" class="ai-send" aria-label="Send">➤</button>
    </form>
  </div>`;

document.body.insertAdjacentHTML("beforeend", widgetHtml);

const fab = document.getElementById("ai-fab");
const panel = document.getElementById("ai-panel");
const messagesEl = document.getElementById("ai-messages");
const formEl = document.getElementById("ai-form");
const inputEl = document.getElementById("ai-input");
const sendBtn = document.getElementById("ai-send");

fab.addEventListener("click", () => {
  panel.classList.toggle("open");
  if (panel.classList.contains("open")) inputEl.focus();
});
document.getElementById("ai-close").addEventListener("click", () => panel.classList.remove("open"));

document.querySelectorAll(".ai-chip").forEach((chip) =>
  chip.addEventListener("click", () => {
    inputEl.value = chip.dataset.q;
    formEl.requestSubmit();
  })
);

function cartPayload() {
  // Reads the same localStorage cart the storefront maintains.
  let cart = {};
  try { cart = JSON.parse(localStorage.getItem("aussiemart-cart")) || {}; } catch {}
  return Object.entries(cart)
    .filter(([, qty]) => qty > 0)
    .map(([id, qty]) => {
      const p = PRODUCTS.find((x) => x.id === Number(id));
      return p ? { id: p.id, name: p.name, qty, price: p.price, unit: p.unit } : null;
    })
    .filter(Boolean);
}

function addMsg(text, who) {
  const div = document.createElement("div");
  div.className = `ai-msg ai-${who}`;
  div.textContent = text; // textContent = safe; CSS pre-line keeps formatting
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div;
}

formEl.addEventListener("submit", async (e) => {
  e.preventDefault();
  const message = inputEl.value.trim();
  if (!message) return;
  inputEl.value = "";
  addMsg(message, "user");
  const thinking = addMsg("Thinking…", "bot");
  sendBtn.disabled = true;
  try {
    const r = await fetch(ASSISTANT_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, cart: cartPayload() }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    thinking.textContent = data.reply;
  } catch (err) {
    thinking.textContent = `Sorry, I couldn't reach the assistant (${err.message}). ` +
      "Make sure the API is running on http://localhost:8000.";
    thinking.classList.add("ai-error");
  }
  sendBtn.disabled = false;
  messagesEl.scrollTop = messagesEl.scrollHeight;
});
