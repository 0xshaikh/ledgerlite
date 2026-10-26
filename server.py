#!/usr/bin/env python3
"""
ledgerlite — an open-source QuickBooks Online alternative.

A single-file FastAPI + Jinja2 + SQLite accounting and invoicing app for
freelancers and micro-businesses. No external APIs, no cloud, no monthly fee.

Run:
    python3 server.py              # start on http://127.0.0.1:8000
    python3 server.py --seed       # start with realistic demo data
    python3 server.py --reset      # wipe the database and start fresh

The database lives at ./data/ledger.db and is created on first run.
"""

import argparse
import csv
import io
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager

# --------------------------------------------------------------------------- #
# Paths and constants
# --------------------------------------------------------------------------- #

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
# Overridable so containers and CI can keep the database outside the image.
DB_PATH = Path(os.environ.get("LEDGERLITE_DB") or DATA_DIR / "ledger.db")
if DB_PATH.suffix == "":
    # Bare directory given: put the database inside it.
    DB_PATH = DB_PATH / "ledger.db"
DATA_DIR = DB_PATH.parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
APP_NAME = "ledgerlite"
VERSION = "1.0.0"

DATA_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

# Default chart of accounts: (name, type, is_system)
# types: asset, liability, equity, revenue, expense
DEFAULT_ACCOUNTS = [
    ("Cash", "asset", True),
    ("Bank", "asset", True),
    ("Credit Cards", "asset", True),
    ("Accounts Receivable", "asset", True),
    ("Accounts Payable", "liability", True),
    ("Tax Payable", "liability", True),
    ("Retained Earnings", "equity", True),
    ("Owner's Equity", "equity", True),
    ("Sales Revenue", "revenue", True),
    ("Other Income", "revenue", True),
    ("Office Expenses", "expense", True),
    ("Software", "expense", True),
    ("Travel", "expense", True),
    ("Marketing", "expense", True),
    ("Other Expenses", "expense", True),
]

CURRENCIES = {
    "USD": ("$", "USD"),
    "EUR": ("€", "EUR"),
    "GBP": ("£", "GBP"),
    "INR": ("₹", "INR"),
    "AUD": ("A$", "AUD"),
    "CAD": ("C$", "CAD"),
}

SEED_TAX_RATE = 8.5

# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #


