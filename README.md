# Multi-Tenant Agent Platform

One chat platform hosting AI agents for two fictional businesses. **Himali Tea House** is an online
store whose agent answers product questions and handles orders. **Lotus Wellness Studio** is a
massage, physiotherapy and yoga studio whose agent answers service questions and handles bookings.
A customer of one can't reach the other's agent or data by any route.

## Setup

You need [uv](https://docs.astral.sh/uv/), Node.js, `make`, a Neon project with Neon Auth enabled,
and a free Gemini API key. Mistral is optional.

```bash
cp backend/.env.example backend/.env       # add your Neon URLs and LLM_API_KEY
cp web/.env.example web/.env.local         # add your Neon Auth URL and a cookie secret
make setup                                 # installs everything and creates the schema
make seed                                  # loads both organisations and their logins
make dev                                   # API on :8000, web on :3000
```

On Windows without `make`, install from the repository root, then run each app from a terminal of
its own:

```powershell
uv sync
npm install
```

```powershell
cd backend
uv run python -m app.scripts.bootstrap_database
uv run python -m app.scripts.migrate_auth_profile
uv run python -m app.scripts.seed
uv run uvicorn app.main:app --reload --port 8000
```

```powershell
npm run dev
```

Or run everything in Docker, with Redis included. Neon stays external, and `make seed` is still
needed once:

```bash
docker compose up --build
docker compose exec api python -m app.scripts.bootstrap_database
docker compose exec api python -m app.scripts.migrate_auth_profile
docker compose exec api python -m app.scripts.seed
```

## Test logins

Sign in at http://localhost:3000. The account's stored organisation decides which themed workspace
it lands in. `make seed` creates these with the password in `DEMO_PASSWORD`, which is
`tea-and-lotus-demo` in `.env.example`. Change it on any public deployment.

| Organisation | Customer | Admin |
| --- | --- | --- |
| Himali Tea House | `priya.himali@example.com` | `admin.himali@example.com` |
| Lotus Wellness Studio | `anita.lotus@example.com` | `admin.lotus@example.com` |

New customers sign up at `/a/himali-tea` or `/a/lotus-wellness`.

## Architecture

```text
Browser (Next.js) ──bearer token──▶ FastAPI ──▶ Agent loop ──▶ Gemini, or Mistral as fallback
       │                               │             │
   Neon Auth                           │             ├──▶ Tools ──▶ Services ──▶ Postgres + pgvector
 (sign-in, sessions)                   │             │
                                       └── Confirm ──┴──▶ Pending actions
```

```text
pyproject.toml      Python dependencies and tool settings, locked by uv.lock
package.json        npm workspace for web/, locked by package-lock.json
backend/
  app/
    main.py         App setup
    api/            HTTP routes: accounts, conversations, orders, bookings, admin
    agents/         Agent loop, tools, confirmations, cards, suggestions, LLM adapters
    core/           Settings, authentication and rate limiting
    db/             Database engine, ORM models and tenant-scoped queries
    services/       Business rules: orders, bookings, knowledge search, analytics
    scripts/        Schema setup and seeding commands
  seed/<org>/       Each organisation's structured data and knowledge documents
  tests/            Automated tests
  evals/            Evaluation suite
web/                Next.js frontend
docker-compose.yml  API, web app and Redis together
```

### The agent loop

The loop is under 200 lines of my own code (`stream_agent` in `backend/app/agents/runtime.py`), with
no agent framework:

1. Save the customer's message, then close the database transaction, so a slow model reply doesn't
   hold a pooled connection.
2. Send the system prompt, conversation history and that organisation's tools to the model.
3. If the model replies with text, stream it to the browser, save it and finish.
4. If it asks for tools, run each one, record it in an audit table, add the results to the history
   and go back to step 2.
5. The last round offers no tools. A model that asks for one anyway gets a safe fixed reply, so a
   customer always hears back.

Responses stream over Server-Sent Events. Stop closes the connection, and the server saves the text
sent so far, so a reload shows the same reply.

### How tenant isolation is enforced

- **Identity comes from a verified token.** The backend checks the EdDSA signature against Neon
  Auth's JWKS, plus the issuer and expiry, on every request.
- **Organisation and role come from my database.** `RequestContext` is built from the `users` row for
  that identity. Nothing in the URL, headers or body can set them.
- **The model can't choose a tenant.** Tools are created per request, already bound to that context,
  and their schemas have no organisation or user field. Each organisation only gets its own tools.
- **Every query is scoped.** Queries filter by `org_id`, and customer data also by user. Another
  organisation's ID gets the same "not found" as an ID that doesn't exist.
- **Knowledge search filters inside the SQL query,** not on the results afterwards.
- **Sign-up is fixed by the entry point.** An identity belongs to one organisation, and claiming a
  pre-created profile needs a verified email, so nobody gets a role by typing an address.

`backend/tests/` proves this with cross-organisation and cross-customer tests. The evals repeat it
against the real model, including prompt injection.

### Why the model can't change data

Write tools only propose a change. `create_order` validates the request, stores a pending action and
shows a Confirm card. **The model has no tool that can confirm one.** Only the customer's own request
to the Confirm endpoint carries it out, and every rule is checked again at that moment. An action
works once, expires after 15 minutes, and is replaced by any newer proposal in that chat. So an
instruction hidden in a document or a message can't place an order, even if it fools the model.

## Key decisions

### Auth and sessions

I used **Neon Auth**, a managed Better Auth service, rather than storing password hashes myself. It
lives on the same database branch as the data, and credentials stay out of my code. The browser
holds a session cookie managed by the auth library, and exchanges it for a short-lived signed token
(15 minutes) for API calls. The backend holds no session state, so a stolen token is only useful
until it expires, and signing out ends the browser session.

### Chunking and embeddings

- **Chunking:** one chunk per `##` section, so every FAQ answer, policy clause, product and bio is
  retrieved whole and cited as `document › section`. Sections over 1,200 characters split on
  paragraph boundaries. The embedded text is prefixed with `Document > Section`, which gives short
  sections their context.
- **Embeddings:** `BAAI/bge-small-en-v1.5` (384 dimensions) runs locally through fastembed. It's free,
  needs no API key and uses none of the LLM's rate limit.
- **Search:** pgvector cosine distance with no approximate index. At about 100 chunks an exact scan
  is fast, and it can't miss in-organisation results the way a post-filtered index can.
- **Relevance cut-off:** on the seed data, questions it can answer score 0.72 or more for their best
  section, and off-topic ones 0.57 or less. Search drops anything under 0.65, and the agent then says
  it doesn't know. This is tuned on a small sample, so some near misses still get through, such as
  "Do you offer acupuncture?" at 0.67. The model has to decline those itself.
- **Live data stays out of the documents.** Prices, stock, hours and availability always come from the
  database, so answers can't go stale.

### Model

`gemini-3.5-flash-lite` was the model that worked best among those I tried on the free tier: reliable
tool calls and fast replies, while the larger Flash models returned overload errors at the time.
`LLM_PROVIDER`, `LLM_MODEL` and `LLM_API_KEY` choose it.

Mistral (`ministral-14b-latest`) is an optional fallback, set with `LLM_FALLBACK_PROVIDER` and
`LLM_FALLBACK_API_KEY`. If the main model errors or stays silent for `LLM_FALLBACK_AFTER_SECONDS`
(10 by default), that turn moves over. This only happens before any of the reply has been shown, so
nobody sees two half-answers. Next-message suggestions also use the fallback, which saves the main
model's quota for chat.

Clicking a time slot skips the model entirely, because the choice is already structured.

### Semantic answer cache

A reworded FAQ question can be answered from Redis Stack with no LLM call, which matters on a free
tier. It's optional: without `REDIS_URL`, or if Redis is down, every question goes to the model. Set
`ANSWER_CACHE_MIN_SIMILARITY` (0.90) and `ANSWER_CACHE_TTL_SECONDS` (one day) to tune it.

- **What gets stored:** only answers built purely from the knowledge base, with citations, for the
  first message of a chat. Anything using live prices, stock, orders or bookings always goes to the
  agent, as do follow-ups, personal messages and multi-part messages.
- **Isolation:** the vector search filters by organisation, and `make seed` clears stale answers.
- **Why similarity isn't enough:** on my embedding model, "cancel more than 24 hours ahead" and "cancel
  within 24 hours" score 0.956, higher than many true rewordings, yet need opposite answers. So a hit
  also needs the same top knowledge section, and no difference on words like inside/outside or
  within/ahead.

Measured on real Redis, embeddings and seed documents: 0 of 7 look-alike questions got a wrong
answer, and 3 of 6 true rewordings were cache hits. It's tuned to miss rather than risk a wrong
policy answer, and a miss only costs one LLM call. Evals run without the cache, so they measure the
model.

**Known risk:** a cached answer is shared across an organisation's customers, so a question that
talks the model into a bad answer could reach others for a day. Length, single-sentence and
knowledge-only limits narrow this, but don't eliminate it.

### Scheduling: build, not integrate

I wrote the slot engine (`backend/app/services/bookings.py`). The rules are small and specific:
opening hours, holidays, service duration, practitioner qualifications and a 24-hour fee. Keeping them
beside the bookings table gives one transaction and one source of truth, with no third-party outage
to handle. Times are stored in UTC and always shown in `Asia/Kathmandu`.

### Concurrency

- **Bookings:** Postgres exclusion constraints (`btree_gist`) reject any two confirmed bookings that
  overlap for the same practitioner or the same customer. The checks before the insert exist to give
  a helpful message, and the constraint is the guarantee. There's no lock to forget and no window for
  a race.
- **Stock:** product rows are locked with `SELECT … FOR UPDATE` in the order transaction, and a check
  constraint keeps stock from going negative.
- **Confirm buttons:** the action row is locked too, so a double click can't carry it out twice.

`backend/tests/test_concurrency_postgres.py` races real connections on real Postgres: three customers
for one slot leave exactly one booking, two reschedules into one slot leave exactly one there, and
buyers for the last units never oversell.

### Delivery charges

The shipping policy charges Rs 150 inside the valley and Rs 300 outside, free from Rs 3,000. Order
totals cover the goods only, and every order Confirm card says so and states the charges. A real
store would add the charge to the total, which needs a reliable way to place an address, so I chose
to be explicit rather than guess from free text.

## Business rules

| Rule | Where it's enforced |
| --- | --- |
| Show a summary and wait for confirmation | Confirm cards and pending actions |
| Quantity can't exceed stock, and offer an alternative | Checked under a row lock. `search_products` supplies real in-stock alternatives |
| Only pending orders change, and refusals give the status | `update_order` and `cancel_order` return the reason |
| "Same as last time" notes price changes | `reorder` compares old and current prices on the card |
| Slot fits hours, holidays and full duration | `validate_slot` |
| No double-booking | Exclusion constraints |
| A conflicting reschedule offers the nearest free times | `nearest_slots`, shown as buttons |
| Cancellation policy | Free over 24 hours ahead, 50% within 24 hours, shown before confirming |
| Relative dates use studio time | The prompt carries the current Kathmandu date and upcoming days |
| Admin moves orders along | Buttons on the Himali admin dashboard |

The chat rate limit is 20 messages a minute per user, held in memory. Several workers would need to
share it through Redis.

## Evaluation

`make eval` runs 35 scenarios (17 store, 18 studio) through the real agent loop and the real model,
then judges the database afterwards: orders, stock, bookings, fees and Confirm cards first, then
which tools ran, then loose checks on the reply text. Pressing Confirm is a scenario step, so each
one can prove that nothing changed before the click.

- **Isolation:** each run creates a private Postgres schema and drops it at the end, and each scenario
  starts from freshly seeded data. Real tables aren't touched.
- **Clear attribution:** the fallback provider stays off, so a result always belongs to one model.
- **Pass, fail or error:** `FAIL` means the agent did the wrong thing. `ERROR` means the run couldn't
  finish, such as an outage or quota, and says nothing about the agent.
- **Free-tier limits:** requests are paced to 15 a minute, so a full run takes about 10 minutes and
  roughly 120 requests. It stops after 3 errors in a row, and `python -m evals.runner --unfinished`
  resumes where it stopped.
- **Results:** `backend/evals/results/` holds every scenario's transcript and tool calls.

Coverage: knowledge answers with citations, questions the knowledge base can't answer, live prices,
the happy path, every business rule, memory, edge cases, cross-organisation and cross-customer
access, and prompt injection, both typed and hidden inside product data.

### Results

The last complete run was on `gemini-3.5-flash-lite` on 2 November 2026, recorded in
`backend/evals/results/full_run_2026-11-02.json`:

| Agent | Passed | Failed | Could not finish |
| --- | --- | --- | --- |
| Himali Tea House | 16 / 17 | 1 | 0 |
| Lotus Wellness Studio | 18 / 18 | 0 | 0 |

That run predates the fix for its one failure. A later run was cut short by the free tier's limit of
500 requests a day per model, so `latest.json` is partial: all 13 scenarios that finished passed, and
the other 22 didn't finish.

### Failures found, and what was done

| Scenario | What went wrong | Fix | Status |
| --- | --- | --- | --- |
| `holiday_is_refused` | Empty slot results gave no reason, so the model searched until the round limit. It then called a tool even with none offered, and the turn crashed. | Empty results now say the studio is closed and why. The limit is enforced in code, so the customer always gets a reply. | Passed on its next 2 runs |
| `customer_cannot_hold_two_bookings_at_once` | Asked for 10am, the agent put up a Confirm card for 11am. Nothing was booked, but nobody asked for 11am. | The prompt forbids proposing a time the customer didn't ask for. | Passed on its next 3 runs |
| `short_stock_refused_with_an_alternative` | Sometimes offered the remaining stock with no alternative. My first fix, a stronger prompt alone, made it worse (1 of 5 passed) and the model invented a product. | `search_products` now returns real in-stock products from the same category, and the model may only name products a tool returned. | Passed 5 of 6 runs |

**Known weakness:** in that one remaining short-stock failure, the agent put up a Confirm card for the
2 pots left without being asked. It can't place an order, because the customer must still press
Confirm. I'd fix this in code by making `create_order` refuse quantities the customer didn't state.

The lesson from that scenario shaped the design: when the model gets something wrong, give it real
data or enforce the rule in code, because a stronger instruction isn't reliable.

## What I'd do with another week

- **Admin analytics agent:** admins have a read-only dashboard today, but not the chat agent from the
  stretch goal. It needs conversion rate and unique chatters, which the stored conversations can
  supply.
- **Delivery in the order total:** with structured delivery zones instead of free-text addresses.
- **Stricter write tools:** refuse quantities and times the customer never stated, in code.
- **Scale the evals:** repeat risky scenarios several times to measure how often they pass, and run
  them on a paid tier in CI with an LLM judge for reply quality.
- **Row Level Security:** a second layer under the existing query scoping.
- **Third organisation by config alone:** seed data and themes are already config, but agent prompts
  and tools still switch on the organisation's slug. I'd make them depend on its type instead.
- **Operations:** tracing and per-organisation token cost, a Redis rate limiter, and an overall time
  limit on streaming replies.
- **Long-term customer memory:** "my usual tea" or a preferred practitioner.

## How I used AI coding tools

I used Claude Code throughout: reviewing the code against this brief, implementing features, writing
tests and evals, and debugging from logs. I chose the direction and tested the app by hand. AI
output needed checking: reading the eval transcripts and retesting showed that some of its fixes
didn't work, including a prompt change that made one scenario worse. Those results are why several
rules ended up in code rather than in prompts.

## Commands

| Command | What it does |
| --- | --- |
| `make dev` | Runs the API and web app together |
| `make test` | Runs the tests. Race tests need a Postgres `DATABASE_URL_UNPOOLED` and take about 2 minutes |
| `make lint` | Runs ruff and ESLint |
| `make eval` | Runs the evaluation suite |
| `make seed` | Rebuilds both organisations and their knowledge bases |

`DATABASE_URL` is the pooled Neon URL for application traffic. `DATABASE_URL_UNPOOLED` is the direct
URL for schema changes, evals and race tests.
