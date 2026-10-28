
from datetime import date
from functools import wraps
from math import isfinite
import os
import re
import sqlite3
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen
from flask import Flask, render_template, request, redirect, url_for, session, flash, make_response
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or "dev-only-change-this-secret-before-deployment"
if os.environ.get("RENDER") and not os.environ.get("FLASK_SECRET_KEY"):
    raise RuntimeError("Set FLASK_SECRET_KEY in the Render environment before starting the app.")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_HTTPS") == "1"
)

DEFAULT_CATEGORIES = (
    "Food", "Transport", "Uber", "Tithes and Offerings",
    "Riotous Living", "Rent", "Electricity", "Other"
)
INVESTMENT_TYPES = ("Shares", "ETF", "Unit trust", "Crypto", "Bond", "Other")
THEMES = ("light", "dark", "midnight", "forest")


def valid_email(email):
    return len(email) <= 254 and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) is not None


def market_data(endpoint, params):
    api_key = os.environ.get("TWELVE_DATA_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Live prices need a TWELVE_DATA_API_KEY configured on the server.")
    query = urlencode({**params, "apikey": api_key})
    try:
        with urlopen(f"https://api.twelvedata.com/{endpoint}?{query}", timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError("The market price service could not be reached. Try again later.") from error
    if "status" in data and data.get("status") == "error":
        raise RuntimeError(data.get("message", "The market price service could not find that symbol."))
    return data

# Connect to our database
def get_db_connection():
    database_path = os.environ.get("DATABASE_PATH", "expenses.db")
    os.makedirs(os.path.dirname(os.path.abspath(database_path)), exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


# Create the database table
def create_table():
    connection = get_db_connection()
    categories_table_existed = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'user_categories'"
    ).fetchone() is not None

    connection.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    user_columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
    if "theme" not in user_columns:
        connection.execute("ALTER TABLE users ADD COLUMN theme TEXT NOT NULL DEFAULT 'light'")
    for column, definition in (
        ("first_name", "TEXT NOT NULL DEFAULT ''"),
        ("last_name", "TEXT NOT NULL DEFAULT ''"),
        ("email_verified", "INTEGER NOT NULL DEFAULT 1"),
        ("verification_token_hash", "TEXT"),
        ("verification_sent_at", "INTEGER"),
    ):
        if column not in user_columns:
            connection.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
    # Verification is disabled for now; release accounts that were waiting on it.
    connection.execute(
        "UPDATE users SET email_verified = 1, verification_token_hash = NULL, verification_sent_at = NULL"
    )

    connection.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            description TEXT NOT NULL,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            month TEXT NOT NULL DEFAULT '',
            user_id INTEGER REFERENCES users(id)
        )
    """)

    # Upgrade databases created before monthly tracking was added.
    columns = {row[1] for row in connection.execute("PRAGMA table_info(expenses)")}
    if "month" not in columns:
        connection.execute("ALTER TABLE expenses ADD COLUMN month TEXT NOT NULL DEFAULT ''")
    if "user_id" not in columns:
        connection.execute("ALTER TABLE expenses ADD COLUMN user_id INTEGER REFERENCES users(id)")
    connection.execute(
        "UPDATE expenses SET month = ? WHERE month = ''",
        (date.today().strftime("%Y-%m"),)
    )

    # Keep the old single-user budget table as a migration source. Budgets
    # going forward are stored separately for each account.
    connection.execute("""
        CREATE TABLE IF NOT EXISTS budget (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            starting_amount REAL NOT NULL DEFAULT 0
        )
    """)

    connection.execute("""
        INSERT OR IGNORE INTO budget (id, starting_amount)
        VALUES (1, 0)
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS user_budgets (
            user_id INTEGER PRIMARY KEY REFERENCES users(id),
            starting_amount REAL NOT NULL DEFAULT 0
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS user_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            name TEXT NOT NULL COLLATE NOCASE,
            UNIQUE(user_id, name)
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS savings_goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            name TEXT NOT NULL,
            target_amount REAL NOT NULL CHECK (target_amount > 0),
            target_date TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS savings_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            goal_id INTEGER NOT NULL REFERENCES savings_goals(id) ON DELETE CASCADE,
            transaction_type TEXT NOT NULL CHECK (transaction_type IN ('deposit', 'withdrawal')),
            amount REAL NOT NULL CHECK (amount > 0),
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS investments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            name TEXT NOT NULL,
            investment_type TEXT NOT NULL,
            amount_invested REAL NOT NULL CHECK (amount_invested > 0),
            current_value REAL NOT NULL DEFAULT 0 CHECK (current_value >= 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    investment_columns = {row[1] for row in connection.execute("PRAGMA table_info(investments)")}
    for column, definition in (
        ("pricing_mode", "TEXT NOT NULL DEFAULT 'manual'"),
        ("symbol", "TEXT"),
        ("exchange", "TEXT"),
        ("quantity", "REAL"),
        ("quote_price", "REAL"),
        ("quote_currency", "TEXT"),
        ("quote_updated_at", "TEXT"),
    ):
        if column not in investment_columns:
            connection.execute(f"ALTER TABLE investments ADD COLUMN {column} {definition}")

    # Migrate old accounts once, when categories are first introduced. Do not
    # reseed on each startup: users must be able to keep deleted categories gone.
    if not categories_table_existed:
        user_ids = [row[0] for row in connection.execute("SELECT id FROM users")]
        for user_id in user_ids:
            connection.executemany(
                "INSERT OR IGNORE INTO user_categories (user_id, name) VALUES (?, ?)",
                ((user_id, name) for name in DEFAULT_CATEGORIES)
            )
        connection.execute("""
            INSERT OR IGNORE INTO user_categories (user_id, name)
            SELECT DISTINCT user_id, category FROM expenses
            WHERE user_id IS NOT NULL AND TRIM(category) != ''
        """)

    connection.commit()
    connection.close()


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(**kwargs)
    return wrapped_view


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        if not valid_email(email):
            flash("Enter a valid email address.", "error")
        elif not first_name or len(first_name) > 50 or not last_name or len(last_name) > 50:
            flash("Enter your first name and surname (up to 50 characters each).", "error")
        elif len(password) < 8:
            flash("Use a password with at least 8 characters.", "error")
        elif password != confirm_password:
            flash("The passwords do not match.", "error")
        else:
            connection = get_db_connection()
            existing_users = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            try:
                cursor = connection.execute(
                    """INSERT INTO users
                       (username, password_hash, first_name, last_name, email_verified)
                       VALUES (?, ?, ?, ?, 1)""",
                    (email, generate_password_hash(password), first_name, last_name)
                )
                user_id = cursor.lastrowid
                starting_amount = 0
                if existing_users == 0:
                    # Preserve the original local install's data for its first account.
                    legacy_budget = connection.execute(
                        "SELECT starting_amount FROM budget WHERE id = 1"
                    ).fetchone()
                    if legacy_budget:
                        starting_amount = legacy_budget["starting_amount"]
                    connection.execute(
                        "UPDATE expenses SET user_id = ? WHERE user_id IS NULL", (user_id,)
                    )
                connection.execute(
                    "INSERT INTO user_budgets (user_id, starting_amount) VALUES (?, ?)",
                    (user_id, starting_amount)
                )
                connection.executemany(
                    "INSERT INTO user_categories (user_id, name) VALUES (?, ?)",
                    ((user_id, name) for name in DEFAULT_CATEGORIES)
                )
                connection.commit()
            except sqlite3.IntegrityError:
                connection.rollback()
                flash("An account with that email already exists.", "error")
                connection.close()
                return render_template("auth.html", mode="signup", email=email,
                                       first_name=first_name, last_name=last_name)
            connection.close()
            flash("Your account is ready. Sign in with your email and password.", "success")
            return redirect(url_for("login"))
    return render_template(
        "auth.html", mode="signup",
        email=request.form.get("email", "") if request.method == "POST" else "",
        first_name=request.form.get("first_name", "") if request.method == "POST" else "",
        last_name=request.form.get("last_name", "") if request.method == "POST" else ""
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        connection = get_db_connection()
        user = connection.execute(
            """SELECT id, username, password_hash, theme, first_name, last_name
               FROM users WHERE username = ?""", (email,)
        ).fetchone()
        connection.close()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["first_name"] = user["first_name"]
            session["last_name"] = user["last_name"]
            session["theme"] = user["theme"] if user["theme"] in THEMES else "light"
            return redirect(url_for("home"))
        flash("Email or password is incorrect.", "error")
    return render_template(
        "auth.html", mode="login",
        email=request.form.get("email", "") if request.method == "POST" else ""
    )


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))

# Home page
@app.route("/")
@login_required
def home():
    selected_month = request.args.get("month", date.today().strftime("%Y-%m"))
    try:
        date.fromisoformat(selected_month + "-01")
    except ValueError:
        selected_month = date.today().strftime("%Y-%m")

    connection = get_db_connection()

    expenses = connection.execute(
        "SELECT * FROM expenses WHERE user_id = ? AND month = ? ORDER BY id DESC",
        (session["user_id"], selected_month)
    ).fetchall()

    categories = connection.execute(
        "SELECT name FROM user_categories WHERE user_id = ? ORDER BY name COLLATE NOCASE",
        (session["user_id"],)
    ).fetchall()

    budget = connection.execute(
        "SELECT starting_amount FROM user_budgets WHERE user_id = ?", (session["user_id"],)
    ).fetchone()

    total_expenses = connection.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE user_id = ? AND month = ?",
        (session["user_id"], selected_month)
    ).fetchone()["total"]

    savings_goals = connection.execute(
        """SELECT g.id, g.name, g.target_amount,
                  COALESCE(SUM(CASE WHEN t.transaction_type = 'deposit'
                                    THEN t.amount ELSE -t.amount END), 0) AS balance
           FROM savings_goals AS g
           LEFT JOIN savings_transactions AS t
             ON t.goal_id = g.id AND t.user_id = g.user_id
           WHERE g.user_id = ? GROUP BY g.id ORDER BY g.id DESC""",
        (session["user_id"],)
    ).fetchall()

    connection.close()

    starting_amount = budget["starting_amount"]
    balance = starting_amount - total_expenses
    response = make_response(render_template(
        "index.html",
        expenses=expenses,
        starting_amount=starting_amount,
        total_expenses=total_expenses,
        balance=balance,
        selected_month=selected_month,
        categories=categories,
        savings_goals=savings_goals
    ))
    # Avoid showing a browser-cached expense list after a delete or edit.
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/graphs")
@login_required
def graphs():
    selected_month = request.args.get("month", date.today().strftime("%Y-%m"))
    try:
        date.fromisoformat(selected_month + "-01")
    except ValueError:
        selected_month = date.today().strftime("%Y-%m")

    chart_type = request.args.get("type", "bar")
    if chart_type not in {"bar", "line", "pie"}:
        chart_type = "bar"

    connection = get_db_connection()
    category_totals = connection.execute(
        """SELECT category, SUM(amount) AS amount
           FROM expenses
           WHERE user_id = ? AND month = ?
           GROUP BY category
           ORDER BY amount DESC, category COLLATE NOCASE""",
        (session["user_id"], selected_month)
    ).fetchall()
    total_expenses = connection.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE user_id = ? AND month = ?",
        (session["user_id"], selected_month)
    ).fetchone()["total"]
    connection.close()

    chart_data = [
        {"category": item["category"], "amount": item["amount"]}
        for item in category_totals
    ]
    return render_template(
        "graphs.html",
        selected_month=selected_month,
        chart_type=chart_type,
        chart_data=chart_data,
        total_expenses=total_expenses
    )


