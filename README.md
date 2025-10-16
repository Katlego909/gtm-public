# 🚀 GTM Validator (Go-To-Market Readiness Tool)

**GTM Validator** is a Django-based web application that helps startups and businesses assess their **Go-To-Market (GTM)** readiness.  
It walks users through structured questions across key GTM pillars — **Demand**, **Conversion**, and **Delivery** — and provides:
- Automated scoring & insights  
- Visualized performance dashboards  
- Tailored recommendations  
- Playbook generation (PDF export)  
- Recommended tools for improvement  

---

## 🌟 Features

### 🧭 GTM Assessment Workflow
- Dynamic multi-step form using Django forms.
- Scored responses stored per session (resumable assessments).
- Weighted categories (Demand, Conversion, Delivery).

### 📊 Dashboard & Visualization
- Real-time radar chart visualization (via Chart.js).
- Category-level breakdowns and progress tracking.
- “Top Strengths” and “Focus Areas” based on scores.

### 🧩 Recommendations Engine
- Automatic “Recommended Next Moves” generated from score bands.
- Category-level tool recommendations (e.g., HubSpot, Notion, Hotjar).

### 🧠 Playbook & Reports
- Auto-generated GTM Playbook with insights, focus areas, and recommendations.
- One-click **Download as PDF**.
- Persistent session history (resume where you left off).

### 🪄 Admin Tools
- Manage categories, questions, recommendation bands, and tool data.
- Load default dataset via management command.

---

## ⚙️ Installation & Setup

### 1️⃣ Clone the repository
```bash
git clone https://github.com/Katlego909/gtm.git
cd gtm
