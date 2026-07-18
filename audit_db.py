import pymysql
c = pymysql.connect(host="hayabusa.proxy.rlwy.net", port=42831, user="openemr", password="openemr_db_pass_2026")
cur = c.cursor()
cur.execute("""
    SELECT table_name, table_rows
    FROM information_schema.tables
    WHERE table_schema='openemr' AND table_rows > 0
    ORDER BY table_rows DESC
    LIMIT 25
""")
print("=== openemr: non-empty tables ===")
for name, n in cur.fetchall():
    print(f"  {name:35} ~{n}")

for t in ("patient_data", "users", "form_encounter", "lists", "prescriptions"):
    try:
        cur.execute(f"SELECT COUNT(1) FROM openemr.`{t}`")
        print(f"EXACT openemr.{t}: {cur.fetchone()[0]}")
    except Exception as e:
        print(f"EXACT openemr.{t}: ERR {e}")
