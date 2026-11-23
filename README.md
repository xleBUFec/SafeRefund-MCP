# SafeRefund MCP

**2nd Brain Labs x Geek Room: Vibe-a-thon — Track 2: "One smart tool from many small functions"**

SafeRefund turns five small refund-processing functions into one safe, policy-aware refund tool with a mandatory human confirmation step.

---

## The problem

An online store's refund flow is a chain of five steps:

1. `get_order_details`: find the order
2. `check_refund_policy`: is it still refundable?
3. `calculate_refund_amount`: how much do we return? (issues a short-lived `quote_id`)
4. `execute_refund_transaction`: execute that quote, move the money and update the DB
5. `send_refund_notification`: send the customer a receipt

If an AI agent gets these as **five separate tools**, the agent has to orchestrate the business process itself, and that is where things go wrong:

- **It can skip the policy check.** If the agent calls `execute_refund_transaction` directly, the store refunds a 45-day-old order or a non-refundable digital license.
- **It can get the math wrong.** The agent can forget the 10% return-shipping fee for `changed_mind`, or make up an amount instead of calling the calculator.
- **It can move money without confirmation.** Nothing stops it from executing a refund before the user has agreed to the amount.
- **It wastes calls and tokens.** The happy path takes 5+ round-trips, and every one of them is another chance to pass the wrong argument.

**One smart tool** builds the correct order of steps into the tool itself. Policy is always checked, the amount is always calculated by code, and money moves only after explicit confirmation.

### The key idea: confirm a quote, not a flag

A `confirmed: bool` parameter is not a real safeguard: nothing stops an agent from passing `confirmed=True` on its very first call, skipping the preview the user was supposed to see.

SafeRefund confirms a **quote** instead. Money can only move with a `quote_id`, and only `calculate_refund_amount` can issue one:

- the amount is fixed in the DB when the quote is issued, so **the amount executed is exactly the amount shown to the user**;
- a quote is **single-use**, **bound to one order**, and **expires after 15 minutes**;
- quote IDs are random, so the agent cannot guess or forge one; it has to get it from a preview;
- executing a quote **re-runs the full policy**, so even an agent calling the atomic tools directly cannot refund a 45-day-old order.

---

## Project structure

```
refund_service.py        # 5 atomic functions + JSON storage (stdlib only)
test_refund_service.py   # unit tests for the safety guarantees
store_db.json            # auto-created next to refund_service.py with mock orders
README.md
```

Requirements: Python 3.8+, no external dependencies.

```bash
python refund_service.py      # read-only smoke check of all mock orders
python -m unittest -v         # run the tests (they use a temporary DB)
```

To reset the demo data (for example after `ORD-101` has been refunded):

```bash
python -c "import refund_service; print(refund_service.reset_database())"
```

### Business rules

| Rule | Value |
|---|---|
| Refund window | purchase made **less than 30 days** ago |
| Non-refundable categories | `digital`, `perishable` |
| Double refunds | rejected (`status == "refunded"`) |
| Undelivered orders | rejected (`status == "in_transit"`) |
| `defective` | 100% refund |
| `changed_mind` | 90% refund (10% return-shipping fee withheld) |
| Refund quote | single-use, bound to one order, valid for 15 minutes |

### Mock data

| Order | Product | Category | Amount | Purchased | Status | Expected result |
|---|---|---|---|---|---|---|
| `ORD-101` | Sony Headphones | electronics | 250.00 | 5 days ago | delivered | ✅ Refundable |
| `ORD-102` | Keyboard | electronics | 120.00 | 45 days ago | delivered | ❌ Window expired |
| `ORD-103` | Software License | digital | 60.00 | 2 days ago | delivered | ❌ Non-refundable category |
| `ORD-104` | Jacket | clothing | 180.00 | 10 days ago | refunded | ❌ Already refunded |
| `ORD-105` | Coffee Grinder | home | 95.00 | 1 day ago | in_transit | ❌ Not delivered yet |

### Built-in safety

The atomic functions also protect themselves, even when they are called directly:

- `execute_refund_transaction` takes a `quote_id`, not an amount. It refuses unknown, expired, already used, or mismatched quotes, and re-runs the full refund policy before moving money.
- `send_refund_notification` only issues a receipt if the `refund_id` and `amount` match a refund recorded in the DB.

The smart tool gets it right on the first call. The atomic guards are the last line of defense: an agent working with 5 separate tools fails with an error instead of doing damage.

