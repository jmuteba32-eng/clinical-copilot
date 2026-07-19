import pymysql
import time

def connect():
    return pymysql.connect(
        host="reseau.proxy.rlwy.net",
        port=21042,
        user="root",
        password="dSWRGVbmNaxOCLfICpdNXJYiiGtevkcw",
        database="openemr",
        charset="utf8mb4",
        max_allowed_packet=256 * 1024 * 1024,
        read_timeout=120,
        write_timeout=120,
    )

conn = connect()
cur = conn.cursor()
cur.execute("SET FOREIGN_KEY_CHECKS=0;")
conn.commit()

with open("openemr_backup.sql", "r", encoding="utf-8") as f:
    sql = f.read()

statements = [s.strip() for s in sql.split(";\n") if s.strip()]
total = len(statements)
ok = 0
fail = 0
i = 0
while i < total:
    stmt = statements[i]
    try:
        cur.execute(stmt)
        ok += 1
    except pymysql.err.InterfaceError:
        # Connection dropped — reconnect and retry this same statement.
        print(f"  [reconnect] at statement {i}, reconnecting...")
        time.sleep(2)
        try:
            conn = connect()
            cur = conn.cursor()
            cur.execute("SET FOREIGN_KEY_CHECKS=0;")
            continue  # retry same statement
        except Exception as e:
            print(f"  [error] reconnect failed: {e}")
            time.sleep(5)
            continue
    except pymysql.err.OperationalError as e:
        if e.args[0] in (2013, 2006):  # lost connection / gone away
            print(f"  [reconnect] at statement {i} ({e.args[0]}), reconnecting...")
            time.sleep(2)
            conn = connect()
            cur = conn.cursor()
            cur.execute("SET FOREIGN_KEY_CHECKS=0;")
            continue
        else:
            fail += 1
    except Exception:
        fail += 1
    i += 1
    if i % 5000 == 0:
        print(f"  ...{i}/{total} processed (ok={ok}, fail={fail})")
        try:
            conn.commit()
        except Exception:
            pass

try:
    conn.commit()
except Exception:
    pass
print(f"Restore complete: {ok} OK, {fail} failed.")

# Fresh connection for the final count
c2 = connect()
cc = c2.cursor()
cc.execute("SELECT COUNT(*) FROM patient_data;")
print("Patients after restore:", cc.fetchone()[0])