@app.route("/savings-investments")
@login_required
def savings_investments():
    user_id = session["user_id"]
    connection = get_db_connection()
    goals = connection.execute(
        """SELECT g.id, g.name, g.target_amount, g.target_date,
                  COALESCE(SUM(CASE WHEN t.transaction_type = 'deposit'
                                    THEN t.amount ELSE -t.amount END), 0) AS balance
           FROM savings_goals AS g
           LEFT JOIN savings_transactions AS t
             ON t.goal_id = g.id AND t.user_id = g.user_id
           WHERE g.user_id = ?
           GROUP BY g.id
           ORDER BY g.id DESC""",
        (user_id,)
    ).fetchall()
    transactions = connection.execute(
        """SELECT t.id, t.transaction_type, t.amount, t.note, t.created_at, g.name AS goal_name
           FROM savings_transactions AS t
           JOIN savings_goals AS g ON g.id = t.goal_id AND g.user_id = t.user_id
           WHERE t.user_id = ? ORDER BY t.id DESC LIMIT 30""",
        (user_id,)
    ).fetchall()
    investments = connection.execute(
        "SELECT * FROM investments WHERE user_id = ? ORDER BY id DESC", (user_id,)
    ).fetchall()
    portfolio = connection.execute(
        """SELECT COALESCE(SUM(amount_invested), 0) AS invested,
                  COALESCE(SUM(current_value), 0) AS current_value
           FROM investments WHERE user_id = ?""",
        (user_id,)
    ).fetchone()
    connection.close()

    goals = [dict(goal, progress=min(100, max(0, goal["balance"] / goal["target_amount"] * 100)))
             for goal in goals]
    total_savings = sum(goal["balance"] for goal in goals)
    investment_gain = portfolio["current_value"] - portfolio["invested"]
    return render_template(
        "savings_investments.html",
        goals=goals,
        transactions=transactions,
        investments=investments,
        investment_types=INVESTMENT_TYPES,
        total_savings=total_savings,
        portfolio=portfolio,
        investment_gain=investment_gain
    )


