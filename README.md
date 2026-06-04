# GTM Validator - Go-To-Market Readiness Tool

GTM Validator is a Django-based web application that helps startups and businesses assess their Go-To-Market (GTM) readiness. It walks users through structured questions across key GTM pillars — Demand, Conversion, and Delivery — and provides automated scoring, insights, visualized dashboards, tailored recommendations, and playbook generation.

---

## Features

### GTM Assessment Workflow
- Dynamic multi-step form using Django forms
- Scored responses stored per session (resumable assessments)
- Weighted categories (Demand, Conversion, Delivery)
- Company and contact information capture (firmographics, website, CRM data)
- UTM tracking and referrer source tracking
- Session persistence and resumable assessments

### Dashboard and Visualization
- Real-time radar chart visualization powered by Chart.js
- Category-level breakdowns and progress tracking
- Top Strengths and Focus Areas identification based on scores
- Overall GTM readiness score calculation

### Recommendations Engine
- Automatic "Recommended Next Moves" generated from score bands
- Category-level tool recommendations (e.g., HubSpot, Notion, Hotjar)
- Contextual recommendations based on assessment responses

### Playbook and Reports
- Auto-generated GTM Playbook with insights and recommendations
- Financial summary and competitor analysis integration
- AI-generated insights for each response
- One-click PDF export of complete playbooks
- Session history tracking and persistent data

### Action Items and Team Collaboration
- Create and manage action items from assessment results
- Assign action items to team members
- Track action item status (To Do, In Progress, Done)
- Add due dates and ownership information
- Comment system for team collaboration on action items
- Real-time status updates

### Workspace Collaboration
- Create and manage team workspaces
- Share assessments within workspaces
- Team member invitations and management
- Workspace-level action item tracking
- Collaborative access control

### Strategic Evidence Upload
- Upload and manage strategic evidence files
- Support for multiple file types:
  - Landing page screenshots
  - Ad creative assets
  - Sales decks and strategy PDFs
  - Other strategic evidence
- AI audit capability for uploaded assets
- Evidence library integration

### Admin Tools
- Manage categories, questions, and weighting
- Manage recommendation bands and stage definitions
- Manage tool recommendations by category
- Load default dataset via management command
- Full Django admin interface for data management

### AI-Powered Insights
- AI-generated insights for individual responses
- Contextual note generation based on user input
- Playbook enrichment with financial and competitive analysis
- Asynchronous AI processing with status tracking

---

## Installation and Setup

### Local Development

#### 1. Clone the repository
```bash
git clone https://github.com/Katlego909/gtm.git
cd gtm
```

#### 2. Create virtual environment
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

#### 3. Install dependencies
```bash
pip install -r requirements.txt
```

#### 4. Configure environment and set up service account credentials
Create a `.env` file in the project root:
```
DEBUG=True
SECRET_KEY=your-secret-key
GOOGLE_APPLICATION_CREDENTIALS=keys/service-account.json
```
By default, local development uses SQLite (no database configuration needed). The `SECRET_KEY` will be auto-generated if not set.

**Important:** You'll need to set up Google Cloud credentials for the application:
1. Create a `keys/` folder in the project root
2. Download your Google Cloud service account JSON file and place it in the `keys/` folder as `service-account.json`
3. This file is git-ignored (see `.gitignore`) and should never be committed to the repository
4. Ask a team lead or project maintainer for the service account credentials if you don't have them

#### 5. Run migrations
```bash
python manage.py migrate
```

#### 6. Load default data
```bash
python manage.py load_gtm_defaults
```

#### 7. Create superuser
```bash
python manage.py createsuperuser
```

#### 8. Run development server
```bash
python manage.py runserver
```

Visit http://localhost:8000 to access the application.

### Production Deployment

For production deployments, set the `DATABASE_URL` environment variable to use Cloud SQL or PostgreSQL:
```bash
export DATABASE_URL=postgresql://user:password@host:port/database
```

When `DATABASE_URL` is set, the application will use PostgreSQL instead of SQLite.

---

## Technology Stack

- Backend: Django
- Frontend: HTML, CSS, JavaScript
- Visualization: Chart.js
- Database: PostgreSQL (production) / SQLite (development)
- File Storage: Cloud Storage (configurable)
- AI Integration: OpenAI API

---

## Project Structure

```
gtm/
├── gtm/                     # Main Django app
│   ├── models.py            # Data models
│   ├── views.py             # View logic
│   ├── urls.py              # URL routing
│   ├── templates/           # HTML templates
│   ├── static/              # CSS, JavaScript, images
│   └── management/          # Management commands
├── config/                  # Django settings
├── requirements.txt         # Python dependencies
└── manage.py               # Django management script
```

---

## Contributing

Contributions are welcome. Please follow these guidelines:
1. Create a feature branch from master
2. Make your changes with clean, descriptive commits
3. Ensure all tests pass
4. Create a pull request with a clear description

---

## License

MIT License - See LICENSE file for details
