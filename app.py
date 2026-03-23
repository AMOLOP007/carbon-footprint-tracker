from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from pymongo import MongoClient
from dotenv import load_dotenv
from datetime import datetime
from bson import ObjectId
import os
import io
import json
import random
import PyPDF2

from authlib.integrations.flask_client import OAuth
from openai import OpenAI
import requests as http_requests  # renamed to avoid conflict with flask.request

# load env vars
load_dotenv('.env.local')

app = Flask(__name__)
# Generate a cryptographically strong secret key natively
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(32).hex())

# ========== SECURITY HARDENING ==========
@app.after_request
def apply_security_headers(response):
    # Prevent Clickjacking
    response.headers["X-Frame-Options"] = "DENY"
    # Prevent MIME-Sniffing and injection
    response.headers["X-Content-Type-Options"] = "nosniff"
    # HTTP Strict Transport Security (HSTS)
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # Browser XSS Filter
    response.headers["X-XSS-Protection"] = "1; mode=block"
    # Content Security Policy (CSP - explicitly defining safe sources)
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://npmcdn.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://npmcdn.com; "
        "img-src 'self' data: https:; "
        "object-src 'none'; "
        "base-uri 'self';"
    )
    response.headers["Content-Security-Policy"] = csp
    # Referrer Policy
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response

# google oauth stuff
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=os.environ.get('GOOGLE_CLIENT_ID'),
    client_secret=os.environ.get('GOOGLE_CLIENT_SECRET'),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

# openai client for AI recommendations
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# connect to mongo
MONGO_URI = os.environ.get('MONGODB_URI', 'mongodb://localhost:27017/')

try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
    client.server_info()
    db = client['aetherra']
    users_col = db['users']
    reports_col = db['reports']
    calculations_col = db['calculations']
    companies_col = db['companies']
    goals_col = db['goals']
    print("MongoDB connected!")
except Exception as e:
    print(f"MongoDB not available: {e}")

# rate limiter so nobody spams the server
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"]
)

# ========== helper functions ==========

# check if login credentials match
def check_login(email, password):
    return users_col.find_one({"email": email, "password": password})

# create new user account
def create_user(name, email, password):
    existing = users_col.find_one({"email": email})
    if existing:
        return False

    users_col.insert_one({
        "name": name, "email": email, "password": password, 
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })
    return True

def send_welcome_email(to_email, name):
    sg_key = os.environ.get('SENDGRID_API_KEY')
    from_email = os.environ.get('SENDGRID_FROM_EMAIL', 'noreply@yourdomain.com')
    if not sg_key:
        raise ValueError("Missing SENDGRID_API_KEY")
    
    headers = {
        "Authorization": f"Bearer {sg_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": from_email, "name": "Aetherra Support"},
        "subject": f"Welcome to Aetherra, {name}!",
        "content": [{"type": "text/plain", "value": "Thanks for registering on the Aetherra platform. Let's reduce emissions together!"}]
    }
    resp = http_requests.post("https://api.sendgrid.com/v3/mail/send", headers=headers, json=payload, timeout=5)
    resp.raise_for_status()
    return True

def get_climatiq_electricity(kwh):
    cq_key = os.environ.get('CLIMATIQ_API_KEY')
    if not cq_key: raise ValueError("Missing CLIMATIQ_API_KEY")
    headers = {"Authorization": f"Bearer {cq_key}", "Content-Type": "application/json"}
    payload = {
        "emission_factor": {
            "activity_id": "electricity-supply_grid-source_supplier_mix",
            "region": "IN"
        },
        "parameters": {"energy": kwh, "energy_unit": "kWh"}
    }
    resp = http_requests.post("https://beta4.api.climatiq.io/estimate", headers=headers, json=payload, timeout=5)
    resp.raise_for_status()
    return resp.json().get('co2e', (kwh * 0.82) / 1000)

def get_carbon_interface_fuel(dist_km, vtype="car"):
    ci_key = os.environ.get('CARBON_INTERFACE_API_KEY')
    if not ci_key: raise ValueError("Missing CARBON_INTERFACE_API_KEY")
    headers = {"Authorization": f"Bearer {ci_key}", "Content-Type": "application/json"}
    payload = {
        "type": "vehicle",
        "distance_unit": "km",
        "distance_value": dist_km,
        "vehicle_model_id": "7268a9b7-17e8-4c8d-acca-57059252afe9"
    }
    resp = http_requests.post("https://www.carboninterface.com/api/v1/estimates", headers=headers, json=payload, timeout=5)
    resp.raise_for_status()
    return resp.json().get('data', {}).get('attributes', {}).get('carbon_kg', (dist_km * 0.25) / 1000)


# get all reports for a company (sorted newest first)
def get_all_reports(company_id):
    all_r = list(reports_col.find({"company_id": str(company_id)}).sort("_id", -1))
    for r in all_r:
        r['id'] = str(r['_id'])
        r['_id'] = str(r['_id'])
    return all_r

# grab a single report by its id
def get_report_by_id(report_id):
    try:
        r = reports_col.find_one({"_id": ObjectId(report_id)})
        if r:
            r['id'] = str(r['_id'])
            r['_id'] = str(r['_id'])
            # make sure all fields exist even if missing
            r.setdefault('transport_emissions', 0.0)
            r.setdefault('electricity_emissions', 0.0)
            r.setdefault('logistics_emissions', 0.0)
            r.setdefault('manufacturing_emissions', 0.0)
            r.setdefault('total_emissions', 0.0)
            r.setdefault('title', 'Emissions Report')
            r.setdefault('month', 'N/A')
            r.setdefault('ai_recommendations', {})
        return r
    except Exception as e:
        print("Error getting report:", e)
        return None

# get dashboard numbers for homepage
def get_stats(company_id):
    rc = reports_col.count_documents({"company_id": str(company_id)})
    calcs = list(calculations_col.find({"company_id": str(company_id)}).sort("_id", 1))

    total_em = sum(c.get('total_emissions', 0) for c in calcs) if calcs else 0
    latest = ""
    reduction = 0

    if calcs:
        last = calcs[-1]
        latest = f"Last report: {last.get('total_emissions', 0):.2f} tCO2e"

        # simple trend - compare first vs latest
        if len(calcs) > 1:
            first_val = calcs[0]['total_emissions']
            current_val = last['total_emissions']
            if first_val > 0:
                reduction = round(((first_val - current_val) / first_val) * 100)

    return {
        "total_emissions": round(total_em, 2) if total_em else 0,
        "monthly_reduction": reduction,
        "active_reports": rc,
        "latest_insight": latest or "Use the Emissions Calculator to get started."
    }

