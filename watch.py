import pymysql, time
c = pymysql.connect(host="hayabusa.proxy.rlwy.net", port=42831, user="openemr", password="openemr_db_pass_2026")
cur = c.cursor()
def n(t):
    try:
        cur.execute(f"SELECT COUNT(1) FROM openemr2.`{t}`")
        return cur.fetchone()[0]
    except Exception:
        return "-"
for i in range(10):
    print(f"[{i}] list_options={n('list_options')}  codes={n('codes')}  layout_options={n('layout_options')}  users={n('users')}")
    time.sleep(30)
