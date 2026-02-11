import sqlite3
from datetime import datetime, timedelta

conn = sqlite3.connect('db.sqlite3')
cursor = conn.cursor()

# Sample action items
sample_items = [
    # To Do items
    {
        'note': 'Review quarterly marketing metrics and prepare report',
        'status': 'todo',
        'owner': 'Marketing Team',
        'due_date': (datetime.now() + timedelta(days=7)).strftime('%Y-%m-%d'),
    },
    {
        'note': 'Update website SEO keywords based on latest research',
        'status': 'todo',
        'owner': 'Content Team',
        'due_date': (datetime.now() + timedelta(days=5)).strftime('%Y-%m-%d'),
    },
    {
        'note': 'Schedule customer feedback sessions for product improvements',
        'status': 'todo',
        'owner': 'Product Team',
        'due_date': (datetime.now() + timedelta(days=10)).strftime('%Y-%m-%d'),
    },
    # In Progress items
    {
        'note': 'Implement new email marketing automation workflows',
        'status': 'doing',
        'owner': 'Marketing Team',
        'due_date': (datetime.now() + timedelta(days=3)).strftime('%Y-%m-%d'),
    },
    {
        'note': 'Analyze conversion funnel drop-off points',
        'status': 'doing',
        'owner': 'Analytics Team',
        'due_date': (datetime.now() + timedelta(days=2)).strftime('%Y-%m-%d'),
    },
    # Done items
    {
        'note': 'Complete Q4 budget review and planning',
        'status': 'done',
        'owner': 'Finance Team',
        'due_date': (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d'),
    },
    {
        'note': 'Launch new landing page for product campaign',
        'status': 'done',
        'owner': 'Marketing Team',
        'due_date': (datetime.now() - timedelta(days=5)).strftime('%Y-%m-%d'),
    },
]

now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

for item in sample_items:
    cursor.execute('''
        INSERT INTO gtm_actionitem 
        (note, status, owner, due_date, created_at, updated_at, session_id, question_id)
        VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
    ''', (item['note'], item['status'], item['owner'], item['due_date'], now, now))

conn.commit()
print(f'Created {len(sample_items)} sample action items')

# Verify
cursor.execute('SELECT COUNT(*) FROM gtm_actionitem')
total = cursor.fetchone()[0]
print(f'Total action items in database: {total}')

cursor.execute("SELECT COUNT(*) FROM gtm_actionitem WHERE status='todo'")
todo = cursor.fetchone()[0]
print(f'  - To Do: {todo}')

cursor.execute("SELECT COUNT(*) FROM gtm_actionitem WHERE status='doing'")
doing = cursor.fetchone()[0]
print(f'  - In Progress: {doing}')

cursor.execute("SELECT COUNT(*) FROM gtm_actionitem WHERE status='done'")
done = cursor.fetchone()[0]
print(f'  - Done: {done}')

conn.close()