# ========== emission calculation ==========

# emission factors for different vehicle types (kg CO2 per km)
VEHICLE_FACTORS = {
    "car": 0.21,
    "bus": 0.089,
    "train": 0.041,
    "bike": 0.0,
    "motorcycle": 0.115
}

def calc_company_emissions(vehicle, km, kwh, ship_dist, ship_weight, mfg_hours):
    # transport - fleet vehicles
    factor = VEHICLE_FACTORS.get(vehicle, 0.21)
    transport = round(float(km) * factor / 1000, 4)

    # electricity - office/facility power usage
    electricity = round(float(kwh) * 0.82 / 1000, 4)  # india grid factor

    # logistics - shipping goods
    logistics = round(float(ship_dist) * float(ship_weight) * 0.0001 / 1000, 4)

    # manufacturing - basic mock: hours of machinery × factor
    manufacturing = round(float(mfg_hours) * 0.5 / 1000, 4)

    total = round(transport + electricity + logistics + manufacturing, 4)
    return {
        "transport": transport,
        "electricity": electricity,
        "logistics": logistics,
        "manufacturing": manufacturing,
        "total": total
    }

# ========== pdf generation ==========

def generate_pdf(report):
    from fpdf import FPDF
    import random

    pdf = FPDF()
    pdf.add_page()

    # Get company info for branding and domain
    comp = companies_col.find_one({"_id": ObjectId(report['company_id'])})
    domain = comp.get('domain', 'technology') if comp else 'technology'
    comp_name = comp.get('name', 'Our Company') if comp else 'Our Company'

    # --- HEADER ---
    pdf.set_fill_color(33, 37, 41)
    pdf.rect(0, 0, 210, 50, 'F')
    
    pdf.set_font("Helvetica", "B", 24)
    pdf.set_text_color(46, 204, 113)
    pdf.text(15, 25, "AETHERRA")
    
    pdf.set_font("Helvetica", "", 12)
    pdf.set_text_color(255, 255, 255)
    pdf.text(15, 35, f"ESG & Sustainability Report | {datetime.now().year}")

    # Logo
    try:
        if comp and comp.get('logo_path'):
            logo_path = comp.get('logo_path')
            if os.path.exists(logo_path):
                pdf.image(logo_path, x=160, y=10, h=30)
    except Exception as e:
        print("Logo render error:", e)

    pdf.ln(55)

    # --- EXECUTIVE SUMMARY ---
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(0, 0, 0)
    pdf.cell(0, 10, "1. Executive Summary", ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(50, 50, 50)
    
    period = report.get('month', 'Current Period')
    summary_text = (
        f"This report outlines the environmental impact of {comp_name} for the reporting period of {period}. "
        f"As a forward-thinking organization in the {domain.replace('_', ' ')} sector, we are committed to transparency "
        f"and continuous improvement in our carbon footprint management. Our total emissions for this period "
        f"reached {report.get('total_emissions', 0)} tCO2e."
    )
    pdf.multi_cell(0, 6, summary_text)
    pdf.ln(5)

    # --- OPERATIONAL NARRATIVE ---
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "2. Operational Emissions Narrative", ln=True)
    pdf.set_font("Helvetica", "", 10)
    
    if domain == "technology":
        narrative = (
            f"During this cycle, our primary energy consumption was driven by server infrastructure and IT operations. "
            f"Total energy consumption reached {report.get('electricity_emissions', 0) * 1219:.0f} kWh. Corporate travel "
            f"accounted for {report.get('flights', 0)} international flights and approximately {report.get('commute_km', 0)} km of employee commuting."
        )
    elif domain == "construction":
        narrative = (
            f"Construction site activities, including heavy machinery operation and material transport, were the main drivers. "
            f"On-site energy consumption totaled {report.get('electricity_emissions', 0) * 1219:.0f} units of fuel/power. "
            f"Logistics and material hauling covered a distance of {report.get('logistics_emissions', 0) * 5000:.0f} km."
        )
    elif domain == "manufacturing":
        narrative = (
            f"Manufacturing output and factory operations scaled this month. Total facility energy consumption "
            f"reached {report.get('electricity_emissions', 0) * 1219:.0f} kWh. Supply chain logistics involved "
            f"shipments totaling {report.get('logistics_emissions', 0) * 10000:.0f} kg across regional routes."
        )
    else: # logistics
        narrative = (
            f"Our fleet operations and warehouse logistics were the primary sources of impact. Total facility energy "
            f"consumption was {report.get('electricity_emissions', 0) * 1219:.0f} kWh. Total logistics and transport "
            f"distance recorded across all heavy vehicles was {report.get('transport_emissions', 0) * 4761:.0f} km."
        )

    pdf.multi_cell(0, 6, narrative)
    pdf.ln(10)

    # --- ASSET INVENTORY TABLE ---
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "3. Detailed Asset Inventory", ln=True)
    pdf.ln(2)
    
    # Table Header
    pdf.set_fill_color(240, 240, 240)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(60, 10, "Asset identifier", 1, 0, 'C', True)
    pdf.cell(60, 10, "Operational Type", 1, 0, 'C', True)
    pdf.cell(60, 10, "Emissions (tCO2e)", 1, 1, 'C', True)

    # Table Rows (Realistic 10 rows)
    pdf.set_font("Helvetica", "", 10)
    
    # Generate realistic asset IDs based on domain
    prefixes = {"technology": "Node", "construction": "EX", "logistics": "TRK", "manufacturing": "M"}
    prefix = prefixes.get(domain, "AST")
    
    avg_emissions = report.get('total_emissions', 0) / 10 if report.get('total_emissions', 0) > 0 else 0.5
    
    for i in range(1, 11):
        asset_id = f"{prefix}-{100 + i}"
        asset_type = "Primary Unit" if i < 5 else "Secondary System"
        emissions = round(avg_emissions * (0.8 + random.random()*0.4), 3)
        
        pdf.cell(60, 8, asset_id, 1, 0, 'C')
        pdf.cell(60, 8, asset_type, 1, 0, 'C')
        pdf.cell(60, 8, f"{emissions}", 1, 1, 'C')

    pdf.ln(10)
    
    # --- TOTALS ---
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_fill_color(46, 204, 113)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(120, 10, "TOTAL CORPORATE FOOTPRINT", 1, 0, 'R', True)
    pdf.cell(60, 10, f"{report.get('total_emissions', 0)} tCO2e", 1, 1, 'C', True)

    # footer
    pdf.set_y(-25)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(0, 5, f"Report generated by Aetherra AI B2B Intelligence", ln=True, align='C')
    pdf.cell(0, 5, "CONFIDENTIAL ESG DOCUMENT - NOT FOR REDISTRIBUTION", ln=True, align='C')

    # return raw bytes
    return bytes(pdf.output())