def get_db():
    """Open a connection to the SQLite database with sane defaults."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL CHECK (type IN ('asset','liability','equity','revenue','expense')),
    is_system INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    address TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vendors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    address TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    invoice_id TEXT PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    issue_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft','sent','paid','overdue')),
    subtotal REAL NOT NULL DEFAULT 0,
    tax REAL NOT NULL DEFAULT 0,
    total REAL NOT NULL DEFAULT 0,
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id TEXT NOT NULL REFERENCES invoices(invoice_id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    qty REAL NOT NULL DEFAULT 1,
    rate REAL NOT NULL DEFAULT 0,
    amount REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    vendor_id INTEGER REFERENCES vendors(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    category TEXT NOT NULL DEFAULT '',
    amount REAL NOT NULL DEFAULT 0,
    description TEXT DEFAULT '',
    receipt_note TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    description TEXT DEFAULT '',
    amount REAL NOT NULL DEFAULT 0,
    type TEXT NOT NULL CHECK (type IN ('debit','credit')),
    ref_type TEXT DEFAULT '',
    ref_id TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def init_db():
    """Create all tables and seed the default chart of accounts."""
    conn = get_db()
    conn.executescript(SCHEMA)
    # Seed chart of accounts only if the table is empty.
    n = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    if n == 0:
        for name, typ, is_sys in DEFAULT_ACCOUNTS:
            conn.execute(
                "INSERT INTO accounts (name, type, is_system) VALUES (?, ?, ?)",
                (name, typ, is_sys),
            )
    # Default settings if absent.
    for k, v in (("currency", "USD"), ("tax_rate", str(SEED_TAX_RATE)),
                 ("business_name", "My Business"), ("next_invoice_no", "1")):
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
        )
    conn.commit()
    conn.close()


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    """Upsert a setting key/value pair."""
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #


def money(value, currency="USD"):
    """Format a number as currency with thousands separators and 2 decimals."""
    sym, _ = CURRENCIES.get(currency, ("$", currency))
    negative = float(value or 0) < 0
    num = abs(float(value or 0))
    return f"{'-' if negative else ''}{sym}{num:,.2f}"


def to_iso(d):
    """Coerce a date/datetime/str into an ISO YYYY-MM-DD string."""
    if isinstance(d, datetime):
        return d.date().isoformat()
    if isinstance(d, date):
        return d.isoformat()
    if isinstance(d, str):
        return d.strip()
    return d.isoformat()


def parse_date(s):
    """Parse an ISO date string into a date object (best-effort)."""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def next_invoice_id(conn, prefix="INV-"):
    """Return the next sequential invoice id and advance the counter."""
    n = int(get_setting(conn, "next_invoice_no", "1") or 1)
    inv_id = f"{prefix}{n:04d}"
    set_setting(conn, "next_invoice_no", str(n + 1))
    return inv_id


# --------------------------------------------------------------------------- #
# Double-entry journal helpers
#
# We keep a simple double-entry ledger in the `transactions` table. Every
# economic event is recorded as one or more balanced debit/credit pairs:
#
#   * Expense          -> debit  expense account, credit cash/bank
#   * Invoice paid     -> debit  cash, credit revenue account
#   * Invoice marked sent -> debit accounts receivable, credit revenue
#
# Debits and credits for a single event always balance, which is what makes
# the balance sheet and P&L come out sane without an ORM or accounting lib.
# --------------------------------------------------------------------------- #


def get_account(conn, name):
    """Fetch an account by name (case-insensitive)."""
    row = conn.execute("SELECT * FROM accounts WHERE LOWER(name)=LOWER(?)", (name,)).fetchone()
    return row


def account_id(conn, name):
    row = get_account(conn, name)
    if not row:
        conn.execute("INSERT INTO accounts (name, type, is_system) VALUES (?, ?, 0)",
                     (name, "expense"))
        conn.commit()
        row = get_account(conn, name)
    return row["id"]


def journal(conn, dt, entries, ref_type="", ref_id=""):
    """Record a balanced journal entry. entries = [(account_name, amount, type)]."""
    for acct, amount, typ in entries:
        aid = account_id(conn, acct)
        conn.execute(
            "INSERT INTO transactions (date, account_id, description, amount, type, ref_type, ref_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (dt, aid, ref_type, round(float(amount), 2), typ, ref_type, str(ref_id), now_iso()),
        )


def delete_refs(conn, ref_type, ref_id):
    """Remove all journal lines produced by a given reference (for reversals)."""
    conn.execute(
        "DELETE FROM transactions WHERE ref_type=? AND ref_id=?", (ref_type, str(ref_id))
    )


def journal_by_id(conn, dt, entries, ref_type="", ref_id=""):
    """Same as journal() but takes account IDs instead of account names.

    entries = [(account_id, amount, 'debit'|'credit'), ...]
    """
    for acct_id_, amount, typ in entries:
        conn.execute(
            "INSERT INTO transactions (date, account_id, description, amount, type, ref_type, ref_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (dt, int(acct_id_), ref_type, round(float(amount), 2), typ, ref_type, str(ref_id), now_iso()),
        )


# --------------------------------------------------------------------------- #
# Reporting engine — everything is computed live from the transactions table.
# No cached figures, no hardcoded totals.
# --------------------------------------------------------------------------- #


def account_balance(conn, account_id, start=None, end=None):
    """Natural-sign balance for one account: positive always means 'normal'.

    Assets and expenses are debit-normal, so debits count positive.
    Liabilities, equity and revenue are credit-normal, so credits count
    positive. This keeps every report and the balance check sane.
    """
    q = """
        SELECT a.type AS type,
               SUM(CASE
                     WHEN a.type IN ('asset','expense')
                          THEN CASE WHEN t.type='debit' THEN t.amount ELSE -t.amount END
                     ELSE CASE WHEN t.type='credit' THEN t.amount ELSE -t.amount END
                   END) AS bal
        FROM transactions t JOIN accounts a ON a.id=t.account_id
        WHERE t.account_id=?
    """
    params = [account_id]
    if start:
        q += " AND t.date >= ?"
        params.append(start)
    if end:
        q += " AND t.date <= ?"
        params.append(end)
    row = conn.execute(q, params).fetchone()
    if not row or row["bal"] is None:
        return 0.0, row["type"] if row else None
    return float(row["bal"]), row["type"]


def _grouped_by_account(conn, start, end, account_type):
    """Sum natural-sign balances by account for accounts of a given type."""
    rows = conn.execute(
        """
        SELECT a.id, a.name, a.type,
               SUM(CASE
                     WHEN a.type IN ('asset','expense')
                          THEN CASE WHEN t.type='debit' THEN t.amount ELSE -t.amount END
                     ELSE CASE WHEN t.type='credit' THEN t.amount ELSE -t.amount END
                   END) AS net
        FROM transactions t JOIN accounts a ON a.id = t.account_id
        WHERE a.type=? AND t.date >= ? AND t.date <= ?
        GROUP BY a.id, a.name, a.type
        HAVING net IS NOT NULL
        ORDER BY net DESC
        """,
        (account_type, start, end),
    ).fetchall()
    return [
        {"id": r["id"], "name": r["name"], "type": r["type"], "net": round(float(r["net"]), 2)}
        for r in rows
    ]


def profit_and_loss(conn, start, end):
    """P&L for a range: revenue and expense grouped by account, net profit."""
    income = _grouped_by_account(conn, start, end, "revenue")
    expenses = _grouped_by_account(conn, start, end, "expense")
    total_income = sum(i["net"] for i in income)
    total_expenses = sum(e["net"] for e in expenses)
    return {
        "start": start,
        "end": end,
        "income": income,
        "expenses": expenses,
        "total_income": round(total_income, 2),
        "total_expenses": round(total_expenses, 2),
        "net_profit": round(total_income - total_expenses, 2),
    }


def balance_sheet(conn, as_of):
    """Balance sheet as of a date: assets, liabilities, equity."""
    assets = _grouped_by_account(conn, "", as_of, "asset")
    liabilities = _grouped_by_account(conn, "", as_of, "liability")
    equity = _grouped_by_account(conn, "", as_of, "equity")
    # Net income to date rolls into equity.
    rev = sum(i["net"] for i in _grouped_by_account(conn, "", as_of, "revenue"))
    exp = sum(e["net"] for e in _grouped_by_account(conn, "", as_of, "expense"))
    net_income = rev - exp
    total_assets = sum(a["net"] for a in assets)
    total_liabilities = sum(l["net"] for l in liabilities)
    total_equity = sum(e["net"] for e in equity) + net_income
    return {
        "as_of": as_of,
        "assets": assets,
        "liabilities": liabilities,
        "equity": equity,
        "net_income": round(net_income, 2),
        "total_assets": round(total_assets, 2),
        "total_liabilities": round(total_liabilities, 2),
        "total_equity": round(total_equity, 2),
        "balanced": abs(total_assets - (total_liabilities + total_equity)) < 0.05,
    }


def _counterparty(conn, r):
    """Account name on the other side of a cash movement.

    Journal rows are paired by ref_type/ref_id. Recognition entries with
    several lines (an invoice has AR, Revenue and Tax) are disambiguated by
    matching the amount, so a cash receipt resolves to Accounts Receivable
    rather than Sales Revenue.
    """
    if not (r["ref_type"] and r["ref_id"]):
        return "Unmatched"
    o = conn.execute(
        "SELECT a.name FROM transactions t "
        "JOIN accounts a ON a.id=t.account_id "
        "WHERE t.ref_type=? AND t.ref_id=? AND t.id<>? "
        "ORDER BY ABS(t.amount-?) DESC LIMIT 1",
        (r["ref_type"], r["ref_id"], r["id"], float(r["amount"])),
    ).fetchone()
    return o["name"] if o else "Unmatched"


def cash_flow(conn, start, end):
    """Cash flow for a range: gross inflows and outflows of cash/bank accounts.

    In double-entry every payment touches a cash, bank or card account, so the
    debits to those accounts are money in and the credits are money out.
    Revenue and expense accounts describe *why* cash moved, not whether it did,
    so they are reported separately as counterparty breakdowns.
    """
    rows = conn.execute(
        """
        SELECT t.id, t.date, a.name, t.type, t.amount, t.description,
               t.ref_type, t.ref_id
        FROM transactions t JOIN accounts a ON a.id=t.account_id
        WHERE a.name IN ('Cash', 'Bank', 'Credit Card')
          AND t.date >= ? AND t.date <= ?
        ORDER BY t.date, t.id
        """,
        (start, end),
    ).fetchall()

    per_cash = {}
    per_reason = {}
    entries = []
    for r in rows:
        amt = round(float(r["amount"]), 2)
        is_in = r["type"] == "debit"  # debit to an asset account = money in
        slot = "in" if is_in else "out"

        c = per_cash.setdefault(r["name"], {"in": 0.0, "out": 0.0})
        c[slot] += amt
        reason = _counterparty(conn, r)
        g = per_reason.setdefault(reason, {"in": 0.0, "out": 0.0})
        g[slot] += amt
        entries.append({
            "date": r["date"],
            "account": r["name"],
            "direction": "inflow" if is_in else "outflow",
            "counterparty": reason,
            "amount": amt,
            "description": r["description"],
        })

    by_cash = [
        {"name": k, "inflows": round(v["in"], 2), "outflows": round(v["out"], 2),
         "net": round(v["in"] - v["out"], 2)}
        for k, v in per_cash.items()
    ]
    by_cash.sort(key=lambda x: -abs(x["net"]))
    by_reason = [
        {"account": k, "inflows": round(v["in"], 2), "outflows": round(v["out"], 2)}
        for k, v in per_reason.items()
    ]
    by_reason.sort(key=lambda x: -(x["inflows"] + x["outflows"]))

    total_in = round(sum(v["in"] for v in per_cash.values()), 2)
    total_out = round(sum(v["out"] for v in per_cash.values()), 2)
    return {
        "start": start,
        "end": end,
        "by_account": by_cash,
        "by_reason": by_reason,
        "entries": entries,
        "total_inflows": total_in,
        "total_outflows": total_out,
        "net_change": round(total_in - total_out, 2),
        # Backwards-compatible aliases used by the template and tests.
        "inflows": [{"name": r["name"], "net": r["inflows"]} for r in by_cash if r["inflows"]],
        "outflows": [{"name": r["name"], "net": -r["outflows"]} for r in by_cash if r["outflows"]],
    }


def taxable_income(conn, start, end):
    """Taxable income = net profit minus tax-exempt/nil accounts (none by default).
    We also surface the tax collected as a liability so the user can see it."""
    pnl = profit_and_loss(conn, start, end)
    tax_rows = conn.execute(
        """
        SELECT SUM(CASE WHEN t.type='credit' THEN t.amount ELSE -t.amount END) AS tax
        FROM transactions t JOIN accounts a ON a.id=t.account_id
        WHERE LOWER(a.name) LIKE '%tax%' AND t.date >= ? AND t.date <= ?
        """,
        (start, end),
    ).fetchone()
    tax_collected = float(tax_rows["tax"] or 0) if tax_rows else 0.0
    return {
        "start": start,
        "end": end,
        "total_income": pnl["total_income"],
        "total_expenses": pnl["total_expenses"],
        "net_profit": pnl["net_profit"],
        "tax_collected": round(tax_collected, 2),
        "taxable_income": round(pnl["net_profit"], 2),
    }


def dashboard_data(conn, today=None):
    """Month-to-date metrics + receivables + overdue list + 6-month series."""
    today = today or date.today()
    m_start = today.replace(day=1).isoformat()
    t_iso = today.isoformat()

    income_mtd = sum(i["net"] for i in _grouped_by_account(conn, m_start, t_iso, "revenue"))
    expenses_mtd = sum(e["net"] for e in _grouped_by_account(conn, m_start, t_iso, "expense"))

    # Outstanding receivables: sent/overdue invoices.
    outstanding = conn.execute(
        "SELECT SUM(total) AS s, COUNT(*) AS c FROM invoices WHERE status IN ('sent','overdue')"
    ).fetchone()
    receivable_total = float(outstanding["s"] or 0)
    receivable_count = int(outstanding["c"] or 0)

    overdue = conn.execute(
        "SELECT * FROM invoices WHERE status='overdue' ORDER BY due_date"
    ).fetchall()

    # 6-month revenue vs expense series (monthly, ending this month).
    months = []
    for back in range(5, -1, -1):
        y, m = today.year, today.month - back
        while m <= 0:
            m += 12
            y -= 1
        first = date(y, m, 1)
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        nxt = date(ny, nm, 1)
        months.append({
            "label": first.strftime("%b %y"),
            "revenue": 0.0,
            "expenses": 0.0,
        })
        rev = _grouped_by_account(conn, first.isoformat(), (nxt - timedelta(days=1)).isoformat(), "revenue")
        exp = _grouped_by_account(conn, first.isoformat(), (nxt - timedelta(days=1)).isoformat(), "expense")
        months[-1]["revenue"] = round(sum(i["net"] for i in rev), 2)
        months[-1]["expenses"] = round(sum(e["net"] for e in exp), 2)

    return {
        "today": t_iso,
        "month_start": m_start,
        "income_mtd": round(income_mtd, 2),
        "expenses_mtd": round(expenses_mtd, 2),
        "net_mtd": round(income_mtd - expenses_mtd, 2),
        "receivable_total": round(receivable_total, 2),
        "receivable_count": receivable_count,
        "overdue": [dict(r) for r in overdue],
        "months": months,
    }


def svg_chart(months, currency="USD"):
    """Render a self-contained inline SVG bar chart (no JS, no CDN)."""
    sym, _ = CURRENCIES.get(currency, ("$", currency))
    w, h = 760, 300
    pad_l, pad_b, pad_t = 64, 44, 24
    plot_w, plot_h = w - pad_l - 24, h - pad_b - pad_t
    vals = [m["revenue"] for m in months] + [m["expenses"] for m in months]
    top = max(vals) if vals else 1.0
    top = max(top, 1.0)
    # round up to a nice axis max
    step = 10 ** max(0, int(len(str(int(top))) - 1))
    axis_max = (top // step + 1) * step

    parts = [f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" role="img" '
             f'aria-label="Revenue versus expenses, last six months">']
    parts.append(f'<rect x="0" y="0" width="{w}" height="{h}" fill="#ffffff"/>')
    # y axis gridlines + labels
    ticks = 4
    for i in range(ticks + 1):
        v = axis_max * i / ticks
        y = pad_t + plot_h - (plot_h * i / ticks)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w-20}" y2="{y:.1f}" stroke="#eef0f4"/>')
        parts.append(f'<text x="{pad_l-8}" y="{y+4:.1f}" text-anchor="end" '
                     f'font-size="10" fill="#8a94a6">{sym}{v:,.0f}</text>')
    # bars
    n = len(months)
    group = plot_w / max(n, 1)
    bw = group * 0.28
    for i, m in enumerate(months):
        gx = pad_l + group * i + group * 0.16
        rh = (m["revenue"] / axis_max) * plot_h if axis_max else 0
        eh = (m["expenses"] / axis_max) * plot_h if axis_max else 0
        ry = pad_t + plot_h - rh
        ey = pad_t + plot_h - eh
        parts.append(f'<rect x="{gx:.1f}" y="{ry:.1f}" width="{bw:.1f}" height="{max(rh,0):.1f}" '
                     f'rx="3" fill="#2f81f7"><title>{m["label"]} revenue {sym}{m["revenue"]:,.2f}</title></rect>')
        parts.append(f'<rect x="{gx+bw+4:.1f}" y="{ey:.1f}" width="{bw:.1f}" height="{max(eh,0):.1f}" '
                     f'rx="3" fill="#f97316"><title>{m["label"]} expenses {sym}{m["expenses"]:,.2f}</title></rect>')
        parts.append(f'<text x="{gx+bw/2+2:.1f}" y="{h-24}" text-anchor="middle" '
                     f'font-size="10" fill="#5b6472">{m["label"]}</text>')
    # legend
    parts.append(f'<rect x="{pad_l}" y="{h-14}" width="10" height="10" rx="2" fill="#2f81f7"/>')
    parts.append(f'<text x="{pad_l+15}" y="{h-6}" font-size="11" fill="#5b6472">Revenue</text>')
    parts.append(f'<rect x="{pad_l+80}" y="{h-14}" width="10" height="10" rx="2" fill="#f97316"/>')
    parts.append(f'<text x="{pad_l+95}" y="{h-6}" font-size="11" fill="#5b6472">Expenses</text>')
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Seed data — a small consultancy, internally consistent so the P&L is sane.
# --------------------------------------------------------------------------- #


def seed_demo():
    """Load a realistic but small consultancy dataset.

    The data is generated with a fixed pseudo-random stream so it is
    reproducible, and every invoice payment and expense is journaled through
    the same helpers the UI uses — so the balance sheet balances.
    """
    import random

    conn = get_db()
    rnd = random.Random(20260101)
    today = date.today()
    # Anchor on the 1st of the current month; cover 6 months back from here.
    anchor = today.replace(day=1)

    # Month arithmetic without any third-party date library.
    def months_back(base, back):
        y, m = base.year, base.month - back
        while m <= 0:
            m += 12
            y -= 1
        return y, m

    customers = [
        ("Northwind Analytics", "finance@northwind.io", "+1 415 555 0142", "550 Howard St, San Francisco"),
        ("Brightpath Media", "hello@brightpath.co", "+1 212 555 0177", "200 5th Ave, New York"),
        ("Cedar & Co. Studio", "studio@cedarandco.com", "+44 20 7946 0912", "14 Shoreditch, London"),
        ("Maplewood Health", "ops@maplewoodhealth.org", "+1 617 555 0188", "900 Boylston St, Boston"),
        ("Fernline Logistics", "contact@fernline.com", "+1 305 555 0121", "1100 Biscayne Blvd, Miami"),
        ("Quartz Robotics", "team@quartzrobotics.ai", "+1 650 555 0163", "440 El Camino Real, Palo Alto"),
        ("Willowbrook Agency", "agency@willowbrook.tv", "+1 512 555 0199", "600 Congress Ave, Austin"),
        ("Halcyon Design", "studio@halcyondesign.co", "+31 20 555 0144", "Keizersgracht 212, Amsterdam"),
    ]
    vendors = [
        ("AWS Cloud", "billing@aws.amazon.com", "", ""),
        ("Figma Inc.", "support@figma.com", "", ""),
        ("WeWork Offices", "billing@wework.com", "", ""),
        ("Airbnb Travel", "travel@airbnb.com", "", ""),
        ("Google Ads", "billing@google.com", "", ""),
    ]

    cur = conn.cursor()
    cust_ids, vend_ids = [], []
    for i, (name, email, phone, addr) in enumerate(customers, 1):
        cur.execute("INSERT INTO customers (name,email,phone,address,created_at) VALUES (?,?,?,?,?)",
                    (name, email, phone, addr, now_iso()))
        cust_ids.append(cur.lastrowid)
    for name, email, phone, addr in vendors:
        cur.execute("INSERT INTO vendors (name,email,phone,address,created_at) VALUES (?,?,?,?,?)",
                    (name, email, phone, addr, now_iso()))
        vend_ids.append(cur.lastrowid)

    def acct_id(name):
        r = conn.execute("SELECT id FROM accounts WHERE LOWER(name)=LOWER(?)", (name,)).fetchone()
        return r["id"]

    rev_acct = acct_id("Sales Revenue")
    tax_acct = acct_id("Tax Payable")
    cash_acct = acct_id("Cash")
    ar_acct = acct_id("Accounts Receivable")

    tax_rate = float(get_setting(conn, "tax_rate", SEED_TAX_RATE)) / 100.0

    def add_invoice(cust_id, issue, due, status, lines, notes=""):
        subtotal = round(sum(q * r for _, q, r in lines), 2)
        tax = round(subtotal * tax_rate, 2)
        total = round(subtotal + tax, 2)
        n = int(get_setting(conn, "next_invoice_no", "1"))
        inv_id = f"INV-{n:04d}"
        conn.execute("INSERT INTO settings (key,value) VALUES ('next_invoice_no', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(n + 1),))
        conn.execute(
            "INSERT INTO invoices (invoice_id,customer_id,issue_date,due_date,status,subtotal,tax,total,notes,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (inv_id, cust_id, issue.isoformat(), due.isoformat(), status, subtotal, tax, total, notes, now_iso()),
        )
        for desc, qty, rate in lines:
            conn.execute(
                "INSERT INTO invoice_lines (invoice_id,description,qty,rate,amount) VALUES (?,?,?,?,?)",
                (inv_id, desc, qty, rate, round(qty * rate, 2)),
            )
        # Journalize based on status. Accrual model:
        #   sent/overdue -> debit AR (total), credit Revenue (subtotal), credit Tax Payable (tax)
        #   paid         -> additionally debit Cash (total), credit AR (total)
        if status in ("sent", "paid", "overdue"):
            conn.execute(
                "INSERT INTO transactions (date,account_id,description,amount,type,ref_type,ref_id,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (issue.isoformat(), ar_acct, f"Invoice {inv_id}", round(total, 2), "debit", "invoice", inv_id, now_iso()),
            )
            conn.execute(
                "INSERT INTO transactions (date,account_id,description,amount,type,ref_type,ref_id,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (issue.isoformat(), rev_acct, f"Invoice {inv_id}", round(subtotal, 2), "credit", "invoice", inv_id, now_iso()),
            )
            if tax > 0:
                conn.execute(
                    "INSERT INTO transactions (date,account_id,description,amount,type,ref_type,ref_id,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (issue.isoformat(), tax_acct, f"Invoice {inv_id} tax", round(tax, 2), "credit", "invoice", inv_id, now_iso()),
                )
        if status == "paid":
            conn.execute(
                "INSERT INTO transactions (date,account_id,description,amount,type,ref_type,ref_id,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (due.isoformat(), cash_acct, f"Invoice {inv_id} paid", round(total, 2), "debit", "invoice", inv_id, now_iso()),
            )
            conn.execute(
                "INSERT INTO transactions (date,account_id,description,amount,type,ref_type,ref_id,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (due.isoformat(), ar_acct, f"Invoice {inv_id} paid", round(total, 2), "credit", "invoice", inv_id, now_iso()),
            )
        return inv_id

    def add_expense(dt, vendor_name, category, amount, desc):
        aid = vend_ids[0]
        vrow = conn.execute("SELECT id FROM vendors WHERE name=?", (vendor_name,)).fetchone()
        if vrow:
            aid = vrow["id"]
        eid = None
        conn.execute(
            "INSERT INTO expenses (date,vendor_id,account_id,category,amount,description,receipt_note,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (dt.isoformat(), aid, acct_id(category), category, round(amount, 2), desc, "", now_iso()),
        )
        eid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        # Debit the expense account, credit cash.
        conn.execute(
            "INSERT INTO transactions (date,account_id,description,amount,type,ref_type,ref_id,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (dt.isoformat(), acct_id(category), desc, round(amount, 2), "debit", "expense", str(eid), now_iso()),
        )
        conn.execute(
            "INSERT INTO transactions (date,account_id,description,amount,type,ref_type,ref_id,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (dt.isoformat(), cash_acct, desc, round(amount, 2), "credit", "expense", str(eid), now_iso()),
        )
        return eid

    services = [
        ("Consulting (hours)", 40, 2500.0),
        ("Development sprint", 20, 3000.0),
        ("Design system audit", 1, 2400.0),
        ("Data pipeline setup", 1, 3800.0),
        ("Advisory retainer", 1, 2000.0),
        ("UX research", 12, 180.0),
        ("Workshop facilitation", 8, 350.0),
    ]

    n_inv = 0
    for back in range(5, -1, -1):
        y, m = months_back(anchor, back)
        day = min(28, today.day if back == 0 else rnd.randint(2, 26))
        base = date(y, m, day)
        for i in range(rnd.randint(4, 6)):
            cust = rnd.choice(cust_ids)
            line_specs = []
            pick = rnd.choice(services)
            qty = rnd.randint(1, max(2, pick[1])) if pick[0].endswith("(hours)") else 1
            desc = pick[0]
            rate = pick[2]
            line_specs.append((desc, qty, rate))
            if rnd.random() < 0.45:
                p2 = rnd.choice(services)
                line_specs.append((p2[0] + " (add-on)", rnd.randint(1, 4), round(p2[2] * 0.4, 2)))
            issue = base + timedelta(days=rnd.randint(0, 12))
            if issue > today:
                # Current month: never issue an invoice dated in the future.
                issue = today
            due = issue + timedelta(days=rnd.choice([7, 14, 14, 30]))
            if back > 0:
                # Historical months: settled long ago.
                status = "paid" if rnd.random() < 0.8 else "overdue"
            elif due > today:
                # Current month, not yet due: the open pipeline.
                status = "sent" if i % 3 else "draft"
            else:
                # Current month, already past due.
                status = "overdue" if i % 2 else "paid"
            add_invoice(cust, issue, due, status, line_specs)
            n_inv += 1

    # Expenses: ~40 across the 6 months. Amounts are realistic for a small
    # consultancy running roughly $35-45k of revenue per month.
    spend_templates = [
        ("AWS Cloud", "Software", 600.0, 2800.0, "Cloud hosting & infra"),
        ("AWS Cloud", "Software", 350.0, 1600.0, "Cloud hosting"),
        ("Figma Inc.", "Software", 90.0, 120.0, "Design tools subscription"),
        ("Figma Inc.", "Software", 35.0, 45.0, "Design tools seat"),
        ("WeWork Offices", "Office Expenses", 900.0, 2600.0, "Co-working space"),
        ("Airbnb Travel", "Travel", 480.0, 1900.0, "Client meeting travel"),
        ("Google Ads", "Marketing", 400.0, 1800.0, "Paid search campaign"),
        ("AWS Cloud", "Other Expenses", 80.0, 320.0, "Domain & tooling"),
        ("WeWork Offices", "Office Expenses", 140.0, 520.0, "Supplies & postage"),
    ]
    for _ in range(40):
        back = rnd.randint(0, 5)
        y, m = months_back(anchor, back)
        dt = date(y, m, rnd.randint(1, min(28, today.day if back == 0 else 28)))
        tpl = rnd.choice(spend_templates)
        amount = round(rnd.uniform(tpl[2], tpl[3]), 2)
        add_expense(dt, tpl[0], tpl[1], amount, tpl[4])

    conn.commit()
    conn.close()
    return n_inv


# --------------------------------------------------------------------------- #
# FastAPI app
# --------------------------------------------------------------------------- #


def _refresh_overdue():
    """Flip any sent invoice past its due date to 'overdue'."""
    conn = get_db()
    conn.execute("UPDATE invoices SET status='overdue' "
                 "WHERE status='sent' AND due_date < ?", (date.today().isoformat(),))
    conn.commit()
    conn.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Startup/shutdown hook: build the schema, seed defaults, refresh statuses."""
    init_db()
    _refresh_overdue()
    yield
    # Nothing to tear down; connections are per-request and already closed.


