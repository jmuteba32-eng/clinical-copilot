import pymysql
c = pymysql.connect(host="hayabusa.proxy.rlwy.net", port=42831, user="openemr", password="openemr_db_pass_2026")
cur = c.cursor()
cur.execute("DROP DATABASE openemr2")
cur.execute("CREATE DATABASE openemr2 CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci")
cur.execute("SELECT COUNT(1) FROM information_schema.tables WHERE table_schema='openemr2'")
print("openemr2 table count after reset:", cur.fetchone()[0])
c.commit()