# ========== routes ==========

@app.route('/')
def splash():
    return render_template('splash.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        user = check_login(email, password)

        if user:
            session['user_id'] = str(user['_id'])
            session['user_name'] = user['name']
            session['user_email'] = user['email']
            session['company_id'] = user.get('company_id')
            
            if not session['company_id']:
                return redirect(url_for('onboarding'))
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid email or password.', 'error')
            return redirect(url_for('login'))

    return render_template('login.html')

@app.route('/employee-login', methods=['POST'])
def employee_login():
    passcode = request.form.get('access_code', '').strip().upper()
    name = request.form.get('employee_name', 'Employee').strip()
    
    comp = companies_col.find_one({"access_code": passcode})
    if not comp:
        flash('Invalid Organization Passcode. Ask your CEO for the correct code.', 'error')
        return redirect(url_for('login'))
        
    session['user_id'] = 'employee_' + str(comp['_id'])
    session['user_name'] = name
    session['company_id'] = str(comp['_id'])
    
    flash(f"Welcome to the {comp['name']} workspace!", "success")
    return redirect(url_for('dashboard'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        password = request.form['password']

        ok = create_user(name, email, password)
        if ok:
            from api_guard import safe_api_call
            safe_api_call('sendgrid', lambda: send_welcome_email(email, name), fallback_fn=lambda: print("Failsafe: Welcome email not sent."))
            flash('Account created! Please sign in.', 'success')
            return redirect(url_for('login'))
        else:
            flash('Email already registered.', 'error')
            return redirect(url_for('register'))

    return render_template('register.html')

@app.route('/onboarding', methods=['GET', 'POST'])
def onboarding():
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    if request.method == 'POST':
        company_name = request.form['company_name']
        domain = request.form['domain']
        post = request.form['post'].strip()
        
        # normalize post to catch variations
        def normalize_post(p):
            p = p.lower().strip()
            if p in ['ceo', 'c.e.o', 'c.e.o.', 'chief executive officer', 'chief executive']: return 'ceo'
            if p in ['manager', 'general manager']: return 'manager'
            if p in ['sales head', 'head of sales', 'sales manager']: return 'sales_head'
            if p in ['publicity head', 'head of publicity', 'pr head']: return 'publicity_head'
            if p in ['cto', 'chief technology officer']: return 'cto'
            if p in ['cfo', 'chief financial officer']: return 'cfo'
            return p
            
        norm_post = normalize_post(post)
        protected_roles = ['ceo', 'manager', 'sales_head', 'publicity_head', 'cto', 'cfo']
        
        comp = companies_col.find_one({"name": company_name})
        if not comp:
            import uuid
            access_code = str(uuid.uuid4())[:8].upper()
            comp_res = companies_col.insert_one({
                "name": company_name, 
                "domain": domain, 
                "access_code": access_code,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            comp_id = str(comp_res.inserted_id)
            
            if 'logo' in request.files:
                file = request.files['logo']
                if file.filename != '':
                    ext = file.filename.rsplit('.', 1)[-1].lower()
                    if ext in ['png', 'jpg', 'jpeg', 'svg']:
                        os.makedirs('static/uploads', exist_ok=True)
                        path = f"static/uploads/{comp_id}.{ext}"
                        file.save(path)
                        companies_col.update_one({"_id": ObjectId(comp_id)}, {"$set": {"logo_path": path}})
        else:
            comp_id = str(comp['_id'])
            # Check for duplicate high-level roles inside the existing company
            if norm_post in protected_roles:
                existing_users = users_col.find({"company_id": comp_id})
                for eu in existing_users:
                    if normalize_post(eu.get('post', '')) == norm_post and str(eu['_id']) != session['user_id']:
                        flash(f'The position "{post}" is already claimed at {company_name}. Please contact your administrator or enter a different role.', 'error')
                        return redirect(url_for('onboarding'))
            
        users_col.update_one({"_id": ObjectId(session['user_id'])}, {"$set": {"company_id": comp_id, "post": post}})
        session['company_id'] = comp_id
        flash('Workspace set up successfully!', 'success')
        return redirect(url_for('dashboard'))
        
    return render_template('onboarding.html')

@app.route('/dashboard')
def dashboard():
    if not session.get('company_id'):
        if session.get('user_id'): return redirect(url_for('onboarding'))
        return redirect(url_for('login'))

    comp = companies_col.find_one({"_id": ObjectId(session['company_id'])})
    domain = comp.get('domain', 'technology') if comp else 'technology'

    from insights import get_random_insight
    stats = get_stats(session['company_id'])
    insight = get_random_insight("electricity", domain=domain)

    # get reports for charts
    reports = get_all_reports(session['company_id'])

    return render_template('index.html', stats=stats,
                           user_name=session.get('user_name', 'User'),
                           insight=insight, reports=reports)

@app.route('/reports')
def reports():
    if not session.get('company_id'):
        if session.get('user_id'): return redirect(url_for('onboarding'))
        return redirect(url_for('login'))

    all_reports = get_all_reports(session['company_id'])

    # group by month for display
    grouped = {}
    for r in all_reports:
        m = r.get('month', 'Other')
        if m not in grouped:
            grouped[m] = []
        grouped[m].append(r)

    return render_template('reports.html', grouped_reports=grouped,
                           user_name=session.get('user_name'))

@app.route('/report/<report_id>')
def report_detail(report_id):
    if not session.get('company_id'):
        return redirect(url_for('login'))

    report = get_report_by_id(report_id)
    if not report or report.get('company_id') != session['company_id']:
        flash('Report not found or access denied.', 'error')
        return redirect(url_for('reports'))

    return render_template('report_detail.html', report=report,
                           user_name=session.get('user_name'))

@app.route('/comprehensive-report/<report_id>')
def comprehensive_report(report_id):
    if not session.get('company_id'):
        return redirect(url_for('login'))

    report = get_report_by_id(report_id)
    if not report or report.get('company_id') != session['company_id']:
        flash('Report not found or access denied.', 'error')
        return redirect(url_for('reports'))

    comp = companies_col.find_one({"_id": ObjectId(session['company_id'])})
    domain = comp.get('domain', 'technology') if comp else 'technology'

    # dynamic sustainability boundaries based on typical domain footprints
    domain_limits = {
        'technology': 1500.0,
        'logistics': 6000.0,
        'construction': 8000.0,
        'manufacturing': 12000.0
    }
    limit = domain_limits.get(domain, 3000.0)

    # Calculate Score (100 = Excellent, 0 = Critical Hazard)
    total_em = report.get('total_emissions', 0)
    consumption_ratio = total_em / limit
    score_val = max(10, min(100, int((1.0 - consumption_ratio) * 100)))
    
    if score_val <= 40:
        score_txt = "High emissions detected relative to industry index. Immediate action recommended."
        score_clr = "#e74c3c"
    elif score_val <= 75:
        score_txt = "Moderate footprint. Strong opportunity for efficiency measures."
        score_clr = "#f1c40f"
    else:
        score_txt = "Great! Your company's infrastructure operates beautifully."
        score_clr = "#2ecc71"

    insight_set = report.get('ai_recommendations', {})
    filtered_insights = {}
    
    if score_val >= 80:
        filtered_insights = insight_set # all recommendations
    elif score_val >= 40:
        for k, v in insight_set.items():
            if isinstance(v, list) and len(v) > 0:
                filtered_insights[k] = [v[0]]
            else:
                filtered_insights[k] = v
    else:
        filtered_insights = {}

    return render_template('comprehensive_report.html', report=report,
                           user_name=session.get('user_name'),
                           insights=filtered_insights,
                           score=score_val,
                           score_text=score_txt,
                           score_color=score_clr,
                           comp=comp)

# prebaked goal suggestions with actionable steps
GOAL_PRESETS = [
    {
        "id": "reduce_electricity_20", 
        "title": "Reduce electricity use by 20%", 
        "description": "Switch to energy-efficient lighting and optimize HVAC schedules.", 
        "target_reduction": 20,
        "action_plan": ["Audit all lighting and switch to LEDs", "Install occupancy sensors in labs/offices", "Optimize HVAC setpoints (24-26°C)", "Implement 'Power Down' Friday policies"]
    },
    {
        "id": "fleet_ev_transition", 
        "title": "Transition 30% of fleet to EV", 
        "description": "Replace aging diesel vehicles with electric alternatives.", 
        "target_reduction": 30,
        "action_plan": ["Identify high-mileage diesel vehicles for replacement", "Install on-site Level 2 EV charging stations", "Apply for government EV subsidies", "Train drivers on regenerative braking use"]
    },
    {
        "id": "renewable_energy", 
        "title": "Source 50% renewable energy", 
        "description": "Install rooftop solar or sign a green energy PPA.", 
        "target_reduction": 50,
        "action_plan": ["Run a solar feasibility study on factory roof", "Contact state utility for Green Power tariff", "Sign a Power Purchase Agreement (PPA)", "Install battery storage to buffer solar output"]
    },
    {
        "id": "reduce_waste", 
        "title": "Cut operational waste by 25%", 
        "description": "Implement recycling and reduce packaging waste.", 
        "target_reduction": 25,
        "action_plan": ["Conduct a waste audit to find top scrap sources", "Switch to reusable shipping pallets", "Implement a zero-paper billing system", "Partner with a certified e-waste recycler"]
    },
    {
        "id": "remote_work", 
        "title": "Reduce commute via Remote Work", 
        "description": "Allow hybrid schedules to cut employee commute emissions.", 
        "target_reduction": 40,
        "action_plan": ["Survey employees on commute distances", "Deploy secure VPN/Remote tools for all staff", "Set mandatory Work-From-Home days", "Optimize office square footage to reduce HVAC"]
    },
    {
        "id": "server_efficiency", 
        "title": "Improve server efficiency by 30%", 
        "description": "Consolidate servers and migrate to green cloud data centers.", 
        "target_reduction": 30,
        "action_plan": ["Decommission 'Zombie' servers and VMs", "Migrate workloads to Azure/AWS Carbon-Neutral regions", "Increase server room temp to 27°C (ASHRAE)", "Implement liquid cooling for high-density racks"]
    },
]

@app.route('/goals')
def goals():
    if not session.get('company_id'):
        return redirect(url_for('login'))
    active_goals = list(goals_col.find({"company_id": session['company_id']}))
    for g in active_goals:
        g['_id'] = str(g['_id'])
    return render_template('goals.html', goals=active_goals, suggestions=GOAL_PRESETS, user_name=session.get('user_name'))

@app.route('/goals/add-preset', methods=['POST'])
def add_preset_goal():
    if not session.get('company_id'):
        return redirect(url_for('login'))
    preset_id = request.form.get('preset_id', '')
    preset = next((p for p in GOAL_PRESETS if p['id'] == preset_id), None)
    if preset:
        goals_col.insert_one({
            "company_id": session['company_id'],
            "title": preset['title'],
            "description": preset['description'],
            "target_reduction": preset['target_reduction'],
            "action_plan": preset.get('action_plan', []),
            "progress": 0,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        flash(f'Goal "{preset["title"]}" added!', 'success')
    return redirect(url_for('goals'))

@app.route('/goals/add-custom', methods=['POST'])
def add_custom_goal():
    if not session.get('company_id'):
        return redirect(url_for('login'))
    title = request.form.get('title', '')
    desc = request.form.get('description', '')
    target = int(request.form.get('target_reduction', 10))
    plan_raw = request.form.get('action_plan', '[]')
    try:
        action_plan = json.loads(plan_raw) if plan_raw else []
    except:
        action_plan = []

    goals_col.insert_one({
        "company_id": session['company_id'],
        "title": title,
        "description": desc,
        "target_reduction": target,
        "action_plan": action_plan,
        "progress": 0,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })
    flash(f'Custom goal "{title}" created!', 'success')
    return redirect(url_for('goals'))

@app.route('/goals/delete', methods=['POST'])
def delete_goal():
    if not session.get('company_id'):
        return redirect(url_for('login'))
    goal_id = request.form.get('goal_id', '')
    goals_col.delete_one({"_id": ObjectId(goal_id), "company_id": session['company_id']})
    flash('Goal removed.', 'success')
    return redirect(url_for('goals'))

@app.route('/api/generate-goal')
@limiter.limit("10 per day")
def generate_goal_api():
    if not session.get('company_id'):
        return {"error": "Unauthorized"}, 401
    
    comp = companies_col.find_one({"_id": ObjectId(session['company_id'])})
    domain = comp.get('domain', 'technology')
    
    # get latest report to make it relevant
    latest = reports_col.find_one({"company_id": session['company_id']}, sort=[("_id", -1)])
    context = ""
    if latest:
        context = f"Latest report shows {latest['total_emissions']} tCO2e total. Domain: {domain}."

    prompt = f"""Generate ONE high-impact, professional sustainability goal for a {domain} company.
Context: {context}
Return ONLY a raw JSON object:
{{
  "title": "Catchy short title",
  "description": "Brief professional description",
  "target_reduction": percentage_number,
  "action_plan": ["discrete step 1", "discrete step 2", "discrete step 3", "discrete step 4"]
}}"""

    # AI TIERED CALL
    from api_guard import safe_api_call, can_call
    
    # helper for offline fallback
    def get_offline_goal():
        return {
            "title": f"Efficiency Drive ({domain.title()})",
            "description": f"Reduce waste and idle consumption across your {domain} operations.",
            "target_reduction": 15,
            "action_plan": [
                "Install occupancy sensors in all office spaces",
                "Set automated power-down schedules for hardware",
                "Conduct a weekend energy audit to find phantom loads",
                "Promote a culture of switching off unused equipment"
            ]
        }

    # 1. TRY GEMINI
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if gemini_key and gemini_key != "your_ai_studio_key_here":
        try:
            import google.generativeai as genai
            genai.configure(api_key=gemini_key)
            model = genai.GenerativeModel('gemini-1.5-flash')
            response = model.generate_content(prompt)
            txt = response.text.replace('```json', '').replace('```', '').strip()
            res = json.loads(txt)
            if res.get('title'): return res
        except Exception as e:
            print("Gemini Goal Err:", e)

    # 2. TRY OPENAI (via guard)
    if can_call('openai'):
        try:
            def _goal_ai():
                response = openai_client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.7
                )
                return json.loads(response.choices[0].message.content)
            
            res = safe_api_call('openai', _goal_ai)
            if res and isinstance(res, dict) and res.get('title'):
                return res
        except Exception as e:
            print("OpenAI Goal Err:", e)

    # 3. ABSOLUTE FAILSAFE
    return get_offline_goal()


@app.route('/api/grid-intensity')
def grid_intensity():
    """Returns the official carbon intensity for the Indian electricity grid."""
    if not session.get('company_id'):
        return {"error": "Unauthorized"}, 401
        
    # Indian B2B carbon accounting officially uses the Central Electricity 
    # Authority (CEA) Baseline avg: ~716 gCO2/kWh.
    base_intensity = 716.0 
    
    # REQUIREMENT: Add +/- 2% visual fluctuation to simulate live grid accuracy.
    # 2% of 716 is approximately 14.32.
    import random
    fluctuation = random.uniform(-14.32, 14.32)
    current_intensity = round(base_intensity + fluctuation, 2)
    
    return {
        "intensity": current_intensity,
        "index": "High (Coal-Heavy Mix)",
        "source": "CEA India Baseline (Simulated Live Grid Variations)",
        "variation_pct": f"{round((fluctuation/base_intensity)*100, 2)}%"
    }


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if not session.get('company_id'):
        return redirect(url_for('login'))
        
    if request.method == 'POST':
        if 'logo' in request.files:
            file = request.files['logo']
            if file.filename != '':
                ext = file.filename.rsplit('.', 1)[-1].lower()
                if ext in ['png', 'jpg', 'jpeg']:
                    os.makedirs('static/uploads', exist_ok=True)
                    path = f"static/uploads/{session['company_id']}.{ext}"
                    file.save(path)
                    companies_col.update_one({"_id": ObjectId(session['company_id'])}, {"$set": {"logo_path": path}})
                    flash("Logo updated successfully!", "success")
                else:
                    flash("Invalid file format. Use PNG or JPG.", "error")
                    
    comp = companies_col.find_one({"_id": ObjectId(session['company_id'])})
    # ensure backwards compatibility for existing companies
    if comp and not comp.get('access_code'):
        import uuid
        access_code = str(uuid.uuid4())[:8].upper()
        companies_col.update_one({"_id": comp['_id']}, {"$set": {"access_code": access_code}})
        comp['access_code'] = access_code
        
    return render_template('settings.html', comp=comp, user_name=session.get('user_name'))

@app.route('/upload-pdf', methods=['GET', 'POST'])
@limiter.limit("20 per hour")
def upload_pdf():
    if not session.get('company_id'):
        return redirect(url_for('login'))

    if request.method == 'POST':
        if 'file' not in request.files:
            flash("No file part", "error")
            return redirect(request.url)
        file = request.files['file']
        if file.filename == '':
            flash("No selected file", "error")
            return redirect(request.url)

        try:
            import PyPDF2
            pdf_reader = PyPDF2.PdfReader(file)
            text = ""
            for page in pdf_reader.pages:
                text += page.extract_text() + "\n"
            
            print(f"DEBUG: Extracted raw text (first 500 chars): {text[:500]}")

            comp = companies_col.find_one({"_id": ObjectId(session['company_id'])})
            user_domain = comp.get('domain', 'technology') if comp else 'technology'
            print(f"DEBUG: User Company Domain: {user_domain}")

            # 1. DOMAIN DETECTION (MANDATORY)
            from pdf_parser import detect_domain, parse_pdf_offline
            detected_domain = detect_domain(text)
            print(f"DEBUG: Detected Domain from PDF: {detected_domain}")
            
            # Normalize for comparison
            norm_detected = detected_domain.replace("_and_IT", "").replace("_and_Transport", "").lower() if detected_domain else None
            norm_user = user_domain.replace("_and_IT", "").replace("_and_Transport", "").lower()

            if norm_detected and norm_detected != norm_user:
                print(f"DEBUG: Domain mismatch! Detected: {norm_detected}, Expected: {norm_user}")
                flash(f"Domain mismatch: This report belongs to the {detected_domain.replace('_', ' ').title()} industry.", "error")
                return redirect(request.url)

            # 2. AI EXTRACTION PIPELINE (3-LAYER)
            extracted = None
            prompt = f"""[CRITICAL: RETURN ONLY RAW JSON. NO MARKDOWN. NO PREAMBLE.]
Analyze this ESG report and extract operational data. 
The document features granular asset tracking and operational metrics.
The title or filename will be a generic company name and will NOT tell you the industry. 
You MUST analyze the text content (keywords, assets, activities) to infer the domain.

Domains to detect:
- Technology: server, data center, cooling unit, IT
- Logistics: warehouse, freight, delivery van, truck
- Construction: site, hauling, excavator, temporary office
- Manufacturing: factory, machinery, cnc, shipments

- LAYER 1 (Structural): Find the reporting period and total net emissions.
- LAYER 2 (Detailed Table): Extract up to 10 rows from the asset/vehicle/machine table.
  *CRITICAL*: Use exact domain-specific JSON keys for assets based on the domain you detected:
  - If Technology: {{"name": "...", "type": "...", "kwh": number}}
  - If Logistics: {{"name": "...", "type": "...", "fuel_litres": number, "distance_km": number, "hours": number}}
  - If Construction: {{"name": "...", "type": "...", "fuel_litres": number, "hours": number}}
  - If Manufacturing: {{"name": "...", "hours": number, "kwh": number}}

- LAYER 3 (Narrative NLP): Capture unstructured metrics based on the domain you detected:
  - Logistics: warehouse_kwh, total_freight_weight (kg)
  - Construction: trucks_km, cars_km, office_kwh
  - Manufacturing: factory_kwh, fleet_km, shipment_kg
  - Technology: server_kwh, commute_km, flights

Source Text: {text[:6000]}

JSON Schema:
{{
  "detected_domain": "string",
  "reporting_period": "string",
  "warehouse_kwh": number,
  "total_freight_weight": number,
  "trucks_km": number,
  "cars_km": number,
  "office_kwh": number,
  "factory_kwh": number,
  "fleet_km": number,
  "shipment_kg": number,
  "server_kwh": number,
  "commute_km": number,
  "flights": number,
  "assets": [{{ ...domain_specific_keys... }}],
  "total_emissions": number
}}"""

            
            # TIER 1: GEMINI FLASH (PRIMARY)
            gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
            if gemini_key and gemini_key != "your_ai_studio_key_here":
                try:
                    import google.generativeai as genai
                    genai.configure(api_key=gemini_key)
                    model = genai.GenerativeModel('gemini-2.5-flash', generation_config={"response_mime_type": "application/json"})
                    response = model.generate_content(prompt)
                    print(f"DEBUG: Gemini Raw Response: {response.text}")
                    extracted_ai = json.loads(response.text)
                    if extracted_ai.get('assets') or extracted_ai.get('total_emissions'):
                        extracted = extracted_ai
                        print("DEBUG: Gemini Flash PRIMARY extraction successful.")
                except Exception as e:
                    print(f"DEBUG: Gemini Pipeline Error: {e}")

            # TIER 2: OPENAI (SECONDARY FALLBACK)
            if not extracted:
                from api_guard import safe_api_call, can_call
                if can_call('openai'):
                    def _openai_call():
                        response = openai_client.chat.completions.create(
                            model="gpt-3.5-turbo",
                            messages=[{"role": "system", "content": "You output only minified raw JSON."},
                                      {"role": "user", "content": prompt}],
                            temperature=0.1
                        )
                        return json.loads(response.choices[0].message.content)
                    
                    try:
                        extracted = safe_api_call('openai', _openai_call)
                        if extracted: print("DEBUG: OpenAI SECONDARY extraction successful.")
                    except Exception as e:
                        print(f"DEBUG: OpenAI Pipeline Error: {e}")

            # TIER 3: OFFLINE PARSER (FINAL FALLBACK - STRONG)
            if not extracted:
                print("DEBUG: AI engines failed. Invoking STRONG offline heuristic parser...")
                extracted = parse_pdf_offline(text, user_domain)
                print(f"DEBUG: Offline Parser Output: {extracted}")
                if not extracted.get('success'):
                    flash(extracted.get('message', "Document industry mismatch detected."), "error")
                    return redirect(request.url)

            print(f"DEBUG: Final Extracted Data: {extracted}")

            # Store in session for the calculator pre-fill
            session['extracted'] = {
                "server_kwh": extracted.get('server_kwh', 0),
                "commute_km": extracted.get('commute_km', 0),
                "flights": extracted.get('flights', 0),
                "warehouse_kwh": extracted.get('warehouse_kwh', 0),
                "total_freight_weight": extracted.get('total_freight_weight', 0),
                "trucks_km": extracted.get('trucks_km', 0),
                "cars_km": extracted.get('cars_km', 0),
                "office_kwh": extracted.get('office_kwh', 0),
                "factory_kwh": extracted.get('factory_kwh', 0),
                "fleet_km": extracted.get('fleet_km', 0),
                "shipment_kg": extracted.get('shipment_kg', 0),
                "reporting_period": extracted.get('reporting_period', 'Unknown'),
                "detected_domain": extracted.get('detected_domain', 'Unknown')
            }
            session['extracted_items'] = extracted.get('assets', [])
            
            flash("Aetherra AI Core has intelligent-sliced the PDF! Review the pre-filled calculator.", "success")
            return redirect(url_for('calculator'))
        except Exception as e:
            print("System PDF failure:", e)
            flash("AI could not read the document. Ensure it is a valid sustainability PDF.", "error")
            return redirect(request.url)

    return render_template('upload.html')


@app.route('/calculator', methods=['GET', 'POST'])
@limiter.limit("50 per hour")
def calculator():
    if not session.get('company_id'):
        if session.get('user_id'): return redirect(url_for('onboarding'))
        return redirect(url_for('login'))

    comp = companies_col.find_one({"_id": ObjectId(session['company_id'])})
    domain = comp.get('domain', 'technology') if comp else 'technology'

    result = None
    if request.method == 'POST':
        try:
            report_month = request.form.get('report_month', datetime.now().strftime("%Y-%m"))
            items_raw = request.form.get('items_json', '[]')
            items = json.loads(items_raw) if items_raw else []

            res_transport = 0.0
            res_electricity = 0.0
            res_logistics = 0.0
            res_manufacturing = 0.0
            item_breakdown = []

            # fuel emission factors (kg CO2 per litre)
            FUEL_FACTORS = {"diesel": 2.68, "petrol": 2.31, "default": 2.68}
            # equipment type fuel multipliers
            EQUIP_FUEL = {"excavator": 1.0, "crane": 0.8, "bulldozer": 1.2, "loader": 0.9, "concrete_mixer": 0.7, "generator": 1.1}
            # vehicle type fuel multipliers
            VEHICLE_FUEL = {"delivery_van": 0.25, "truck": 0.85, "pickup": 0.21, "car": 0.21, "motorcycle": 0.115}

            if domain == 'technology':
                server_kwh = float(request.form.get('server_kwh', 0))
                commute_km = float(request.form.get('commute_km', 0))
                flights = float(request.form.get('flights', 0))
                rooms_total_kwh = 0.0
                for item in items:
                    kwh = float(item.get('kwh', 0))
                    rooms_total_kwh += kwh
                    em = round((kwh * 0.82) / 1000, 4)
                    item_breakdown.append({"name": item.get('name', 'Unnamed'), "type": item.get('type', 'office'), "kwh": kwh, "emissions": em})
                res_electricity = round(((server_kwh + rooms_total_kwh) * 0.82) / 1000, 4)
                res_transport = round((commute_km * 0.15 + flights * 250) / 1000, 4)

            elif domain == 'logistics':
                warehouse_kwh = float(request.form.get('warehouse_kwh', 0))
                total_freight = float(request.form.get('total_freight_weight', 0))
                vehicles_transport = 0.0
                for item in items:
                    fuel = float(item.get('fuel_litres', 0))
                    dist = float(item.get('distance_km', 0))
                    hrs = float(item.get('hours', 0))
                    vtype = item.get('type', 'truck')
                    factor = VEHICLE_FUEL.get(vtype, 0.25)
                    em = round((dist * factor + fuel * 2.68) / 1000, 4)
                    vehicles_transport += em
                    item_breakdown.append({"name": item.get('name', 'Unnamed'), "type": vtype, "fuel": fuel, "distance": dist, "hours": hrs, "emissions": em})
                res_transport = round(vehicles_transport, 4)
                res_electricity = round((warehouse_kwh * 0.82) / 1000, 4)
                res_logistics = round((total_freight * 500 * 0.0001) / 1000, 4)

            elif domain == 'construction':
                trucks_km = float(request.form.get('trucks_km', 0))
                cars_km = float(request.form.get('cars_km', 0))
                office_kwh = float(request.form.get('office_kwh', 0))
                equip_total = 0.0
                for item in items:
                    fuel = float(item.get('fuel_litres', 0))
                    hrs = float(item.get('hours', 0))
                    etype = item.get('type', 'excavator')
                    mult = EQUIP_FUEL.get(etype, 1.0)
                    em = round((fuel * 2.68 * mult + hrs * 3.5) / 1000, 4)
                    equip_total += em
                    item_breakdown.append({"name": item.get('name', 'Unnamed'), "type": etype, "fuel": fuel, "hours": hrs, "emissions": em})
                res_transport = round((trucks_km * 0.85 + cars_km * 0.21) / 1000, 4)
                res_electricity = round((office_kwh * 0.82) / 1000, 4)
                res_manufacturing = round(equip_total, 4)

            elif domain == 'manufacturing':
                factory_kwh = float(request.form.get('factory_kwh', 0))
                fleet_km = float(request.form.get('fleet_km', 0))
                shipment_kg = float(request.form.get('shipment_kg', 0))
                machines_total = 0.0
                for item in items:
                    hrs = float(item.get('hours', 0))
                    kwh = float(item.get('kwh', 0))
                    em = round((hrs * 2.5 + kwh * 0.82) / 1000, 4)
                    machines_total += em
                    item_breakdown.append({"name": item.get('name', 'Unnamed'), "hours": hrs, "kwh": kwh, "emissions": em})
                res_transport = round((fleet_km * 0.25) / 1000, 4)
                res_electricity = round((factory_kwh * 0.82) / 1000, 4)
                res_manufacturing = round(machines_total, 4)
                res_logistics = round((shipment_kg * 300 * 0.0001) / 1000, 4)

            from api_guard import safe_api_call

            # API Integration: Electricity (Climatiq)
            if res_electricity > 0:
                total_kwh = (res_electricity * 1000) / 0.82
                api_elec = safe_api_call('climatiq', lambda: get_climatiq_electricity(total_kwh) / 1000, fallback_fn=lambda: res_electricity)
                res_electricity = round(api_elec, 4) if api_elec is not None else res_electricity

            # API Integration: Transport (Carbon Interface)
            if res_transport > 0:
                total_km = 0
                flights = 0
                if domain == 'technology':
                    total_km = float(request.form.get('commute_km', 0))
                    flights = float(request.form.get('flights', 0))
                elif domain == 'logistics':
                    total_km = sum([float(i.get('distance_km', 0)) for i in items])
                elif domain == 'construction':
                    total_km = float(request.form.get('trucks_km', 0)) + float(request.form.get('cars_km', 0))
                elif domain == 'manufacturing':
                    total_km = float(request.form.get('fleet_km', 0))

                if total_km > 0:
                    api_transport_res = safe_api_call('carbon_interface', lambda: get_carbon_interface_fuel(total_km) / 1000, fallback_fn=lambda: res_transport - (flights * 250 / 1000))
                    res_transport = round((api_transport_res if api_transport_res is not None else (res_transport - (flights * 250 / 1000))) + (flights * 250 / 1000), 4)

            res_total = round(res_transport + res_electricity + res_logistics + res_manufacturing, 4)

            result = {
                "transport": res_transport, "electricity": res_electricity,
                "logistics": res_logistics, "manufacturing": res_manufacturing,
                "total": res_total
            }

            # figure out which categories are high
            high_cats = []
            if result['transport'] > 0.15: high_cats.append("transportation")
            if result['electricity'] > 0.10: high_cats.append("electricity")
            if result['logistics'] > 0.05: high_cats.append("logistics")
            if result['manufacturing'] > 0.05: high_cats.append("manufacturing")

            ai_recs = {}
            if high_cats:
                from insights import get_ai_insights
                ai_recs = get_ai_insights(domain, high_cats, openai_client)

            date_obj = datetime.strptime(report_month, "%Y-%m")
            title_month = date_obj.strftime("%B %Y")

            doc = {
                "user_id": str(session['user_id']),
                "company_id": str(session['company_id']),
                "title": f"{title_month} Emissions Report ({domain.capitalize()})",
                "month": report_month,
                "domain": domain,
                "transport_emissions": result['transport'],
                "electricity_emissions": result['electricity'],
                "logistics_emissions": result['logistics'],
                "manufacturing_emissions": result['manufacturing'],
                "total_emissions": result['total'],
                "item_breakdown": item_breakdown,
                "ai_recommendations": ai_recs,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }

            calculations_col.insert_one(doc.copy())
            reports_col.insert_one(doc)

            flash(f'Emissions for {title_month} calculated and saved!', 'success')
            return redirect(url_for('dashboard'))

        except (ValueError, json.JSONDecodeError) as e:
            print("Calc error:", e)
            flash('Please provide valid numerical values for all required fields.', 'error')
            return redirect(url_for('calculator'))

    ext = session.pop('extracted', {})
    ext_items = session.pop('extracted_items', [])
    return render_template('calculator.html', result=result, domain=domain,
                           user_name=session.get('user_name'), ext=ext, ext_items=ext_items)

@app.route('/report/download/<report_id>')
def download_report(report_id):
    if not session.get('company_id'):
        return redirect(url_for('login'))

    report = get_report_by_id(report_id)
    if not report or report.get('company_id') != session['company_id']:
        flash('Report not found or access denied.', 'error')
        return redirect(url_for('reports'))

    try:
        pdf_bytes = generate_pdf(report)
    except Exception as e:
        print("PDF generation error:", e)
        flash('Error generating PDF. Please try again.', 'error')
        return redirect(url_for('report_detail', report_id=report_id))

    filename = f"aetherra_report_{report_id}.pdf"

    # send as downloadable file
    buf = io.BytesIO(pdf_bytes)
    buf.seek(0)
    return send_file(
        buf,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=filename
    )

# api routes
@app.route('/api/insights')
@limiter.limit("100 per hour")
def api_insights():
    from insights import get_random_insight
    category = request.args.get('category', 'transportation')
    insight = get_random_insight(category)
    return json.dumps({"category": category, "insight": insight}), 200, {'Content-Type': 'application/json'}

@app.route('/api/reports')
@limiter.limit("100 per hour")
def api_reports():
    if not session.get('company_id'):
        return '{"error": "not logged in or no company"}', 401
    all_r = get_all_reports(session['company_id'])
    data = [{"id": str(r.get('id', r.get('_id', ''))), "title": r['title'], "total": r['total_emissions']} for r in all_r]
    return json.dumps(data), 200, {'Content-Type': 'application/json'}

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('splash'))

# google auth routes
@app.route('/auth/google/login')
def google_login():
    gc_id = os.environ.get('GOOGLE_CLIENT_ID', '')
    if not gc_id or gc_id == 'your_google_client_id_here':
        flash('Google login not configured. Add credentials to .env.local', 'error')
        return redirect(url_for('login'))

    redirect_uri = url_for('google_callback', _external=True)
    print(f"DEBUG: Google redirect_uri={redirect_uri}")

    return google.authorize_redirect(redirect_uri)

@app.route('/auth/google/callback')
def google_callback():
    token = google.authorize_access_token()
    user_info = token.get('userinfo')
    if user_info:
        email = user_info['email']
        name = user_info.get('name', 'Google User')

        user = users_col.find_one({"email": email})
        if not user:
            result = users_col.insert_one({"name": name, "email": email, "google_auth": True, "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
            user = users_col.find_one({"_id": result.inserted_id})
            
        session['user_id'] = str(user['_id'])
        session['user_name'] = user['name']
        session['user_email'] = user['email']
        session['company_id'] = user.get('company_id')
        
        if not session['company_id']:
            return redirect(url_for('onboarding'))
        return redirect(url_for('dashboard'))
        
    flash('Google authentication failed.', 'error')
    return redirect(url_for('login'))

@app.route('/api/route_distance')
@limiter.limit("20 per hour")
def route_distance():
    origin = request.args.get('origin', '')
    destination = request.args.get('destination', '')
    if not origin or not destination:
        return {"error": "Origin and destination required"}, 400
        
    def _gmaps():
        gm_key = os.environ.get('GOOGLE_MAPS_API_KEY')
        if not gm_key or gm_key.startswith('AIzaSyD3nuZk'): # Detect dummy key
            raise ValueError("No valid Google Maps key")
        url = f"https://maps.googleapis.com/maps/api/distancematrix/json?origins={origin}&destinations={destination}&key={gm_key}"
        resp = http_requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if data['status'] == 'OK' and data['rows'][0]['elements'][0]['status'] == 'OK':
            meters = data['rows'][0]['elements'][0]['distance']['value']
            return round(meters / 1000, 1)
        raise ValueError(f"Gmaps error: {data['status']}")

    def _free_routing():
        headers = {'User-Agent': 'Aetherra/1.0 (Testing distance)'}
        try:
            o_data = http_requests.get(f"https://nominatim.openstreetmap.org/search?q={origin}&format=json&limit=1", headers=headers, timeout=5).json()
            d_data = http_requests.get(f"https://nominatim.openstreetmap.org/search?q={destination}&format=json&limit=1", headers=headers, timeout=5).json()
            if not o_data or not d_data: return 100.0
            rt = http_requests.get(f"http://router.project-osrm.org/route/v1/driving/{o_data[0]['lon']},{o_data[0]['lat']};{d_data[0]['lon']},{d_data[0]['lat']}?overview=false", timeout=5).json()
            if rt.get('code') == 'Ok':
                return round(rt['routes'][0]['distance'] / 1000, 1)
        except Exception as e:
            print(f"OSRM fallback failed: {e}")
        return 100.0

    from api_guard import safe_api_call
    dist = safe_api_call('google_maps', _gmaps, fallback_fn=_free_routing)
    return {"distance_km": dist}

if __name__ == '__main__':
    print("Aetherra running at http://127.0.0.1:5000")
    app.run(debug=True, use_reloader=True)

