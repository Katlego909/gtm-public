import sqlite3
from datetime import datetime

conn = sqlite3.connect('db.sqlite3')
cursor = conn.cursor()

today = datetime.now().strftime('%Y-%m-%d')

# Update existing analytics to today's date
cursor.execute('UPDATE dashboard_channelanalytics SET date = ?', (today,))

# Update with more realistic data
updates = [
    (4500.0, 12.5, 1),  # Test channel - $4,500, +12.5% growth
    (3200.0, 8.3, 2),   # Social media - $3,200, +8.3% growth
    (2800.0, -5.2, 3),  # Website - $2,800, -5.2% decline
]

for revenue, change, channel_id in updates:
    cursor.execute('''
        UPDATE dashboard_channelanalytics 
        SET revenue = ?, change = ?
        WHERE channel_id = ?
    ''', (revenue, change, channel_id))

conn.commit()

# Verify
cursor.execute('SELECT * FROM dashboard_channelanalytics')
print('Updated Channel Analytics:')
for row in cursor.fetchall():
    print('  ', row)

print(f'\nAnalytics date updated to: {today}')
print('Total revenue:', sum([r[1] for r in cursor.execute('SELECT * FROM dashboard_channelanalytics').fetchall()]))

conn.close()
