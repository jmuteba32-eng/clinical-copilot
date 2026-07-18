import pymysql
c = pymysql.connect(host="hayabusa.proxy.rlwy.net", port=42831, user="openemr", password="openemr_db_pass_2026")
cur = c.cursor()

def rows(db, t):
    try:
        cur.execute(f"SELECT COUNT(1) FROM `{db}`.`{t}`")
        return cur.fetchone()[0]
    except Exception as e:
        return f"ERR {e}"

for t in ("users", "list_options", "globals", "facility", "patient_data", "version"):
    print(f"{t:15} openemr={rows('openemr', t):<8} openemr2={rows('openemr2', t)}")

print()
cur.execute("SELECT username FROM openemr2.users LIMIT 5")
print("openemr2 users:", cur.fetchall())
