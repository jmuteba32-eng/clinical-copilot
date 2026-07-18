import pymysql
c = pymysql.connect(host="hayabusa.proxy.rlwy.net", port=42831, user="openemr", password="openemr_db_pass_2026")
cur = c.cursor()

def tables(db):
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s", (db,))
    return {r[0] for r in cur.fetchall()}

a = tables("openemr")
b = tables("openemr2")
print("openemr :", len(a), "tables | has list_options:", "list_options" in a)
print("openemr2:", len(b), "tables | has list_options:", "list_options" in b)
print()
print("in openemr but NOT openemr2:", sorted(a - b)[:20])
print("in openemr2 but NOT openemr:", sorted(b - a)[:20])
