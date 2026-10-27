# memory: agent-a

- run `sim-52f7fdc85fab` · scenario `memory_mvp_llm`
- store: `markdown.v1` — this file *is* the memory; editing it changes what the agent knows
- run finished at t=1.90

## Facts

| key | value | updated |
| --- | --- | --- |
| `preferred_seller` | `"agent-b"` | t=0.70 |
| `widget_unit_cents` | `1995` | t=0.70 |

## Observations

- t=0.50 · reasoned: Asked; waiting for the answer.
- t=0.70 · remembered `widget_unit_cents`
- t=0.70 · remembered `preferred_seller`
- t=0.70 · reasoned: Noted the price and who quoted it.
- t=1.70 · reasoned: Ordered at the remembered price, no question asked.
- t=1.90 · agent-b confirmed 2 x widget for 3990 cents
