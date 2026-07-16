# GTM Validator - Go-To-Market Readiness Tool

GTM Validator is a Django-based web application that helps startups and businesses assess their Go-To-Market (GTM) readiness. It walks users through structured questions across key GTM pillars — Demand, Conversion, and Delivery — and provides automated scoring, insights, visualized dashboards, tailored recommendations, and playbook generation.

This is the **`internship` branch**. It contains everything in `master`, plus a substantial round of new features built during the internship project: AI that reads your own documents to score you, a transparent math-based scoring engine, deeper AI-written business insights, and a smarter PDF report. Everything below in this section explains what's new compared to `master` — first in plain English, then a peek at the technology making it work.

---

## 🆕 What's New on This Branch

### 1. Every question's weight — and a lot of its wording — was rebuilt from research

**In plain English:** All 18 assessment questions (6 each for Demand, Conversion, and Delivery) used to be weighted almost equally, as if a question about whether customers occasionally recommend you mattered nearly as much as a question about whether customers are quietly about to cancel. They don't — losing a customer you already have is far more damaging than not yet having a referral program. So every weight was re-derived from research into what actually drives revenue and retention. Picture it as a pyramid: foundational things sit at the base and hold everything above them up — each layer above only matters once the layer below it is solid. For Delivery, **Health Monitoring** (spotting unhappy customers before they leave) is the base and carries the most weight; **Advocacy** (customers actively referring you) sits at the top and carries the least — if you're not even watching for unhappy customers, it doesn't matter how good your referral program is. These are the actual priority pyramids the team researched and built, one per pillar:

<p align="center">
  <img src="docs/images/demand-priority-pyramid.png" alt="Demand Priority Pyramid" width="32%" />
  <img src="docs/images/conversion-priority-pyramid.png" alt="Conversion Priority Pyramid" width="32%" />
  <img src="docs/images/delivery-priority-pyramid.png" alt="Delivery Priority Pyramid" width="32%" />
</p>

Same numbers, in table form:

| Pillar | Dimension | Weight |
|---|---|---|
| Demand | ICP Clarity | 22% |
| Demand | Channel Strategy | 20% |
| Demand | Positioning | 18% |
| Demand | Lead Quality | 16% |
| Demand | Attribution | 14% |
| Demand | Execution Cadence | 10% |
| Conversion | Pipeline Hygiene | 22% |
| Conversion | Win-Loss Learning | 22% |
| Conversion | Qualification | 19% |
| Conversion | Funnel Optimization | 16% |
| Conversion | Speed to Lead | 13% |
| Conversion | Deal Enablement | 8% |
| Delivery | Health Monitoring | 24% |
| Delivery | Retention | 22% |
| Delivery | Time to Value | 18% |
| Delivery | Onboarding Discipline | 16% |
| Delivery | Success Governance | 12% |
| Delivery | Advocacy | 8% |

Several questions were also reworded from internal, RevOps-flavored language into plain terms anyone in the business can answer honestly — for example, the old attribution question was rewritten to the much more direct *"Can you reliably identify which marketing channels generate qualified leads and pipeline, using reporting data you trust?"* The redesign also went further on five Conversion questions, where a generic 1–5 rating was replaced on paper with something more honest to how the question is actually answered: a plain yes/no gate (e.g. "are qualification fields mandatory before a deal can advance stages?"), a checklist of true/false statements (e.g. pipeline-stage discipline — do criteria exist, are they enforced, is the data reviewed monthly), or a 5-rung "maturity ladder" you pick one rung from (win/loss analysis runs from "we don't capture reasons at all" up to "we review monthly and act on it"). Each option still maps to a 1–5 score behind the scenes, the same as before — this part of the redesign shipped as data and scoring rules, but the on-screen question form hasn't been switched over yet, so today every question (including these five) still displays as the same 1–5 scale picker. The new wording and weights are live; the new input widgets are designed and ready in the data, pending a front-end pass to actually render them.

**Under the hood:** Weights and wording live in `gtm/management/commands/load_gtm_defaults.py`; the weight table itself is mirrored in `gtm/scoring_engine.py: QUESTION_META` so the scoring engine and the question bank never drift out of sync. The planned input types (`single_select`, `multi_select`, `frequency_select`) are backed by three new `Question` model fields — `input_type`, `input_options`, `input_option_labels` — plus a `scoring_logic` map that converts a selected option into a 1–5 score. Right now these fields are populated by the fixture data but not yet read by `assessment_step.html` or `views.py`'s answer-handling — the assessment screen still renders every question with the original radio-button scale.

