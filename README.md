# Aetherra: AI-Driven Corporate Sustainability & Carbon Intelligence

**A Collaborative Engineering Project by students of Pillai College of Engineering (PCE)** 

Aetherra is an advanced ESG (Environmental, Social, and Governance) intelligence platform engineered to automate corporate carbon footprint tracking through high-precision AI extraction and real-time emission factor analysis. Specifically designed for the multi-domain operational landscapes of **Technology**, **Logistics**, **Manufacturing**, and **Construction**, the platform transforms raw corporate reports into actionable, audit-ready sustainability metrics.

---

## 👥 The Engineering Team

This project was a collaborative effort by four SE students at **Pillai College of Engineering (PCE)**, each specializing in a critical layer of the platform's architecture:

### **Amol Manish Tamhankar** — *AI Architect & System Lead*
Amol designed the core AI orchestration layer, moving away from brittle SDKs to robust direct-HTTP implementations. He engineered the **Multi-Tier AI Pipeline** (Groq + Gemini fallbacks) and optimized the deterministic prompting logic that ensures zero-hallucination data extraction from complex ESG PDFs.

### **Rudra Thakur** — *Lead Frontend Engineer & Data Visualization*
Rudra was responsible for the high-fidelity user interface and the dynamic reporting engine. He implemented the complex **Chart.js** logic that allows the dashboard to intelligently switch between Scope-based (Yearly) and Category-based (Monthly) visualizations based on the available data payloads.

### **Onkar Vagere** — *Backend & Database Infrastructure*
Onkar focused on the stability and security of the platform's foundation. He designed the **MongoDB schema** to handle semi-structured ESG data and implemented the **API Guard** security headers (CSP, HSTS) while hardening the Flask authentication and session management.

### **Rohan Shedge** — *ESG Research & Emission Integration*
Rohan spearheaded the sustainability research, mapping various global emission factors to the application's domain logic. He integrated the **Climatiq** and **Carbon Interface APIs** to ensure that Aetherra provides real-time, mathematically accurate carbon intensity data for corporate operations.

---

## 🚀 Vision & Innovation

Corporate carbon auditing is traditionally manual, siloed, and prone to error. Aetherra solves this by integrating **Large Language Models (LLMs)** with real-time **Grid Intensity APIs**, ensuring that every gram of CO2 is accounted for with mathematical certainty.

### 1. Multi-Tiered AI Extraction Pipeline (Groq + Gemini)
The platform utilizes a resilient, tiered AI architecture to parse unstructured PDF reports:
- **Groq Llama 3.3 (Primary Engine)**: Ultra-fast extraction of asset-level metrics and emission scopes.
- **Gemini 1.5 Pro (Stability Fallback)**: Robust secondary analysis to bypass rate limits or endpoint failures.
- **Strict Determinism**: Enforced `temperature: 0.0` extraction logic to eliminate AI hallucination and ensure 100% data consistency for every audit cycle.

### 2. Sector-Specific Domain Logic
Unlike generic trackers, Aetherra implements strict structural processing based on the company's operating sector:
- **Strict Domain Gatekeeper**: Prevents data contamination by rejecting mismatched reports (e.g., a Logistics report uploaded to a Technology workspace).
- **Proactive Industry Insights**: 80+ unique, peer-reviewed sustainability optimizations tailored to your specific field.

### 3. Real-time Carbon Synergy
- **Climatiq API Integration**: Fetches live regional grid intensity (gCO2/kWh) for dynamic operational updates.
- **Carbon Interface Engine**: Live calculation of logistics and fleet emissions based on the latest global emission factors.

## 🛠 Technical Architecture

- **Backend Logic**: Python 3.14 (Flask Micro-framework)
- **Database Architecture**: MongoDB (NoSQL) 
- **Natural Language Processing**: Groq Cloud SDK & Google AI Studio
- **Interactive UI**: Responsive Vanilla CSS & Chart.js 
- **Cyber-Security Infrastructure**: Fully hardened with CSP, XSS-block, and HSTS headers.

## 📦 Local Deployment Strategy

1. **Repository Initialization**
   ```bash
   git clone https://github.com/AMOLOP007/carbon-footprint-tracker.git
   cd carbon-footprint-tracker
   ```

2. **Environment Configuration**
   Create a `.env.local` file in the root directory:
   ```ini
   # Database Gateway
   MONGODB_URI=mongodb://localhost:27017/aetherra
   
   # Orchestration Keys (Groq, Gemini, etc.)
   # ... [Details in local setup guide]
   ```

3. **Runtime Execution**
   ```bash
   pip install -r requirements.txt
   py app.py --no-reload
   ```

---
*Aetherra — Data-driven sustainability for the future.*