---

## Build the smart tool with Synapse CLI (Custom mode)

```bash
synapse build --custom --query "Compose order lookup, refund policy check, refund amount calculation, refund execution, and customer notification into one smart refund tool smart_refund(order_id: str, reason: str, quote_id: str = ''). When quote_id is empty: look up the order, check the refund policy, and if it is eligible calculate the refund amount and return a summary (paid amount, fee, refund amount, quote_id, expiry) that asks the user to confirm; if it is not eligible return the violations and do not calculate anything. When quote_id is given: execute the refund with that quote_id and then send the customer notification with the returned refund_id and amount. Never accept a raw refund amount from the caller. Always return one of three statuses: 'rejected', 'needs_confirmation', or 'refunded'."
```

How the resulting tool behaves:

```
smart_refund(order_id, reason, quote_id="")
│
├─ quote_id == ""   (preview, no money moves)
│  ├─ get_order_details ──── not found? ─────► status: rejected (error)
│  ├─ check_refund_policy ── not eligible? ──► status: rejected + violations
│  └─ calculate_refund_amount ───────────────► status: needs_confirmation
│                                              amount, fee, quote_id, expires_at
│
└─ quote_id given   (only after the user agreed)
   ├─ execute_refund_transaction  re-checks quote + policy, DB updated, refund_id issued
   └─ send_refund_notification    receipt returned ─► status: refunded
```

### What the confirmation does and does not guarantee

The quote guarantees that the agent **must show a preview first** and that **the amount executed is exactly the previewed amount**. It cannot prove that a *human* read the preview: an agent could still pass the `quote_id` back without asking. To close that gap in production, put the second call behind the MCP client's per-tool approval prompt (or MCP elicitation), so the user approves the call that carries the `quote_id`.

---

## Demo prompts

Run `reset_database()` before the demo so `ORD-101` is refundable again.

### 1. Successful refund with confirmation: `ORD-101`

> **User:** I'd like to return my Sony headphones from order ORD-101, I just changed my mind.

Expected: the tool runs without a `quote_id` and returns `needs_confirmation`: 250.00 paid, 10% fee (25.00), **225.00 to be refunded**, a `quote_id` such as `Q-ORD-101-…`, and a confirmation request. The order is not changed yet.

> **User:** Yes, I confirm. Please go ahead.

Expected: the tool runs with that `quote_id`. The order is set to `refunded`, a `refund_id` such as `RF-ORD-101-…` is returned, and the customer receipt is shown.

**Bonus (shows why quotes matter):** ask the agent to confirm the same refund again. The quote has already been used, so it is rejected and no second refund is issued.

### 2. Rejection: refund window expired, `ORD-102`

> **User:** My keyboard from order ORD-102 is defective, I want my money back.

Expected: rejected with *"Refund window expired: purchased 45 days ago (limit is less than 30 days)."* No amount is offered and no confirmation is requested.

### 3. Rejection: non-refundable digital product, `ORD-103`

> **User:** Please refund the software license I bought in order ORD-103, I changed my mind.

Expected: rejected with *"Category 'digital' is non-refundable."* The database is not changed.

---

## Comparison: 5 small tools vs 1 smart tool

Fill in the "measured" values during your demo run. The expected values describe the design intent.

| Metric | 5 small tools (expected) | 1 smart tool (expected) | 5 small tools (measured) | 1 smart tool (measured) |
|---|---|---|---|---|
| Tool calls: successful refund (`ORD-101`) | 5 (+1 if the agent re-checks) | 2 (preview + confirm) | | |
| Tool calls: rejected refund (`ORD-102` / `ORD-103`) | 2–3 | 1 | | |
| Policy check always performed | ❌ Depends on the agent | ✅ Enforced in code | | |
| Refund amount always computed by code (10% fee) | ⚠️ Enforced by the quote, but the agent must discover the quote flow by trial and error | ✅ Enforced in code | | |
| Preview before moving money | ⚠️ Enforced by the quote, but the agent can skip showing the amount | ✅ Built in: `needs_confirmation` with the amount | | |
| Error handling | Agent has to interpret 5 different error shapes | One consistent result: rejected / needs confirmation / done | | |
| Risk of refund without notification | ⚠️ Agent may stop after step 4 | ✅ Notification is part of the same call | | |
| Tokens spent on orchestration | High | Low | | |
