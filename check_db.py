import pymysql
c = pymysql.connect(host="hayabusa.proxy.rlwy.net", port=42831, user="openemr", password="openemr_db_pass_2026")
cur = c.cursor()
cur.execute("SHOW DATABASES")
print("DATABASES:", [r[0] for r in cur.fetchall()])
cur.execute("SELECT table_schema, COUNT(1) FROM information_schema.tables WHERE table_schema IN ('openemr','openemr2') GROUP BY table_schema")
print("TABLE COUNTS:", cur.fetchall())
cur.execute("SHOW GRANTS")
for g in cur.fetchall():
    print("GRANT:", g[0])