@app.route("/investments/market", methods=["POST"])
@login_required
def add_market_investment():
    name = request.form.get("name", "").strip()
    symbol = request.form.get("symbol", "").strip().upper()
    exchange = request.form.get("exchange", "")
    investment_type = request.form.get("investment_type", "Shares")
    try:
        quantity = float(request.form.get("quantity", ""))
        amount_invested = float(request.form.get("amount_invested", ""))
        stock_exchange = exchange in {"XJSE", "NASDAQ", "NYSE", "AMEX"}
        crypto_exchange = exchange in {"Binance", "Coinbase Pro"}
        crypto_pair = symbol.split("/")
        valid_crypto_symbol = (
            len(crypto_pair) == 2 and crypto_pair[0].isalnum() and
            crypto_pair[1] in {"USD", "ZAR"}
        )
        valid_instrument = (
            (investment_type in {"Shares", "ETF"} and stock_exchange) or
            (investment_type == "Crypto" and crypto_exchange and valid_crypto_symbol)
        )
        if (not isfinite(quantity) or quantity <= 0 or not isfinite(amount_invested) or
                amount_invested <= 0 or not valid_instrument):
            raise ValueError
    except ValueError:
        flash("Check the ticker, exchange, units, and invested amount. Crypto symbols should look like BTC/USD.", "error")
        return redirect(url_for("savings_investments"))
    if not name or len(name) > 80 or not symbol or len(symbol) > 20:
        flash("Enter a name and ticker symbol (up to 80 and 20 characters).", "error")
        return redirect(url_for("savings_investments"))

    connection = get_db_connection()
    connection.execute(
        """INSERT INTO investments
           (user_id, name, investment_type, amount_invested, current_value,
            pricing_mode, symbol, exchange, quantity)
           VALUES (?, ?, ?, ?, ?, 'market', ?, ?, ?)""",
        (session["user_id"], name, investment_type,
         amount_invested, amount_invested, symbol, exchange, quantity)
    )
    connection.commit()
    connection.close()
    flash("Market holding added. Refresh its quote to get the latest available price.", "success")
    return redirect(url_for("savings_investments"))