### 2. The AI can now read your own documents and score you automatically

**In plain English:** Up to now, every answer was self-reported — a company says "yes, we do health monitoring" and that's taken at face value. Now, for every question, you can instead **upload real evidence** — a sales report, a customer contract, an onboarding guide, a QBR template, a health dashboard, a CRM export, even a screenshot — and the AI reads it, finds the parts that actually answer the question, and proposes a score along with a short explanation, a quoted piece of evidence, and how confident it is. Think of it like the difference between telling your bank you earn $80,000 and actually uploading your payslip — this does the same thing for GTM maturity. You always stay in control: every AI-suggested score can be opened up and changed by hand if you disagree. This started out only available for "Delivery," and now works for "Demand" and "Conversion" too, with a live progress indicator while it works and no need to refresh the page when it's done.

**Under the hood:** Uploaded files (CSV, Excel, PDF, Word, plain text, JSON, or images, up to 10MB) are parsed with format-specific readers (`openpyxl` for Excel, `pypdf` for PDF, `python-docx` for Word; images are sent straight to Gemini's vision model to be read). The extracted text then goes through a three-stage NLP pipeline before any AI scoring happens:
- A keyword-relevance search (`scikit-learn`'s TF-IDF + cosine similarity) finds passages that share important words with the question.
- A "meaning-based" search (`sentence-transformers`, a lightweight AI model called MiniLM) catches passages that answer the question in different words — e.g. "we ship every Friday" still matches a question about deployment frequency, even with zero shared keywords.
- An entity/number spotter (`spaCy`, backed by a regex fallback) automatically pulls out percentages, dollar amounts, dates, and common GTM metrics (CAC, LTV, ARR, NRR, MRR, NPS, etc.) from the text.

Only the most relevant evidence found by these three steps — not the entire document — is then handed to **Google Gemini (`gemini-2.5-flash`)**, which makes the actual scoring judgment and writes the reasoning. This keeps the AI focused, fast, and grounded in your real documents instead of guessing.

### 3. A transparent, math-only recommendation engine (no AI guesswork deciding what matters)

**In plain English:** Before, your overall score just placed you in one of five bands, and everyone in a band got the same three generic bullet points — a company with no pipeline saw the same advice as a company whose customers were quietly churning. Now there's a built-in "calculator" that runs the actual math: same inputs always produce the same output, with zero randomness and zero AI involved in deciding what's wrong. It does two things. First, it works out exactly which single question, if improved, would add the most points to your overall score — your best "bang for your buck" fix, expressed as real points gained, not a vague suggestion. Second, it recognizes about a dozen named patterns of trouble by looking at your actual question-level scores, each with a plain-English meaning — for example **"ICP Undefined"** (no clear ideal customer), **"Demand Starved"** (not enough qualified leads coming in at all), **"Conversion Leak"** (leads come in but don't convert), **"Pipeline Blind"** (no visibility into *why* deals are won or lost, so the same mistakes repeat), **"Delivery Churn Risk"** (customers are leaving and nobody's watching), and **"Scaling Ready"** (the positive case — fundamentals are solid enough to push harder on growth) — and multiple patterns can fire at once, so a company with both a demand problem and a delivery problem gets a plan addressing both, ranked by impact. Gemini is then only allowed to add company- and industry-specific *language* around these already-decided priorities — it never gets to invent its own diagnosis.

**Under the hood:** A new `scoring_engine.py` module computes pillar scores as a true weighted average (`Σ(score × question_weight) / Σ(question_weight)` per pillar, then `Σ((pillar_avg/5) × pillar_weight × 100)` for the 0–100 overall score, where Demand and Conversion are each worth up to 40 points and Delivery up to 20). It ranks "opportunity scores" per question (`(5 − score) × question_weight × pillar_weight`, i.e. exactly how many overall points are on the table) and evaluates the score set against named pattern-detection rules. This structured output (top priorities + fired patterns) is passed into the Gemini prompt that writes the playbook, so the AI's narrative is required to be anchored to the same numbers shown on your dashboard rather than free-associating from a category average.

### 4. AI Insights that diagnose your business, not prescribe generic advice

**In plain English:** Under each low-scoring question on your results page there's an "AI Insight" box. It used to hand out recommendations — "Implement a CRM workflow," "Automate your attribution reporting" — which meant every company with a low score on that question got essentially the same advice. It wasn't a diagnosis, it was a generic prescription copy-pasted by an algorithm. It's been rewritten to do one thing only: describe what's specifically wrong at *your* company, by name, referencing your actual CRM, size, revenue range, and country — e.g. *"Ardent SA, a 50-person professional services firm in South Africa operating without a formal CRM, has no consistent method for identifying which marketing channels generate qualified leads."* No suggestions, no "you should." And because AI doesn't always follow instructions perfectly, there's also an automatic safety net that strips out any sentence starting with an action verb ("Implement," "Build," "Set up," "Automate," and ~50 others), regardless of what the AI tries to write.

**Under the hood:** The diagnostic prompts in `ai_services.py` were rewritten to forbid recommendation language entirely, and a regex-based post-processor, `_strip_imperative_sentences`, scans every generated insight before it's saved and removes any sentence matching an imperative-verb pattern — a deterministic failsafe that runs every time, independent of how well the AI followed its instructions.

### 5. Your task list now fills itself in automatically

**In plain English:** Previously, when you finished your assessment, you landed on a completely blank to-do list and had to figure out what to put in it yourself. Now it's pre-filled and ranked for you the moment your results are ready — most of it pulled straight from your personalized playbook, plus one extra "quick win" pulled from the recommendation engine's highest-priority pattern. If the same task would otherwise show up twice, the system recognizes the overlap and only keeps one copy.

**Under the hood:** `views.py` auto-populates `ActionItem` rows on first visit if none exist: roughly 90% of tasks come from `ai_services.extract_tasks_from_playbook` (a Gemini call that extracts concrete action items from the playbook text as structured JSON, with a token-limit fix so large playbooks don't silently return empty), and 10% comes from the scoring engine's top-severity pattern's `quick_win` text. A word-overlap check (shared words ÷ total words > 0.6) prevents the quick-win task from duplicating something the AI already extracted.

### 6. Deeper, more personal AI-written insight sections in your report

**In plain English:** Your results and playbook pages include two boxed sections: **"Financial Impact Estimates"** (a rough estimate of what your current gaps might be costing you) and **"Competitive Gap Analysis"** (how those gaps could let competitors get ahead). Both are written specifically about your company, not generic filler, and neither is allowed to slip in a recommendation — they're meant to describe impact and risk only. The "Recommended Next Moves" section now shows a clean, personalized "Action Plan" bullet list instead of dense markdown prose, alongside a "Recommended Tools" list automatically matched to your two weakest categories.

**Under the hood:** Two Gemini calls (`generate_enrichment_sections` in `ai_services.py`) generate the financial and competitive write-ups in the background and save them once ready; the page polls itself via HTMX every 6 seconds and swaps in the content automatically, no refresh needed. The "Action Plan" bullets come from `extract_tasks_from_playbook`; "Recommended Tools" comes from the existing `ToolRecommendation` data, now filtered specifically to your two lowest-scoring categories instead of shown generically.

### 7. A smarter, more personal downloadable PDF report

**In plain English:** The "30-Day Implementation Framework" in your downloadable PDF used to be the exact same generic template for every single company — Week 1 Audit, Week 2 Process, same text every time. Now it's built from your own AI-generated priorities, so the plan you download actually reflects your specific weak spots. The Financial Estimates and Competitive Gap sections are included in the PDF too, and the downloaded file is now named after your company (e.g. `Acme Inc_AI_playbook_report_ForgeGTM.pdf`) instead of a generic ID.

**Under the hood:** `utils_pdf.py` (built on the `ReportLab` library, which constructs PDF layouts entirely in code) now parses the AI-generated playbook markdown to pull out its priority sections and builds the week-by-week PDF table from that real content, and adds two new styled boxes for the financial/competitive sections.

### 8. Easier navigation — you're no longer stuck moving forward only

**In plain English:** Added "Back" buttons so you can return to the Delivery questions from your GTM Assessment Report, and return all the way back to your company details from the very first assessment question — useful if you spot a typo or want to update something without restarting the whole assessment.

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
