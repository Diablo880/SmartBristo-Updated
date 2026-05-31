"""SmartBistro RMS: self-contained web application and REST API.

Run with:
    python -m src.app

The project intentionally uses only the Python standard library so the
assessment prototype can run on a clean lab machine without dependency setup.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "static"
DB_PATH = Path(os.getenv("SMARTBISTRO_DB", ROOT / "smartbistro.db"))
SECRET = os.getenv("SMARTBISTRO_SECRET", "dev-smartbistro-secret").encode()
TOKEN_TTL_SECONDS = 8 * 60 * 60
STATUSES = ("received", "prepping", "ready", "served", "cancelled")
TABLE_STATUSES = ("available", "occupied", "dirty", "reserved")


class ApiError(Exception):
    """HTTP-aware application exception."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class SmartBistroService:
    """Business layer for ordering, KDS, inventory, analytics, and auth."""

    def __init__(self, db_path: Path | str = DB_PATH):
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL UNIQUE,
                    role TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tables (
                    id INTEGER PRIMARY KEY,
                    label TEXT NOT NULL,
                    seats INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    qr_slug TEXT NOT NULL UNIQUE,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS menu_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    description TEXT NOT NULL,
                    price_cents INTEGER NOT NULL,
                    prep_minutes INTEGER NOT NULL,
                    allergens TEXT NOT NULL DEFAULT '',
                    dietary TEXT NOT NULL DEFAULT '',
                    image_url TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS ingredients (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    unit TEXT NOT NULL,
                    stock REAL NOT NULL,
                    par REAL NOT NULL,
                    opening_stock REAL NOT NULL,
                    purchases REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS recipes (
                    menu_item_id INTEGER NOT NULL REFERENCES menu_items(id),
                    ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
                    qty REAL NOT NULL,
                    PRIMARY KEY (menu_item_id, ingredient_id)
                );
                CREATE TABLE IF NOT EXISTS customers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    contact TEXT NOT NULL UNIQUE,
                    loyalty_points INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    table_id INTEGER NOT NULL REFERENCES tables(id),
                    customer_id INTEGER REFERENCES customers(id),
                    status TEXT NOT NULL,
                    subtotal_cents INTEGER NOT NULL,
                    discount_cents INTEGER NOT NULL DEFAULT 0,
                    total_cents INTEGER NOT NULL,
                    payment_status TEXT NOT NULL,
                    receipt_contact TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS order_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    menu_item_id INTEGER NOT NULL REFERENCES menu_items(id),
                    qty INTEGER NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    modifiers TEXT NOT NULL DEFAULT '',
                    price_cents INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES orders(id),
                    method TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    receipt_target TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stock_movements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
                    order_id INTEGER REFERENCES orders(id),
                    kind TEXT NOT NULL,
                    qty REAL NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    acknowledged INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
                self.seed(conn)
            self.ensure_menu_image_urls(conn)

    def seed(self, conn: sqlite3.Connection) -> None:
        now = utcnow()
        users = [
            ("Manager", "manager@smartbistro.test", "manager", hash_password("manager123"), now),
            ("Kitchen Lead", "kitchen@smartbistro.test", "kitchen", hash_password("kitchen123"), now),
            ("FOH Staff", "staff@smartbistro.test", "staff", hash_password("staff123"), now),
        ]
        conn.executemany(
            "INSERT INTO users(name,email,role,password_hash,created_at) VALUES (?,?,?,?,?)",
            users,
        )
        for table_id in range(1, 13):
            seats = 2 if table_id <= 4 else 4 if table_id <= 10 else 6
            status = "available" if table_id not in (3, 7, 11) else "reserved"
            conn.execute(
                "INSERT INTO tables(id,label,seats,status,qr_slug,updated_at) VALUES (?,?,?,?,?,?)",
                (table_id, f"T{table_id}", seats, status, f"table-{table_id}", now),
            )

        ingredients = [
            ("Pasta", "g", 8000, 1500, 8000),
            ("Tomato sauce", "ml", 6000, 1200, 6000),
            ("Mozzarella", "g", 3500, 800, 3500),
            ("Chicken breast", "g", 5000, 1000, 5000),
            ("Beef patty", "each", 42, 10, 42),
            ("Burger bun", "each", 48, 12, 48),
            ("Lettuce", "g", 2400, 500, 2400),
            ("Rice", "g", 7000, 1200, 7000),
            ("Salmon", "g", 4200, 900, 4200),
            ("Coffee beans", "g", 2500, 600, 2500),
        ]
        conn.executemany(
            "INSERT INTO ingredients(name,unit,stock,par,opening_stock) VALUES (?,?,?,?,?)",
            ingredients,
        )
        menu = [
            ("Margherita Pizza", "Mains", "San Marzano tomato, mozzarella, basil.", 1890, 14, "gluten,dairy", "vegetarian", "/assets/pizza.png"),
            ("Chicken Alfredo", "Mains", "Creamy pasta with grilled chicken and parmesan.", 2290, 16, "gluten,dairy", "", "/assets/pasta.png"),
            ("Bistro Burger", "Mains", "Beef patty, lettuce, cheese, house sauce.", 2190, 12, "gluten,dairy", "", "/assets/burger.png"),
            ("Salmon Rice Bowl", "Mains", "Grilled salmon, rice, greens, citrus dressing.", 2490, 15, "fish", "high-protein", "/assets/salmon.png"),
            ("Garden Salad", "Sides", "Crisp lettuce, tomato, herbs, vinaigrette.", 1290, 7, "", "vegan,gluten-free", "/assets/salad.png"),
            ("Espresso", "Drinks", "Double-shot espresso using house beans.", 450, 3, "", "vegan,gluten-free", "/assets/coffee.png"),
        ]
        conn.executemany(
            """
            INSERT INTO menu_items(name,category,description,price_cents,prep_minutes,allergens,dietary,image_url)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            menu,
        )
        ingredient_ids = {r["name"]: r["id"] for r in conn.execute("SELECT id,name FROM ingredients")}
        menu_ids = {r["name"]: r["id"] for r in conn.execute("SELECT id,name FROM menu_items")}
        recipes = [
            ("Margherita Pizza", "Tomato sauce", 100), ("Margherita Pizza", "Mozzarella", 120), ("Margherita Pizza", "Pasta", 80),
            ("Chicken Alfredo", "Pasta", 180), ("Chicken Alfredo", "Chicken breast", 160), ("Chicken Alfredo", "Mozzarella", 60),
            ("Bistro Burger", "Beef patty", 1), ("Bistro Burger", "Burger bun", 1), ("Bistro Burger", "Lettuce", 60), ("Bistro Burger", "Mozzarella", 40),
            ("Salmon Rice Bowl", "Salmon", 170), ("Salmon Rice Bowl", "Rice", 220), ("Salmon Rice Bowl", "Lettuce", 50),
            ("Garden Salad", "Lettuce", 180),
            ("Espresso", "Coffee beans", 20),
        ]
        conn.executemany(
            "INSERT INTO recipes(menu_item_id,ingredient_id,qty) VALUES (?,?,?)",
            [(menu_ids[item], ingredient_ids[ing], qty) for item, ing, qty in recipes],
        )

    def ensure_menu_image_urls(self, conn: sqlite3.Connection) -> None:
        image_urls = {
            "Margherita Pizza": "/assets/pizza.png",
            "Chicken Alfredo": "/assets/pasta.png",
            "Bistro Burger": "/assets/burger.png",
            "Salmon Rice Bowl": "/assets/salmon.png",
            "Garden Salad": "/assets/salad.png",
            "Espresso": "/assets/coffee.png",
        }
        conn.executemany(
            "UPDATE menu_items SET image_url = ? WHERE name = ?",
            [(url, name) for name, url in image_urls.items()],
        )

    def login(self, email: str, password: str) -> dict[str, Any]:
        with self.connect() as conn:
            user = conn.execute("SELECT * FROM users WHERE email = ?", (email.lower(),)).fetchone()
        if not user or not verify_password(password, user["password_hash"]):
            raise ApiError(HTTPStatus.UNAUTHORIZED, "Invalid email or password")
        return {
            "token": create_token(user["id"], user["role"]),
            "user": row_to_dict(user, exclude={"password_hash"}),
        }

    def require_user(self, auth_header: str | None, roles: tuple[str, ...] = ()) -> dict[str, Any]:
        if not auth_header or not auth_header.startswith("Bearer "):
            raise ApiError(HTTPStatus.UNAUTHORIZED, "Missing Bearer token")
        payload = verify_token(auth_header.removeprefix("Bearer ").strip())
        if roles and payload["role"] not in roles:
            raise ApiError(HTTPStatus.FORBIDDEN, "This role cannot access that action")
        return payload

    def menu_for_table(self, table_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            table = conn.execute("SELECT * FROM tables WHERE id = ?", (table_id,)).fetchone()
            if not table:
                raise ApiError(HTTPStatus.NOT_FOUND, "Table not found")
            items = [self.menu_item_payload(row) for row in conn.execute("SELECT * FROM menu_items ORDER BY category,name")]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            grouped.setdefault(item["category"], []).append(item)
        return {"table": row_to_dict(table), "categories": grouped, "items": items}

    def all_menu_items(self) -> dict[str, Any]:
        with self.connect() as conn:
            items = [self.menu_item_payload(row) for row in conn.execute("SELECT * FROM menu_items ORDER BY category,name")]
        return {"items": items}

    def save_menu_item(self, payload: dict[str, Any], menu_item_id: int | None = None) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        category = str(payload.get("category") or "Mains").strip()
        description = str(payload.get("description") or "").strip()
        price_cents = int(round(float(payload.get("price") or 0) * 100))
        prep_minutes = int(payload.get("prep_minutes") or 10)
        allergens = ",".join(payload.get("allergens") or split_csv(str(payload.get("allergens_text") or "")))
        dietary = ",".join(payload.get("dietary") or split_csv(str(payload.get("dietary_text") or "")))
        image_url = str(payload.get("image_url") or "/assets/pizza.png").strip()
        if not name or price_cents <= 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Menu item needs a name and positive price")
        with self.connect() as conn:
            if menu_item_id:
                exists = conn.execute("SELECT id FROM menu_items WHERE id = ?", (menu_item_id,)).fetchone()
                if not exists:
                    raise ApiError(HTTPStatus.NOT_FOUND, "Menu item not found")
                conn.execute(
                    """
                    UPDATE menu_items
                    SET name = ?, category = ?, description = ?, price_cents = ?, prep_minutes = ?, allergens = ?, dietary = ?, image_url = ?
                    WHERE id = ?
                    """,
                    (name, category, description, price_cents, prep_minutes, allergens, dietary, image_url, menu_item_id),
                )
            else:
                cur = conn.execute(
                    """
                    INSERT INTO menu_items(name,category,description,price_cents,prep_minutes,allergens,dietary,image_url)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (name, category, description, price_cents, prep_minutes, allergens, dietary, image_url),
                )
                menu_item_id = int(cur.lastrowid)
            row = conn.execute("SELECT * FROM menu_items WHERE id = ?", (menu_item_id,)).fetchone()
            return self.menu_item_payload(row)

    def delete_menu_item(self, menu_item_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            used = conn.execute("SELECT id FROM order_items WHERE menu_item_id = ? LIMIT 1", (menu_item_id,)).fetchone()
            if used:
                raise ApiError(HTTPStatus.CONFLICT, "This item has order history; edit it instead of deleting")
            conn.execute("DELETE FROM recipes WHERE menu_item_id = ?", (menu_item_id,))
            cur = conn.execute("DELETE FROM menu_items WHERE id = ?", (menu_item_id,))
            if cur.rowcount == 0:
                raise ApiError(HTTPStatus.NOT_FOUND, "Menu item not found")
            return {"ok": True}

    def menu_item_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        data = row_to_dict(row)
        data["price"] = cents_to_money(data.pop("price_cents"))
        data["allergens"] = split_csv(data["allergens"])
        data["dietary"] = split_csv(data["dietary"])
        return data

    def create_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        table_id = int(payload.get("table_id", 0))
        items = payload.get("items") or []
        if not table_id or not items:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Order needs a table and at least one item")
        customer_payload = payload.get("customer") or {}
        receipt_contact = (customer_payload.get("contact") or payload.get("receipt_contact") or "").strip()
        redeem_points = int(payload.get("redeem_points") or 0)
        now = utcnow()
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            table = conn.execute("SELECT * FROM tables WHERE id = ?", (table_id,)).fetchone()
            if not table:
                raise ApiError(HTTPStatus.NOT_FOUND, "Table not found")
            normalized_items = self.validate_items(conn, items)
            self.assert_stock_available(conn, normalized_items)
            customer_id = self.upsert_customer(conn, customer_payload) if receipt_contact else None
            subtotal = sum(item["price_cents"] * item["qty"] for item in normalized_items)
            available_points = 0
            if customer_id:
                available_points = conn.execute("SELECT loyalty_points FROM customers WHERE id = ?", (customer_id,)).fetchone()[0]
            points_to_use = min(max(redeem_points, 0), available_points, subtotal // 100)
            discount = points_to_use * 100
            total = max(subtotal - discount, 0)
            cur = conn.execute(
                """
                INSERT INTO orders(table_id,customer_id,status,subtotal_cents,discount_cents,total_cents,payment_status,receipt_contact,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (table_id, customer_id, "received", subtotal, discount, total, "paid", receipt_contact, now, now),
            )
            order_id = cur.lastrowid
            for item in normalized_items:
                conn.execute(
                    """
                    INSERT INTO order_items(order_id,menu_item_id,qty,notes,modifiers,price_cents)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (order_id, item["menu_item_id"], item["qty"], item["notes"], item["modifiers"], item["price_cents"]),
                )
            self.deduct_stock(conn, order_id, normalized_items)
            if customer_id:
                earned = total // 100
                conn.execute(
                    "UPDATE customers SET loyalty_points = loyalty_points - ? + ? WHERE id = ?",
                    (points_to_use, earned, customer_id),
                )
            conn.execute(
                "INSERT INTO payments(order_id,method,amount_cents,status,receipt_target,created_at) VALUES (?,?,?,?,?,?)",
                (order_id, payload.get("payment_method", "table-card"), total, "approved", receipt_contact, now),
            )
            conn.execute("UPDATE tables SET status = 'occupied', updated_at = ? WHERE id = ?", (now, table_id))
            conn.execute("COMMIT")
        order_payload = self.get_order(order_id)
        if customer_id:
            order_payload["loyalty"] = self.customer_loyalty_by_id(customer_id)
        return order_payload

    def customer_loyalty_by_id(self, customer_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            customer = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
            if not customer:
                raise ApiError(HTTPStatus.NOT_FOUND, "Customer not found")
            return row_to_dict(customer)

    def customer_loyalty(self, contact: str) -> dict[str, Any]:
        if not contact.strip():
            raise ApiError(HTTPStatus.BAD_REQUEST, "Contact is required")
        with self.connect() as conn:
            customer = conn.execute("SELECT * FROM customers WHERE contact = ?", (contact.strip(),)).fetchone()
            if not customer:
                return {"name": "Guest", "contact": contact.strip(), "loyalty_points": 0}
            return row_to_dict(customer)

    def validate_items(self, conn: sqlite3.Connection, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized = []
        for raw in items:
            menu_item_id = int(raw.get("menu_item_id") or raw.get("id") or 0)
            qty = int(raw.get("qty") or 0)
            if menu_item_id <= 0 or qty <= 0:
                raise ApiError(HTTPStatus.BAD_REQUEST, "Each order item needs menu_item_id and positive qty")
            item = conn.execute("SELECT * FROM menu_items WHERE id = ?", (menu_item_id,)).fetchone()
            if not item:
                raise ApiError(HTTPStatus.NOT_FOUND, f"Menu item {menu_item_id} was not found")
            normalized.append(
                {
                    "menu_item_id": menu_item_id,
                    "qty": qty,
                    "price_cents": item["price_cents"],
                    "notes": str(raw.get("notes") or ""),
                    "modifiers": ",".join(raw.get("modifiers") or []),
                    "name": item["name"],
                }
            )
        return normalized

    def assert_stock_available(self, conn: sqlite3.Connection, items: list[dict[str, Any]]) -> None:
        required: dict[int, float] = {}
        for item in items:
            for recipe in conn.execute("SELECT ingredient_id,qty FROM recipes WHERE menu_item_id = ?", (item["menu_item_id"],)):
                required[recipe["ingredient_id"]] = required.get(recipe["ingredient_id"], 0) + recipe["qty"] * item["qty"]
        for ingredient_id, qty in required.items():
            ingredient = conn.execute("SELECT * FROM ingredients WHERE id = ?", (ingredient_id,)).fetchone()
            if ingredient["stock"] < qty:
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    f"Insufficient {ingredient['name']}: need {qty:g}{ingredient['unit']}, have {ingredient['stock']:g}{ingredient['unit']}",
                )

    def deduct_stock(self, conn: sqlite3.Connection, order_id: int, items: list[dict[str, Any]]) -> None:
        now = utcnow()
        required: dict[int, float] = {}
        for item in items:
            for recipe in conn.execute("SELECT ingredient_id,qty FROM recipes WHERE menu_item_id = ?", (item["menu_item_id"],)):
                required[recipe["ingredient_id"]] = required.get(recipe["ingredient_id"], 0) + recipe["qty"] * item["qty"]
        for ingredient_id, qty in required.items():
            conn.execute("UPDATE ingredients SET stock = stock - ? WHERE id = ?", (qty, ingredient_id))
            conn.execute(
                "INSERT INTO stock_movements(ingredient_id,order_id,kind,qty,reason,created_at) VALUES (?,?,?,?,?,?)",
                (ingredient_id, order_id, "sale", qty, "automatic order deduction", now),
            )
        self.create_low_stock_alerts(conn)

    def upsert_customer(self, conn: sqlite3.Connection, payload: dict[str, Any]) -> int | None:
        contact = (payload.get("contact") or "").strip()
        if not contact:
            return None
        name = (payload.get("name") or "Guest").strip()
        existing = conn.execute("SELECT id FROM customers WHERE contact = ?", (contact,)).fetchone()
        if existing:
            conn.execute("UPDATE customers SET name = ? WHERE id = ?", (name, existing["id"]))
            return int(existing["id"])
        cur = conn.execute("INSERT INTO customers(name,contact,loyalty_points) VALUES (?,?,0)", (name, contact))
        return int(cur.lastrowid)

    def get_order(self, order_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if not order:
                raise ApiError(HTTPStatus.NOT_FOUND, "Order not found")
            return self.order_payload(conn, order)

    def order_history(self) -> dict[str, Any]:
        with self.connect() as conn:
            orders = [
                self.order_payload(conn, row)
                for row in conn.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 100")
            ]
        return {"orders": orders}

    def order_payload(self, conn: sqlite3.Connection, order: sqlite3.Row) -> dict[str, Any]:
        items = []
        for row in conn.execute(
            """
            SELECT oi.*, mi.name, mi.category, mi.prep_minutes, mi.allergens, mi.dietary
            FROM order_items oi JOIN menu_items mi ON mi.id = oi.menu_item_id
            WHERE oi.order_id = ?
            """,
            (order["id"],),
        ):
            items.append(
                {
                    "id": row["id"],
                    "menu_item_id": row["menu_item_id"],
                    "name": row["name"],
                    "category": row["category"],
                    "qty": row["qty"],
                    "notes": row["notes"],
                    "modifiers": split_csv(row["modifiers"]),
                    "allergens": split_csv(row["allergens"]),
                    "dietary": split_csv(row["dietary"]),
                    "prep_minutes": row["prep_minutes"],
                    "price": cents_to_money(row["price_cents"]),
                }
            )
        data = row_to_dict(order)
        data["subtotal"] = cents_to_money(data.pop("subtotal_cents"))
        data["discount"] = cents_to_money(data.pop("discount_cents"))
        data["total"] = cents_to_money(data.pop("total_cents"))
        data["items"] = items
        data["max_prep_minutes"] = max([item["prep_minutes"] for item in items], default=0)
        return data

    def kds_orders(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            orders = [
                self.order_payload(conn, row)
                for row in conn.execute("SELECT * FROM orders WHERE status IN ('received','prepping','ready') ORDER BY created_at")
            ]
        return sorted(orders, key=lambda order: (-order["max_prep_minutes"], order["created_at"]))

    def update_order_status(self, order_id: int, status: str) -> dict[str, Any]:
        if status not in STATUSES:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Invalid order status")
        now = utcnow()
        with self.connect() as conn:
            order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if not order:
                raise ApiError(HTTPStatus.NOT_FOUND, "Order not found")
            conn.execute("UPDATE orders SET status = ?, updated_at = ? WHERE id = ?", (status, now, order_id))
            if status == "served":
                conn.execute("UPDATE tables SET status = 'dirty', updated_at = ? WHERE id = ?", (now, order["table_id"]))
        return self.get_order(order_id)

    def tables(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [row_to_dict(row) for row in conn.execute("SELECT * FROM tables ORDER BY id")]

    def update_table(self, table_id: int, status: str) -> dict[str, Any]:
        if status not in TABLE_STATUSES:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Invalid table status")
        with self.connect() as conn:
            table = conn.execute("SELECT * FROM tables WHERE id = ?", (table_id,)).fetchone()
            if not table:
                raise ApiError(HTTPStatus.NOT_FOUND, "Table not found")
            conn.execute("UPDATE tables SET status = ?, updated_at = ? WHERE id = ?", (status, utcnow(), table_id))
            return row_to_dict(conn.execute("SELECT * FROM tables WHERE id = ?", (table_id,)).fetchone())

    def table_qr_svg(self, table_id: int, base_url: str) -> str:
        table = next((t for t in self.tables() if t["id"] == table_id), None)
        if not table:
            raise ApiError(HTTPStatus.NOT_FOUND, "Table not found")
        url = f"{base_url.rstrip('/')}/?table={table_id}"
        digest = hashlib.sha256(url.encode()).digest()
        cells = []
        for y in range(21):
            for x in range(21):
                finder = (x < 7 and y < 7) or (x > 13 and y < 7) or (x < 7 and y > 13)
                bit = digest[(x + y * 21) % len(digest)] & (1 << (x % 8))
                if finder or bit:
                    cells.append(f'<rect x="{x}" y="{y}" width="1" height="1"/>')
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 21 21" shape-rendering="crispEdges">'
            '<rect width="21" height="21" fill="white"/>'
            '<g fill="#111827">' + "".join(cells) + "</g></svg>"
        )

    def inventory(self) -> dict[str, Any]:
        with self.connect() as conn:
            ingredients = [row_to_dict(row) for row in conn.execute("SELECT * FROM ingredients ORDER BY name")]
            alerts = [row_to_dict(row) for row in conn.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT 12")]
        for item in ingredients:
            item["low"] = item["stock"] <= item["par"]
        return {"ingredients": ingredients, "alerts": alerts}

    def update_ingredient(self, ingredient_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        stock = float(payload.get("stock"))
        par = float(payload.get("par"))
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM ingredients WHERE id = ?", (ingredient_id,)).fetchone()
            if not row:
                raise ApiError(HTTPStatus.NOT_FOUND, "Ingredient not found")
            conn.execute("UPDATE ingredients SET stock = ?, par = ? WHERE id = ?", (stock, par, ingredient_id))
            self.create_low_stock_alerts(conn)
            return row_to_dict(conn.execute("SELECT * FROM ingredients WHERE id = ?", (ingredient_id,)).fetchone())

    def log_waste(self, payload: dict[str, Any]) -> dict[str, Any]:
        ingredient_id = int(payload.get("ingredient_id") or 0)
        qty = float(payload.get("qty") or 0)
        if ingredient_id <= 0 or qty <= 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Waste entry needs ingredient_id and positive qty")
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM ingredients WHERE id = ?", (ingredient_id,)).fetchone()
            if not row:
                raise ApiError(HTTPStatus.NOT_FOUND, "Ingredient not found")
            conn.execute("UPDATE ingredients SET stock = MAX(stock - ?, 0) WHERE id = ?", (qty, ingredient_id))
            conn.execute(
                "INSERT INTO stock_movements(ingredient_id,kind,qty,reason,created_at) VALUES (?,?,?,?,?)",
                (ingredient_id, "waste", qty, payload.get("reason", "waste"), utcnow()),
            )
            self.create_low_stock_alerts(conn)
            return {"ok": True}

    def create_low_stock_alerts(self, conn: sqlite3.Connection) -> None:
        now = utcnow()
        for row in conn.execute("SELECT * FROM ingredients WHERE stock <= par"):
            message = f"{row['name']} is at {row['stock']:g}{row['unit']} (par {row['par']:g}{row['unit']})"
            exists = conn.execute("SELECT id FROM alerts WHERE acknowledged = 0 AND message = ?", (message,)).fetchone()
            if not exists:
                conn.execute("INSERT INTO alerts(level,message,created_at) VALUES (?,?,?)", ("warning", message, now))

    def analytics_dashboard(self) -> dict[str, Any]:
        with self.connect() as conn:
            orders = [row_to_dict(row) for row in conn.execute("SELECT * FROM orders WHERE payment_status = 'paid'")]
            top = [
                row_to_dict(row)
                for row in conn.execute(
                    """
                    SELECT mi.name, SUM(oi.qty) AS qty, SUM(oi.qty * oi.price_cents) AS revenue_cents
                    FROM order_items oi JOIN menu_items mi ON mi.id = oi.menu_item_id
                    JOIN orders o ON o.id = oi.order_id
                    WHERE o.payment_status = 'paid'
                    GROUP BY mi.id ORDER BY qty DESC LIMIT 6
                    """
                )
            ]
            waste = conn.execute("SELECT COALESCE(SUM(qty),0) FROM stock_movements WHERE kind = 'waste'").fetchone()[0]
            customers = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        revenue = sum(order["total_cents"] for order in orders)
        by_day: dict[str, int] = {}
        heatmap: dict[str, int] = {}
        for order in orders:
            dt = datetime.fromisoformat(order["created_at"])
            by_day[dt.strftime("%a")] = by_day.get(dt.strftime("%a"), 0) + order["total_cents"]
            key = f"{dt.strftime('%a')} {dt.hour:02d}:00"
            heatmap[key] = heatmap.get(key, 0) + 1
        return {
            "summary": {
                "orders": len(orders),
                "revenue": cents_to_money(revenue),
                "average_order": cents_to_money(revenue // len(orders) if orders else 0),
                "loyalty_customers": customers,
                "waste_units": round(float(waste), 2),
            },
            "revenue_trend": [{"label": k, "value": cents_to_money(v)} for k, v in sorted(by_day.items())],
            "heatmap": [{"label": k, "orders": v} for k, v in sorted(heatmap.items())],
            "top_dishes": [{**dish, "revenue": cents_to_money(dish.pop("revenue_cents"))} for dish in top],
        }


class SmartBistroHandler(SimpleHTTPRequestHandler):
    service: SmartBistroService | None = None

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def do_GET(self) -> None:
        self.dispatch()

    def do_POST(self) -> None:
        self.dispatch()

    def do_PATCH(self) -> None:
        self.dispatch()

    def do_DELETE(self) -> None:
        self.dispatch()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.add_cors()
        self.end_headers()

    def dispatch(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if not path.startswith("/api"):
                return super().do_GET()
            query = parse_qs(parsed.query)
            method = self.command
            if path == "/api/health" and method == "GET":
                return self.json({"status": "ok", "service": "SmartBistro RMS"})
            if path == "/api/auth/login" and method == "POST":
                body = self.body()
                return self.json(self.app.login(body.get("email", ""), body.get("password", "")))
            if path.startswith("/api/menu/") and method == "GET":
                return self.json(self.app.menu_for_table(int(path.split("/")[-1])))
            if path == "/api/menu-items" and method == "GET":
                self.app.require_user(self.headers.get("Authorization"), ("manager", "staff", "kitchen"))
                return self.json(self.app.all_menu_items())
            if path == "/api/menu-items" and method == "POST":
                self.app.require_user(self.headers.get("Authorization"), ("manager",))
                return self.json(self.app.save_menu_item(self.body()), HTTPStatus.CREATED)
            if path.startswith("/api/menu-items/") and method == "PATCH":
                self.app.require_user(self.headers.get("Authorization"), ("manager",))
                return self.json(self.app.save_menu_item(self.body(), int(path.split("/")[-1])))
            if path.startswith("/api/menu-items/") and method == "DELETE":
                self.app.require_user(self.headers.get("Authorization"), ("manager",))
                return self.json(self.app.delete_menu_item(int(path.split("/")[-1])))
            if path == "/api/orders" and method == "POST":
                return self.json(self.app.create_order(self.body()), HTTPStatus.CREATED)
            if path == "/api/orders" and method == "GET":
                self.app.require_user(self.headers.get("Authorization"), ("manager", "staff"))
                return self.json(self.app.order_history())
            if path == "/api/customers/loyalty" and method == "GET":
                return self.json(self.app.customer_loyalty(query.get("contact", [""])[0]))
            if path == "/api/kds/orders" and method == "GET":
                self.app.require_user(self.headers.get("Authorization"), ("kitchen", "manager", "staff"))
                return self.json({"orders": self.app.kds_orders()})
            if path.startswith("/api/orders/") and path.endswith("/status") and method == "PATCH":
                self.app.require_user(self.headers.get("Authorization"), ("kitchen", "manager", "staff"))
                return self.json(self.app.update_order_status(int(path.split("/")[3]), self.body().get("status", "")))
            if path == "/api/tables" and method == "GET":
                return self.json({"tables": self.app.tables()})
            if path.startswith("/api/tables/") and method == "PATCH":
                self.app.require_user(self.headers.get("Authorization"), ("manager", "staff"))
                return self.json(self.app.update_table(int(path.split("/")[-1]), self.body().get("status", "")))
            if path.startswith("/api/tables/") and path.endswith("/qr") and method == "GET":
                svg = self.app.table_qr_svg(int(path.split("/")[3]), self.origin())
                return self.raw(svg.encode(), "image/svg+xml")
            if path == "/api/inventory" and method == "GET":
                self.app.require_user(self.headers.get("Authorization"), ("manager", "kitchen"))
                return self.json(self.app.inventory())
            if path.startswith("/api/inventory/") and method == "PATCH":
                self.app.require_user(self.headers.get("Authorization"), ("manager",))
                return self.json(self.app.update_ingredient(int(path.split("/")[-1]), self.body()))
            if path == "/api/inventory/waste" and method == "POST":
                self.app.require_user(self.headers.get("Authorization"), ("manager", "kitchen"))
                return self.json(self.app.log_waste(self.body()), HTTPStatus.CREATED)
            if path == "/api/analytics/dashboard" and method == "GET":
                self.app.require_user(self.headers.get("Authorization"), ("manager",))
                return self.json(self.app.analytics_dashboard())
            if path == "/api/reports/weekly" and method == "GET":
                self.app.require_user(self.headers.get("Authorization"), ("manager",))
                if query.get("format", ["json"])[0] == "csv":
                    data = self.app.analytics_dashboard()["top_dishes"]
                    lines = ["dish,quantity,revenue"] + [f"{d['name']},{d['qty']},{d['revenue']}" for d in data]
                    return self.raw("\n".join(lines).encode(), "text/csv")
                return self.json(self.app.analytics_dashboard())
            raise ApiError(HTTPStatus.NOT_FOUND, "Endpoint not found")
        except ApiError as exc:
            self.json({"error": exc.message}, exc.status)
        except Exception as exc:  # pragma: no cover - visible during manual demos
            self.json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    @property
    def app(self) -> SmartBistroService:
        if SmartBistroHandler.service is None:
            SmartBistroHandler.service = SmartBistroService()
        return SmartBistroHandler.service

    def origin(self) -> str:
        host = self.headers.get("Host", "localhost:8000")
        return f"http://{host}"

    def json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        self.raw(json.dumps(payload, indent=2).encode("utf-8"), "application/json", status)

    def raw(self, data: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.add_cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def add_cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization")


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def row_to_dict(row: sqlite3.Row, exclude: set[str] | None = None) -> dict[str, Any]:
    exclude = exclude or set()
    return {key: row[key] for key in row.keys() if key not in exclude}


def split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def cents_to_money(cents: int) -> float:
    return round(cents / 100, 2)


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return f"pbkdf2_sha256${salt}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    _, salt, expected = stored.split("$", 2)
    return hmac.compare_digest(hash_password(password, salt).split("$", 2)[2], expected)


def create_token(user_id: int, role: str) -> str:
    payload = {"sub": user_id, "role": role, "exp": int(time.time()) + TOKEN_TTL_SECONDS}
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(SECRET, encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def verify_token(token: str) -> dict[str, Any]:
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(SECRET, encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if payload["exp"] < time.time():
            raise ApiError(HTTPStatus.UNAUTHORIZED, "Token expired")
        return payload
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(HTTPStatus.UNAUTHORIZED, "Invalid token") from exc


def run(host: str = "127.0.0.1", port: int = 8000) -> None:
    server = ThreadingHTTPServer((host, port), SmartBistroHandler)
    print(f"SmartBistro RMS running at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run(port=int(os.getenv("PORT", "8000")))