def refresh_market_investment(connection, investment):
    is_crypto = investment["investment_type"] == "Crypto"
    quote = market_data(
        "price" if is_crypto else "quote",
        {"symbol": investment["symbol"], "exchange": investment["exchange"]}
    )
    try:
        price = float(quote.get("price") if is_crypto else (quote.get("close") or quote.get("price") or ""))
        if not isfinite(price) or price < 0:
            raise ValueError
    except (TypeError, ValueError) as error:
        raise RuntimeError("The price service returned an invalid quote for this symbol.") from error
    currency = (
        investment["symbol"].rsplit("/", 1)[1]
        if is_crypto else (quote.get("currency") or quote.get("currency_code") or "")
    ).upper()
    if currency == "ZAR":
        zar_rate = 1.0
    elif currency == "ZAC":
        # JSE ordinary shares are commonly quoted in South African cents.
        zar_rate = 0.01
    elif currency == "USD":
        exchange_rate = market_data("exchange_rate", {"symbol": "USD/ZAR"})
        try:
            zar_rate = float(exchange_rate.get("rate", ""))
            if not isfinite(zar_rate) or zar_rate <= 0:
                raise ValueError
        except (TypeError, ValueError) as error:
            raise RuntimeError("Could not convert the US dollar quote to rand.") from error
    else:
        raise RuntimeError(f"Quotes in {currency or 'an unknown currency'} are not supported yet.")

    current_value = price * investment["quantity"] * zar_rate
    connection.execute(
        """UPDATE investments
           SET quote_price = ?, quote_currency = ?, current_value = ?,
               quote_updated_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
           WHERE id = ? AND user_id = ?""",
        (price, currency, current_value, investment["id"], investment["user_id"])
    )


@app.route("/investments/<int:investment_id>/refresh", methods=["POST"])
@login_required
def refresh_investment(investment_id):
    connection = get_db_connection()
    investment = connection.execute(
        "SELECT * FROM investments WHERE id = ? AND user_id = ? AND pricing_mode = 'market'",
        (investment_id, session["user_id"])
    ).fetchone()
    if not investment:
        connection.close()
        flash("Market holding not found.", "error")
        return redirect(url_for("savings_investments"))
    try:
        refresh_market_investment(connection, investment)
        connection.commit()
        flash("Latest available market quote refreshed.", "success")
    except RuntimeError as error:
        connection.rollback()
        flash(str(error), "error")
    finally:
        connection.close()
    return redirect(url_for("savings_investments"))


