## Product Context

This backend powers an automated AI sales assistant for one e-commerce business.

The system connects with:
- Meta Messenger / Instagram DM APIs to receive and send buyer messages
- DeepSeek LLM to understand buyer intent and generate responses
- Shopify APIs to check product availability and create orders
- A simple frontend dashboard for stats and configuration

Core flow:
1. Buyer sends a message on Messenger or Instagram.
2. Backend receives the message through Meta webhooks.
3. Backend sends the conversation context to DeepSeek.
4. DeepSeek may respond directly or request tool calls.
5. Backend executes tool calls such as:
   - search products
   - check inventory
   - collect buyer details
   - create Shopify order
6. Backend sends the final response back to the buyer through Meta APIs.

The backend must be designed as an automation/orchestration system, not just a CRUD API.

## Important Architecture Rules

- Keep Shopify logic inside a dedicated Shopify module/service.
- Keep Meta Messenger/Instagram logic inside a dedicated Meta module/service.
- Keep LLM logic inside a dedicated AI/LLM module.
- Keep tool-calling logic inside an orchestration module.
- Do not call Shopify or Meta APIs directly from route handlers.
- Do not mix webhook handling, LLM prompts, and Shopify order logic in the same file.
- All external API calls must go through service classes/functions.
- All buyer conversations should be stored so the LLM can receive useful context.
- Order creation must only happen after required buyer details are collected and validated.