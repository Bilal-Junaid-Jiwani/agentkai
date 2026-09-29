---
name: payments
description: Create and inspect Stripe payment intents (STRIPE_SECRET_KEY). All tools are high risk and gated — every charge needs the user's explicit approval.
version: 0.1.0
when: payment, payments, charge, invoice, stripe, pay link, payment intent, checkout
---

# Payments

Create and inspect **Stripe payment intents** — the server-side object
that represents "the user intends to pay X". Every tool in this skill
is **high risk** and goes through the permission gate: nothing moves
money without the user's explicit approval, every time.

Tools:

- `payments_create_intent(amount, currency, description)` — create a
  payment intent. `amount` is in the currency's minor unit (cents for
  USD/EUR, so 1000 = $10.00).
- `payments_get_intent(payment_intent_id)` — inspect an intent's status
  (`requires_payment_method`, `requires_confirmation`, `succeeded`, …).

## Setup (required — nothing works without this)

1. In the Stripe dashboard (https://dashboard.stripe.com → Developers →
   API keys), create a **restricted** key with only
   `payment_intents: write` / `payment_intents: read` if possible;
   otherwise use the secret key. Test mode (`sk_test_…`) for trying
   things out.
2. Provide it as the `STRIPE_SECRET_KEY` environment variable, or as
   `~/.agentkai/stripe.json` containing `{"access_token": "sk_…"}`.

Without a key every tool returns a configuration error.

## Usage notes

- Creating an intent does **not** charge anyone — it prepares the
  payment. Confirming/capturing still needs the user's explicit say-so
  each time; the gate will ask.
- The agent **never handles raw card numbers**. Card entry happens in
  Stripe's hosted UI (Checkout / Payment Links / Elements) using the
  returned `client_secret`. If card details are pasted into chat, do
  not store or forward them — point the user at the Stripe-hosted flow.
- `amount` must be a positive integer in minor units; `currency` is a
  3-letter ISO code (`usd`, `eur`, `pkr` where Stripe supports it).
- Keys are never printed or logged; error messages carry status only.

## What's real / what's not

- REAL: creating and inspecting Stripe payment intents via the live
  Stripe API.
- NOT PROVIDED: card storage, PCI-scope card handling, payouts,
  subscriptions, refunds (out of scope for this skill version). Test
  mode exists for a reason — use `sk_test_…` until real money is truly
  intended.
