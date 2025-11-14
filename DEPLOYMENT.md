# Deploying to PythonAnywhere

This guide walks you through deploying the Funti3r GTM Validator to PythonAnywhere.

## Prerequisites

1. PythonAnywhere account (Free or Paid)
2. GitHub repository with your code pushed
3. Google Gemini API key
4. Gmail App Password for email functionality

## Step 1: Clone Your Repository

```bash
cd ~
git clone https://github.com/Katlego909/gtm.git
cd gtm
```

## Step 2: Create Virtual Environment

```bash
mkvirtualenv --python=/usr/bin/python3.11 gtmenv
```

Activate it (if not already active):
```bash
workon gtmenv
```

## Step 3: Install Dependencies

```bash
pip install -r requirements.txt
```

## Step 4: Set Up Environment Variables

Create a `.env` file in your project directory:

```bash
nano .env
```

Add the following (replace with your actual values):

```env
# Django Settings
DJANGO_SECRET_KEY=your-super-secret-key-here-generate-new-one
DEBUG=False
ALLOWED_HOSTS=yourusername.pythonanywhere.com,localhost,127.0.0.1

# Google Gemini AI
GEMINI_API_KEY=your-gemini-api-key-here

# Email Settings (Gmail)
EMAIL_HOST_USER=your-email@gmail.com
EMAIL_HOST_PASSWORD=your-gmail-app-password

# Optional: Redirect all emails to yourself for testing
# EMAIL_REDIRECT_TO=your-testing-email@gmail.com

# Optional: Use console backend for email testing
# EMAIL_CONSOLE=true
```

**Important:** Generate a new SECRET_KEY for production:
```bash
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

## Step 5: Configure WSGI File

Go to the PythonAnywhere **Web** tab and click on your WSGI configuration file.

Replace the contents with:

```python
import os
import sys
from dotenv import load_dotenv

# Add your project directory to the sys.path
project_home = '/home/yourusername/gtm'  # CHANGE THIS
if project_home not in sys.path:
    sys.path.insert(0, project_home)

# Load environment variables from .env file
load_dotenv(os.path.join(project_home, '.env'))

# Set the Django settings module
os.environ['DJANGO_SETTINGS_MODULE'] = 'gtm_validator.settings'

# Import Django's WSGI handler
from django.core.wsgi import get_wsgi_application
application = get_wsgi_application()
```

**Replace `yourusername` with your actual PythonAnywhere username!**

## Step 6: Run Database Migrations

```bash
cd ~/gtm
python manage.py migrate
```

## Step 7: Load Default Data

Load the default GTM assessment questions and tool recommendations:

```bash
python manage.py load_gtm_defaults
```

## Step 8: Create Superuser

```bash
python manage.py createsuperuser
```

Follow the prompts to create your admin account.

## Step 9: Collect Static Files

```bash
python manage.py collectstatic --noinput
```

## Step 10: Configure Static Files in PythonAnywhere

Go to the **Web** tab on PythonAnywhere and scroll down to **Static files**.

Add the following mappings:

| URL | Directory |
|-----|-----------|
| `/static/` | `/home/yourusername/gtm/staticfiles` |

**Replace `yourusername` with your actual username!**

## Step 11: Configure Virtualenv Path

In the **Web** tab, find the **Virtualenv** section and enter:

```
/home/yourusername/.virtualenvs/gtmenv
```

## Step 12: Reload Your Web App

Click the green **Reload** button at the top of the Web tab.

## Step 13: Test Your Application

Visit: `https://yourusername.pythonanywhere.com`

### Things to Test:
1. ✅ Landing page loads
2. ✅ Start new assessment
3. ✅ Complete assessment (check AI playbook generates)
4. ✅ View results with radar chart
5. ✅ Tool recommendations display
6. ✅ Chat assistant works
7. ✅ Email reports send (if configured)
8. ✅ Admin panel at `/admin`

## Troubleshooting

### 500 Internal Server Error

Check error logs:
1. Go to **Web** tab
2. Click on **Error log** link
3. Check for Python exceptions

Common issues:
- `.env` file not found or has wrong path
- ALLOWED_HOSTS doesn't include your domain
- Static files not collected
- Missing environment variables

### Static Files Not Loading

1. Verify STATIC_ROOT in settings.py: `STATIC_ROOT = BASE_DIR / "staticfiles"`
2. Run `python manage.py collectstatic` again
3. Check static files mapping in Web tab

### Database Issues

If you need to reset the database:
```bash
cd ~/gtm
rm db.sqlite3
python manage.py migrate
python manage.py load_gtm_defaults
python manage.py createsuperuser
```

### AI Features Not Working

1. Check GEMINI_API_KEY is set correctly in `.env`
2. Verify your Google AI Studio quota hasn't been exceeded
3. Check error logs for API-related errors

### Email Not Sending

1. Verify EMAIL_HOST_USER and EMAIL_HOST_PASSWORD in `.env`
2. Ensure you're using a Gmail **App Password**, not your regular password
3. Enable 2-factor authentication on your Google account
4. Generate App Password: https://myaccount.google.com/apppasswords
5. For testing, set `EMAIL_CONSOLE=true` to print emails to console

## Updating Your Application

When you push changes to GitHub:

```bash
cd ~/gtm
git pull origin master
workon gtmenv
pip install -r requirements.txt  # if dependencies changed
python manage.py migrate  # if models changed
python manage.py collectstatic --noinput  # if CSS/JS changed
```

Then click **Reload** in the Web tab.

## Production Considerations

### For Heavy Usage:

1. **Upgrade to Paid Plan**: Free tier has limited CPU/bandwidth
2. **Use PostgreSQL/MySQL**: Instead of SQLite for better concurrency
3. **Configure Caching**: Add Redis/Memcached for session/cache storage
4. **Rate Limiting**: Add rate limiting to prevent API quota exhaustion
5. **Monitoring**: Set up uptime monitoring and error tracking

### Security Checklist:

- ✅ `DEBUG=False` in production
- ✅ Strong SECRET_KEY (different from development)
- ✅ ALLOWED_HOSTS properly configured
- ✅ .env file not committed to Git
- ✅ CSRF protection enabled (default)
- ✅ XSS protection enabled (default)
- ✅ Use HTTPS (PythonAnywhere provides this)

## Support

If you encounter issues:
1. Check PythonAnywhere error logs
2. Check `gtm_errors.log` in your project directory
3. Review PythonAnywhere forums: https://www.pythonanywhere.com/forums/
4. Check Django documentation: https://docs.djangoproject.com/

## Environment Variables Reference

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `DJANGO_SECRET_KEY` | Yes | Django secret key (generate new for production) | `django-insecure-...` |
| `DEBUG` | Yes | Debug mode (False for production) | `False` |
| `ALLOWED_HOSTS` | Yes | Comma-separated list of allowed hosts | `yourusername.pythonanywhere.com,localhost` |
| `GEMINI_API_KEY` | Yes | Google Gemini API key for AI features | `AIzaSy...` |
| `EMAIL_HOST_USER` | Yes | Gmail address for sending emails | `your-email@gmail.com` |
| `EMAIL_HOST_PASSWORD` | Yes | Gmail App Password (not regular password) | `abcd efgh ijkl mnop` |
| `EMAIL_REDIRECT_TO` | No | Redirect all emails (for testing) | `test@example.com` |
| `EMAIL_CONSOLE` | No | Print emails to console instead of sending | `true` |
| `NPM_BIN_PATH` | No | Path to npm (not needed on server) | `/usr/bin/npm` |