@app.route("/savings/goals", methods=["POST"])
@login_required
def add_savings_goal():
    name = request.form.get("name", "").strip()
    target_date = request.form.get("target_date", "").strip() or None
    try:
        target_amount = float(request.form.get("target_amount", ""))
        opening_balance = float(request.form.get("opening_balance", "0") or "0")
        if (not isfinite(target_amount) or target_amount <= 0 or
                not isfinite(opening_balance) or opening_balance < 0):
            raise ValueError
        if target_date:
            date.fromisoformat(target_date)
    except ValueError:
        flash("Enter a valid target, starting balance, and target date.", "error")
        return redirect(url_for("savings_investments"))
    if not name or len(name) > 80:
        flash("Goal names must be between 1 and 80 characters.", "error")
        return redirect(url_for("savings_investments"))

    connection = get_db_connection()
    cursor = connection.execute(
        "INSERT INTO savings_goals (user_id, name, target_amount, target_date) VALUES (?, ?, ?, ?)",
        (session["user_id"], name, target_amount, target_date)
    )
    if opening_balance > 0:
        connection.execute(
            """INSERT INTO savings_transactions
               (user_id, goal_id, transaction_type, amount, note)
               VALUES (?, ?, 'deposit', ?, 'Starting balance')""",
            (session["user_id"], cursor.lastrowid, opening_balance)
        )
    connection.commit()
    connection.close()
    flash("Savings goal added.", "success")
    return redirect(url_for("savings_investments"))


@app.route("/savings/transactions", methods=["POST"])
@app.route("/savings/goals/<int:goal_id>/transactions", methods=["POST"])
@login_required
def add_savings_transaction(goal_id=None):
    if goal_id is None:
        try:
            goal_id = int(request.form.get("goal_id", ""))
        except ValueError:
            flash("Choose a savings goal first.", "error")
            return redirect(url_for("savings_investments"))
    transaction_type = request.form.get("transaction_type", "")
    note = request.form.get("note", "").strip()[:120]
    try:
        amount = float(request.form.get("amount", ""))
        if transaction_type not in {"deposit", "withdrawal"} or not isfinite(amount) or amount <= 0:
            raise ValueError
    except ValueError:
        flash("Enter a valid deposit or withdrawal amount.", "error")
        return redirect(url_for("savings_investments"))

    connection = get_db_connection()
    connection.execute("BEGIN IMMEDIATE")
    goal = connection.execute(
        "SELECT id FROM savings_goals WHERE id = ? AND user_id = ?",
        (goal_id, session["user_id"])
    ).fetchone()
    if not goal:
        connection.rollback()
        connection.close()
        flash("Savings goal not found.", "error")
        return redirect(url_for("savings_investments"))

    balance = connection.execute(
        """SELECT COALESCE(SUM(CASE WHEN transaction_type = 'deposit'
                                    THEN amount ELSE -amount END), 0)
           FROM savings_transactions WHERE goal_id = ? AND user_id = ?""",
        (goal_id, session["user_id"])
    ).fetchone()[0]
    if transaction_type == "withdrawal" and amount > balance:
        connection.rollback()
        connection.close()
        flash("A withdrawal can’t be greater than this goal’s saved balance.", "error")
        return redirect(url_for("savings_investments"))

    connection.execute(
        """INSERT INTO savings_transactions
           (user_id, goal_id, transaction_type, amount, note) VALUES (?, ?, ?, ?, ?)""",
        (session["user_id"], goal_id, transaction_type, amount, note)
    )
    connection.commit()
    connection.close()
    flash("Savings activity recorded.", "success")
    return redirect(url_for("savings_investments"))


@app.route("/savings/transactions/<int:transaction_id>/delete", methods=["POST"])
@login_required
def delete_savings_transaction(transaction_id):
    connection = get_db_connection()
    connection.execute("BEGIN IMMEDIATE")
    transaction = connection.execute(
        """SELECT goal_id, transaction_type, amount FROM savings_transactions
           WHERE id = ? AND user_id = ?""",
        (transaction_id, session["user_id"])
    ).fetchone()
    if not transaction:
        connection.rollback()
        connection.close()
        flash("Savings activity not found.", "error")
        return redirect(url_for("savings_investments"))

    if transaction["transaction_type"] == "deposit":
        balance = connection.execute(
            """SELECT COALESCE(SUM(CASE WHEN transaction_type = 'deposit'
                                        THEN amount ELSE -amount END), 0)
               FROM savings_transactions WHERE goal_id = ? AND user_id = ?""",
            (transaction["goal_id"], session["user_id"])
        ).fetchone()[0]
        if transaction["amount"] > balance:
            connection.rollback()
            connection.close()
            flash("Remove or adjust later withdrawals before removing this deposit.", "error")
            return redirect(url_for("savings_investments"))

    cursor = connection.execute(
        "DELETE FROM savings_transactions WHERE id = ? AND user_id = ?",
        (transaction_id, session["user_id"])
    )
    connection.commit()
    connection.close()
    flash("Savings activity removed." if cursor.rowcount else "Savings activity not found.",
          "success" if cursor.rowcount else "error")
    return redirect(url_for("savings_investments"))


