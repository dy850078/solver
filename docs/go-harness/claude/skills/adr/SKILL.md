---
name: adr
description: Write a mentor-style Architecture Decision Record (ADR) in docs/decisions/ for the current change. Use after modifying contract assembly (internal/solverclient, internal/planning), candidate derivation, demand-book semantics, or Inventory schema — or when the Stop hook demands one.
---

# Write a mentor-style ADR

Produce one decision record for the change at hand, written for an engineer
who is learning distributed-system contract design and capacity planning.

## Steps

1. Determine the next number: Glob `docs/decisions/*.md`, find the highest
   `ADR-NNN`, use NNN+1 (zero-padded to 3 digits). TEMPLATE.md doesn't count.
2. Review the actual diff of this change (`git diff` against the branch base)
   — the ADR must describe what was really done, not what was planned.
3. Write `docs/decisions/ADR-NNN-<short-slug>.md` following
   `docs/decisions/TEMPLATE.md` exactly — every section, in order.

## Quality bar (this is the point of the exercise)

- **Traditional Chinese** prose; code identifiers and field names stay in
  English.
- Section 2 (alternatives) must contain at least one genuinely viable
  rejected option with the real reason it lost — "we didn't consider
  anything else" means the ADR is not done.
- Section 4 (implementation walkthrough) must cite concrete `file.go:line`
  locations and explain **which invariant each piece protects** — not just
  what the code does. Name the failure mode it prevents (契約漂移 / 容量
  高估 / join 斷鏈 / 交易半完成…).
- If the change touches the solver contract, state explicitly **which side
  is authoritative** and what happens when the two disagree.
- Section 6 (takeaways) is a max-3-bullet distillation a reader could quote
  from memory a week later.
- Keep the whole ADR under ~120 lines. Density over length — a bloated ADR
  won't be read, and an unread ADR teaches nothing.