app = FastAPI(
    title="ledgerlite",
    version=VERSION,
    description="Open-source QuickBooks Online alternative — invoicing, expenses, "
                "P&L and balance sheet in one FastAPI + SQLite app.",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

templates.env.filters["money"] = money
templates.env.filters["date_fmt"] = lambda s: (parse_date(s).strftime("%b %d, %Y") if parse_date(s) else (s or ""))


def db():
    return get_db()


def flash_msg(request, kind="success", msg=""):
    return {"flash": {"kind": kind, "msg": msg}} if msg else {}


# --------------------------------------------------------------------------- #
# Shared render helper
# --------------------------------------------------------------------------- #


def render(request, name, conn=None, **ctx):
    """Render a template with common context (currency, business name, helpers).

    Always opens its own short-lived connection for the display settings, so
    routes are free to close theirs first. Templates never touch the DB
    directly, so no connection is forwarded to them.
    """
    ctx.pop("conn", None)
    conn2 = get_db()
    try:
        cur = get_setting(conn2, "currency", "USD")
        biz = get_setting(conn2, "business_name", "My Business")
    finally:
        conn2.close()
    ctx.setdefault("currency", cur)
    ctx.setdefault("business_name", biz)
    ctx.setdefault("app_name", APP_NAME)
    ctx.setdefault("version", VERSION)
    ctx.setdefault("today", date.today().isoformat())
    return templates.TemplateResponse(request, name, ctx)


def open_conn():
    return get_db()


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    conn = get_db()
    data = dashboard_data(conn)
    chart = svg_chart(data["months"], get_setting(conn, "currency", "USD"))
    cur = get_setting(conn, "currency", "USD")
    # Enrich overdue rows with customer names.
    for row in data["overdue"]:
        c = conn.execute("SELECT name FROM customers WHERE id=?", (row["customer_id"],)).fetchone()
        row["customer_name"] = c["name"] if c else "—"
        row["total_fmt"] = money(row["total"], cur)
    conn.close()
    return render(request, "dashboard.html", conn=conn, data=data, chart=chart)


@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    conn = get_db()
    return render(request, "about.html", conn=conn)


# --------------------------------------------------------------------------- #
# Customers
# --------------------------------------------------------------------------- #


def _contact_rows(conn):
    rows = conn.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM invoices i WHERE i.customer_id=c.id) AS inv_count, "
        "(SELECT COALESCE(SUM(i.total),0) FROM invoices i WHERE i.customer_id=c.id) AS inv_total "
        "FROM customers c ORDER BY c.name"
    ).fetchall()
    return [dict(r) for r in rows]


