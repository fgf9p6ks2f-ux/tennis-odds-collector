from mlb import data
import datetime as dt
gs = data.probables(dt.date.today().isoformat())
print(f"{len(gs)} probable starters today")
want = ("Rasmussen", "Schultz", "Mahle", "Gausman", "Wheeler")
for g in gs:
    if any(n in g["pitcher"] for n in want):
        print("   %-20s team_id=%s opp=%r" % (g["pitcher"], g["team_id"], g["opp"]))
