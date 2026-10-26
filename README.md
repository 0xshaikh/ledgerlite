<div align="center">

<img src="static/icon.svg" alt="ledgerlite logo" width="84" height="84" />

# ledgerlite

### Open-source accounting for people who actually want to run their own books.

**Double-entry bookkeeping, invoicing, expenses and live P&L — in one FastAPI app with a single SQLite file. No API keys, no cloud, no monthly fee.**

[![CI](https://img.shields.io/github/actions/workflow/status/0xshaikh/ledgerlite/ci.yml?branch=main&style=flat-square&label=CI)](https://github.com/0xshaikh/ledgerlite/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![License MIT](https://img.shields.io/badge/license-MIT-f4c430?style=flat-square)](LICENSE)
[![API keys: 0](https://img.shields.io/badge/API%20keys-0-yellow?style=flat-square)]()
[![Deps: 4](https://img.shields.io/badge/deps-4-2EA043?style=flat-square)]()
[![Tests: 35](https://img.shields.io/badge/tests-35-success?style=flat-square)]()
[![Runs offline](https://img.shields.io/badge/network-at%20runtime-none-blueviolet?style=flat-square)]()

[Quick start](#quick-start) · [Docker](#docker) · [Features](#features) · [JSON API](#json-api) · [Testing](#testing) · [What this is NOT](#what-this-is-not)

<br />

<code>python3 server.py --seed</code> &nbsp;→&nbsp; <code>http://127.0.0.1:8000</code>

</div>

---

## What is ledgerlite

[QuickBooks Online](https://quickbooks.intuit.com/) starts at **$35 a month per user** and climbs from there. [Odoo](https://www.odoo.com/) and [ERPNext](https://www.odoo.com/) do the same job but ship as a full ERP you have to learn, host and configure — far more machinery than a freelancer or a five-person shop needs.

ledgerlite is the narrow, honest version: real double-entry accounting, real invoices with line items and tax, real expenses, and four reports computed live out of your own journal — served from a single Python file on your own machine. The whole database is one SQLite file you can copy, back up and inspect with any SQL tool. There is nothing to configure, nothing to pay, and nothing phoning home.

## How it compares

| | **ledgerlite** | **QuickBooks Online** | **Odoo** | **ERPNext** |
|---|---|---|---|---|
| Licence | MIT, free forever | Proprietary | Proprietary (open-core) | Open-source (GPL) |
| Cost | **$0** | $35–$90 / user / month | $24–$78 / user / month | Free (hosted from ~$35/mo) |
| Setup | One `pip install`, one command | Web signup, bank linking | Module-by-module install | Full ERP install + config |
| Data storage | Your SQLite file, on your disk | Vendor cloud | Vendor cloud / your host | Your host |
| External APIs needed | **None** | Several | Many | Several |
| Accounts model | Double-entry, seeded chart of accounts | Double-entry | Double-entry | Double-entry |
| Complexity | A web app for one business | Moderate | ERP-scale | ERP-scale |
| Self-hosting | Yes, trivially | No | Possible, hard | Yes, effortful |
| Offline operation | Yes, fully | No | No | Partial |

ledgerlite is deliberately small. If you need multi-entity consolidation, inventory, manufacturing or a 40-user approval workflow, use Odoo or ERPNext. If you need one freelancer's books to balance, this is it.

## Features

### Invoicing
- Multi-line invoices: description, quantity, rate, per-line amounts
- Automatic subtotal, configurable tax rate, tax total and grand total
- Draft → **Mark sent** → **Mark paid** lifecycle with colour-coded status pills
- Invoice number auto-increments and never repeats
- Detail page showing every line, journal entries and payment status
- Printable view at `/invoices/{id}/print` — plain HTML, no PDF dependency

### Expenses
- Date, vendor, category, amount, description, receipt note
- Month filter and running totals
- Editing or deleting an expense reverses its journal entry
- Category dropdowns built from your own chart of accounts

### Accounting
- Double-entry bookkeeping against a seeded chart of accounts: Cash, Bank, Credit Cards, Accounts Receivable, Sales Revenue, Tax Payable, Office Expenses, Software, Travel, Marketing, Other Expenses
- Add your own accounts at any time
- Accrual model: sent invoices recognise revenue and receivables; payment moves the cash; expenses debit the category and credit cash
- Every report is computed live from the `transactions` table — nothing is pre-aggregated or cached

### Reports
- **Profit & Loss** for any date range, income and expense grouped by account, net profit
- **Balance sheet** as of any date: assets, liabilities, equity — always balances
- **Cash flow** for any range: gross inflows and outflows, by cash account and by what the money was for
- **Taxable income** summary including tax collected as a liability

### Dashboard
- Month-to-date revenue, expenses and net profit
- Outstanding invoice count and total receivable
- Overdue invoice list
- Six-month revenue vs expense chart, drawn as **inline SVG** — no Chart.js, no CDN

### Import & export
- CSV import of expenses (`date,vendor,category,amount,description`) with vendor auto-creation
- CSV export of every journal transaction, of all invoices and of all expenses

### API
- JSON REST endpoints for invoices, expenses, customers, all four reports and the dashboard
- Documented with copy-pasteable `curl` examples below

## Quick start

Requires Python 3.10 or newer.

```bash
git clone https://github.com/0xshaikh/ledgerlite.git
cd ledgerlite

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python3 server.py --seed           # demo data, then serve
# → http://127.0.0.1:8000
```

The database is created at `./data/ledger.db` on first run. Delete that one
file to start over.

### Command line

```
python3 server.py [--seed] [--reset] [--db PATH] [--host HOST] [--port PORT]
```

| Flag | Effect |
|---|---|
| `--seed` | Load realistic demo data: 8 customers, 5 vendors, 28 invoices over 6 months, 40 expenses, with several payments recorded |
| `--reset` | Delete the database file before starting |
| `--db PATH` | Use a different SQLite file (or a directory) than `./data/ledger.db` |
| `--host` | Bind address, default `127.0.0.1` |
| `--port` | Port, default `8000` |

Equivalent environment variables: `LEDGERLITE_DB` sets the database path.
ledgerlite needs **no API keys and no network access** at runtime.

## Docker

```bash
docker compose up -d --build
# → http://localhost:8000
```

The image runs as a non-root user (UID 10001), exposes port 8000, has a
`/api/health` health check, and mounts `/app/data` as a named volume so your
ledger survives image rebuilds.

## JSON API

Every endpoint returns JSON. Write paths accept `application/json`.

```bash
# Health
curl -s http://127.0.0.1:8000/api/health

# List customers, then create one
curl -s http://127.0.0.1:8000/api/customers
curl -s -X POST http://127.0.0.1:8000/api/customers \
  -H 'Content-Type: application/json' \
  -d '{"name":"Acme Ltd","email":"ap@acme.test"}'

# Create a multi-line invoice
curl -s -X POST http://127.0.0.1:8000/api/invoices \
  -H 'Content-Type: application/json' \
  -d '{"customer_id":1,"issue_date":"2026-10-01","due_date":"2026-10-31",
       "tax_rate":12.5,
       "lines":[{"description":"Consulting","qty":20,"rate":175},
                {"description":"Deployment","qty":1,"rate":1200}]}'

# Add an expense
curl -s -X POST http://127.0.0.1:8000/api/expenses \
  -H 'Content-Type: application/json' \
  -d '{"date":"2026-10-05","category":"Software","amount":99.0,
       "description":"Hosting","vendor":"AWS Cloud"}'

# Reports
curl -s 'http://127.0.0.1:8000/api/reports/pnl?start=2026-01-01&end=2026-10-31'
curl -s 'http://127.0.0.1:8000/api/reports/balance-sheet'
curl -s 'http://127.0.0.1:8000/api/reports/cashflow?start=2026-01-01&end=2026-10-31'
curl -s 'http://127.0.0.1:8000/api/dashboard'
```

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | Liveness + version |
| `GET /api/customers` · `POST /api/customers` | Customer list / create |
| `GET /api/invoices` · `POST /api/invoices` | Invoice list / create |
| `GET /api/expenses` · `POST /api/expenses` | Expense list / create |
| `GET /api/dashboard` | Month-to-date metrics, receivables, overdue, 6-month series |
| `GET /api/reports/pnl?start=&end=` | Profit & loss |
| `GET /api/reports/balance-sheet` | Balance sheet as of today (or `?as_of=`) |
| `GET /api/reports/cashflow?start=&end=` | Cash flow summary |

CSV exports: `GET /export/transactions.csv`, `GET /export/invoices.csv`,
`GET /expenses/export.csv`.

## Testing

```bash
pip install pytest httpx
pytest test -q
```

35 tests cover the seeded dataset, customer/vendor/expense CRUD, the full
invoice lifecycle, P&L arithmetic, balance sheet balancing, cash flow signs,
CSV import and the shape of every JSON endpoint.

## Screenshots

No screenshot section here on purpose. This environment has no browser
engine available, so any images would have to be fabricated — and a fake
screenshot is exactly the kind of thing this project is built to avoid. The
UI is plain HTML and CSS; run it and look.

## What this is NOT

Read this before reaching for it. ledgerlite is a small, honest tool, not an
enterprise accounting suite.

- **Not audit-compliant.** No statutory audit trail, no immutable revisions,
  no sign-off workflow. Use it to see your numbers clearly, not to satisfy a
  regulator.
- **No multi-currency.** One currency per ledger, chosen in Settings.
  Conversions are not computed.
- **No payroll**, no HR, no timesheets, no project billing.
- **No tax filing.** It tracks tax collected as a liability and tells you your
  taxable income. It does not prepare or submit a return.
- **No bank sync or payment processing.** Bank feeds, cards and payment links
  are out of scope by design — that is the part of the industry that charges
  a subscription.
- **No multi-user or permissions.** Single operator, single machine.
- **No inventory, purchasing or manufacturing.**
- **SQLite, single process.** Fine for one bookkeeper; not a shared multi-user
  database. Back up the `.db` file.

## Development

One file. `server.py` holds the schema, the double-entry helpers, the report
engine, the FastAPI routes and the demo-data generator. Templates live in
`templates/` with a single inline stylesheet in `base.html` — no build step,
no bundler, no external CSS or JavaScript.

```
ledgerlite/
├── server.py              # everything: schema, reports, routes, CLI
├── templates/             # 22 Jinja2 templates, one shared stylesheet
├── static/icon.svg        # app icon
├── favicon.svg
├── test/test_app.py       # 35 tests
├── Dockerfile / docker-compose.yml
├── requirements.txt       # fastapi, uvicorn, jinja2, python-multipart
└── data/ledger.db         # created on first run, gitignored
```

## Contributing

Issues and pull requests are welcome. Please keep it boring: standard library
where possible, no new runtime dependencies without a strong reason, and every
behaviour change comes with a test.

## Licence

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Shaikh.

