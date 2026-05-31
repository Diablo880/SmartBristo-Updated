"""Lightweight model names used by tests and documentation.

The runtime persists records in SQLite tables defined in src.app. These small
classes keep the domain vocabulary explicit without requiring an ORM package.
"""
from dataclasses import dataclass


@dataclass
class Restaurant:
    name: str
    address: str
    phone: str = ""


@dataclass
class Menu:
    name: str
    description: str = ""


@dataclass
class OrderLine:
    menu_item_id: int
    qty: int
    notes: str = ""
