import csv

with open('data/observations.csv', 'r') as f:
    reader = csv.reader(f)
    header = next(reader)
    rows = [r for r in reader if len(r) >= 11]

# Sort by net profit descending, then gross profit
sorted_rows = sorted(rows, key=lambda r: (float(r[7]), float(r[6])), reverse=True)

seen = set()
top_opps = []
for r in sorted_rows:
    key = (r[1], r[2], r[3], r[4])
    if key not in seen:
        seen.add(key)
        top_opps.append(r)

print(f"Total unique quote snapshots evaluated: {len(top_opps)}")
print("\n=== TOP 10 OPPORTUNITIES BY NET PROFIT ===")
for idx, r in enumerate(top_opps[:10], 1):
    time_str = r[0][11:19]
    event = r[1]
    direction = r[2]
    k_ask, p_ask, cost = r[3], r[4], r[5]
    gross = float(r[6])
    net = float(r[7])
    depth = r[10]
    print(f"{idx}. [{event}]")
    print(f"   • Direction: {direction}")
    print(f"   • Prices: Kalshi ${k_ask} + Poly ${p_ask} = ${cost} Total Cost")
    print(f"   • Gross Spread: {gross*100:+.2f}% (${gross:.4f})")
    print(f"   • Net Profit (After Fees): {net*100:+.2f}% (${net:.4f})")
    print(f"   • Book Depth: {depth} contracts\n")
