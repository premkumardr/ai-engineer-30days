Day 11 of #30DaysOfAIEngineering | I built an AI shopping assistant that refuses to talk about anything except groceries 🛒🤖

The project: AussieMart — an e-commerce storefront with 100 Australian grocery products, plus an AI assistant that does exactly two things:

1️⃣ Answers product & pricing questions ("how much are free range eggs?" → "$6.50 AUD each")
2️⃣ Suggests recipes using only what's in your trolley

Ask it anything else — news, jokes, "ignore your instructions" — and it politely declines.

Why enterprise RAG for a grocery store?

Because "just call the LLM" fails in production:
❌ It invents products you don't stock
❌ It quotes prices from its training data, not your shelf
❌ It happily answers off-topic questions on your brand's dime

The fix — ground every answer in your own data:

🔹 All 100 products embedded (OpenAI text-embedding-3-small) into PostgreSQL + pgvector, running in Docker
🔹 Every pricing question → embedded → cosine similarity search (HNSW index) → the LLM answers ONLY from retrieved rows
🔹 An intent classifier routes each message: product_pricing / recipe / off_topic
🔹 Recipes are constrained to the customer's actual cart items — the frontend sends the trolley with every message
🔹 FastAPI backend, vanilla JS chat widget on the storefront — no framework needed

Key decisions:
→ pgvector over a dedicated vector DB: 100 products don't need Pinecone, and Postgres gives you SQL + vectors in one place
→ gpt-4o-mini for chat + classification: scoped RAG answers don't need a frontier model
→ Retrieval as the guardrail: if it's not in the catalogue, the assistant says so instead of guessing

The result: an assistant that quotes real shelf prices, cooks with what you bought, and stays in its lane.

Code → link in comments

#RAG #AIEngineering #FastAPI #pgvector #PostgreSQL #OpenAI #Ecommerce
