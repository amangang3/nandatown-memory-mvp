# memory: agent-a

- run `sim-7293bd016a69` · scenario `memory_mvp`
- store: `markdown.v1` — this file *is* the memory; editing it changes what the agent knows
- run finished at t=1.90

## Facts

| key | value | updated |
| --- | --- | --- |
| `last_order_total_cents` | `3990` | t=1.90 |
| `preferred_seller` | `"agent-b"` | t=0.70 |
| `widget_unit_cents` | `1995` | t=0.70 |

## Observations

- t=0.50 · round 1: nothing remembered about widget; asking agent-b
- t=0.70 · agent-b answered: widget at 1995 cents each
- t=0.70 · remembered `preferred_seller`
- t=0.70 · remembered `widget_unit_cents`
- t=1.70 · round 2: recalled widget at 1995 cents from agent-b; ordering straight from memory
- t=1.90 · agent-b confirmed the order: 3990 cents total
- t=1.90 · remembered `last_order_total_cents`
