# memory: agent-b

- run `sim-6ea9bb54a07a` · scenario `memory_mvp`
- store: `markdown.v1` — this file *is* the memory; editing it changes what the agent knows
- run finished at t=1.90

## Facts

| key | value | updated |
| --- | --- | --- |
| `last_buyer` | `"agent-a"` | t=1.80 |
| `last_question_from` | `"agent-a"` | t=0.60 |
| `orders_filled` | `1` | t=1.80 |
| `questions_answered` | `1` | t=0.60 |

## Observations

- t=0.00 · open for business: widget at 1995 cents each
- t=0.60 · agent-a asked what widget costs; quoted 1995 cents
- t=0.60 · remembered `last_question_from`
- t=0.60 · remembered `questions_answered`
- t=1.80 · agent-a ordered 2 x widget without asking the price again; their remembered price matched mine
- t=1.80 · remembered `last_buyer`
- t=1.80 · remembered `orders_filled`