@app.route("/savings/goals/<int:goal_id>/delete", methods=["POST"])
@login_required
def delete_savings_goal(goal_id):
    connection = get_db_connection()
    cursor = connection.execute(
        "DELETE FROM savings_goals WHERE id = ? AND user_id = ?",
        (goal_id, session["user_id"])
    )
    connection.commit()
    connection.close()
    flash("Savings goal and its activity removed." if cursor.rowcount else "Savings goal not found.",
          "success" if cursor.rowcount else "error")
    return redirect(url_for("savings_investments"))


@app.route("/investments", methods=["POST"])
@login_required
def add_investment():
    name = request.form.get("name", "").strip()
    investment_type = request.form.get("investment_type", "")
    try:
        amount_invested = float(request.form.get("amount_invested", ""))
        current_value = float(request.form.get("current_value", ""))
        if (not isfinite(amount_invested) or amount_invested <= 0 or
                not isfinite(current_value) or current_value < 0 or
                investment_type not in INVESTMENT_TYPES):
            raise ValueError
    except ValueError:
        flash("Enter valid invested and current values, and choose an investment type.", "error")
        return redirect(url_for("savings_investments"))
    if not name or len(name) > 80:
        flash("Investment names must be between 1 and 80 characters.", "error")
        return redirect(url_for("savings_investments"))

    connection = get_db_connection()
    connection.execute(
        """INSERT INTO investments
           (user_id, name, investment_type, amount_invested, current_value)
           VALUES (?, ?, ?, ?, ?)""",
        (session["user_id"], name, investment_type, amount_invested, current_value)
    )
    connection.commit()
    connection.close()
    flash("Investment added to your tracker.", "success")
    return redirect(url_for("savings_investments"))


@app.route("/investments/<int:investment_id>/value", methods=["POST"])
@login_required
def update_investment_value(investment_id):
    try:
        current_value = float(request.form.get("current_value", ""))
        if not isfinite(current_value) or current_value < 0:
            raise ValueError
    except ValueError:
        flash("Enter a valid current value.", "error")
        return redirect(url_for("savings_investments"))

    connection = get_db_connection()
    cursor = connection.execute(
        "UPDATE investments SET current_value = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ? AND pricing_mode = 'manual'",
        (current_value, investment_id, session["user_id"])
    )
    connection.commit()
    connection.close()
    flash("Investment value updated." if cursor.rowcount else "Investment not found.",
          "success" if cursor.rowcount else "error")
    return redirect(url_for("savings_investments"))


@app.route("/investments/<int:investment_id>/delete", methods=["POST"])
@login_required
def delete_investment(investment_id):
    connection = get_db_connection()
    cursor = connection.execute(
        "DELETE FROM investments WHERE id = ? AND user_id = ?",
        (investment_id, session["user_id"])
    )
    connection.commit()
    connection.close()
    flash("Investment removed." if cursor.rowcount else "Investment not found.",
          "success" if cursor.rowcount else "error")
    return redirect(url_for("savings_investments"))

@app.route("/set-budget", methods=["POST"])
@login_required
def set_budget():
    starting_amount_text = request.form.get("starting_amount", "").strip()

    try:
        starting_amount = float(starting_amount_text)
        if starting_amount < 0:
            raise ValueError
    except ValueError:
        return "Please enter a valid starting amount.", 400

    connection = get_db_connection()
    connection.execute(
        "UPDATE user_budgets SET starting_amount = ? WHERE user_id = ?",
        (starting_amount, session["user_id"])
    )
    connection.commit()
    connection.close()

    return redirect("/")

