
## Role and goal

You are a senior Python engineer building a working hackathon prototype called **SignalShift AI** for AIBuilderCup 2026 (Hack2skill). Build it phase by phase. After each phase, run the tests, summarize what works, and stop for my review before starting the next one.

## Product in one paragraph

SignalShift AI is an inventory-aware ad bidding assistant. Marketing systems and inventory systems don't talk to each other, so ads keep running for products that are about to sell out, and every wasted click burns money and customer trust. SignalShift connects live sales, stock, and ad campaigns. When a product (or a specific size) is about to stock out while its ad is live, it estimates when, explains why, recommends what to do (pause the ad, shift budget, reorder), and sends a short report to the business owner like a personal assistant. The owner approves with one tap, and the action runs.

Goals the product must demonstrate: save ad spend, detect stockouts before they happen, protect customer satisfaction.

**Design rule (non-negotiable): code does the math, Gemini does the language.** Forecasts, days of cover, and wasted spend are computed in Python. Gemini only turns a structured facts JSON into an explanation and recommendation. Every number in Gemini's text must be checked against the facts JSON.

## Demo business

Fictional Indian D2C footwear and accessories brand **Solenest**, heading into the Diwali season. Inventory and campaigns are tracked at product + size variant level (UK 6 to 11). This is the hero insight: a dashboard can say "120 pairs in stock" while UK 8 and 9, which get most of the ad clicks, are almost gone.

Keep the engine generic: only the meaning of "stock unit" should be market-specific, so the same code could serve other markets later.

## Tech stack

- Python 3.11+, FastAPI, Pydantic v2, SQLAlchemy 2 over SQLite (file `signalshift.db`), APScheduler, pandas, numpy
- Gemini via the `google-genai` SDK. Read model names from env vars `GEMINI_MODEL` and `GEMINI_FALLBACK_MODEL`. Do not hardcode model names. Check the current official Gemini docs for valid model names and the structured-output API before writing the client.
- Frontend: a single FastAPI-served web app (HTML, vanilla JS, Plotly) with live updates over Server-Sent Events. No separate Node build step.
- Telegram delivery through the Bot API using `httpx`, optional via env vars. The in-app inbox must always work without Telegram.
- Tests: pytest. Config: `.env` plus `.env.example`. Dependencies in `pyproject.toml` or `requirements.txt`.

## Repository layout

```
signalshift/
  app/
    main.py               # FastAPI app, routers, SSE, scheduler startup
    config.py             # env settings
    db.py                 # engine, session, schema creation
    models.py             # SQLAlchemy models
    schemas.py            # Pydantic contracts (API + Gemini facts/report)
    services/
      inventory.py        # apply order, decrement stock, log movement, emit event
      events.py           # asyncio queue + SSE broadcaster
      forecast.py         # velocity + seasonal + campaign factors, confidence band
      engine.py           # days of cover, severity, cooldown, budget shift, wasted spend
      reporter.py         # Gemini call, structured output, fallback template
      guardrails.py       # number and claim checks on generated text
      delivery.py         # in-app inbox + Telegram
      actions.py          # approve/dismiss, mock Ads API executor, savings ledger
      ask.py              # router, whitelisted SQL, retrieval, answer composer
      replay.py           # counterfactual savings replay
    routers/              # orders, dashboards, alerts, ask, demo, telegram
    web/                  # index.html, app.js, styles.css (dashboards + ask + inbox)
  scripts/
    generate_data.py      # synthetic data generator (seeded)
    run_replay.py
  tests/
  README.md
```

## Data model

Create these tables (adjust column details sensibly, keep the names):

- `products`: id, sku_group, name, category, price, cost, seasonality_tag
- `variants`: id, product_id, size, color, sku (unique)
- `inventory`: variant_id, on_hand, reserved, in_transit, reorder_point, lead_time_days, restock_eta
- `inventory_movements`: id, ts, variant_id, type (sale/restock/return), qty
- `orders`, `order_items`: ts, variant_id, qty, price, channel, city
- `campaigns`: id, platform, product_id, daily_budget, bid, status (active/paused), started_at
- `campaign_size_mix`: campaign_id, size, click_share (the share of campaign clicks that want each size)
- `ad_metrics_hourly`: ts, campaign_id, impressions, clicks, cpc, spend, conversions
- `site_events`: ts, variant_id, views, add_to_cart, oos_page_views
- `calendar_events`: date, name (Diwali, Dhanteras, end-of-season sale), category_uplift
- `alerts`: id, ts, variant_id, campaign_id, severity, facts_json, report_text, status (open/approved/dismissed/expired), cooldown_until
- `actions`: id, alert_id, type (pause_campaign/shift_budget/reorder), payload_json, executed_at
- `savings_ledger`: alert_id, ad_spend_avoided, sales_protected, ts
- `ask_log`: id, ts, question, route, answer, evidence_json
- FTS5 virtual table over alert and report text for the "why" questions

