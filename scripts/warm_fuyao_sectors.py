#!/usr/bin/env python3
"""Bounded sector cache warm-up only; no model decisions, orders or messages."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data.services.fuyao_sector_service import FuyaoSectorService
from data.services.stock_sector_membership_service import load_stock_memberships
from data.store.sqlite_store import StockStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codes', default='', help='Prioritize sectors attached to these stock codes')
    parser.add_argument('--limit', type=int, default=30, help='Maximum sectors to warm')
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error('--limit must be between 1 and 100')
    store = StockStore()
    service = FuyaoSectorService(store=store)
    codes = [c.strip().zfill(6) for c in args.codes.split(',') if c.strip()]
    facts = load_stock_memberships(store, codes)
    names = {r['sector_name'] for rows in facts.values() for r in rows}
    catalog = service.catalog()
    quotes = service.quotes(catalog)
    sectors = sorted(catalog, key=lambda row: (row['name'] not in names,
        -float(quotes.get(row['thscode'], {}).get('price_change_ratio_pct') or 0)))[:args.limit]
    result = {'selected':len(sectors), 'history':0, 'members':0, 'errors':[]}
    for index, sector in enumerate(sectors, 1):
        for kind, fetch in (('history', lambda: service.history(sector['thscode'], [1])),
                            ('members', lambda: service.members(sector, [1]))):
            try:
                fetch();result[kind] += 1
            except Exception as exc:
                result['errors'].append(f"{sector['thscode']} {kind}: {exc}")
        if index % 10 == 0:
            print(json.dumps({'processed':index, **result}, ensure_ascii=False), flush=True)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 1 if result['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