# Receive and save expenses
@app.route("/add-expense", methods=["POST"])
@login_required
def add_expense():
    description = request.form.get("description", "").strip()
    amount_text = request.form.get("amount", "").strip()
    category = request.form.get("category", "").strip()
    month = date.today().strftime("%Y-%m")

    # Validate the submitted information
    try:
        amount = float(amount_text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        return "Please enter a valid amount greater than zero.", 400

    if not description:
        return "Please enter a valid description and category.", 400

    connection = get_db_connection()
    category_exists = connection.execute(
        "SELECT 1 FROM user_categories WHERE user_id = ? AND name = ?",
        (session["user_id"], category)
    ).fetchone()
    if not category_exists:
        connection.close()
        return "Please choose one of your saved categories.", 400

    connection.execute(
        """INSERT INTO expenses (description, amount, category, month, user_id)
           VALUES (?, ?, ?, ?, ?)""",
        (description, amount, category, month, session["user_id"])
    )

    connection.execute("""
        CREATE TABLE IF NOT EXISTS budget (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            starting_amount REAL NOT NULL DEFAULT 0
        )
    """)

    connection.execute("""
        INSERT OR IGNORE INTO budget (id, starting_amount)
        VALUES (1, 0)
    """)

    connection.commit()
    connection.close()

    return render_template(
        "expense_saved.html",
        description=description,
        amount=amount,
        category=category
    )

@app.route("/clear-expenses", methods=["POST"])
@login_required
def clear_expenses():
    month = request.form.get("month", date.today().strftime("%Y-%m"))
    try:
        date.fromisoformat(month + "-01")
    except ValueError:
        month = date.today().strftime("%Y-%m")

    connection = get_db_connection()
    connection.execute(
        "DELETE FROM expenses WHERE user_id = ? AND month = ?",
        (session["user_id"], month)
    )
    connection.commit()
    connection.close()

    return redirect(url_for("home", month=month))


@app.route("/delete-expense/<int:expense_id>", methods=["POST"])
@login_required
def delete_expense(expense_id):
    month = request.form.get("month", date.today().strftime("%Y-%m"))
    try:
        date.fromisoformat(month + "-01")
    except ValueError:
        month = date.today().strftime("%Y-%m")

    connection = get_db_connection()
    cursor = connection.execute(
        "DELETE FROM expenses WHERE id = ? AND user_id = ?",
        (expense_id, session["user_id"])
    )
    connection.commit()
    connection.close()

    if cursor.rowcount:
        flash("Expense removed.", "success")
    else:
        flash("That expense was already removed or could not be found.", "error")

    return redirect(url_for("home", month=month))


@app.route("/settings")
@login_required
def settings():
    connection = get_db_connection()
    categories = connection.execute(
        "SELECT id, name FROM user_categories WHERE user_id = ? ORDER BY name COLLATE NOCASE",
        (session["user_id"],)
    ).fetchall()
    connection.close()
    return render_template("settings.html", categories=categories)


@app.route("/settings/clear-data", methods=["POST"])
@login_required
def clear_account_data():
    if request.form.get("confirmation", "").strip() != "CLEAR":
        flash("Type CLEAR exactly to confirm removing your tracker data.", "error")
        return redirect(url_for("settings"))

    user_id = session["user_id"]
    connection = get_db_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM expenses WHERE user_id = ?", (user_id,))
        connection.execute("DELETE FROM savings_transactions WHERE user_id = ?", (user_id,))
        connection.execute("DELETE FROM savings_goals WHERE user_id = ?", (user_id,))
        connection.execute("DELETE FROM investments WHERE user_id = ?", (user_id,))
        connection.execute("DELETE FROM user_categories WHERE user_id = ?", (user_id,))
        connection.execute(
            "UPDATE user_budgets SET starting_amount = 0 WHERE user_id = ?", (user_id,)
        )
        connection.execute(
            "INSERT OR IGNORE INTO user_budgets (user_id, starting_amount) VALUES (?, 0)",
            (user_id,)
        )
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        flash("Could not clear your tracker data. Nothing was changed; please try again.", "error")
    else:
        flash("All tracker data cleared, including categories. Your account and login are unchanged.", "success")
    finally:
        connection.close()
    return redirect(url_for("settings"))


@app.route("/settings/theme", methods=["POST"])
@login_required
def update_theme():
    theme = request.form.get("theme", "light")
    if theme not in THEMES:
        flash("Choose a valid appearance theme.", "error")
        return redirect(url_for("settings"))
    connection = get_db_connection()
    connection.execute("UPDATE users SET theme = ? WHERE id = ?", (theme, session["user_id"]))
    connection.commit()
    connection.close()
    session["theme"] = theme
    flash("Appearance saved.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/account", methods=["POST"])
@login_required
def update_account():
    action = request.form.get("action")
    connection = get_db_connection()
    user = connection.execute(
        "SELECT id, password_hash FROM users WHERE id = ?", (session["user_id"],)
    ).fetchone()

    current_password = request.form.get("current_password", "")
    if not user or not check_password_hash(user["password_hash"], current_password):
        flash("Your current password is incorrect.", "error")
        connection.close()
        return redirect(url_for("settings"))

    if action == "profile":
        email = request.form.get("email", "").strip().lower()
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        if not valid_email(email):
            flash("Enter a valid email address.", "error")
        elif not first_name or len(first_name) > 50 or not last_name or len(last_name) > 50:
            flash("Enter your first name and surname (up to 50 characters each).", "error")
        else:
            try:
                connection.execute(
                    "UPDATE users SET username = ?, first_name = ?, last_name = ? WHERE id = ?",
                    (email, first_name, last_name, session["user_id"])
                )
                connection.commit()
                session["username"] = email
                session["first_name"] = first_name
                session["last_name"] = last_name
                flash("Profile updated.", "success")
            except sqlite3.IntegrityError:
                connection.rollback()
                flash("That email address is already in use.", "error")
    elif action == "password":
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        if len(new_password) < 8:
            flash("Use a new password with at least 8 characters.", "error")
        elif new_password != confirm_password:
            flash("The new passwords do not match.", "error")
        else:
            connection.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(new_password), session["user_id"])
            )
            connection.commit()
            flash("Password updated.", "success")
    else:
        flash("Choose a valid account setting.", "error")

    connection.close()
    return redirect(url_for("settings"))


