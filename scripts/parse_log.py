"""Parse purchase log output."""
import re
import sys

log_path = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\Root\AppData\Local\Temp\claude\e--c5games\tasks\b2b2gujwe.output"

with open(log_path, "rb") as f:
    data = f.read()

lines = data.split(b"\n")
buy_confirmed = [l for l in lines if b"cs2dt_first_buy:1016" in l or b"cs2dt_first_buy:1028" in l]

total_spent = 0.0
total_items = 0
items = []

for line in buy_confirmed:
    text = line.decode("cp1251", errors="replace")
    # Find xN pattern
    qty_m = re.search(r"x(\d+)", text)
    # Find $N.NN pattern
    price_m = re.search(r"\$(\d+\.\d+)", text)
    # Find item name between ": " and " x"
    name_m = re.search(r": (.+?) x\d+", text)

    if qty_m and price_m:
        qty = int(qty_m.group(1))
        price = float(price_m.group(1))
        name = name_m.group(1) if name_m else "?"
        total_items += qty
        total_spent += price
        items.append((name, qty, price))

print(f"Confirmed purchases: {len(buy_confirmed)}")
print(f"Total unique items: {len(items)}")
print(f"Total qty: {total_items}")
print(f"Total spent: ${total_spent:.2f}")
print()
print("First 10:")
for name, qty, price in items[:10]:
    print(f"  {name} x{qty} = ${price}")
print("...")
print("Last 5:")
for name, qty, price in items[-5:]:
    print(f"  {name} x{qty} = ${price}")
