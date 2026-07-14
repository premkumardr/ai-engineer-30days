# AussieMart — Day 11 Front End

A zero-dependency e-commerce storefront listing **100 Australian grocery products** with AUD prices (GST inclusive), plus an **AI shopping assistant widget** backed by the FastAPI + pgvector RAG service in the parent project.

## Run it

The storefront itself has no build step — serve the folder:

```powershell
cd frontend
python -m http.server 8123
# then open http://localhost:8123
```

For the AI widget to work, the backend must also be running (from the project root):

```powershell
docker compose up -d postgres                                  # pgvector DB on :5433
python -m uvicorn src.api.main:app --port 8000                 # FastAPI on :8000
# one-time: embed the catalogue into pgvector
Invoke-RestMethod -Method Post http://localhost:8000/assistant/ingest-products
```

The OpenAI key is read from `.env.openai` (`OPENAI_KEY=...`) at the project root.

## AI Assistant widget

The 🤖 button (bottom-right) opens a chat panel that calls `POST /assistant/chat`, sending the message plus the current trolley contents. The assistant is scoped to exactly two jobs:

1. **Product & pricing questions** — answered via RAG: the question is embedded, matched against the 100 catalogue embeddings in PostgreSQL/pgvector (cosine similarity, HNSW index), and answered only from the retrieved rows.
2. **Recipes from the trolley** — suggests a dish using only the items in the cart.

Anything else (general knowledge, jokes, prompt-injection attempts) gets a polite refusal via an LLM intent classifier.

## Features

- 100 products across 9 categories (Fruit & Veg, Bakery, Dairy & Eggs, Meat & Seafood, Pantry, Frozen, Drinks, Snacks, Household)
- Live search across product names and categories
- Category filter chips and sorting (price low→high, high→low, name A–Z)
- Slide-out trolley with quantity controls, AUD totals, and GST breakdown (1/11 of the GST-inclusive total)
- Cart persists in `localStorage` across refreshes
- Responsive layout with automatic light/dark theme

## Files

| File | Purpose |
|------|---------|
| `index.html` | Page shell: header, toolbar, product grid, cart drawer |
| `products.js` | The 100-product catalogue (id, name, category, AUD price, unit) |
| `app.js` | Search/filter/sort/cart state and rendering |
| `styles.css` | Theming and layout |

Prices are indicative of typical Australian supermarket shelf prices; this is a fictional demo store.
