# Day 2: How LLMs Actually Work

## Overview
Understanding the internals of Large Language Models is essential for building reliable AI systems. You don't need to understand transformer math — but you do need to understand tokens, context, and generation parameters.

## Key Concepts

### 1. Tokens
- Text is split into **tokens**, not words
- ~0.75 words per token on average
- "engineering" = 2 tokens; "AI" = 1 token
- GPT-4o pricing: ~$5/million input tokens, ~$15/million output tokens
- **Why it matters**: token count drives cost and context window limits

### 2. Context Window
- The model's "working memory"
- Everything in the context window influences the output
- Once you exceed the limit, older content is dropped
- **GPT-4o**: 128K tokens | **Claude 3.5**: 200K tokens | **Gemini 1.5 Pro**: 1M tokens
- **Why it matters**: long documents need chunking strategies

### 3. Temperature
- Controls randomness/creativity of output
- `0.0` = deterministic (same output every time)
- `1.0` = creative/variable
- `>1.0` = increasingly random (rarely used)
- **Enterprise default**: 0.0–0.3 for consistent, reliable outputs

### 4. System Prompt
- Instructions set before any user message
- Defines: persona, constraints, output format, domain knowledge
- The most powerful lever you have as an AI Engineer
- **Example**:
```
You are a legal assistant for Smith & Jones LLP.
- Always cite the source document and page number
- Never provide definitive legal advice
- Respond in formal Australian English
- If uncertain, say so explicitly
```

### 5. Completion vs Chat APIs
- **Completion** (legacy): single prompt → single response
- **Chat** (modern): conversation with roles
  - `system`: your instructions
  - `user`: what the human says
  - `assistant`: model responses (can be pre-filled)

## Code Example
```python
from openai import OpenAI

client = OpenAI()

response = client.chat.completions.create(
    model="gpt-4o",
    temperature=0.1,  # Low for enterprise reliability
    max_tokens=1000,
    messages=[
        {
            "role": "system",
            "content": "You are a legal document assistant. Always cite sources."
        },
        {
            "role": "user", 
            "content": "What are the termination conditions in this contract?"
        }
    ]
)

print(response.choices[0].message.content)
print(f"Tokens used: {response.usage.total_tokens}")
print(f"Estimated cost: ${response.usage.total_tokens * 0.000005:.4f}")
```

## Production Considerations
| Parameter | Development | Production |
|-----------|------------|------------|
| Temperature | 0.7–1.0 | 0.0–0.3 |
| Max tokens | Uncapped | Set explicit limit |
| Timeout | 60s | 30s with retry |
| Retries | 0 | 3 with exponential backoff |
