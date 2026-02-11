import sqlite3

conn = sqlite3.connect('db.sqlite3')
cursor = conn.cursor()

cursor.execute('SELECT * FROM dashboard_channel')
print('Channels:')
for row in cursor.fetchall():
    print('  ', row)

cursor.execute('SELECT * FROM dashboard_channelanalytics')
print('\nChannel Analytics:')
for row in cursor.fetchall():
    print('  ', row)

conn.close()