@app.route("/settings/categories/add", methods=["POST"])
@login_required
def add_category():
    name = request.form.get("name", "").strip()
    if not name or len(name) > 40:
        flash("Category names must be between 1 and 40 characters.", "error")
    else:
        connection = get_db_connection()
        try:
            connection.execute(
                "INSERT INTO user_categories (user_id, name) VALUES (?, ?)",
                (session["user_id"], name)
            )
            connection.commit()
            flash("Category added.", "success")
        except sqlite3.IntegrityError:
            connection.rollback()
            flash("That category already exists.", "error")
        connection.close()
    return redirect(url_for("settings"))


@app.route("/settings/categories/<int:category_id>/rename", methods=["POST"])
@login_required
def rename_category(category_id):
    name = request.form.get("name", "").strip()
    if not name or len(name) > 40:
        flash("Category names must be between 1 and 40 characters.", "error")
        return redirect(url_for("settings"))

    connection = get_db_connection()
    category = connection.execute(
        "SELECT name FROM user_categories WHERE id = ? AND user_id = ?",
        (category_id, session["user_id"])
    ).fetchone()
    if not category:
        flash("Category not found.", "error")
    else:
        try:
            connection.execute(
                "UPDATE user_categories SET name = ? WHERE id = ? AND user_id = ?",
                (name, category_id, session["user_id"])
            )
            connection.execute(
                "UPDATE expenses SET category = ? WHERE user_id = ? AND category = ?",
                (name, session["user_id"], category["name"])
            )
            connection.commit()
            flash("Category updated.", "success")
        except sqlite3.IntegrityError:
            connection.rollback()
            flash("That category already exists.", "error")
    connection.close()
    return redirect(url_for("settings"))


@app.route("/settings/categories/<int:category_id>/delete", methods=["POST"])
@login_required
def remove_category(category_id):
    connection = get_db_connection()
    user_id = session["user_id"]
    connection.execute("BEGIN IMMEDIATE")
    category = connection.execute(
        "SELECT name FROM user_categories WHERE id = ? AND user_id = ?",
        (category_id, user_id)
    ).fetchone()
    if category:
        fallback = connection.execute(
            """SELECT name FROM user_categories
               WHERE user_id = ? AND id != ?
               ORDER BY CASE WHEN name COLLATE NOCASE = 'Other' THEN 0 ELSE 1 END,
                        name COLLATE NOCASE LIMIT 1""",
            (user_id, category_id)
        ).fetchone()
        replacement = fallback["name"] if fallback else "Uncategorized"
        connection.execute(
            "UPDATE expenses SET category = ? WHERE user_id = ? AND category = ?",
            (replacement, user_id, category["name"])
        )
        cursor = connection.execute(
            "DELETE FROM user_categories WHERE id = ? AND user_id = ?",
            (category_id, user_id)
        )
    else:
        cursor = None
    connection.commit()
    connection.close()
    flash("Category removed. Existing expenses were reassigned so the category no longer appears." if cursor and cursor.rowcount else "Category not found.",
          "success" if cursor and cursor.rowcount else "error")
    return redirect(url_for("settings"))

create_table()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
