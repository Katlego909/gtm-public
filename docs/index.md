# GTM Validator: The Big Picture

> **Project Mission:** To provide startups with a "Data-Driven Second Opinion" on their Go-To-Market strategy, bridging the gap between qualitative maturity and quantitative growth.

## 🌟 The Core Value Proposition

The GTM Validator isn't just a survey tool; it's a **Maturity Operating System**. It solves the three most common problems for scaling companies:

1.  **Blind Spots:** Identifying exactly where execution is failing (e.g., "We have leads, but our Win Rate is low").
2.  **Strategic Paralysis:** Removing the "What now?" by using AI to write a specific, actionable 30-day playbook.
3.  **Proof Gap:** Validating that your "Best-in-Class" claims match the reality of your sales decks and landing pages.

---

## 🛠️ The Tech Stack Vision

We chose a "Modern Monolith" architecture to ensure maximum speed of delivery and zero friction between the AI and the Database.

-   **The Brain (Django 5.2.7):** Handles the complex weighted scoring and multi-tenant security.
-   **The Eyes (Google Vertex AI / Gemini):** Performs multimodal audits of images and PDFs.
-   **The Interaction (HTMX & Tailwind):** Provides a seamless, "Single Page App" experience without the overhead of a heavy JS framework.
-   **The Memory (PostgreSQL & Redis Cache):** Ensures results are snapshotted and AI responses are throttled for reliability.

---

## 🚀 Key Innovation: The "Vision-to-Scoring" Bridge

One of the project's unique architectural features is the **Automated Auditor**. In most platforms, the user self-reports their score. In GTM Validator, the user uploads their **Strategic Evidence**, and the AI Auditor calculates a **Score Modifier** (-3 to +3) that adjusts their maturity rating based on actual asset quality.

---

## 🗺️ Documentation Roadmap

This documentation is designed to serve as a **Single Source of Truth** for three distinct audiences:

-   **Business Leaders:** Use the [Plain English Guide](plain_english_guide.md) to understand the ROI and product vision.
-   **Senior Developers:** Use the [Developer Deep Dive](developer_deep_dive.md) and [Core Logic](core_logic.md) to understand the weighted math and AI orchestration.
-   **DevOps & Infrastructure:** Use the [Management Tools](maintenance_tools.md) and [Data Models](data_models.md) to understand environment setup and data persistence.

---
*The GTM Validator is built to turn business uncertainty into strategic momentum.*
