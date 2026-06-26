# **GTM Question Audit — Week 1 Summary**

**Scope:** 18 assessment questions across Demand (6), Conversion (6), and Delivery (6)  
**Source:** `gtm/management/commands/load_gtm_defaults.py`  
**Matrix:** `docs/question_audit_matrix.csv`

---

## **Top 3 Redundancies**

### **1\. DEL-TTV-01 ↔ DEL-ONB-02 (Time to Value vs Onboarding Discipline)**

**Severity:** Partial — highest overlap in the matrix

Both questions probe how quickly customers reach value post-sale. TTV-01 measures the *outcome* (time to first value by segment); ONB-02 measures the *process* (milestone-based onboarding with owners and tracking). A company with strong onboarding will score high on both; weak onboarding will score low on both. Respondents may not distinguish between them.

**Recommendation:** Keep both, but clarify wording. Merge into one Delivery question and add a net-new capability.

---

### **2\. DEM-FIT-02 ↔ CON-QLF-02 (Lead Quality vs Qualification)**

**Severity:** Partial — cross-pillar overlap

DEM-FIT-02 asks whether inbound leads match ICP (Demand). CON-QLF-02 asks whether a shared qualification checklist gates pipeline stages (Conversion). Both test *fit assessment* at different funnel stages. High correlation likely for companies with unified RevOps; low distinction for respondents.

**Recommendation:** Differentiate explicitly. DEM-FIT \= *top-of-funnel fit rate*; CON-QLF \= *sales-stage gating discipline*. Consider adding "inbound only" language to DEM-FIT-02.

---

### **3\. DEM-CHN-04 ↔ DEM-ATT-05 (Channel Strategy vs Attribution)**

**Severity:** Partial — same pillar, adjacent capabilities

CHN-04 tests whether a channel *plan* exists with CAC and pipeline targets. ATT-05 tests whether *attribution reporting* shows first-touch and influence. Companies with good channel plans often have attribution; companies without attribution cannot validate channel plans. Thematic clustering may cause similar scores.

**Recommendation:** Not redundant enough to remove either. Flag in scoring that both are RevOps-owned and may correlate. ATT-05 proposed weight reduced to 1.1 (see weight concerns).

---

## **Top 3 Weight Concerns**

### **1\. DEM-ATT-05 weighted 1.2 at "optimized" maturity**

Attribution is the most mature-stage Demand question but carries equal weight to DEM-FIT-02 (1.2) and near DEM-ICP-01 (1.25). Early-stage companies will consistently score low here, dragging Demand scores without reflecting foundational gaps. **Proposed weight: 1.1** — still important but not penalizing immature orgs as heavily.

### **2\. DEM-CNT-06 weighted 1.0 (lowest in Demand)**

Execution cadence is the operational engine of pipeline creation but is the lowest-weighted Demand question. Measurement questions (ATT, FIT, ICP) dominate scoring while execution — often the first fix for early-stage teams — is underrepresented. **Proposed weight: 1.1**.

### **3\. DEL-ADV-06 weighted 0.95 (lowest in entire matrix)**

Advocacy (case studies, references, proof points) closes the GTM flywheel back to Demand/Positioning (DEM-MSG-03) but has the lowest weight of all 18 questions. Underweighting advocacy undervalues a key growth lever for B2B. **Proposed weight: 1.05**.

---

## **Missing Capabilities**

The current 18-question framework does not explicitly measure:

| Missing Capability | Why It Matters | Suggested Pillar |
| ----- | ----- | ----- |
| **Pricing & packaging** | Pricing alignment with ICP and value message is a core GTM decision; mispricing kills conversion regardless of pipeline quality | Demand or Conversion |
| **Sales-marketing SLA / handoff** | Formal lead handoff rules between marketing and sales are industry standard (SiriusDecisions demand waterfall) but only partially covered by CON-SLA-01 | Conversion |
| **Sales methodology / discovery** | No question on structured discovery (MEDDIC, SPIN, Challenger) — distinct from objection handling (CON-OBJ-04) | Conversion |
| **RevOps / data hygiene** | CRM adoption and data completeness underpin FIT, QLF, STG, and ATT questions but are never measured directly | Cross-pillar |
| **Partner / ecosystem channel** | DEM-CHN-04 covers paid/owned channels but not partner reseller or integration-led GTM | Demand |
| **Product-led growth motion** | No question on self-serve trial/freemium conversion — common B2B SaaS motion | Conversion |
| **Expansion / upsell motion** | DEL-QBR-05 touches expansion in reviews but no dedicated measure of land-and-expand revenue growth | Delivery |

**Priority adds for Week 2:** Sales-marketing handoff SLA, RevOps data hygiene, and pricing/packaging alignment would close the largest gaps without exceeding \~21 questions.

---

## 

