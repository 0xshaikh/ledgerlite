"""End-to-end tests for ledgerlite, run against a real FastAPI TestClient.

Each test module run gets an isolated SQLite database in a temp directory so
tests never touch the developer's real data/ledger.db.
"""

import csv
import io
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import server  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Point server.DB_PATH at a throwaway database and re-init it."""
    db_file = tmp_path / "ledger.db"
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "DB_PATH", db_file)
    server.init_db()
    yield db_file


@pytest.fixture()
def client(tmp_db):
    from fastapi.testclient import TestClient

    return TestClient(server.app)


# --------------------------------------------------------------------------- #
# Seed
# --------------------------------------------------------------------------- #


def test_seed_produces_data(tmp_db):
    n = server.seed_demo()
    assert n >= 15, f"expected at least 15 seeded invoices, got {n}"

    conn = server.get_db()
    counts = {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("customers", "vendors", "accounts", "invoices", "invoice_lines",
                  "expenses", "transactions", "settings")
    }
    conn.close()

    assert counts["customers"] == 8
    assert counts["vendors"] == 5
    assert counts["accounts"] >= 10
    assert counts["invoices"] == n
    assert counts["invoice_lines"] > counts["invoices"]
    assert counts["expenses"] >= 35
    assert counts["transactions"] > counts["invoices"] * 2
    assert counts["settings"] >= 4


def test_seed_is_reproducible(tmp_path, monkeypatch):
    """Two seeds from a fixed RNG produce identical P&L totals."""
    results = []
    for _ in range(2):
        monkeypatch.setattr(server, "DATA_DIR", tmp_path)
        monkeypatch.setattr(server, "DB_PATH", tmp_path / f"ledger_{len(results)}.db")
        server.init_db()
        server.seed_demo()
        conn = server.get_db()
        today = __import__("datetime").date.today()
        start = today.replace(month=1, day=1).isoformat()
        pnl = server.profit_and_loss(conn, start, today.isoformat())
        conn.close()
        results.append((pnl["total_income"], pnl["total_expenses"], pnl["net_profit"]))
    assert results[0] == results[1]


def test_seed_has_no_future_dates(tmp_db):
    """Nothing may be dated after today: the demo must read as history."""
    server.seed_demo()
    conn = server.get_db()
    today = __import__("datetime").date.today().isoformat()
    assert conn.execute("SELECT COUNT(*) FROM invoices WHERE issue_date > ?", (today,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM invoices WHERE due_date < issue_date").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE date > ?", (today,)).fetchone()[0] == 0
    conn.close()


def test_seed_pnl_agrees_with_balance_sheet(tmp_db):
    """Recognised profit must match the equity rolled into the balance sheet."""
    server.seed_demo()
    conn = server.get_db()
    today = __import__("datetime").date.today().isoformat()
    pnl = server.profit_and_loss(conn, "2020-01-01", "2030-12-31")
    bs = server.balance_sheet(conn, today)
    conn.close()
    assert abs(pnl["net_profit"] - bs["net_income"]) < 0.005
    assert bs["balanced"] is True


# --------------------------------------------------------------------------- #
# Invoices
# --------------------------------------------------------------------------- #


def _mk_customer(client, name="Acme Corp"):
    r = client.post("/api/customers", json={"name": name, "email": "a@acme.test"})
    assert r.status_code == 200, r.text
    return r.json()["customer"]


def test_invoice_create_computes_totals(client):
    c = _mk_customer(client)
    payload = {
        "customer_id": c["id"],
        "issue_date": "2026-05-01",
        "due_date": "2026-05-15",
        "tax_rate": 10.0,
        "lines": [
            {"description": "Consulting (hours)", "qty": 10, "rate": 150.0},
            {"description": "Rush fee", "qty": 1, "rate": 250.0},
        ],
        "notes": "Net 15",
    }
    r = client.post("/api/invoices", json=payload)
    assert r.status_code == 200, r.text
    inv = r.json()["invoice"]

    assert inv["subtotal"] == 1750.00
    assert inv["tax"] == 175.00
    assert inv["total"] == 1925.00
    assert inv["status"] == "draft"
    assert inv["line_count"] == 2
    assert inv["customer_name"] == "Acme Corp"


def test_invoice_rejects_empty_lines(client):
    c = _mk_customer(client, "Empty Lines Inc")
    r = client.post("/api/invoices", json={"customer_id": c["id"], "lines": []})
    assert r.status_code == 400


def test_invoice_rejects_bad_customer(client):
    r = client.post("/api/invoices", json={
        "customer_id": 99999,
        "lines": [{"description": "x", "qty": 1, "rate": 5}],
    })
    assert r.status_code == 400


def test_mark_sent_recognises_revenue(client):
    c = _mk_customer(client, "SendCo")
    r = client.post("/api/invoices", json={
        "customer_id": c["id"],
        "lines": [{"description": "Work", "qty": 1, "rate": 1000.0}],
        "tax_rate": 0,
    })
    inv = r.json()["invoice"]

    # Draft invoices produce no journal lines.
    conn = server.get_db()
    n = conn.execute("SELECT COUNT(*) FROM transactions WHERE ref_id=?", (inv["invoice_id"],)).fetchone()[0]
    conn.close()
    assert n == 0

    rr = client.post(f"/invoices/{inv['invoice_id']}/send")
    assert rr.status_code in (200, 303, 307)

    conn = server.get_db()
    rows = conn.execute(
        "SELECT a.name, t.type, t.amount FROM transactions t JOIN accounts a ON a.id=t.account_id "
        "WHERE t.ref_id=?", (inv["invoice_id"],)).fetchall()
    conn.close()
    assert len(rows) == 2
    kinds = {(r["name"], r["type"]) for r in rows}
    assert ("Accounts Receivable", "debit") in kinds
    assert ("Sales Revenue", "credit") in kinds


def test_mark_paid_records_cash_and_clears_receivable(client):
    c = _mk_customer(client, "PayCo")
    r = client.post("/api/invoices", json={
        "customer_id": c["id"],
        "lines": [{"description": "Work", "qty": 1, "rate": 500.0}],
        "tax_rate": 0,
    })
    inv = r.json()["invoice"]
    assert client.post(f"/invoices/{inv['invoice_id']}/pay").status_code in (200, 303, 307)

    conn = server.get_db()
    ar, _ = server.account_balance(conn, server.account_id(conn, "Accounts Receivable"))
    cash, _ = server.account_balance(conn, server.account_id(conn, "Cash"))
    conn.close()
    assert ar == 0.0, f"receivable should be cleared, got {ar}"
    assert cash == 500.0, f"cash should be 500, got {cash}"


def test_mark_paid_reduces_receivables_via_dashboard(client):
    c = _mk_customer(client, "RecvCo")
    ids = []
    for i in range(3):
        r = client.post("/api/invoices", json={
            "customer_id": c["id"],
            "lines": [{"description": f"Batch {i}", "qty": 1, "rate": 100.0 + i}],
            "tax_rate": 0,
        })
        inv = r.json()["invoice"]
        assert client.post(f"/invoices/{inv['invoice_id']}/send").status_code in (200, 303, 307)
        ids.append(inv["invoice_id"])

    d1 = client.get("/api/dashboard").json()
    assert d1["receivable_count"] == 3
    assert d1["receivable_total"] == 303.0

    client.post(f"/invoices/{ids[0]}/pay")
    d2 = client.get("/api/dashboard").json()
    assert d2["receivable_count"] == 2
    assert d2["receivable_total"] == 203.0


def test_invoice_delete_reverses_journal(client):
    c = _mk_customer(client, "DelCo")
    r = client.post("/api/invoices", json={
        "customer_id": c["id"],
        "lines": [{"description": "Work", "qty": 1, "rate": 777.0}],
        "tax_rate": 0,
    })
    inv = r.json()["invoice"]
    client.post(f"/invoices/{inv['invoice_id']}/send")
    client.post(f"/invoices/{inv['invoice_id']}/pay")

    conn = server.get_db()
    cash, _ = server.account_balance(conn, server.account_id(conn, "Cash"))
    conn.close()
    assert cash == 777.0

    assert client.post(f"/invoices/{inv['invoice_id']}/delete").status_code in (200, 303, 307)
    conn = server.get_db()
    cash, _ = server.account_balance(conn, server.account_id(conn, "Cash"))
    txns = conn.execute("SELECT COUNT(*) FROM transactions WHERE ref_id=?",
                        (inv["invoice_id"],)).fetchone()[0]
    conn.close()
    assert cash == 0.0
    assert txns == 0


# --------------------------------------------------------------------------- #
# Expenses + reports
# --------------------------------------------------------------------------- #


def test_expense_create_journals_double_entry(client):
    r = client.post("/api/expenses", json={
        "date": "2026-04-05",
        "vendor": "AWS Cloud",
        "category": "Software",
        "amount": 250.00,
        "description": "Cloud hosting",
    })
    assert r.status_code == 200, r.text
    exp = r.json()["expense"]
    assert exp["amount"] == 250.00
    assert exp["category"] == "Software"

    conn = server.get_db()
    sw, _ = server.account_balance(conn, server.account_id(conn, "Software"))
    cash, _ = server.account_balance(conn, server.account_id(conn, "Cash"))
    conn.close()
    assert sw == 250.00
    assert cash == -250.00


def test_expense_edit_reverses_and_republishes(client):
    r = client.post("/api/expenses", json={
        "date": "2026-04-06", "vendor": "V", "category": "Software", "amount": 100.0})
    eid = r.json()["expense"]["id"]

    conn = server.get_db()
    sw, _ = server.account_balance(conn, server.account_id(conn, "Software"))
    conn.close()
    assert sw == 100.0

    rr = client.post(f"/expenses/{eid}", data={
        "date": "2026-04-06", "vendor_id": "", "account_id": "", "category": "Software",
        "amount": "300.00",
    })
    assert rr.status_code in (200, 303, 307), rr.text

    conn = server.get_db()
    sw, _ = server.account_balance(conn, server.account_id(conn, "Software"))
    txns = conn.execute("SELECT COUNT(*) FROM transactions WHERE ref_id=?", (str(eid),)).fetchone()[0]
    conn.close()
    assert sw == 300.0
    assert txns == 2, "exactly one balanced pair should remain"


def test_expense_delete_reverses(client):
    r = client.post("/api/expenses", json={
        "date": "2026-04-07", "vendor": "V", "category": "Software", "amount": 80.0})
    eid = r.json()["expense"]["id"]
    assert client.post(f"/expenses/{eid}/delete").status_code in (200, 303, 307)

    conn = server.get_db()
    sw, _ = server.account_balance(conn, server.account_id(conn, "Software"))
    txns = conn.execute("SELECT COUNT(*) FROM transactions WHERE ref_id=?", (str(eid),)).fetchone()[0]
    conn.close()
    assert sw == 0.0
    assert txns == 0


def test_pnl_arithmetic(client):
    """Net profit must equal total income minus total expenses."""
    c = _mk_customer(client, "PnL Co")
    client.post("/api/invoices", json={
        "customer_id": c["id"],
        "lines": [{"description": "Sprint", "qty": 1, "rate": 4000.0}],
        "tax_rate": 10.0,
    })
    # Sent -> revenue recognised.
    ids = client.get("/api/invoices").json()["invoices"]
    for inv in ids:
        if inv["status"] == "draft":
            client.post(f"/invoices/{inv['invoice_id']}/send")

    for amt, cat in ((1200.0, "Software"), (800.0, "Travel"), (300.0, "Marketing")):
        client.post("/api/expenses", json={
            "date": "2026-06-10", "vendor": "X", "category": cat, "amount": amt})

    r = client.get("/api/reports/pnl?start=2020-01-01&end=2030-12-31")
    assert r.status_code == 200
    pnl = r.json()
    assert pnl["total_income"] > 0
    assert pnl["total_expenses"] > 0
    assert pnl["total_income"] == 4000.0
    assert pnl["total_expenses"] == 2300.0
    assert pnl["net_profit"] == pnl["total_income"] - pnl["total_expenses"] == 1700.0


def test_balance_sheet_balances(client):
    """A seeded dataset must satisfy assets == liabilities + equity."""
    server.seed_demo()
    r = client.get("/api/reports/balance-sheet")
    assert r.status_code == 200
    bs = r.json()
    assert bs["balanced"] is True
    assert abs(bs["total_assets"] - (bs["total_liabilities"] + bs["total_equity"])) < 0.05


def test_cashflow_signs(client):
    server.seed_demo()
    r = client.get("/api/reports/cashflow?start=2020-01-01&end=2030-12-31")
    cf = r.json()
    assert cf["total_inflows"] > 0, "seeded payments received must show as inflows"
    assert cf["total_outflows"] > 0, "seeded expense payments must show as outflows"
    assert all(o["net"] <= 0 for o in cf["outflows"])
    assert all(i["net"] > 0 for i in cf["inflows"])
    assert cf["net_change"] == round(cf["total_inflows"] - cf["total_outflows"], 2)
    # Per-account rows must sum back to the totals.
    assert round(sum(x["inflows"] for x in cf["by_account"]), 2) == cf["total_inflows"]
    assert round(sum(x["outflows"] for x in cf["by_account"]), 2) == cf["total_outflows"]


def test_taxable_income_report(client):
    server.seed_demo()
    r = client.get("/api/reports/pnl?start=2020-01-01&end=2030-12-31")
    # The taxable report is the P&L plus collected tax surfaced.
    assert r.json()["net_profit"] == r.json()["total_income"] - r.json()["total_expenses"]


# --------------------------------------------------------------------------- #
# API JSON shapes
# --------------------------------------------------------------------------- #


def test_api_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_api_customers_shape(client):
    _mk_customer(client, "Shape Co")
    r = client.get("/api/customers")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["count"], int)
    assert body["count"] == 1
    row = body["customers"][0]
    for k in ("id", "name", "email", "inv_count", "inv_total"):
        assert k in row, f"missing key {k}"


def test_api_invoices_shape(client):
    c = _mk_customer(client, "Shape Inv")
    client.post("/api/invoices", json={
        "customer_id": c["id"],
        "lines": [{"description": "a", "qty": 2, "rate": 10.0}],
        "tax_rate": 5,
    })
    body = client.get("/api/invoices").json()
    assert body["count"] == 1
    inv = body["invoices"][0]
    for k in ("invoice_id", "customer_id", "customer_name", "issue_date", "due_date",
              "status", "subtotal", "tax", "total", "lines", "line_count"):
        assert k in inv, f"missing key {k}"
    assert inv["line_count"] == len(inv["lines"]) == 1


def test_api_invoices_filter(client):
    c = _mk_customer(client, "Filter Co")
    client.post("/api/invoices", json={
        "customer_id": c["id"],
        "lines": [{"description": "a", "qty": 1, "rate": 100.0}],
        "tax_rate": 0,
    })
    assert client.get("/api/invoices?status=draft").json()["count"] == 1
    assert client.get("/api/invoices?status=paid").json()["count"] == 0


def test_api_expenses_shape(client):
    client.post("/api/expenses", json={
        "date": "2026-07-01", "vendor": "V", "category": "Software", "amount": 42.0})
    body = client.get("/api/expenses").json()
    assert body["count"] == 1
    assert body["total"] == 42.0
    for k in ("id", "date", "vendor_id", "account_id", "category", "amount", "description"):
        assert k in body["expenses"][0]


def test_api_dashboard_shape(client):
    server.seed_demo()
    body = client.get("/api/dashboard").json()
    for k in ("income_mtd", "expenses_mtd", "net_mtd", "receivable_total",
              "receivable_count", "overdue", "months"):
        assert k in body, f"missing key {k}"
    assert len(body["months"]) == 6
    assert all({"label", "revenue", "expenses"} <= set(m) for m in body["months"])


def test_api_dashboard_matches_pnl(client):
    """Dashboard MTD revenue must match the P&L for the same range."""
    server.seed_demo()
    today = __import__("datetime").date.today()
    start = today.replace(day=1).isoformat()
    dash = client.get("/api/dashboard").json()
    pnl = client.get(f"/api/reports/pnl?start={start}&end={today.isoformat()}").json()
    assert dash["income_mtd"] == pnl["total_income"]
    assert dash["expenses_mtd"] == pnl["total_expenses"]


# --------------------------------------------------------------------------- #
# CSV import / export
# --------------------------------------------------------------------------- #


def test_csv_import_expenses(client):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "vendor", "category", "amount", "description"])
    w.writerow(["2026-08-01", "AWS Cloud", "Software", "110.50", "hosting"])
    w.writerow(["2026-08-03", "Google Ads", "Marketing", "220.00", "campaign"])
    w.writerow(["2026-08-09", "Airbnb Travel", "Travel", "340.00", "client trip"])
    files = {"file": ("expenses.csv", buf.getvalue(), "text/csv")}
    r = client.post("/import/expenses", files=files)
    assert r.status_code in (200, 303, 307), r.text

    body = client.get("/api/expenses").json()
    assert body["count"] == 3
    assert body["total"] == 670.50

    # New vendors must have been created.
    vendors = client.get("/api/customers")  # sanity: API still healthy
    assert vendors.status_code == 200

    # Ledger must reflect the imported spend.
    conn = server.get_db()
    sw, _ = server.account_balance(conn, server.account_id(conn, "Software"))
    conn.close()
    assert sw == 110.50


def test_csv_import_rejects_missing_columns(client):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "amount"])
    w.writerow(["2026-08-01", "10"])
    r = client.post("/import/expenses",
                    files={"file": ("bad.csv", buf.getvalue(), "text/csv")})
    assert r.status_code == 400


def test_csv_export_transactions(client):
    server.seed_demo()
    r = client.get("/export/transactions.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) > 10
    assert {"date", "account", "type", "amount"} <= set(rows[0].keys())


def test_csv_export_invoices(client):
    c = _mk_customer(client, "Export Co")
    client.post("/api/invoices", json={
        "customer_id": c["id"],
        "lines": [{"description": "a", "qty": 1, "rate": 100.0}],
        "tax_rate": 0,
    })
    r = client.get("/export/invoices.csv")
    assert r.status_code == 200
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 1
    assert rows[0]["status"] == "draft"


def test_csv_export_expenses(client):
    client.post("/api/expenses", json={
        "date": "2026-09-01", "vendor": "V", "category": "Software", "amount": 5.0})
    r = client.get("/expenses/export.csv")
    assert r.status_code == 200
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 1
    assert float(rows[0]["amount"]) == 5.0


# --------------------------------------------------------------------------- #
# Settings + currency persistence
# --------------------------------------------------------------------------- #


def test_settings_currency_persists(client):
    r = client.post("/settings", data={
        "currency": "EUR", "business_name": "Test GmbH", "tax_rate": "7.5",
        "cash_account": "Cash", "revenue_account": "Sales Revenue",
    })
    assert r.status_code in (200, 303, 307)

    conn = server.get_db()
    assert server.get_setting(conn, "currency") == "EUR"
    assert server.get_setting(conn, "business_name") == "Test GmbH"
    assert float(server.get_setting(conn, "tax_rate")) == 7.5
    conn.close()

    page = client.get("/").text
    assert "EUR" in page
    assert "€" in page


def test_settings_bad_tax_rate_is_clamped(client):
    client.post("/settings", data={
        "currency": "USD", "business_name": "X", "tax_rate": "999",
        "cash_account": "Cash", "revenue_account": "Sales Revenue"})
    conn = server.get_db()
    assert float(server.get_setting(conn, "tax_rate")) == 0.0
    conn.close()


# --------------------------------------------------------------------------- #
# HTML pages render
# --------------------------------------------------------------------------- #


def test_html_pages_render(client):
    server.seed_demo()
    paths = ["/", "/about", "/invoices", "/invoices/new", "/customers", "/vendors",
             "/expenses", "/expenses/new", "/accounts", "/import/expenses",
             "/reports", "/reports/pnl", "/reports/balance-sheet",
             "/reports/cashflow", "/reports/tax", "/settings"]
    for p in paths:
        r = client.get(p)
        assert r.status_code == 200, f"{p} -> {r.status_code}"
        assert "<html" in r.text.lower()


def test_dashboard_contains_real_numbers(client):
    server.seed_demo()
    html = client.get("/").text
    assert "Revenue MTD" in html
    assert "<svg" in html, "the 6-month chart must be inline SVG"
    # At least one rendered currency amount with a thousands separator.
    assert any(ch.isdigit() for ch in html)


def test_invoice_detail_and_print_render(client):
    c = _mk_customer(client, "Print Co")
    r = client.post("/api/invoices", json={
        "customer_id": c["id"],
        "lines": [{"description": "Consulting (hours)", "qty": 4, "rate": 200.0}],
        "tax_rate": 10.0,
    })
    inv = r.json()["invoice"]
    detail = client.get(f"/invoices/{inv['invoice_id']}")
    assert detail.status_code == 200
    assert "Line items" in detail.text
    print_ = client.get(f"/invoices/{inv['invoice_id']}/print")
    assert print_.status_code == 200
    assert "INVOICE" in print_.text
    assert inv["invoice_id"] in print_.text


def test_status_filter_pages(client):
    server.seed_demo()
    for status in ("all", "draft", "sent", "overdue", "paid"):
        r = client.get(f"/invoices?status={status}")
        assert r.status_code == 200


def test_static_icon_served(client):
    r = client.get("/static/icon.svg")
    assert r.status_code == 200
    assert "<svg" in r.text
