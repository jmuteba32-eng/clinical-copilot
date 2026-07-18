import pymysql
c = pymysql.connect(host="hayabusa.proxy.rlwy.net", port=42831, user="openemr", password="openemr_db_pass_2026")
cur = c.cursor()
cur.execute("SHOW DATABASES")
dbs = [r[0] for r in cur.fetchall()]
print("databases:", dbs)
if "openemr2" in dbs:
    cur.execute("SELECT COUNT(1) FROM information_schema.tables WHERE table_schema='openemr2'")
    print("openemr2 tables:", cur.fetchone()[0])
    cur.execute("SELECT COUNT(1) FROM information_schema.tables WHERE table_schema='openemr2' AND table_name='users'")
    if cur.fetchone()[0]:
        cur.execute("SELECT COUNT(1) FROM openemr2.users")
        print("openemr2 users:", cur.fetchone()[0])
    else:
        print("openemr2 users: table not created yet")
else:
    print("openemr2 DOES NOT EXIST")
