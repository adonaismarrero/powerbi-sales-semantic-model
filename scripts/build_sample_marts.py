"""
Builds the sample "marts" CSV files used by the Power BI model in Sample mode.

It reproduces, in plain Python, the transformations of the Dataform project in
https://github.com/adonaismarrero/gcp-dataform-terraform-pipeline (staging ->
intermediate -> marts), starting from the same synthetic raw files. The output has
exactly the same columns as the BigQuery marts, so the Power BI model can switch
between Sample and BigQuery mode without any change.

Usage:
    python scripts/build_sample_marts.py                       # raw files from GitHub
    python scripts/build_sample_marts.py --raw ../other/raw    # raw files from a folder

Only the Python standard library is used.
"""
from __future__ import annotations

import argparse
import csv
import io
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "sample-data"
RAW_URL = ("https://raw.githubusercontent.com/adonaismarrero/"
           "gcp-dataform-terraform-pipeline/main/data/raw/")

VAT_RATE = Decimal("0.21")
REVENUE_STATUSES = {"delivered", "shipped"}
CALENDAR = (date(2023, 1, 1), date(2027, 12, 31))
LOADED_AT = "2026-01-01 06:00:00"          # fixed so the sample files are reproducible
CENT = Decimal("0.01")
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def r2(value: Decimal) -> Decimal:
    """BigQuery ROUND(x, 2) on NUMERIC rounds half away from zero."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def read_raw(source: str, table: str) -> list[dict]:
    if source.startswith("http"):
        with urllib.request.urlopen(f"{source}{table}.csv", timeout=60) as resp:
            text = resp.read().decode("utf-8")
    else:
        text = (Path(source) / f"{table}.csv").read_text(encoding="utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def madrid_date(ts: datetime) -> date:
    """DATE(ts, 'Europe/Madrid') for a UTC timestamp (EU summer time rules)."""
    def last_sunday(year: int, month: int) -> datetime:
        d = date(year, month + 1, 1) - timedelta(days=1)
        return datetime(d.year, d.month, d.day - (d.weekday() + 1) % 7, 1, 0)
    summer = last_sunday(ts.year, 3) <= ts < last_sunday(ts.year, 10)
    return (ts + timedelta(hours=2 if summer else 1)).date()


def title(text: str) -> str:
    """INITCAP(TRIM(x))."""
    return " ".join(w.capitalize() for w in text.strip().split(" ") if w != "")


def build(source: str) -> dict[str, list[dict]]:
    # ---------------- staging
    latest: dict[int, dict] = {}
    for row in read_raw(source, "customers"):
        cid = int(row["customer_id"])
        if cid not in latest or row["updated_at"] > latest[cid]["updated_at"]:
            latest[cid] = row
    customers = {
        cid: {
            "customer_id": cid,
            "first_name": title(r["first_name"]),
            "last_name": title(r["last_name"]),
            "email": r["email"].strip().lower(),
            "city": r["city"].strip(),
            "region": r["region"].strip(),
            "country": r["country"].strip().upper(),
            "signup_date": r["signup_date"],
        }
        for cid, r in latest.items()
    }

    products = {}
    for r in read_raw(source, "products"):
        price, cost = Decimal(r["unit_price"]), Decimal(r["unit_cost"])
        products[int(r["product_id"])] = {
            "product_id": int(r["product_id"]),
            "product_name": r["product_name"].strip(),
            "category": r["category"].strip(),
            "unit_price": price,
            "unit_cost": cost,
            "unit_margin": price - cost,
            "is_active": r["is_active"].strip().lower() == "true",
        }

    orders = {}
    for r in read_raw(source, "orders"):
        ts = datetime.fromisoformat(r["order_ts"])
        status = r["status"].strip().lower()
        orders[int(r["order_id"])] = {
            "order_ts": ts,
            "order_date": madrid_date(ts),
            "customer_id": int(r["customer_id"]),
            "channel": r["channel"].strip().lower(),
            "payment_method": r["payment_method"].strip().lower(),
            "status": status,
            "is_revenue": status in REVENUE_STATUSES,
            "shipping_cost": Decimal(r["shipping_cost"]),
        }

    items = []
    for r in read_raw(source, "order_items"):
        qty, price = int(r["quantity"]), Decimal(r["unit_price"])
        disc = Decimal(r["discount_pct"] or "0")
        gross = qty * price
        discount = r2(gross * disc)
        items.append({
            "order_item_id": int(r["order_item_id"]), "order_id": int(r["order_id"]),
            "product_id": int(r["product_id"]), "quantity": qty, "unit_price": price,
            "discount_pct": disc, "gross_amount": gross, "discount_amount": discount,
            "net_amount": gross - discount,
        })

    # ---------------- intermediate: lines + order + product, cost, margin, shipping
    net_per_order: dict[int, Decimal] = defaultdict(Decimal)
    for it in items:
        net_per_order[it["order_id"]] += it["net_amount"]

    fct = []
    for it in sorted(items, key=lambda x: x["order_item_id"]):
        o, p = orders[it["order_id"]], products[it["product_id"]]
        cost = it["quantity"] * p["unit_cost"]
        total = net_per_order[it["order_id"]]
        shipping = r2(o["shipping_cost"] * it["net_amount"] / total) if total else None
        fct.append({
            "order_item_id": it["order_item_id"], "order_id": it["order_id"],
            "order_date": o["order_date"], "order_ts": o["order_ts"],
            "customer_id": o["customer_id"], "product_id": it["product_id"],
            "channel": o["channel"], "payment_method": o["payment_method"],
            "status": o["status"], "is_revenue": o["is_revenue"],
            "quantity": it["quantity"], "unit_price": it["unit_price"],
            "discount_pct": it["discount_pct"], "gross_amount": it["gross_amount"],
            "discount_amount": it["discount_amount"], "net_amount": it["net_amount"],
            "vat_amount": r2(it["net_amount"] * VAT_RATE),
            "cost_amount": cost, "margin_amount": it["net_amount"] - cost,
            "shipping_allocated": shipping, "loaded_at": LOADED_AT,
        })

    # ---------------- marts: dimensions
    activity: dict[int, dict] = {}
    for f in fct:
        if not f["is_revenue"]:
            continue
        a = activity.setdefault(f["customer_id"], {"first": f["order_date"], "last": f["order_date"],
                                                   "orders": set(), "revenue": Decimal(0)})
        a["first"], a["last"] = min(a["first"], f["order_date"]), max(a["last"], f["order_date"])
        a["orders"].add(f["order_id"])
        a["revenue"] += f["net_amount"]

    dim_customers = []
    for cid in sorted(customers):
        c, a = customers[cid], activity.get(cid)
        n, rev = (len(a["orders"]), a["revenue"]) if a else (0, Decimal(0))
        segment = ("new" if a is None else "vip" if n >= 12 or rev >= 3000
                   else "regular" if n >= 5 else "occasional")
        dim_customers.append({
            "customer_id": cid, "full_name": f"{c['first_name']} {c['last_name']}",
            "email": c["email"], "city": c["city"], "region": c["region"],
            "country": c["country"], "signup_date": c["signup_date"],
            "first_order_date": a["first"] if a else None,
            "last_order_date": a["last"] if a else None,
            "lifetime_orders": n, "lifetime_revenue": rev, "customer_segment": segment,
        })

    dim_products = []
    for pid in sorted(products):
        p = products[pid]
        price = p["unit_price"]
        band = ("0-25" if price < 25 else "25-100" if price < 100
                else "100-300" if price < 300 else "300+")
        dim_products.append({
            "product_id": pid, "product_name": p["product_name"], "category": p["category"],
            "unit_price": price, "unit_cost": p["unit_cost"], "unit_margin": p["unit_margin"],
            "unit_margin_pct": (p["unit_margin"] / price).quantize(Decimal("0.0001"), ROUND_HALF_UP)
            if price else None,
            "price_band": band, "is_active": p["is_active"],
        })

    dim_date = []
    d = CALENDAR[0]
    while d <= CALENDAR[1]:
        dow = d.isoweekday() % 7 + 1                       # BigQuery: Sunday=1 ... Saturday=7
        month_end = (date(d.year + d.month // 12, d.month % 12 + 1, 1) - timedelta(days=1))
        dim_date.append({
            "date_day": d, "year": d.year, "quarter": (d.month - 1) // 3 + 1, "month": d.month,
            "year_month": f"{d:%Y-%m}", "iso_week": d.isocalendar()[1], "day_of_week": dow,
            "day_name": DAY_NAMES[d.weekday()],                # FORMAT_DATE('%A'), locale-independent
            "is_weekend": dow in (1, 7), "month_start": d.replace(day=1), "month_end": month_end,
        })
        d += timedelta(days=1)

    return {"fct_sales": fct, "dim_customers": dim_customers,
            "dim_products": dim_products, "dim_date": dim_date}


def fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--raw", default=RAW_URL, help="folder or URL with the raw CSV files")
    args = ap.parse_args()
    source = args.raw if args.raw.startswith("http") or args.raw.endswith(("/", "\\")) \
        else args.raw.rstrip("/\\") + "/"
    OUT.mkdir(exist_ok=True)
    for name, rows in build(source).items():
        with open(OUT / f"{name}.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows({k: fmt(v) for k, v in row.items()} for row in rows)
        print(f"{name:<14} {len(rows):>6} rows -> sample-data/{name}.csv")


if __name__ == "__main__":
    main()
