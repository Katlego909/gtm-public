import sqlite3

conn = sqlite3.connect('db.sqlite3')
cursor = conn.cursor()

cursor.execute('SELECT id, company_name, ai_playbook FROM gtm_resultsnapshot LIMIT 1')
result = cursor.fetchone()

if result:
    print(f"ID: {result[0]}")
    print(f"Company: {result[1]}")
    print(f"\nRaw Playbook Content (first 500 chars):")
    print(result[2][:500] if result[2] else "None")
    print("\n" + "="*50)
    print("Full content:")
    print(result[2])
else:
    print("No insights found")

conn.close()
