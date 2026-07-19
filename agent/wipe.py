import pymysql
c = pymysql.connect(host="reseau.proxy.rlwy.net", port=21042, user="root",
    password="dSWRGVbmNaxOCLfICpdNXJYiiGtevkcw", database="openemr", charset="utf8mb4")
cur = c.cursor()
cur.execute("SET FOREIGN_KEY_CHECKS=0;")
cur.execute("SHOW TABLES;")
tables = [r[0] for r in cur.fetchall()]
for t in tables:
    cur.execute(f"DROP TABLE IF EXISTS `{t}`;")
cur.execute("SET FOREIGN_KEY_CHECKS=1;")
c.commit()
print(f"Dropped {len(tables)} tables. Database is now empty.")
