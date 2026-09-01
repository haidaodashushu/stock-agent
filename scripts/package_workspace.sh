#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
STAGE="/tmp/workspace-stock-package-$STAMP/workspace-stock"
PKG="$ROOT/dist/workspace-stock-$STAMP.tar.zst"
mkdir -p "$STAGE" "$ROOT/dist"
cd "$ROOT"

rsync -a --delete \
  --exclude '.venv/' \
  --exclude '.git/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude 'logs/' \
  --exclude 'memory/' \
  --exclude 'dist/' \
  --exclude '*.pyc' \
  --exclude '*.pid' \
  --exclude 'data/*.db' \
  --exclude 'stock_data.db' \
  ./ "$STAGE"/

mkdir -p "$STAGE/data"
for db in data/stock_data.db data/trade.db stock_data.db; do
  if [ -f "$ROOT/$db" ]; then
    mkdir -p "$STAGE/$(dirname "$db")"
    if [ -s "$ROOT/$db" ]; then
      sqlite3 "$ROOT/$db" ".backup '$STAGE/$db'"
    else
      cp "$ROOT/$db" "$STAGE/$db"
    fi
  fi
done

tar --use-compress-program='zstd -3 -T0' -cf "$PKG" -C "$(dirname "$STAGE")" "$(basename "$STAGE")"
sha256sum "$PKG" > "$PKG.sha256"
du -h "$PKG" | tee "$PKG.size.txt"
find "$STAGE" -maxdepth 2 -type f | sed "s#^$STAGE/##" | sort | head -200 > "$PKG.contents.head.txt"
rm -rf "$(dirname "$STAGE")"
echo "$PKG"