## Phase 1: data generator and schema

Build `scripts/generate_data.py` (seeded, reproducible, `--reset` flag):

- About 40 products and 250 size-level variants. Groups: hero sneakers (limited retro runner, Festive White Sneaker, chunky trend sneaker), core sneakers (everyday runner, canvas low-top), accessories (socks 3-pack, cleaning kit, backpack, cap), slow movers (last-season colorways). Indian prices in INR with realistic margins.
- Size demand curve for UK 6 to 11 where sizes 8 and 9 are about 55% of sales.
- 2 years of order history, hourly ad metrics for the last 30 days.
- Demand = base rate x day-of-week x festival multiplier (Diwali window roughly 3x to 5x for hero products and gifting accessories) x campaign uplift x noise.
- Calendar: Make dates configurable and add a README note to verify them. Use Indian Calendar events. 
- Plant a hidden truth: 4 to 5 hero variants that deliberately stock out during live campaigns, with a flag stored in a separate `ground_truth` table the engine never reads. Replay uses it to validate detection.
- Seed the "demo state": Festive White Sneaker has a live campaign, UK 8 and 9 are low (UK 9 at about 4 pairs, UK 8 at about 11), other sizes are comfortable, and Everyday Runner has about 22 days of cover as the budget-shift target.

**Acceptance:** running the script creates the database, prints a summary, and tests confirm the size curve, the festive uplift, and the planted stockouts.

## Phase 2: order to inventory to event pipeline

- `POST /orders` accepts items (variant sku, qty), applies the order atomically (never oversell, return a clear error), logs an `inventory_movements` row, and publishes an event.
- Event bus: asyncio queue with a consumer that triggers the engine for only the affected variant. SSE endpoint `/stream` broadcasts sales, inventory, and alert updates.
- A small storefront demo page (`/store`) where I can buy a size of a product.

**Acceptance:** ordering from `/store` decrements stock, updates a live counter via SSE, and tests cover concurrency and the no-oversell rule.

## Phase 3: forecast, engine, rules

Implement in `forecast.py` and `engine.py`, pure functions where possible so they are easy to test:

```
available      = on_hand - reserved
demand_per_hr  = base_velocity * seasonal_uplift * campaign_uplift * size_mix_factor
hours_of_cover = available / demand_per_hr
stockout_eta   = now + hours_of_cover
```

- `base_velocity`: exponentially weighted moving average of recent hourly sales per variant. Seasonal and campaign factors are learned from history (ratios against baseline) and stored in config tables. Return a confidence band (low/expected/high demand) and show it in the report facts.
- Size-aware campaign logic: the campaign's remaining healthy click share is the sum of `click_share` over sizes that still have cover. Compute the share of clicks landing on sizes that are low or out.
- Severity rules:
  - **Critical:** hours_of_cover < `AD_PAUSE_LAG_HOURS` (default 4, configurable) and an active campaign targets the variant's product.
  - **Warning:** stockout_eta < restock_eta (or lead_time_days + buffer).
  - **Opportunity:** a calendar event is upcoming and projected festive demand over the window exceeds available plus in-transit stock. Include a recommended reorder quantity.
- Wasted spend estimate: `clicks_per_hr * avg_cpc * hours_ad_live_after_stockout`, using the size click share.
- Budget shift: rank other products by `expected_roas * margin` where cover is above a threshold, and propose a split of the freed budget.
- Cooldown: per variant and severity, default 6 hours. Re-alert only if severity escalates or hours_of_cover drops by more than 30%.
- Triggers: event-driven (each order), scheduled (APScheduler hourly refresh for variants with live campaigns, an 08:00 IST daily briefing, a nightly recalibration), and anomaly-driven (velocity above 2x forecast, campaign switched on for a low-stock variant, delayed restock).
- Output: a `Facts` Pydantic model containing every number the report may mention.

**Acceptance:** unit tests for each rule, the cooldown, the size-share logic, and the budget shift ranking. The demo-state data produces a Critical alert for Festive White UK 9 after a few orders.

## Phase 4: Gemini report with guardrails

- `reporter.py` takes `Facts` and calls Gemini with structured output (a Pydantic `Report` schema): `headline`, `summary`, `why` (2 to 3 reasons), `recommendations` (ordered actions, each tied to an action type), `estimated_impact`. The prompt must state that Gemini may only use numbers present in the facts JSON, must not invent figures, and must write short, plain, assistant-like prose for a busy shop owner.
- `guardrails.py`: extract every number, currency amount, percentage, and duration from the generated text and verify each appears in the facts (allow rounding tolerance and unit formatting such as 3 h and 3 hours). Also verify named products and sizes exist in the facts. On failure, retry once with the violations listed; if it still fails, use a deterministic template report built straight from the facts.
- The deterministic template must also be used when no API key is set or Gemini errors, so the demo never breaks. Log which path produced each report (`gemini`, `gemini_fallback_model`, `template`).
- Never let Gemini perform math or decide severity.

