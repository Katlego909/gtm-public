# GTM Validator - Go-To-Market Readiness Tool

GTM Validator is a Django-based web application that helps startups and businesses assess their Go-To-Market (GTM) readiness. It walks users through structured questions across key GTM pillars — Demand, Conversion, and Delivery — and provides automated scoring, insights, visualized dashboards, tailored recommendations, and playbook generation.

This is the **`internship` branch**. It contains everything in `master`, plus a substantial round of new features built during the internship project: AI that reads your own documents to score you, a transparent math-based scoring engine, deeper AI-written business insights, and a smarter PDF report. Everything below in this section explains what's new compared to `master` — first in plain English, then a peek at the technology making it work.

---

## 🆕 What's New on This Branch

### 1. The AI can now read your own documents and score you automatically

**In plain English:** Up to now, you had to read each assessment question and decide for yourself what score (1–5) your company deserves. Now, for every question, you can instead **upload real evidence** — a sales report, a spreadsheet of pipeline numbers, a CRM export, a marketing deck, a screenshot of a dashboard, even a Word doc — and the AI will read it for you, find the parts that actually answer the question, and propose a score along with a short explanation, a quoted piece of evidence, and how confident it is. You always stay in control: every AI-suggested score can be opened up and changed by hand if you disagree. This started out only available for the "Delivery" questions, and now works for "Demand" and "Conversion" questions too. You also get a live progress indicator while it works, and the result appears automatically once it's done — no need to refresh the page.

