# SmartBistro RMS

SmartBistro is a fully functional restaurant management prototype based on the SENG205 Assessment 3 report. It includes guest QR ordering, kitchen display, front-of-house table management, inventory deduction, low-stock alerts, loyalty points, payments, analytics, CSV export, and role-based access control.

## Run

No third-party packages are required.

On Windows, double-click:

```text
start-smartbistro.bat
```

Or run manually:

```bash
python -m src.app
```

Open:

```text
http://127.0.0.1:8000
```

If your computer does not have `python`, use `py -m src.app` on Windows.

## Demo Accounts

| Role | Email | Password |
| --- | --- | --- |
| Manager | manager@smartbistro.test | manager123 |
| Kitchen | kitchen@smartbistro.test | kitchen123 |
| Staff | staff@smartbistro.test | staff123 |

## Included Modules

- Guest mobile menu with table selection and paid order submission.
- Table QR SVG endpoint at `/api/tables/{id}/qr`.
- Interactive floor plan with available, occupied, dirty, and reserved states.
- Kitchen display queue ranked by prep time, with allergen badges and status transitions.
- Inventory and recipe mapping with atomic SQLite stock deduction.
- Waste logging and in-app low-stock alerts.
- Loyalty accrual at 1 point per paid dollar and checkout redemption.
- Manager analytics with revenue, average order value, heatmap data, top dishes, and CSV export.
- Signed bearer tokens and role checks for staff, kitchen, and manager actions.

## API Highlights

| Method | Endpoint | Purpose |
| --- | --- | --- |
| POST | `/api/auth/login` | Sign in and receive bearer token |
| GET | `/api/menu/{tableId}` | Load guest table menu |
| POST | `/api/orders` | Create paid order and send to KDS |
| GET | `/api/kds/orders` | Kitchen queue |
| PATCH | `/api/orders/{id}/status` | Advance order status |
| GET | `/api/tables` | Floor plan |
| PATCH | `/api/tables/{id}` | Update table status |
| GET | `/api/inventory` | Ingredient stock and alerts |
| POST | `/api/inventory/waste` | Record waste |
| GET | `/api/analytics/dashboard` | Manager dashboard |
| GET | `/api/reports/weekly?format=csv` | CSV export |

## Test

```bash
python -m unittest
```

The test suite covers authentication, guest ordering, KDS flow, stock deduction, low-stock safety, table state, QR output, and analytics.