@app.get("/customers", response_class=HTMLResponse)
def customers_list(request: Request, flash_kind: str = "", flash_msg_: str = ""):
    conn = get_db()
    rows = _contact_rows(conn)
    cur = get_setting(conn, "currency", "USD")
    for r in rows:
        r["inv_total_fmt"] = money(r["inv_total"], cur)
    conn.close()
    return render(request, "customers.html", conn=conn, rows=rows,
                  flash={"kind": flash_kind, "msg": flash_msg_} if flash_msg_ else {})


@app.post("/customers")
def customers_create(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    address: str = Form(""),
    notes: str = Form(""),
):
    name = (name or "").strip()
    if not name:
        return render(request, "customers.html", conn=get_db(), rows=_contact_rows(get_db()),
                      flash={"kind": "error", "msg": "Customer name is required."})
    conn = get_db()
    conn.execute("INSERT INTO customers (name,email,phone,address,notes,created_at) VALUES (?,?,?,?,?,?)",
                 (name, email, phone, address, notes, now_iso()))
    conn.commit()
    conn.close()
    return RedirectResponse("/customers?flash=success&msg=Customer%20created", status_code=303)


@app.get("/customers/{cid}/edit", response_class=HTMLResponse)
def customers_edit_get(request: Request, cid: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, "Customer not found")
    conn.close()
    return render(request, "customer_form.html", conn=conn, row=dict(row),
                  mode="edit", page_title=f"Edit {row['name']}")


@app.post("/customers/{cid}")
def customers_edit(cid: int, name: str = Form(""), email: str = Form(""), phone: str = Form(""),
                   address: str = Form(""), notes: str = Form("")):
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "Customer name is required")
    conn = get_db()
    conn.execute("UPDATE customers SET name=?, email=?, phone=?, address=?, notes=? WHERE id=?",
                 (name, email, phone, address, notes, cid))
    conn.commit()
    conn.close()
    return RedirectResponse("/customers?flash=success&msg=Customer%20updated", status_code=303)


@app.post("/customers/{cid}/delete")
def customers_delete(cid: int):
    conn = get_db()
    n = conn.execute("SELECT COUNT(*) FROM invoices WHERE customer_id=?", (cid,)).fetchone()[0]
    if n:
        conn.close()
        return RedirectResponse(f"/customers?flash=error&msg=Customer%20has%20{n}%20invoices", status_code=303)
    conn.execute("DELETE FROM customers WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    return RedirectResponse("/customers?flash=success&msg=Customer%20deleted", status_code=303)


# --------------------------------------------------------------------------- #
# Vendors
# --------------------------------------------------------------------------- #


def _vendor_rows(conn):
    rows = conn.execute(
        "SELECT v.*, (SELECT COUNT(*) FROM expenses e WHERE e.vendor_id=v.id) AS exp_count, "
        "(SELECT COALESCE(SUM(e.amount),0) FROM expenses e WHERE e.vendor_id=v.id) AS exp_total "
        "FROM vendors v ORDER BY v.name"
    ).fetchall()
    return [dict(r) for r in rows]


@app.get("/vendors", response_class=HTMLResponse)
def vendors_list(request: Request, flash_kind: str = "", flash_msg_: str = ""):
    conn = get_db()
    rows = _vendor_rows(conn)
    cur = get_setting(conn, "currency", "USD")
    for r in rows:
        r["exp_total_fmt"] = money(r["exp_total"], cur)
    conn.close()
    return render(request, "vendors.html", conn=conn, rows=rows,
                  flash={"kind": flash_kind, "msg": flash_msg_} if flash_msg_ else {})


@app.post("/vendors")
def vendors_create(name: str = Form(""), email: str = Form(""), phone: str = Form(""),
                   address: str = Form(""), notes: str = Form("")):
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "Vendor name is required")
    conn = get_db()
    conn.execute("INSERT INTO vendors (name,email,phone,address,notes,created_at) VALUES (?,?,?,?,?,?)",
                 (name, email, phone, address, notes, now_iso()))
    conn.commit()
    conn.close()
    return RedirectResponse("/vendors?flash=success&msg=Vendor%20created", status_code=303)


@app.get("/vendors/{vid}/edit", response_class=HTMLResponse)
def vendors_edit_get(request: Request, vid: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM vendors WHERE id=?", (vid,)).fetchone()
    if not row:
        raise HTTPException(404, "Vendor not found")
    conn.close()
    return render(request, "vendor_form.html", conn=conn, row=dict(row),
                  mode="edit", page_title=f"Edit {row['name']}")


@app.post("/vendors/{vid}")
def vendors_edit(vid: int, name: str = Form(""), email: str = Form(""), phone: str = Form(""),
                 address: str = Form(""), notes: str = Form("")):
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "Vendor name is required")
    conn = get_db()
    conn.execute("UPDATE vendors SET name=?, email=?, phone=?, address=?, notes=? WHERE id=?",
                 (name, email, phone, address, notes, vid))
    conn.commit()
    conn.close()
    return RedirectResponse("/vendors?flash=success&msg=Vendor%20updated", status_code=303)


@app.post("/vendors/{vid}/delete")
def vendors_delete(vid: int):
    conn = get_db()
    n = conn.execute("SELECT COUNT(*) FROM expenses WHERE vendor_id=?", (vid,)).fetchone()[0]
    if n:
        conn.close()
        return RedirectResponse(f"/vendors?flash=error&msg=Vendor%20has%20{n}%20expenses", status_code=303)
    conn.execute("DELETE FROM vendors WHERE id=?", (vid,))
    conn.commit()
    conn.close()
    return RedirectResponse("/vendors?flash=success&msg=Vendor%20deleted", status_code=303)


# --------------------------------------------------------------------------- #
# Invoices
# --------------------------------------------------------------------------- #


