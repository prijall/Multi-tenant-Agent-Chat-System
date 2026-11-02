# Multi-Tenant Agent Platform

An authenticated AI platform for Himali Tea House and Lotus Wellness Studio.

## Current scope

The non-AI product core is implemented:

- Neon PostgreSQL configuration with separate pooled and direct URLs
- server-derived tenant and user context
- tenant-scoped organizations, users, products, and orders
- customer ownership checks in repository queries
- managed Neon Auth with application-owned tenant roles
- tea ordering: quote, explicit confirmation, edit, cancel, tracking and reorder
- appointment booking: availability, confirmation, cancellation and rescheduling
- read-only per-organization admin analytics
- one tenant-neutral login with admin-controlled user access and roles
- automated tenant, customer, stock and booking conflict coverage

The first agent foundation is also in place:

- tenant-scoped conversations, messages, tool-run audits and pending actions in Postgres
- a provider-neutral agent loop with a Gemini REST adapter
- server-selected Himali and Lotus tool registries; the model cannot supply an organization
- every tool in the brief for both agents, reading live data and scoped to the signed-in customer
- write tools that only propose a change: the customer's Confirm click carries it out, and the
  model has no tool that can confirm, so nothing it reads or is told can change an order or booking
- per-user chat rate limiting, bounded tool rounds and persisted knowledge citations
- one chat workspace per organization, with its own theme, conversation history and citations

Switch the LLM with environment variables alone: set `LLM_PROVIDER` to `gemini` or `mistral`, and
`LLM_API_KEY` to that provider's key. `LLM_MODEL` is optional and defaults to
`gemini-3.5-flash-lite` or `ministral-14b-latest`.

Optionally set `LLM_FALLBACK_PROVIDER` and `LLM_FALLBACK_API_KEY` too. If the main provider errors or
stays silent for `LLM_FALLBACK_AFTER_SECONDS` (10 by default), that turn moves to the fallback. The
switch only happens before any of the reply has been shown, so a customer never sees two half-answers.
Next-message suggestions also run on the fallback when one is set, which keeps the main provider's
quota for chat.

## Project structure

```text
backend/            FastAPI service (run its commands from this folder)
  app/
    main.py         HTTP routes
    agents/         Agent loop, tools, confirmations and the LLM adapter
    core/           Settings, authentication and rate limiting
    db/             Database engine, ORM models and tenant-scoped queries
    services/       Business rules: orders, bookings, knowledge search, analytics
    scripts/        Schema setup and seeding commands
  seed/<org>/       Each organisation's structured data and knowledge documents
  tests/            Automated tests
  evals/            Evaluation suite
web/                Next.js frontend
neon.ts             Neon project configuration (read from the repository root)
```

## Local setup

1. Copy `backend/.env.example` to `backend/.env` and `web/.env.example` to `web/.env.local`, then
   fill in the Neon and Gemini values.
2. Run `make install`.
3. Run `make migrate`.
4. Run `make seed`.
5. Run `make api` and `make web` in separate terminals.

On Windows without `make`, run the same steps from each folder:

```powershell
cd backend
uv sync
uv run python -m app.scripts.bootstrap_database
uv run python -m app.scripts.migrate_auth_profile
uv run python -m app.scripts.seed
uv run uvicorn app.main:app --reload --port 8000
```

```powershell
cd web
npm install
npm run dev
```

Run tests with `make test`.

Run the deterministic product evaluation checklist with `make eval`.

## Seed data and demo logins

`make seed` rebuilds both organisations from `backend/seed/<slug>/`: `org.json` holds the structured
data (products, services, practitioners, hours, holidays, demo customers with past orders and
bookings) and `kb/*.md` holds the documents. A new folder there is a new organisation. Re-running
is safe: catalogue data resets to the seed values and each knowledge base is re-embedded.

| Org | Customer | Admin |
| --- | --- | --- |
| Himali Tea House | `priya.himali@example.com` | `admin.himali@example.com` |
| Lotus Wellness Studio | `anita.lotus@example.com` | `admin.lotus@example.com` |

`make seed` also creates these logins in Neon Auth, using the password in `DEMO_PASSWORD`
(`tea-and-lotus-demo` in `.env.example`). Sign in at the plain page at `/`: the account's stored
organization decides which themed chat it lands in, and that page can't choose one. A login that
already exists keeps its password. These are shared test credentials for local review; set another
password on any public deployment.

The entry point fixes the organization; it is never accepted from a form field or trusted from a
later request. New customers can sign up there directly. Administrators can add, remove, or
change users from the workspace's **Users** section. An added user's profile stays pending until
someone signs up with that email and a verified address, so nobody can claim a role or history
just by typing an email they don't own.

## Knowledge base

- **Prices, stock, hours and availability are not in the documents.** The agents read them
  from the database, so answers always match live data.
- **Chunking:** one chunk per `##` section, so every FAQ answer, policy clause, product and
  bio is retrieved whole and cited as `document › section`. Sections over 1,200 characters
  are split on paragraph boundaries. The text that is embedded is prefixed with
  `Document > Section`, which gives short sections their context.
- **Embeddings:** `BAAI/bge-small-en-v1.5` (384 dimensions) runs locally through fastembed.
  It is free, needs no API key and uses none of the LLM's free-tier rate limit. Queries
  carry BGE's retrieval instruction; passages don't.
- **Search:** pgvector cosine distance, with `org_id` filtered inside the SQL query rather
  than after it. There is no approximate index: at about 100 chunks an exact scan is fast,
  and it can't miss in-org results the way post-filtered HNSW can.
- **Relevance cut-off:** on the seed data, questions it can answer score 0.72 or more for their
  best section, and questions it can't score 0.57 or less. Search drops anything under 0.65, and
  when nothing is left the agent says it doesn't know instead of guessing.

## Bookings and concurrency

Postgres exclusion constraints (`btree_gist`) reject any two confirmed bookings that overlap
for the same practitioner or the same customer. When two customers grab one slot, the
database lets exactly one insert succeed.

## Business workflows

The browser first receives a live quote for an order or booking, then submits an explicit
confirmation. The server rechecks all live values in the mutation transaction. Order stock
is locked and decremented only when a pending order is created; cancellation restores it.
Only pending orders can be edited or cancelled. Booking slots are validated against local
opening hours and holidays in `Asia/Kathmandu`, while timestamps are stored in UTC.

The `/api/knowledge/search` endpoint is limited to 20 requests per minute per authenticated
user. In a multi-worker deployment, move the same sliding-window policy to Redis so all
workers share the counter.

## Remaining AI work

Still to build: agent evals that run real conversations and check the database afterwards.

## Neon connection strategy

`DATABASE_URL` is the pooled Neon URL used by the application. `DATABASE_URL_UNPOOLED` is
the direct Neon URL used by schema migrations and administrative commands. The backend
uses `pool_pre_ping` and does not depend on connection-local tenant state when using the
transaction pooler. Every data query is explicitly scoped by trusted server-side tenant
context. PostgreSQL Row Level Security will be added as a second defensive layer after the
core schema is migrated.

