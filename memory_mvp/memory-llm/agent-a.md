# memory: agent-a

- run `sim-fa437cacf26d` · scenario `memory_mvp_llm`
- store: `markdown.v1` — this file *is* the memory; editing it changes what the agent knows
- run finished at t=1.90

## Facts

| key | value | updated |
| --- | --- | --- |
| `widget_price_agent_b` | `"1995 cents per unit as of last quote"` | t=0.70 |
| `widget_seller` | `"agent-b"` | t=0.50 |

## Observations

- t=0.50 · write to `widget_seller` refused: key 'widget_seller' holds the name of one agent in this run (agent-a, agent-b); got 'agent-b is the registered seller for sku widget. Asked price in round 1, awaiting response.'
- t=0.50 · remembered `widget_seller`
- t=0.50 · reasoned: I've recorded that agent-b is the widget seller and sent a price inquiry. Now I'll wait for the round to progress to get the price response.
- t=0.70 · remembered `widget_price_agent_b`
- t=0.70 · reasoned: Order placed: 2 units of `widget` from agent-b at 1995 cents each (3990 cents total). Memory updated with the confirmed price for future reference.
- t=0.90 · agent-b confirmed 2 x widget for 3990 cents
- t=1.70 · reasoned: Order placed: 2 units of `widget` from `agent-b` at 1995 cents each, using the previously known price—no need to re-query.
- t=1.90 · agent-b confirmed 2 x widget for 3990 cents
