# Project Overview

This is a Django project called **GTM Validator**, a "Go-To-Market Readiness Tool". It assesses a business's go-to-market strategy through a multi-step questionnaire. It then uses AI to provide insights and generates a final "Playbook" report.

## Key Technologies

*   **Backend:** Django
*   **Frontend:** Server-rendered HTML with HTMX for dynamic interactions and Tailwind CSS for styling.
*   **AI:** Google Cloud's Vertex AI with Gemini models is used for insights, the final report, chat features, and multimodal auditing.
*   **PDF Generation:** ReportLab is used to create downloadable PDF reports.
*   **Authentication:** `django-allauth` is used for user authentication.

## Application Structure

The project is divided into two main Django apps:
1.  `gtm`: The main user-facing application containing the assessment, results, and AI chat.
2.  `dashboard`: An administrative area for viewing analytics and managing KPIs.

## Key Features

*   **Anonymous User Tracking:** It uses a persistent cookie to track users (even if not logged in), allowing them to return and resume their assessment.
*   **AI Integration:** AI is deeply integrated to analyze user responses and generate personalized content.
*   **Admin Dashboard:** A separate interface exists for internal monitoring and data management.

# Building and Running

## 1. Installation

```bash
# Clone the repository
git clone <repository-url>
cd gtm

# Create a virtual environment and activate it
python -m venv venv
source venv/bin/activate # on Windows, use `venv\Scripts\activate`

# Install dependencies
pip install -r requirements.txt

# Set up environment variables
cp .env.example .env
# Edit .env and add your DJANGO_SECRET_KEY, GCP_PROJECT_ID, GOOGLE_APPLICATION_CREDENTIALS, and email settings
```

## 2. Running the Development Server

```bash
# Apply migrations
python manage.py migrate

# Run the development server
python manage.py runserver
```

The application will be available at `http://127.0.0.1:8000/`.

## 3. Running Tests

```bash
# TODO: Add instructions for running tests.
# It is not clear from the project structure how to run tests.
```

# Development Conventions

*   **Styling:** The project uses Tailwind CSS. The `theme` app is configured as the Tailwind app. To re-build the CSS, you may need to run `npm install` in the `theme/static_src` directory and then use a command like `npm run build`.
*   **AI Services:** All interactions with Vertex AI are handled in the `gtm/ai_services.py`, `gtm/ai_chat.py`, and `gtm/ai_auditor.py` modules. Uses the unified `google.genai` SDK with `vertexai=True` flag.
*   **Views:** The core application logic is in `gtm/views.py`.
*   **Templates:** Templates are located in the `templates` directory of each app.
*   **Middleware:** The `gtm/middleware.py` file contains custom middleware, including `EnsureClientIdMiddleware` for tracking anonymous users.
*   **Logging:** The project is configured to log errors to a file named `gtm_errors.log`.