def _invoice_with_lines(conn, invoice_id):
    inv = conn.execute("SELECT * FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
    if not inv:
        return None
    inv = dict(inv)
    lines = conn.execute(
        "SELECT * FROM invoice_lines WHERE invoice_id=? ORDER BY id", (invoice_id,)
    ).fetchall()
    inv["lines"] = [dict(l) for l in lines]
    c = conn.execute("SELECT * FROM customers WHERE id=?", (inv["customer_id"],)).fetchone()
    inv["customer"] = dict(c) if c else {"name": "—"}
    return inv


@app.get("/invoices", response_class=HTMLResponse)
def invoices_list(request: Request, status: str = "all", flash_kind: str = "", flash_msg_: str = ""):
    conn = get_db()
    cur = get_setting(conn, "currency", "USD")
    q = "SELECT i.*, c.name AS customer_name FROM invoices i JOIN customers c ON c.id=i.customer_id"
    params = []
    if status and status != "all":
        q += " WHERE i.status=?"
        params.append(status)
    q += " ORDER BY i.issue_date DESC, i.invoice_id DESC"
    rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    for r in rows:
        r["total_fmt"] = money(r["total"], cur)
        r["due_fmt"] = r["due_date"]
    today = date.today().isoformat()
    n_overdue = sum(1 for r in rows if r["status"] in ("sent",) and r["due_date"] < today)
    conn.close()
    return render(request, "invoices.html", conn=conn, rows=rows, status=status,
                  flash={"kind": flash_kind, "msg": flash_msg_} if flash_msg_ else {},
                  today=today)


@app.get("/invoices/new", response_class=HTMLResponse)
def invoice_new(request: Request):
    conn = get_db()
    customers = [dict(r) for r in conn.execute("SELECT id,name FROM customers ORDER BY name").fetchall()]
    tax_rate = get_setting(conn, "tax_rate", SEED_TAX_RATE)
    conn.close()
    if not customers:
        return render(request, "invoice_form.html", conn=conn, customers=[],
                      flash={"kind": "error", "msg": "Add a customer before creating an invoice."},
                      tax_rate=tax_rate)
    return render(request, "invoice_form.html", conn=conn, customers=customers,
                  tax_rate=tax_rate,
                  today=date.today().isoformat(),
                  due_default=(date.today() + timedelta(days=14)).isoformat())


@app.post("/invoices")
def invoice_create(
    customer_id: str = Form(""),
    issue_date: str = Form(""),
    due_date: str = Form(""),
    tax_rate: str = Form(""),
    notes: str = Form(""),
    line_desc: list[str] = Form(default=[""]),
    line_qty: list[str] = Form(default=[""]),
    line_rate: list[str] = Form(default=[""]),
):
    conn = get_db()
    cust = conn.execute("SELECT id,name FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not cust:
        raise HTTPException(400, "Customer is required")

    # Parse line items, dropping any fully blank rows.
    lines = []
    n = max(len(line_desc), len(line_qty), len(line_rate))
    for i in range(n):
        d = (line_desc[i] if i < len(line_desc) else "").strip()
        q = (line_qty[i] if i < len(line_qty) else "").strip()
        r = (line_rate[i] if i < len(line_rate) else "").strip()
        if not d and not q and not r:
            continue
        try:
            qty = float(q or 0)
            rate = float(r or 0)
        except ValueError:
            raise HTTPException(400, "Line qty and rate must be numbers")
        lines.append((d or "Service", qty, rate))
    if not lines:
        raise HTTPException(400, "At least one line item is required")

    issue = parse_date(issue_date) or date.today()
    due = parse_date(due_date)
    if due is None or due <= issue:
        due = issue + timedelta(days=14)
    try:
        tr = float(tax_rate or 0) / 100.0
    except ValueError:
        tr = 0.0

    subtotal = round(sum(q * rt for _, q, rt in lines), 2)
    tax = round(subtotal * tr, 2)
    total = round(subtotal + tax, 2)
    inv_id = next_invoice_id(conn)

    conn.execute(
        "INSERT INTO invoices (invoice_id,customer_id,issue_date,due_date,status,subtotal,tax,total,notes,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (inv_id, cust["id"], issue.isoformat(), due.isoformat(), "draft", subtotal, tax, total, notes, now_iso()),
    )
    for desc, qty, rate in lines:
        conn.execute(
            "INSERT INTO invoice_lines (invoice_id,description,qty,rate,amount) VALUES (?,?,?,?,?)",
            (inv_id, desc, qty, rate, round(qty * rate, 2)),
        )
    conn.commit()
    conn.close()
    return RedirectResponse(f"/invoices/{inv_id}", status_code=303)


@app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_detail(request: Request, invoice_id: str):
    conn = get_db()
    cur = get_setting(conn, "currency", "USD")
    inv = _invoice_with_lines(conn, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    inv["total_fmt"] = money(inv["total"], cur)
    inv["subtotal_fmt"] = money(inv["subtotal"], cur)
    inv["tax_fmt"] = money(inv["tax"], cur)
    for l in inv["lines"]:
        l["amount_fmt"] = money(l["amount"], cur)
        l["rate_fmt"] = money(l["rate"], cur)
    # All journals for this invoice.
    j = [dict(r) for r in conn.execute(
        "SELECT t.*, a.name AS account_name FROM transactions t JOIN accounts a ON a.id=t.account_id "
        "WHERE t.ref_type='invoice' AND t.ref_id=? ORDER BY t.date, t.id", (invoice_id,)).fetchall()]
    for x in j:
        x["amount_fmt"] = money(x["amount"], cur)
    today = date.today().isoformat()
    if inv["status"] == "sent" and inv["due_date"] < today:
        conn.execute("UPDATE invoices SET status='overdue' WHERE invoice_id=?", (invoice_id,))
        conn.commit()
        inv["status"] = "overdue"
    conn.close()
    return render(request, "invoice_detail.html", conn=conn, inv=inv, journals=j, today=today)


@app.get("/invoices/{invoice_id}/print", response_class=HTMLResponse)
def invoice_print(request: Request, invoice_id: str):
    conn = get_db()
    cur = get_setting(conn, "currency", "USD")
    inv = _invoice_with_lines(conn, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    inv["total_fmt"] = money(inv["total"], cur)
    inv["subtotal_fmt"] = money(inv["subtotal"], cur)
    inv["tax_fmt"] = money(inv["tax"], cur)
    for l in inv["lines"]:
        l["amount_fmt"] = money(l["amount"], cur)
        l["rate_fmt"] = money(l["rate"], cur)
    biz = get_setting(conn, "business_name", "My Business")
    conn.close()
    return render(request, "invoice_print.html", conn=conn, inv=inv, business_name=biz)


@app.get("/invoices/{invoice_id}/edit", response_class=HTMLResponse)
def invoice_edit_get(request: Request, invoice_id: str):
    conn = get_db()
    inv = _invoice_with_lines(conn, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv["status"] != "draft":
        raise HTTPException(400, "Only draft invoices can be edited")
    customers = [dict(r) for r in conn.execute("SELECT id,name FROM customers ORDER BY name").fetchall()]
    tax_rate = get_setting(conn, "tax_rate", SEED_TAX_RATE)
    conn.close()
    return render(request, "invoice_form.html", conn=conn, customers=customers, inv=inv,
                  tax_rate=tax_rate)


@app.post("/invoices/{invoice_id}")
def invoice_edit(invoice_id: str, customer_id: str = Form(""), issue_date: str = Form(""),
                 due_date: str = Form(""), tax_rate: str = Form(""), notes: str = Form(""),
                 line_desc: list[str] = Form(default=[""]),
                 line_qty: list[str] = Form(default=[""]),
                 line_rate: list[str] = Form(default=[""])):
    conn = get_db()
    inv = conn.execute("SELECT * FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv["status"] != "draft":
        raise HTTPException(400, "Only draft invoices can be edited")
    cust = conn.execute("SELECT id FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not cust:
        raise HTTPException(400, "Customer is required")

    lines = []
    n = max(len(line_desc), len(line_qty), len(line_rate))
    for i in range(n):
        d = (line_desc[i] if i < len(line_desc) else "").strip()
        q = (line_qty[i] if i < len(line_qty) else "").strip()
        r = (line_rate[i] if i < len(line_rate) else "").strip()
        if not d and not q and not r:
            continue
        try:
            qty = float(q or 0)
            rate = float(r or 0)
        except ValueError:
            raise HTTPException(400, "Line qty and rate must be numbers")
        lines.append((d or "Service", qty, rate))
    if not lines:
        raise HTTPException(400, "At least one line item is required")

    issue = parse_date(issue_date) or parse_date(inv["issue_date"])
    due = parse_date(due_date) or parse_date(inv["due_date"])
    try:
        tr = float(tax_rate or 0) / 100.0
    except ValueError:
        tr = 0.0
    subtotal = round(sum(q * rt for _, q, rt in lines), 2)
    tax = round(subtotal * tr, 2)
    total = round(subtotal + tax, 2)

    conn.execute(
        "UPDATE invoices SET customer_id=?, issue_date=?, due_date=?, subtotal=?, tax=?, total=?, notes=? "
        "WHERE invoice_id=?",
        (cust["id"], issue.isoformat(), due.isoformat(), subtotal, tax, total, notes, invoice_id),
    )
    conn.execute("DELETE FROM invoice_lines WHERE invoice_id=?", (invoice_id,))
    for desc, qty, rate in lines:
        conn.execute(
            "INSERT INTO invoice_lines (invoice_id,description,qty,rate,amount) VALUES (?,?,?,?,?)",
            (invoice_id, desc, qty, rate, round(qty * rate, 2)),
        )
    conn.commit()
    conn.close()
    return RedirectResponse(f"/invoices/{invoice_id}?flash=success&msg=Invoice%20updated", status_code=303)


def _journal_invoice_sent(conn, inv):
    """Record revenue recognition when an invoice moves draft -> sent.

    Debit Accounts Receivable for the gross amount, credit Sales Revenue for
    the subtotal, and credit Tax Payable for the tax collected. Balanced.
    """
    aid = account_id(conn, "Accounts Receivable")
    conn.execute(
        "INSERT INTO transactions (date,account_id,description,amount,type,ref_type,ref_id,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (inv["issue_date"], aid, f"Invoice {inv['invoice_id']}", round(inv["total"], 2), "debit",
         "invoice", inv["invoice_id"], now_iso()),
    )
    rev_acct = account_id(conn, get_setting(conn, "revenue_account", "Sales Revenue"))
    conn.execute(
        "INSERT INTO transactions (date,account_id,description,amount,type,ref_type,ref_id,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (inv["issue_date"], rev_acct, f"Invoice {inv['invoice_id']}", round(inv["subtotal"], 2), "credit",
         "invoice", inv["invoice_id"], now_iso()),
    )
    if inv["tax"] > 0:
        tax_acct = account_id(conn, "Tax Payable")
        conn.execute(
            "INSERT INTO transactions (date,account_id,description,amount,type,ref_type,ref_id,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (inv["issue_date"], tax_acct, f"Invoice {inv['invoice_id']} tax", round(inv["tax"], 2), "credit",
             "invoice", inv["invoice_id"], now_iso()),
        )


def _journal_invoice_paid(conn, inv):
    """Record cash collection when an invoice is marked paid.

    Debit Cash for the gross amount, credit Accounts Receivable for the same
    amount. This removes the receivable and moves real money into Cash.
    """
    cash_acct = account_id(conn, get_setting(conn, "cash_account", "Cash"))
    ar_acct = account_id(conn, "Accounts Receivable")
    conn.execute(
        "INSERT INTO transactions (date,account_id,description,amount,type,ref_type,ref_id,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (date.today().isoformat(), cash_acct, f"Invoice {inv['invoice_id']} paid", round(inv["total"], 2),
         "debit", "invoice", inv["invoice_id"], now_iso()),
    )
    conn.execute(
        "INSERT INTO transactions (date,account_id,description,amount,type,ref_type,ref_id,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (date.today().isoformat(), ar_acct, f"Invoice {inv['invoice_id']} paid", round(inv["total"], 2),
         "credit", "invoice", inv["invoice_id"], now_iso()),
    )


@app.post("/invoices/{invoice_id}/send")
def invoice_send(invoice_id: str):
    """Mark a draft invoice as sent — recognises revenue in the ledger."""
    conn = get_db()
    inv = conn.execute("SELECT * FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv["status"] != "draft":
        raise HTTPException(400, f"Cannot send an invoice in '{inv['status']}' status")
    _journal_invoice_sent(conn, dict(inv))
    today = date.today().isoformat()
    new_status = "overdue" if inv["due_date"] < today else "sent"
    conn.execute("UPDATE invoices SET status=? WHERE invoice_id=?", (new_status, invoice_id))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/invoices/{invoice_id}?flash=success&msg=Invoice%20sent", status_code=303)


@app.post("/invoices/{invoice_id}/pay")
def invoice_pay(invoice_id: str):
    """Mark an invoice as paid — records cash and clears the receivable."""
    conn = get_db()
    inv = conn.execute("SELECT * FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv["status"] == "paid":
        raise HTTPException(400, "Invoice is already paid")
    if inv["status"] == "draft":
        # Revenue hasn't been recognised yet — do both legs.
        _journal_invoice_sent(conn, dict(inv))
    _journal_invoice_paid(conn, dict(inv))
    conn.execute("UPDATE invoices SET status='paid' WHERE invoice_id=?", (invoice_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/invoices/{invoice_id}?flash=success&msg=Invoice%20marked%20paid", status_code=303)


@app.post("/invoices/{invoice_id}/delete")
def invoice_delete(invoice_id: str):
    """Delete an invoice and reverse every journal line it produced."""
    conn = get_db()
    inv = conn.execute("SELECT * FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
    if not inv:
        raise HTTPException(404, "Invoice not found")
    delete_refs(conn, "invoice", invoice_id)
    conn.execute("DELETE FROM invoice_lines WHERE invoice_id=?", (invoice_id,))
    conn.execute("DELETE FROM invoices WHERE invoice_id=?", (invoice_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/invoices?flash=success&msg=Invoice%20deleted", status_code=303)


# --------------------------------------------------------------------------- #
# Expenses
# --------------------------------------------------------------------------- #


def _expense_rows(conn, start=None, end=None):
    q = ("SELECT e.*, v.name AS vendor_name, a.name AS account_name FROM expenses e "
         "LEFT JOIN vendors v ON v.id=e.vendor_id "
         "LEFT JOIN accounts a ON a.id=e.account_id WHERE 1=1")
    params = []
    if start:
        q += " AND e.date >= ?"
        params.append(start)
    if end:
        q += " AND e.date <= ?"
        params.append(end)
    q += " ORDER BY e.date DESC, e.id DESC"
    return [dict(r) for r in conn.execute(q, params).fetchall()]


@app.get("/expenses", response_class=HTMLResponse)
def expenses_list(request: Request, month: str = "", flash_kind: str = "", flash_msg_: str = ""):
    conn = get_db()
    cur = get_setting(conn, "currency", "USD")
    start = end = None
    if month:
        ym = month.split("-")
        if len(ym) == 2:
            y, m = int(ym[0]), int(ym[1])
            start = date(y, m, 1).isoformat()
            ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
            end = (date(ny, nm, 1) - timedelta(days=1)).isoformat()
    rows = _expense_rows(conn, start, end)
    total = 0.0
    for r in rows:
        r["amount_fmt"] = money(r["amount"], cur)
        total += r["amount"]
    conn.close()
    return render(request, "expenses.html", conn=conn, rows=rows, month=month,
                  total=round(total, 2), total_fmt=money(total, cur),
                  flash={"kind": flash_kind, "msg": flash_msg_} if flash_msg_ else {})


@app.get("/expenses/new", response_class=HTMLResponse)
def expense_new(request: Request):
    conn = get_db()
    vendors = [dict(r) for r in conn.execute("SELECT id,name FROM vendors ORDER BY name").fetchall()]
    accounts = [dict(r) for r in conn.execute(
        "SELECT id,name FROM accounts WHERE type='expense' ORDER BY name").fetchall()]
    conn.close()
    return render(request, "expense_form.html", conn=conn, vendors=vendors, accounts=accounts,
                  today=date.today().isoformat())


@app.post("/expenses")
def expense_create(date_: str = Form(""), vendor_id: str = Form(""), acct_id: str = Form(""),
                   category: str = Form(""), amount: str = Form(""), description: str = Form(""),
                   receipt_note: str = Form("")):
    conn = get_db()
    try:
        amt = round(float(amount), 2)
    except (TypeError, ValueError):
        raise HTTPException(400, "Amount must be a number")
    if amt <= 0:
        raise HTTPException(400, "Amount must be greater than zero")
    dt = parse_date(date_) or date.today()
    # Accept either an explicit account id or just a category name, creating
    # the account on demand when it does not exist yet.
    if str(acct_id).strip().isdigit() and int(acct_id):
        ac = conn.execute("SELECT id FROM accounts WHERE id=?", (int(acct_id),)).fetchone()
    else:
        ac = conn.execute("SELECT id FROM accounts WHERE LOWER(name)=LOWER(?)",
                          (category or "Other Expenses",)).fetchone()
    if not ac:
        raise HTTPException(400, "An expense account is required")
    vid = int(vendor_id) if str(vendor_id).isdigit() and int(vendor_id) else None
    cat = (category or "").strip() or conn.execute("SELECT name FROM accounts WHERE id=?", (ac["id"],)).fetchone()["name"]

    conn.execute(
        "INSERT INTO expenses (date,vendor_id,account_id,category,amount,description,receipt_note,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (dt.isoformat(), vid, ac["id"], cat, amt, description, receipt_note, now_iso()),
    )
    eid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    # Double entry: debit the chosen expense account, credit cash/bank.
    cash_id = account_id(conn, get_setting(conn, "cash_account", "Cash"))
    journal_by_id(conn, dt.isoformat(), [
        (ac["id"], amt, "debit"),
        (cash_id, amt, "credit"),
    ], ref_type="expense", ref_id=str(eid))
    conn.commit()
    conn.close()
    return RedirectResponse("/expenses?flash=success&msg=Expense%20added", status_code=303)


@app.get("/expenses/{eid}/edit", response_class=HTMLResponse)
def expense_edit_get(request: Request, eid: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM expenses WHERE id=?", (eid,)).fetchone()
    if not row:
        raise HTTPException(404, "Expense not found")
    vendors = [dict(r) for r in conn.execute("SELECT id,name FROM vendors ORDER BY name").fetchall()]
    accounts = [dict(r) for r in conn.execute(
        "SELECT id,name FROM accounts WHERE type='expense' ORDER BY name").fetchall()]
    conn.close()
    return render(request, "expense_form.html", conn=conn, vendors=vendors, accounts=accounts,
                  row=dict(row), mode="edit")


@app.post("/expenses/{eid}")
def expense_edit(eid: int, date_: str = Form(""), vendor_id: str = Form(""),
                 acct_id: str = Form(""), category: str = Form(""), amount: str = Form(""),
                 description: str = Form(""), receipt_note: str = Form("")):
    """Edit an expense — reverses the old journal lines then writes new ones."""
    conn = get_db()
    row = conn.execute("SELECT * FROM expenses WHERE id=?", (eid,)).fetchone()
    if not row:
        raise HTTPException(404, "Expense not found")
    try:
        amt = round(float(amount), 2)
    except (TypeError, ValueError):
        raise HTTPException(400, "Amount must be a number")
    if amt <= 0:
        raise HTTPException(400, "Amount must be greater than zero")
    dt = parse_date(date_) or parse_date(row["date"])
    ac = conn.execute("SELECT id,name FROM accounts WHERE id=?", (acct_id,)).fetchone()
    if not ac:
        # Fall back to the account already on the expense (lets the user edit
        # only the amount/date without re-picking the category).
        existing = conn.execute("SELECT account_id FROM expenses WHERE id=?", (eid,)).fetchone()
        if existing:
            ac = conn.execute("SELECT id,name FROM accounts WHERE id=?", (existing["account_id"],)).fetchone()
    if not ac:
        raise HTTPException(400, "An expense account is required")
    vid = int(vendor_id) if str(vendor_id).isdigit() and int(vendor_id) else None
    cat = (category or "").strip() or ac["name"]

    # Reverse the old double-entry lines, then post the new ones.
    delete_refs(conn, "expense", str(eid))
    conn.execute(
        "UPDATE expenses SET date=?, vendor_id=?, account_id=?, category=?, amount=?, description=?, receipt_note=? "
        "WHERE id=?",
        (dt.isoformat(), vid, ac["id"], cat, amt, description, receipt_note, eid),
    )
    cash_id = account_id(conn, get_setting(conn, "cash_account", "Cash"))
    journal_by_id(conn, dt.isoformat(), [
        (ac["id"], amt, "debit"),
        (cash_id, amt, "credit"),
    ], ref_type="expense", ref_id=str(eid))
    conn.commit()
    conn.close()
    return RedirectResponse("/expenses?flash=success&msg=Expense%20updated", status_code=303)


@app.post("/expenses/{eid}/delete")
def expense_delete(eid: int):
    """Delete an expense — reverses its journal lines so reports stay correct."""
    conn = get_db()
    row = conn.execute("SELECT * FROM expenses WHERE id=?", (eid,)).fetchone()
    if not row:
        raise HTTPException(404, "Expense not found")
    delete_refs(conn, "expense", str(eid))
    conn.execute("DELETE FROM expenses WHERE id=?", (eid,))
    conn.commit()
    conn.close()
    return RedirectResponse("/expenses?flash=success&msg=Expense%20deleted", status_code=303)


@app.get("/expenses/export.csv")
def expenses_export():
    conn = get_db()
    rows = _expense_rows(conn)
    conn.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "vendor", "category", "account", "amount", "description"])
    for r in rows:
        w.writerow([r["date"], r["vendor_name"] or "", r["category"], r["account_name"] or "",
                    f"{r['amount']:.2f}", r["description"] or ""])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=expenses.csv"})


# --------------------------------------------------------------------------- #
# Chart of accounts
# --------------------------------------------------------------------------- #


@app.get("/accounts", response_class=HTMLResponse)
def accounts_page(request: Request, flash_kind: str = "", flash_msg_: str = ""):
    conn = get_db()
    cur = get_setting(conn, "currency", "USD")
    rows = []
    for a in conn.execute("SELECT * FROM accounts ORDER BY type, name").fetchall():
        a = dict(a)
        bal, _ = account_balance(conn, a["id"])
        a["balance"] = round(bal, 2)
        a["balance_fmt"] = money(bal, cur)
        rows.append(a)
    conn.close()
    return render(request, "accounts.html", conn=conn, rows=rows,
                  flash={"kind": flash_kind, "msg": flash_msg_} if flash_msg_ else {})


@app.post("/accounts")
def accounts_create(name: str = Form(""), type_: str = Form("expense")):
    name = (name or "").strip()
    valid = ("asset", "liability", "equity", "revenue", "expense")
    if not name:
        raise HTTPException(400, "Account name is required")
    if type_ not in valid:
        raise HTTPException(400, "Invalid account type")
    conn = get_db()
    if conn.execute("SELECT 1 FROM accounts WHERE LOWER(name)=LOWER(?)", (name,)).fetchone():
        conn.close()
        return RedirectResponse("/accounts?flash=error&msg=Account%20already%20exists", status_code=303)
    conn.execute("INSERT INTO accounts (name, type, is_system) VALUES (?, ?, 0)", (name, type_))
    conn.commit()
    conn.close()
    return RedirectResponse("/accounts?flash=success&msg=Account%20added", status_code=303)


@app.post("/accounts/{aid}/delete")
def accounts_delete(aid: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    if not row:
        raise HTTPException(404, "Account not found")
    if row["is_system"]:
        raise HTTPException(400, "System accounts cannot be deleted")
    n = conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id=?", (aid,)).fetchone()[0]
    if n:
        conn.close()
        return RedirectResponse(f"/accounts?flash=error&msg=Account%20has%20{n}%20transactions", status_code=303)
    conn.execute("DELETE FROM accounts WHERE id=?", (aid,))
    conn.commit()
    conn.close()
    return RedirectResponse("/accounts?flash=success&msg=Account%20deleted", status_code=303)


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #


def _default_range():
    """Year-to-date by default."""
    return date.today().replace(month=1, day=1).isoformat(), date.today().isoformat()


@app.get("/reports", response_class=HTMLResponse)
def reports_hub(request: Request):
    conn = get_db()
    start, end = _default_range()
    conn.close()
    return render(request, "reports.html", conn=conn, start=start, end=end)


@app.get("/reports/pnl", response_class=HTMLResponse)
def report_pnl(request: Request, start: str = "", end: str = ""):
    conn = get_db()
    cur = get_setting(conn, "currency", "USD")
    if not start or not end:
        start, end = _default_range()
    if start > end:
        start, end = end, start
    data = profit_and_loss(conn, start, end)
    # Anomaly: verify arithmetic.
    calc = round(data["total_income"] - data["total_expenses"], 2)
    data["arithmetic_ok"] = abs(calc - data["net_profit"]) < 0.01
    for grp in ("income", "expenses"):
        for row in data[grp]:
            row["net_fmt"] = money(row["net"], cur)
    conn.close()
    return render(request, "report_pnl.html", conn=conn, data=data, start=start, end=end,
                  income_fmt=money(data["total_income"], cur),
                  expense_fmt=money(data["total_expenses"], cur),
                  net_fmt=money(data["net_profit"], cur))


@app.get("/reports/balance-sheet", response_class=HTMLResponse)
def report_balance(request: Request, as_of: str = ""):
    conn = get_db()
    cur = get_setting(conn, "currency", "USD")
    as_of = as_of or date.today().isoformat()
    data = balance_sheet(conn, as_of)
    for grp in ("assets", "liabilities", "equity"):
        for row in data[grp]:
            row["net_fmt"] = money(row["net"], cur)
    conn.close()
    return render(request, "report_balance.html", conn=conn, data=data, as_of=as_of,
                  assets_fmt=money(data["total_assets"], cur),
                  liab_fmt=money(data["total_liabilities"], cur),
                  equity_fmt=money(data["total_equity"], cur))


@app.get("/reports/cashflow", response_class=HTMLResponse)
def report_cashflow(request: Request, start: str = "", end: str = ""):
    conn = get_db()
    cur = get_setting(conn, "currency", "USD")
    if not start or not end:
        start, end = _default_range()
    if start > end:
        start, end = end, start
    data = cash_flow(conn, start, end)
    for row in data["by_account"]:
        row["in_fmt"] = money(row["inflows"], cur)
        row["out_fmt"] = money(row["outflows"], cur)
        row["net_fmt"] = money(row["net"], cur)
    for row in data["by_reason"]:
        row["in_fmt"] = money(row["inflows"], cur)
        row["out_fmt"] = money(row["outflows"], cur)
    conn.close()
    return render(request, "report_cashflow.html", conn=conn, data=data, start=start, end=end,
                  in_fmt=money(data["total_inflows"], cur),
                  out_fmt=money(data["total_outflows"], cur),
                  net_fmt=money(data["net_change"], cur))


@app.get("/reports/tax", response_class=HTMLResponse)
def report_tax(request: Request, start: str = "", end: str = ""):
    conn = get_db()
    cur = get_setting(conn, "currency", "USD")
    if not start or not end:
        start, end = _default_range()
    if start > end:
        start, end = end, start
    data = taxable_income(conn, start, end)
    conn.close()
    return render(request, "report_tax.html", conn=conn, data=data, start=start, end=end,
                  income_fmt=money(data["total_income"], cur),
                  expense_fmt=money(data["total_expenses"], cur),
                  net_fmt=money(data["net_profit"], cur),
                  tax_fmt=money(data["tax_collected"], cur),
                  taxable_fmt=money(data["taxable_income"], cur))


# --------------------------------------------------------------------------- #
# Import / export
# --------------------------------------------------------------------------- #


@app.get("/import/expenses", response_class=HTMLResponse)
def import_page(request: Request):
    conn = get_db()
    conn.close()
    return render(request, "import.html", conn=conn)


@app.post("/import/expenses")
def import_expenses(file: UploadFile = File(...)):
    """Bulk-import expenses from CSV: date,vendor,category,amount,description."""
    conn = get_db()
    raw = file.file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(400, "CSV is empty")
    required = {"date", "vendor", "category", "amount"}
    norm = {k.strip().lower(): k.strip() for k in reader.fieldnames}
    missing = required - set(norm.keys())
    if missing:
        raise HTTPException(400, f"CSV missing required columns: {', '.join(sorted(missing))}")

    added = 0
    errors = []
    cash_id = account_id(conn, get_setting(conn, "cash_account", "Cash"))
    for lineno, row in enumerate(reader, start=2):
        try:
            dt = parse_date(row[norm["date"]]) or date.today()
            amt = round(float((row[norm["amount"]] or "0").replace("$", "").replace(",", "")), 2)
            if amt <= 0:
                raise ValueError("amount must be positive")
            vendor = (row[norm["vendor"]] or "").strip()
            category = (row[norm["category"]] or "Other Expenses").strip()
            desc = (row.get(norm.get("description", "description"), "") or "").strip()
            # Vendor: create on demand if unknown.
            vrow = conn.execute("SELECT id FROM vendors WHERE LOWER(name)=LOWER(?)", (vendor,)).fetchone()
            if not vrow and vendor:
                conn.execute("INSERT INTO vendors (name, created_at) VALUES (?, ?)", (vendor, now_iso()))
                conn.commit()
                vrow = conn.execute("SELECT id FROM vendors WHERE LOWER(name)=LOWER(?)", (vendor,)).fetchone()
            vid = vrow["id"] if vrow else None
            # Category: map to an existing expense account by name, else create one.
            arow = conn.execute("SELECT id FROM accounts WHERE LOWER(name)=LOWER(?)", (category,)).fetchone()
            if not arow:
                conn.execute("INSERT INTO accounts (name, type, is_system) VALUES (?, 'expense', 0)", (category,))
                conn.commit()
                arow = conn.execute("SELECT id FROM accounts WHERE LOWER(name)=LOWER(?)", (category,)).fetchone()
            conn.execute(
                "INSERT INTO expenses (date,vendor_id,account_id,category,amount,description,receipt_note,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (dt.isoformat(), vid, arow["id"], category, amt, desc, "imported", now_iso()),
            )
            eid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            journal_by_id(conn, dt.isoformat(), [
                (arow["id"], amt, "debit"),
                (cash_id, amt, "credit"),
            ], ref_type="expense", ref_id=str(eid))
            added += 1
        except Exception as exc:  # noqa: BLE001 — report per-row, keep going
            errors.append(f"line {lineno}: {exc}")
    conn.commit()
    conn.close()
    msg = f"Imported {added} expense(s)" + (f" with {len(errors)} error(s)" if errors else "")
    return RedirectResponse(f"/expenses?flash=success&msg={msg.replace(' ', '%20')}", status_code=303)


@app.get("/export/transactions.csv")
def export_transactions():
    conn = get_db()
    rows = conn.execute(
        "SELECT t.date, a.name AS account, a.type AS account_type, t.type, t.description, t.amount, "
        "t.ref_type, t.ref_id FROM transactions t JOIN accounts a ON a.id=t.account_id "
        "ORDER BY t.date, t.id"
    ).fetchall()
    conn.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "account", "account_type", "type", "description", "amount", "ref_type", "ref_id"])
    for r in rows:
        w.writerow([r["date"], r["account"], r["account_type"], r["type"], r["description"],
                    f"{r['amount']:.2f}", r["ref_type"], r["ref_id"]])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=transactions.csv"})


@app.get("/export/invoices.csv")
def export_invoices():
    conn = get_db()
    rows = conn.execute(
        "SELECT i.invoice_id, c.name AS customer, i.issue_date, i.due_date, i.status, "
        "i.subtotal, i.tax, i.total FROM invoices i JOIN customers c ON c.id=i.customer_id "
        "ORDER BY i.invoice_id"
    ).fetchall()
    conn.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["invoice_id", "customer", "issue_date", "due_date", "status", "subtotal", "tax", "total"])
    for r in rows:
        w.writerow([r["invoice_id"], r["customer"], r["issue_date"], r["due_date"], r["status"],
                    f"{r['subtotal']:.2f}", f"{r['tax']:.2f}", f"{r['total']:.2f}"])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=invoices.csv"})


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


@app.get("/settings", response_class=HTMLResponse)
def settings_get(request: Request, flash_kind: str = "", flash_msg_: str = ""):
    conn = get_db()
    accounts = [dict(r) for r in conn.execute("SELECT id,name,type FROM accounts ORDER BY type, name").fetchall()]
    cash_accts = [a for a in accounts if a["type"] == "asset"]
    rev_accts = [a for a in accounts if a["type"] == "revenue"]
    cur = get_setting(conn, "currency", "USD")
    ctx = {
        "currency": cur,
        "business_name": get_setting(conn, "business_name", "My Business"),
        "tax_rate": get_setting(conn, "tax_rate", SEED_TAX_RATE),
        "cash_account": get_setting(conn, "cash_account", "Cash"),
        "revenue_account": get_setting(conn, "revenue_account", "Sales Revenue"),
        "currencies": CURRENCIES,
        "cash_accts": cash_accts,
        "rev_accts": rev_accts,
        "counts": {
            "customers": conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
            "invoices": conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0],
            "expenses": conn.execute("SELECT COUNT(*) FROM expenses").fetchone()[0],
            "transactions": conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
        },
    }
    conn.close()
    return render(request, "settings.html", conn=conn, **ctx,
                  flash={"kind": flash_kind, "msg": flash_msg_} if flash_msg_ else {})


@app.post("/settings")
def settings_save(currency: str = Form("USD"), business_name: str = Form(""),
                  tax_rate: str = Form(""), cash_account: str = Form("Cash"),
                  revenue_account: str = Form("Sales Revenue")):
    conn = get_db()
    if currency not in CURRENCIES:
        currency = "USD"
    set_setting(conn, "currency", currency)
    set_setting(conn, "business_name", (business_name or "My Business").strip())
    try:
        tr = round(float(tax_rate), 4)
    except ValueError:
        tr = 0.0
    if not (0 <= tr <= 100):
        tr = 0.0
    set_setting(conn, "tax_rate", tr)
    set_setting(conn, "cash_account", cash_account or "Cash")
    set_setting(conn, "revenue_account", revenue_account or "Sales Revenue")
    conn.commit()
    conn.close()
    return RedirectResponse("/settings?flash=success&msg=Settings%20saved", status_code=303)


# --------------------------------------------------------------------------- #
# JSON REST API
# --------------------------------------------------------------------------- #


def _invoice_json(conn, row):
    inv = dict(row)
    c = conn.execute("SELECT name FROM customers WHERE id=?", (inv["customer_id"],)).fetchone()
    inv["customer_name"] = c["name"] if c else None
    lines = [dict(l) for l in conn.execute(
        "SELECT description, qty, rate, amount FROM invoice_lines WHERE invoice_id=? ORDER BY id",
        (inv["invoice_id"],)).fetchall()]
    inv["lines"] = lines
    inv["line_count"] = len(lines)
    return inv


@app.get("/api/health")
def api_health():
    conn = get_db()
    conn.close()
    return {"status": "ok", "service": APP_NAME, "version": VERSION}


@app.get("/api/customers")
def api_customers():
    conn = get_db()
    rows = [_contact_rows(conn)]
    out = rows[0]
    conn.close()
    return {"count": len(out), "customers": out}


@app.post("/api/customers")
def api_customers_create(payload: dict):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    conn = get_db()
    conn.execute(
        "INSERT INTO customers (name,email,phone,address,notes,created_at) VALUES (?,?,?,?,?,?)",
        (name, payload.get("email", ""), payload.get("phone", ""), payload.get("address", ""),
         payload.get("notes", ""), now_iso()),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM customers WHERE id=?",
                       (conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"],)).fetchone()
    conn.close()
    return {"customer": dict(row)}


@app.get("/api/invoices")
def api_invoices(status: str = Query("", alias="status")):
    conn = get_db()
    q = "SELECT * FROM invoices"
    params = []
    if status:
        q += " WHERE status=?"
        params.append(status)
    q += " ORDER BY issue_date DESC, invoice_id DESC"
    rows = [_invoice_json(conn, r) for r in conn.execute(q, params).fetchall()]
    conn.close()
    return {"count": len(rows), "invoices": rows}


@app.post("/api/invoices")
def api_invoices_create(payload: dict):
    conn = get_db()
    cid = payload.get("customer_id")
    cust = conn.execute("SELECT id FROM customers WHERE id=?", (cid,)).fetchone()
    if not cust:
        raise HTTPException(400, "customer_id must reference an existing customer")
    lines_in = payload.get("lines") or []
    lines = []
    for ln in lines_in:
        try:
            qty = float(ln.get("qty", 1))
            rate = float(ln.get("rate", 0))
        except (TypeError, ValueError):
            raise HTTPException(400, "line qty and rate must be numbers")
        lines.append(((ln.get("description") or "Service").strip() or "Service", qty, rate))
    if not lines:
        raise HTTPException(400, "at least one line item is required")
    issue = parse_date(payload.get("issue_date")) or date.today()
    due = parse_date(payload.get("due_date")) or (issue + timedelta(days=14))
    try:
        tr = float(payload.get("tax_rate", get_setting(conn, "tax_rate", SEED_TAX_RATE))) / 100.0
    except (TypeError, ValueError):
        tr = 0.0
    subtotal = round(sum(q * r for _, q, r in lines), 2)
    tax = round(subtotal * tr, 2)
    total = round(subtotal + tax, 2)
    inv_id = next_invoice_id(conn)
    conn.execute(
        "INSERT INTO invoices (invoice_id,customer_id,issue_date,due_date,status,subtotal,tax,total,notes,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (inv_id, cust["id"], issue.isoformat(), due.isoformat(), "draft", subtotal, tax, total,
         payload.get("notes", ""), now_iso()),
    )
    for desc, qty, rate in lines:
        conn.execute(
            "INSERT INTO invoice_lines (invoice_id,description,qty,rate,amount) VALUES (?,?,?,?,?)",
            (inv_id, desc, qty, rate, round(qty * rate, 2)),
        )
    conn.commit()
    row = conn.execute("SELECT * FROM invoices WHERE invoice_id=?", (inv_id,)).fetchone()
    out = _invoice_json(conn, row)
    conn.close()
    return {"invoice": out}


@app.get("/api/expenses")
def api_expenses(month: str = ""):
    conn = get_db()
    start = end = None
    if month:
        parts = month.split("-")
        if len(parts) == 2:
            y, m = int(parts[0]), int(parts[1])
            start = date(y, m, 1).isoformat()
            ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
            end = (date(ny, nm, 1) - timedelta(days=1)).isoformat()
    rows = _expense_rows(conn, start, end)
    conn.close()
    return {"count": len(rows), "total": round(sum(r["amount"] for r in rows), 2), "expenses": rows}


@app.post("/api/expenses")
def api_expenses_create(payload: dict):
    conn = get_db()
    try:
        amt = round(float(payload.get("amount", 0)), 2)
    except (TypeError, ValueError):
        raise HTTPException(400, "amount must be a number")
    if amt <= 0:
        raise HTTPException(400, "amount must be greater than zero")
    cat = (payload.get("category") or "Other Expenses").strip()
    arow = conn.execute("SELECT id FROM accounts WHERE LOWER(name)=LOWER(?)", (cat,)).fetchone()
    if not arow:
        conn.execute("INSERT INTO accounts (name, type, is_system) VALUES (?, 'expense', 0)", (cat,))
        conn.commit()
        arow = conn.execute("SELECT id FROM accounts WHERE LOWER(name)=LOWER(?)", (cat,)).fetchone()
    vendor = (payload.get("vendor") or "").strip()
    vid = None
    if vendor:
        vrow = conn.execute("SELECT id FROM vendors WHERE LOWER(name)=LOWER(?)", (vendor,)).fetchone()
        if not vrow:
            conn.execute("INSERT INTO vendors (name, created_at) VALUES (?, ?)", (vendor, now_iso()))
            conn.commit()
            vrow = conn.execute("SELECT id FROM vendors WHERE LOWER(name)=LOWER(?)", (vendor,)).fetchone()
        vid = vrow["id"]
    dt = parse_date(payload.get("date")) or date.today()
    conn.execute(
        "INSERT INTO expenses (date,vendor_id,account_id,category,amount,description,receipt_note,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (dt.isoformat(), vid, arow["id"], cat, amt, payload.get("description", ""), "", now_iso()),
    )
    eid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    cash_id = account_id(conn, get_setting(conn, "cash_account", "Cash"))
    journal_by_id(conn, dt.isoformat(), [(arow["id"], amt, "debit"), (cash_id, amt, "credit")],
                  ref_type="expense", ref_id=str(eid))
    conn.commit()
    row = conn.execute("SELECT * FROM expenses WHERE id=?", (eid,)).fetchone()
    conn.close()
    return {"expense": dict(row)}


@app.get("/api/reports/pnl")
def api_pnl(start: str = "", end: str = ""):
    conn = get_db()
    if not start or not end:
        start, end = _default_range()
    out = profit_and_loss(conn, start, end)
    conn.close()
    return out


@app.get("/api/reports/balance-sheet")
def api_balance(as_of: str = ""):
    conn = get_db()
    out = balance_sheet(conn, as_of or date.today().isoformat())
    conn.close()
    return out


@app.get("/api/reports/cashflow")
def api_cashflow(start: str = "", end: str = ""):
    conn = get_db()
    if not start or not end:
        start, end = _default_range()
    out = cash_flow(conn, start, end)
    conn.close()
    return out


@app.get("/api/dashboard")
def api_dashboard():
    conn = get_db()
    data = dashboard_data(conn)
    data["months"] = data["months"]
    conn.close()
    return data


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv=None):
    global DB_PATH
    parser = argparse.ArgumentParser(description="ledgerlite — open-source QuickBooks alternative")
    parser.add_argument("--seed", action="store_true", help="load realistic demo data on startup")
    parser.add_argument("--reset", action="store_true", help="delete the database file before starting")
    parser.add_argument("--db", default=None,
                        help="path to the SQLite file (or a directory) instead of ./data/ledger.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    if args.db:
        DB_PATH = Path(args.db)
        if DB_PATH.suffix == "":
            DB_PATH = DB_PATH / "ledger.db"
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    if args.reset:
        try:
            os.remove(str(DB_PATH))
            for suffix in ("-wal", "-shm", "-journal"):
                p = Path(str(DB_PATH) + suffix)
                if p.exists():
                    os.remove(str(p))
            print(f"[ledgerlite] removed {DB_PATH}")
        except FileNotFoundError:
            pass

    init_db()
    if args.seed:
        n = seed_demo()
        print(f"[ledgerlite] seeded demo data ({n} invoices)")

    try:
        import uvicorn

        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    except ModuleNotFoundError:
        print("[ledgerlite] uvicorn is not installed. Run: pip install -r requirements.txt")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())