**Under the hood:** Uploaded files (CSV, Excel, PDF, Word, plain text, JSON, or images, up to 10MB) are parsed with format-specific readers (`openpyxl` for Excel, `pypdf` for PDF, `python-docx` for Word; images are sent straight to Gemini's vision model to be read). The extracted text then goes through a three-stage pipeline before any AI scoring happens:
- A keyword-relevance search (`scikit-learn`'s TF-IDF + cosine similarity) finds passages that share important words with the question.
- A "meaning-based" search (`sentence-transformers`, a lightweight AI model called MiniLM) catches passages that answer the question in different words — e.g. "we ship every Friday" still matches a question about deployment frequency, even with zero shared keywords.
- An entity/number spotter (`spaCy`, backed by a regex fallback) automatically pulls out percentages, dollar amounts, dates, and common GTM metrics (CAC, LTV, ARR, NRR, MRR, NPS, etc.) from the text.

Only the most relevant evidence found by these three steps — not the entire document — is then handed to **Google Gemini (`gemini-2.5-flash`)**, which makes the actual scoring judgment and writes the reasoning. This keeps the AI focused, fast, and grounded in your real documents instead of guessing.

### 2. A transparent, math-only scoring engine (no AI guesswork involved)

**In plain English:** Separate from the AI, there's now a built-in "calculator" that scores your answers using fixed, published rules — the same answers always produce the exact same score, with no randomness and no AI involved. It does three useful things: it tells you which single question, if improved, would boost your overall score the most (your best "bang for your buck" fix); it recognizes common, named patterns of trouble (for example, "you don't have a clearly defined ideal customer" or "you're losing deals at the qualification stage") and explains each one in a sentence; and it feeds these same priorities to the AI, so the written playbook you get is required to talk about your *actual* top issues rather than generic advice.

**Under the hood:** A new `scoring_engine.py` module computes weighted averages per pillar (Demand/Conversion/Delivery, weighted 40/40/20), ranks "opportunity scores" (the points you'd gain by maxing out each question, scaled by that question's importance), and runs your scores through about a dozen hardcoded pattern-detection rules. This structured output is then passed into the Gemini prompt that writes your playbook, so the AI's narrative is anchored to the same numbers shown on your dashboard.

### 3. Deeper, more personal AI-written insights in your report

**In plain English:** Your results and playbook pages now include two new boxed sections: **"Financial Impact Estimates"** (a rough estimate of what your current gaps might be costing you) and **"Competitive Gap Analysis"** (how those gaps could let competitors get ahead). Both are written specifically about your company, not generic filler. The "Recommended Next Moves" section is now genuinely tailored to your top priorities, and a matching "Recommended Tools" list is generated to go with it.

**Under the hood:** Two additional calls to **Google Gemini** generate the financial and competitive write-ups, with prompts that explicitly forbid the AI from giving recommendations in these two sections (they're meant to describe impact and risk, not prescribe fixes — recommendations live elsewhere). A separate Gemini call extracts a clean, short bullet-point action list out of the longer playbook text for the "Recommended Tools"/action-plan UI.

### 4. A smarter, more personal downloadable PDF report

**In plain English:** The "30-Day Implementation Framework" in your downloadable PDF used to be the exact same generic template for every single company. Now it's built from your own AI-generated priorities, so the plan you download actually reflects your specific weak spots. The new Financial Estimates and Competitive Gap sections are included in the PDF too, and the downloaded file is now named after your company (e.g. `Acme Inc_AI_playbook_report_ForgeGTM.pdf`) instead of a generic ID.

**Under the hood:** `utils_pdf.py` now parses the AI-generated playbook markdown to pull out its priority sections and builds the week-by-week PDF table from that real content, and adds two new styled boxes (built with `ReportLab` tables) for the financial/competitive sections.

### 5. Easier navigation — you're no longer stuck moving forward only

**In plain English:** Added "Back" buttons so you can return to the Delivery questions from your GTM Assessment Report, and return all the way back to your company details from the very first assessment question — useful if you spot a typo or want to update something without restarting the whole assessment.

### 6. Smarter question types, not just 1–5 sliders

**In plain English:** A few of the Conversion questions used to force a generic 1–5 rating even when a real answer would fit better. Some now use checkboxes, single-choice pickers, or a frequency selector ("never / monthly / weekly / daily") that maps to a score behind the scenes — for example, checking your numbers weekly scores best, slightly better than checking them every single day, which the tool treats as a sign of micromanaging rather than rigor.

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
git clone https://github.com/Katlego909/gtm-public.git
cd gtm-public
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
Copy the `.env.example` file to create your `.env` file:
```bash
cp .env.example .env
```

Then edit the `.env` file with your configuration. See `.env.example` for all available options.

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
- Frontend: HTML, CSS, JavaScript, [HTMX](https://htmx.org/) (lets parts of a page — like a "still analyzing..." spinner — refresh themselves automatically by quietly polling the server every few seconds, without a full page reload)
- Visualization: Chart.js
- Database: PostgreSQL (production) / SQLite (development)
- File Storage: Cloud Storage (configurable)
- AI Integration: Google Gemini (via Vertex AI) — the model used for scoring evidence and writing insights/playbooks is `gemini-2.5-flash`
- PDF Generation: ReportLab
- Document Parsing (for AI-scored evidence uploads): `openpyxl` (Excel), `pypdf` (PDF), `python-docx` (Word) — plus Gemini's own vision capability for reading screenshots/images directly
- NLP / Evidence Ranking (used to find the most relevant passage in an uploaded document before the AI scores it):
  - `scikit-learn` — keyword-relevance matching (TF-IDF + cosine similarity)
  - `sentence-transformers` (MiniLM model) — meaning-based matching, catches paraphrased answers that don't share exact keywords
  - `spaCy` — automatically detects numbers, dates, percentages, and GTM-specific metrics (CAC, LTV, ARR, etc.) in free text

---

## Project Structure

```
gtm-public/
├── .env                     # Environment configuration (CREATE THIS - not in repo)
├── .env.example             # Example environment variables
├── .gitignore               # Git ignore file
├── manage.py                # Django management script
├── requirements.txt         # Python dependencies
├── Dockerfile               # Docker configuration
├── cloudbuild.yaml          # Google Cloud Build config
│
├── keys/                    # GCP Service Account Credentials (CREATE THIS - not in repo)
│   └── service-account.json # Service account JSON key (git-ignored)
│
├── config/                  # Django settings
│   ├── settings.py          # Main settings
│   ├── urls.py              # URL routing
│   └── wsgi.py              # WSGI config
│
├── gtm/                     # Main GTM assessment app
│   ├── models.py            # Data models
│   ├── views.py             # View logic
│   ├── urls.py              # URL routing
│   ├── ai_services.py       # AI/Gemini integration
│   ├── templates/           # HTML templates
│   ├── static/              # CSS, JavaScript, images
│   └── management/          # Management commands
│
├── dashboard/               # Dashboard & analytics app
│   ├── models.py            # Dashboard models
│   ├── views.py             # Dashboard views
│   ├── analytics.py         # Analytics logic
│   └── templates/           # Dashboard templates
│
├── conductor/               # Deployment & automation
│   └── deploy_cloud_run.md  # Cloud Run deployment guide
│
├── docs/                    # Documentation
├── theme/                   # UI theme/styling
├── venv/                    # Python virtual environment (auto-created)
└── workspace_resources/     # Workspace-related resources
```

**Important:** After cloning, create these missing directories and files:
- Create `keys/` folder and add `service-account.json` (from GCP)
- Create `.env` file based on `.env.example`

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