**Acceptance:** tests with a mocked Gemini client for pass, fail-then-retry, and fallback cases; a test showing that a hallucinated number is rejected.

## Phase 5: dashboards and owner inbox

A single-page web app with live updates:

- **Sales:** live sales counter, revenue today, orders per hour chart, top products.
- **Inventory:** variant-level table with on_hand, hours of cover, and a size heatmap per product showing stock against demand, with red cells where cover is short.
- **Campaigns:** active campaigns, spend today, clicks, a "wasted on out-of-stock clicks" counter, and size click mix.
- **Alerts / inbox:** alert cards with severity, the report, confidence band, Approve and Dismiss buttons, and status.
- **Savings:** ledger totals (ad spend avoided, sales protected, stockouts prevented).

Style: clean, modern, readable on a projector. Rupee formatting with Indian digit grouping.

## Phase 6: delivery and actions

- `delivery.py`: always write to the in-app inbox. If `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set, also send the report with inline Approve and Dismiss buttons. Handle the callback (webhook or long polling, pick the simpler one for local demo and document it).
- `actions.py`: Approve runs the recommended actions through a mock Ads API module whose request and response shapes mimic Google Ads campaign pause and budget update calls. Clearly label it as a mock in code and README. Record `actions` rows and write the `savings_ledger` entry (ad spend avoided = hourly wasted spend x hours until restock or campaign end, bounded and explained in the facts). Dismiss records the decision so the cooldown applies.

## Phase 7: Ask mode

A chat box where the owner asks about the database. Implement a router:

- **Numbers (sales, stock, spend, savings):** map the question to a whitelisted, parameterized SQL template or pandas function. Do not let an LLM write free-form SQL. Use Gemini only to pick the template and extract parameters (structured output), then execute in code.
- **Why / how questions:** retrieve from alerts and reports using FTS5, and answer from that evidence only.
- Gemini composes the final answer from evidence only; run the guardrail number check against the query result. Show the evidence (the SQL template name, rows, or alert ids) under each answer.
- Log every question in `ask_log`.

Example questions to support:

- "How many Festive White UK 9 did we sell today?"
- "Which products will run out this week?"
- "How much ad spend did we save this week?"
- "Why did you recommend pausing the white sneaker campaign?"

## Phase 8: demo controls and counterfactual replay

- `POST /demo/reset`: restore the seeded demo state.
- `POST /demo/simulate-rush`: fire a burst of orders (configurable size and duration) weighted to UK 8 and 9, to emulate an influencer post or Diwali rush.
- `scripts/run_replay.py` and `/savings/replay`: replay the synthetic history with and without SignalShift and report ad spend saved, stockouts prevented, and sales protected. Validate against the hidden `ground_truth` table and report detection precision and recall.

## Demo script the finished app must support (3 minutes)

1. Open the dashboard. Festive White Sneaker shows healthy total stock but UK 9 is nearly gone, and its campaign is live.
2. On `/store`, buy a pair of UK 9. Sales and inventory tiles update instantly.
3. Press "simulate rush". Cover for UK 9 and UK 8 drops below threshold.
4. A Critical alert appears and the report arrives in the inbox (and on Telegram if configured). Example tone: "UK 9 will sell out in about 3 hours. 68% of this campaign's clicks are for UK 8 and 9. The ad is still spending Rs 1,800/day on clicks that can't convert. Pause the size-linked ads, move Rs 2,000 to Everyday Runner (22 days cover), reorder 60 pairs."
5. Approve. The mock campaign pauses and the savings ledger updates.
6. Ask mode: "How much ad spend did we save today?"

## Engineering requirements

- Type hints everywhere, small pure functions in the engine, no business logic in routers.
- Idempotent event handling and database transactions around stock changes.
- Config through env vars with sane defaults. Provide `.env.example` listing `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `AD_PAUSE_LAG_HOURS`, `ALERT_COOLDOWN_HOURS`.
- The app must run fully offline from Gemini and Telegram (template reports, in-app inbox only).
- Structured logging for every alert decision, including the facts used.
- Tests for each phase. Run them before declaring a phase done.
- README with setup, one-command run, the demo script, an architecture diagram in Mermaid, and an honest "limitations" section (mock Ads API, synthetic data, simple forecast).

## Honesty constraints

- Do not claim real Google Ads or Merchant Center integration. Label the executor as a mock with the same contract shape.
- Do not present synthetic numbers as real benchmarks. Label sample data in the UI.
- Keep the forecast simple and explainable (weighted velocity times learned factors, with a confidence band). Do not add heavy ML models unless I ask.

## How to work

1. Start by restating the plan in a short checklist and list any assumptions you are making.
2. Build Phase 1, run tests, summarize, and stop.
3. Wait for my "go" before each next phase.
4. If a requirement is ambiguous and the choice is hard to undo, ask me. Otherwise pick the sensible default and note it.
