from dotenv import load_dotenv
import os
load_dotenv('.env.local')

from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from pymongo import MongoClient
from datetime import datetime
from bson import ObjectId
import io
import json
import random
import PyPDF2

from authlib.integrations.flask_client import OAuth
from openai import OpenAI
import requests as http_requests  # renamed to avoid conflict with flask.request
from api_guard import safe_api_call, run_tiered_ai

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

# groq config
_groq_key = os.environ.get("GROQ_API_KEY")
if _groq_key and "gsk_" in _groq_key:
    groq_client = True # Just a boolean flag now
    print("Groq AI: Found key successfully.")
else:
    groq_client = False
    print("Groq AI: Key missing or invalid in .env.local")

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

    # Try to render company logo on the left side of header
    logo_rendered = False
    text_x = 15  # default text position if no logo
    try:
        if comp and comp.get('logo_path'):
            logo_path = comp.get('logo_path')
            if os.path.exists(logo_path):
                render_path = logo_path
                # Convert SVG to a temp PNG so fpdf can handle it
                if logo_path.lower().endswith('.svg'):
                    try:
                        from PIL import Image
                        import subprocess, tempfile
                        # Use Pillow if it can open the SVG, otherwise skip
                        tmp_png = os.path.join(tempfile.gettempdir(), 'aetherra_logo_tmp.png')
                        # Try cairosvg first, fallback to skipping
                        try:
                            import cairosvg
                            cairosvg.svg2png(url=logo_path, write_to=tmp_png, output_width=200)
                            render_path = tmp_png
                        except ImportError:
                            render_path = None  # can't convert SVG, skip it
                    except Exception:
                        render_path = None

                if render_path and os.path.exists(render_path):
                    pdf.image(render_path, x=12, y=10, h=30)
                    logo_rendered = True
                    text_x = 48  # shift text right to make room for logo
    except Exception as e:
        print("Logo render error:", e)

    pdf.set_font("Helvetica", "B", 24)
    pdf.set_text_color(46, 204, 113)
    pdf.text(text_x, 25, "AETHERRA")

    # Company name in bold
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(255, 255, 255)
    pdf.text(text_x, 33, comp_name)

    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(200, 200, 200)
    pdf.text(text_x, 40, f"ESG & Sustainability Report | {datetime.now().year}")

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

    # --- ASSET INVENTORY / SCOPE BREAKDOWN (Table Base) ---
    is_yearly = (report.get('type') == 'yearly')
    
    if is_yearly:
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 10, "3. Scope Emissions Breakdown", ln=True)
        pdf.ln(2)
        
        pdf.set_fill_color(240, 240, 240)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(60, 10, "Emission Category", 1, 0, 'C', True)
        pdf.cell(60, 10, "Description", 1, 0, 'C', True)
        pdf.cell(60, 10, "Emissions (tCO2e)", 1, 1, 'C', True)
        
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(60, 8, "Scope 1", 1, 0, 'C')
        pdf.cell(60, 8, "Direct Emissions", 1, 0, 'C')
        pdf.cell(60, 8, f"{report.get('scope_1', 0)}", 1, 1, 'C')

        pdf.cell(60, 8, "Scope 2", 1, 0, 'C')
        pdf.cell(60, 8, "Indirect Energy", 1, 0, 'C')
        pdf.cell(60, 8, f"{report.get('scope_2', 0)}", 1, 1, 'C')

        pdf.cell(60, 8, "Scope 3", 1, 0, 'C')
        pdf.cell(60, 8, "Value Chain", 1, 0, 'C')
        pdf.cell(60, 8, f"{report.get('scope_3', 0)}", 1, 1, 'C')
        
        pdf.ln(10)
        
        # Monthly graph representation (Simple fpdf.rect bars)
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 10, "4. Month-by-Month Distribution", ln=True)
        pdf.ln(5)
        
        breakdown = report.get('monthly_breakdown', [])
        if breakdown:
            max_em = max([m.get("emissions", 0) for m in breakdown]) if breakdown else 0.01
            pdf.set_font("Helvetica", "", 8)
            start_x = 15
            start_y = pdf.get_y()
            chart_height = 40
            
            # draw simple bars
            for i, m in enumerate(breakdown):
                bar_h = (m.get("emissions", 0) / max(max_em, 0.01)) * chart_height
                pdf.set_fill_color(46, 204, 113)
                pdf.rect(start_x + (i * 14), start_y + chart_height - bar_h, 10, bar_h, 'F')
                # label
                pdf.text(start_x + (i * 14) + 1, start_y + chart_height + 5, m.get("month", "")[:3])
            
            pdf.ln(chart_height + 15)

    else:
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

    # footer - confidential notice first, then "generated by" line last in smaller font
    pdf.set_y(-25)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(0, 5, "CONFIDENTIAL ESG DOCUMENT - NOT FOR REDISTRIBUTION", ln=True, align='C')
    # "Generated by Aetherra" at the very bottom, smaller but still noticeable
    pdf.set_font("Helvetica", "I", 6)
    pdf.set_text_color(130, 130, 130)
    pdf.cell(0, 4, f"Report generated by Aetherra", ln=True, align='C')

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

    # Generate AI Narrative if missing
    if not report.get('ai_narrative'):
        narrative = get_chart_narrative(report)
        reports_col.update_one({"_id": ObjectId(report_id)}, {"$set": {"ai_narrative": narrative}})
        report['ai_narrative'] = narrative

    return render_template('report_detail.html', report=report,
                           user_name=session.get('user_name'))

@app.route('/delete-report/<report_id>', methods=['POST'])
def delete_report(report_id):
    if not session.get('company_id'):
        return redirect(url_for('login'))

    res = reports_col.delete_one({
        "_id": ObjectId(report_id),
        "company_id": str(session['company_id'])
    })

    if res.deleted_count > 0:
        flash('Report deleted successfully.', 'success')
    else:
        flash('Could not delete report or access denied.', 'error')

    return redirect(url_for('reports'))
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
                           comp=comp,
                           ai_narrative=report.get('ai_narrative', 'Generating analysis...'))

# prebaked goal suggestions with actionable steps
GOAL_PRESETS = [
    {
        "id": "reduce_electricity_20", 
        "title": "Reduce electricity use by 20%", 
        "description": "Switch to energy-efficient lighting and optimize HVAC schedules.", 
        "target_reduction": 20,
        "action_plan": [
            "- **Energy Audit**: Conduct a comprehensive energy audit across all office floors, server rooms, and common areas to identify which zones consume the most electricity during peak and off-peak hours.",
            "- **LED Retrofit**: Replace all existing fluorescent and halogen lighting fixtures with energy-efficient LED panels and install motion-activated occupancy sensors in corridors, restrooms, and meeting rooms.",
            "- **HVAC Optimization**: Reconfigure HVAC setpoints to operate within the 24-26°C range during business hours and implement automatic shutdown schedules for evenings and weekends.",
            "- **Power Down Fridays**: Launch an internal 'Power Down Friday' initiative encouraging all departments to fully shut down non-essential equipment and monitors before leaving for the weekend."
        ]
    },
    {
        "id": "fleet_ev_transition", 
        "title": "Transition 30% of fleet to EV", 
        "description": "Replace aging diesel vehicles with electric alternatives.", 
        "target_reduction": 30,
        "action_plan": [
            "- **Fleet Utilization Analysis**: Analyze fleet utilization data to identify the top 30% of vehicles with the highest annual mileage and fuel consumption — these are the prime candidates for EV replacement.",
            "- **Infrastructure Setup**: Install Level 2 EV charging stations at your primary office or warehouse locations, ensuring at least one charger per every 3 EVs to avoid queuing during shifts.",
            "- **Incentive Sourcing**: Research and apply for applicable government subsidies such as FAME-II incentives, state-level EV purchase rebates, and corporate green fleet tax deductions.",
            "- **Driver Training**: Schedule mandatory driver training sessions on EV-specific techniques including regenerative braking, optimal charging habits, and route planning for range efficiency."
        ]
    },
    {
        "id": "renewable_energy", 
        "title": "Source 50% renewable energy", 
        "description": "Install rooftop solar or sign a green energy PPA.", 
        "target_reduction": 50,
        "action_plan": [
            "- **Solar Feasibility Study**: Commission a professional solar feasibility study on your factory or office rooftop to assess available surface area, structural load capacity, and annual solar irradiance potential.",
            "- **Green Tariff Negotiation**: Contact your state electricity utility provider to explore Green Power tariff plans or open-access renewable energy options that allow direct purchase from solar/wind farms.",
            "- **PPA Implementation**: Negotiate and sign a long-term Power Purchase Agreement (PPA) with a certified renewable energy developer, locking in fixed rates for 10-15 years to hedge against grid price increases.",
            "- **BESS Sizing**: Install on-site battery energy storage systems (BESS) to buffer solar output fluctuations, store excess daytime generation, and provide backup during grid outages."
        ]
    },
    {
        "id": "reduce_waste", 
        "title": "Cut operational waste by 25%", 
        "description": "Implement recycling and reduce packaging waste.", 
        "target_reduction": 25,
        "action_plan": [
            "- **Waste Stream Audit**: Conduct a detailed waste stream audit spanning at least 4 weeks, categorizing all waste by type (paper, plastic, e-waste, organic, hazardous) and tracking volume per department.",
            "- **Reusable Logistics Container**: Switch from single-use packaging materials to reusable shipping containers, biodegradable packing peanuts, and returnable pallet systems for outbound logistics.",
            "- **Paperless Operations**: Digitize all internal invoicing, purchase orders, and approval workflows to eliminate paper consumption — target a fully zero-paper billing and documentation system.",
            "- **Certified E-Waste Disposal**: Partner with a certified e-waste recycling vendor to responsibly dispose of obsolete IT hardware, batteries, and peripherals while recovering valuable materials."
        ]
    },
    {
        "id": "remote_work", 
        "title": "Reduce commute via Remote Work", 
        "description": "Allow hybrid schedules to cut employee commute emissions.", 
        "target_reduction": 40,
        "action_plan": [
            "- **Commute Survey & Baseline Setup**: Survey all employees to collect data on their daily commute distances, modes of transport, and willingness to work remotely — use this to model potential emission savings.",
            "- **Remote IT Stack**: Deploy enterprise-grade secure VPN access, cloud collaboration tools (Teams/Slack), and virtual desktop infrastructure so all staff can work productively from home.",
            "- **Hybrid Policy Rollout**: Establish a formal hybrid work policy with 2-3 mandatory work-from-home days per week, prioritizing roles that don't require physical presence on site.",
            "- **Facility Downsizing**: Downsize or consolidate office floor space based on reduced daily occupancy, which directly lowers HVAC, lighting, and overall facility energy consumption."
        ]
    },
    {
        "id": "server_efficiency", 
        "title": "Improve server efficiency by 30%", 
        "description": "Consolidate servers and migrate to green cloud data centers.", 
        "target_reduction": 30,
        "action_plan": [
            "- **Zombie Server Audit**: Run a full infrastructure audit to identify and decommission 'zombie' servers — idle virtual machines and physical hosts that consume power but serve no active workloads.",
            "- **Cloud Migration**: Migrate eligible workloads to carbon-neutral cloud regions (e.g., Azure West Europe, AWS eu-north-1) that run on 100% renewable energy and publish sustainability reports.",
            "- **Thermal Control Guidelines**: Raise server room ambient temperature to 27°C in line with ASHRAE thermal guidelines, which can reduce cooling energy by up to 20% without impacting hardware reliability.",
            "- **Advanced Cooling Setup**: Evaluate and implement liquid cooling or hot-aisle containment systems for high-density server racks, reducing reliance on traditional CRAC/CRAH air conditioning units."
        ]
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
    comp_name = comp.get('name', 'Company')
    
    # pull the last 5 reports to build a real data picture
    recent_reports = list(reports_col.find(
        {"company_id": session['company_id']}
    ).sort("_id", -1).limit(5))
    
    # build a rich context string from actual report data
    context_parts = [f"Company: {comp_name}. Industry: {domain}."]
    
    if recent_reports:
        latest = recent_reports[0]
        context_parts.append(f"Latest report ({latest.get('month', 'N/A')}): Total {latest.get('total_emissions', 0)} tCO2e.")
        
        # identify the highest emission category from latest report
        categories = {
            "transport": latest.get('transport_emissions', 0),
            "electricity": latest.get('electricity_emissions', 0),
            "logistics": latest.get('logistics_emissions', 0),
            "manufacturing": latest.get('manufacturing_emissions', 0)
        }
        highest_cat = max(categories, key=categories.get)
        highest_val = categories[highest_cat]
        context_parts.append(f"Highest emission category: {highest_cat} at {highest_val} tCO2e ({round(highest_val / max(latest.get('total_emissions', 0.01), 0.01) * 100, 1)}% of total).")
        
        # breakdown string
        breakdown = ", ".join([f"{k}: {v} tCO2e" for k, v in categories.items() if v > 0])
        context_parts.append(f"Full breakdown: {breakdown}.")
        
        # trend analysis across reports
        if len(recent_reports) > 1:
            totals = [r.get('total_emissions', 0) for r in reversed(recent_reports)]
            if totals[0] > 0:
                trend_pct = round(((totals[-1] - totals[0]) / totals[0]) * 100, 1)
                direction = "increasing" if trend_pct > 0 else "decreasing"
                context_parts.append(f"Emission trend over {len(recent_reports)} reports: {direction} by {abs(trend_pct)}%.")
        
        # item-level insights from latest report
        items = latest.get('item_breakdown', [])
        if items:
            top_items = sorted(items, key=lambda x: x.get('emissions', 0), reverse=True)[:3]
            top_str = ", ".join([f"{i.get('name', 'Unknown')} ({i.get('emissions', 0)} tCO2e)" for i in top_items])
            context_parts.append(f"Top 3 emitting assets: {top_str}.")
    else:
        context_parts.append("No reports submitted yet. Suggest a starter goal appropriate for their domain.")
    
    context = " ".join(context_parts)

    prompt = f"""You are an expert sustainability consultant analyzing a real company's emission data.
Based on the following data, generate ONE highly specific, actionable sustainability goal.
The goal must directly address the company's actual weak points shown in their data.
Make it deeply personalized by citing the exact numbers and categories from the breakdown.

{context}

Return ONLY a raw JSON object (no markdown, no preamble):
{{
  "title": "Concise, impactful goal title",
  "description": "2-3 sentence professional description explaining WHY this goal matters for this specific company based on their data",
  "target_reduction": percentage_number,
  "action_plan": [
    "- **Detailed Objective 1:** Specific implementation instruction (2-3 sentences with measurable actions). Provide an exact metric to track.",
    "- **Detailed Objective 2:** Specific implementation instruction (2-3 sentences with measurable actions). Provide an exact metric to track.",
    "- **Detailed Objective 3:** Specific implementation instruction (2-3 sentences with measurable actions). Provide an exact metric to track.",
    "- **Detailed Objective 4:** Specific implementation instruction (2-3 sentences with measurable actions). Provide an exact metric to track."
  ]
}}"""

    # AI TIERED CALL
    from api_guard import safe_api_call, can_call
    
    # helper for offline fallback that uses actual report data
    def get_offline_goal():
        # even the fallback should be data-aware
        if recent_reports:
            latest = recent_reports[0]
            cats = {
                "transport": latest.get('transport_emissions', 0),
                "electricity": latest.get('electricity_emissions', 0),
                "logistics": latest.get('logistics_emissions', 0),
                "manufacturing": latest.get('manufacturing_emissions', 0)
            }
            worst = max(cats, key=cats.get)
            worst_val = cats[worst]
            total = latest.get('total_emissions', 0.01)
            pct = round(worst_val / max(total, 0.01) * 100, 1)
            
            domain_goals = {
                "transport": {
                    "title": f"Reduce Transport Emissions ({pct}% of total footprint)",
                    "description": f"Your transport emissions account for {pct}% of your total carbon footprint at {worst_val} tCO2e. Transitioning to fuel-efficient or electric vehicles and optimizing route planning can significantly reduce this.",
                    "target_reduction": min(int(pct * 0.6), 50),
                    "action_plan": [
                        f"- **Fleet Audit**: Audit your current fleet: your transport emissions of {worst_val} tCO2e suggest high fuel consumption. Map every vehicle's monthly distance, fuel type, and idling time to identify the worst offenders.",
                        "- **EV Replacement**: Replace the highest-emission vehicles with electric or hybrid alternatives. Prioritize vehicles that exceed 3,000 km/month, as they offer the biggest ROI on EV conversion.",
                        "- **Route Optimization**: Implement a GPS-based route optimization system to minimize total distance driven. Studies show that optimized routing can reduce fleet fuel consumption by 15-25%.",
                        "- **Driver Scorecards**: Establish a monthly transport emissions dashboard and set per-vehicle emission targets. Review and benchmark each driver's performance against the fleet average."
                    ]
                },
                "electricity": {
                    "title": f"Cut Electricity Consumption ({pct}% of total footprint)",
                    "description": f"Electricity is your dominant emission source at {worst_val} tCO2e ({pct}% of total). Reducing energy waste through smart building management and renewable sourcing can deliver significant cuts.",
                    "target_reduction": min(int(pct * 0.5), 40),
                    "action_plan": [
                        f"- **Energy Audit**: Your electricity emissions of {worst_val} tCO2e indicate substantial grid dependency. Commission a full energy audit to identify the top 5 consumption hotspots in your facilities.",
                        "- **Smart Metering**: Install smart meters and IoT energy monitors on every major circuit. Set up automated alerts when any zone exceeds its baseline consumption by more than 10%.",
                        "- **HVAC & Lighting Upgrade**: Replace legacy HVAC and lighting systems with energy-efficient alternatives. LED retrofits and variable-speed HVAC drives typically pay for themselves within 18 months.",
                        "- **Renewable PPA**: Explore signing a renewable energy Power Purchase Agreement (PPA) or installing rooftop solar to offset at least 30% of your grid electricity consumption."
                    ]
                },
                "logistics": {
                    "title": f"Optimize Logistics Footprint ({pct}% of total footprint)",
                    "description": f"Logistics emissions contribute {worst_val} tCO2e ({pct}% of total). Consolidating shipments, choosing low-carbon freight options, and optimizing warehouse operations can reduce this significantly.",
                    "target_reduction": min(int(pct * 0.5), 35),
                    "action_plan": [
                        f"- **Shipment Consolidation**: Your logistics emissions stand at {worst_val} tCO2e. Start by analyzing shipment frequency and consolidation opportunities — reducing the number of half-full trucks can cut emissions by 20-30%.",
                        "- **Intermodal Transition**: Negotiate with logistics partners to use rail or intermodal transport for long-distance routes (over 500 km), which produces up to 75% less CO2 per tonne-km than road freight.",
                        "- **Warehouse Efficiency**: Optimize warehouse energy consumption by upgrading to high-efficiency lighting, installing dock door seals to reduce HVAC loss, and automating conveyor idle-shutdown timers.",
                        "- **Carrier KPI Tracking**: Implement a carbon-per-shipment tracking KPI in your logistics dashboard. Set quarterly targets to reduce the average CO2 per kg delivered by 15%."
                    ]
                },
                "manufacturing": {
                    "title": f"Reduce Manufacturing Emissions ({pct}% of total footprint)",
                    "description": f"Manufacturing/machinery operations are your largest emission source at {worst_val} tCO2e ({pct}% of total). Improving process efficiency and equipment maintenance can drive significant reductions.",
                    "target_reduction": min(int(pct * 0.5), 35),
                    "action_plan": [
                        f"- **Machinery Efficiency Study**: Your manufacturing emissions of {worst_val} tCO2e suggest high energy consumption from machinery. Conduct a process efficiency study to identify which machines have the highest energy draw per unit of output.",
                        "- **Predictive Maintenance Data**: Implement a predictive maintenance program using IoT sensors on critical equipment. Well-maintained machines run 10-20% more efficiently and produce fewer emissions than poorly maintained ones.",
                        "- **VFD Upgrades**: Evaluate whether switching to variable frequency drives (VFDs) on motors and compressors could reduce energy waste. VFDs adjust motor speed to match actual load, saving 20-40% of energy.",
                        "- **Shift-Level Monitoring**: Set up shift-level emission tracking so each production shift has visibility into their carbon impact. Create weekly leaderboards highlighting the most efficient shifts."
                    ]
                }
            }
            return domain_goals.get(worst, domain_goals["electricity"])
        
        # no reports at all — generic starter goal
        return {
            "title": f"Baseline Carbon Assessment ({domain.title()})",
            "description": f"Your company hasn't submitted any emission reports yet. The first step toward sustainability is establishing a clear baseline of your {domain.replace('_', ' ')} operations' carbon footprint.",
            "target_reduction": 15,
            "action_plan": [
                "- **Digital Invoice Uploads**: Collect 3 months of utility bills (electricity, gas, water) from all facilities and upload them through Aetherra's PDF Analyzer to establish your baseline energy consumption metrics.",
                "- **Vehicle Fleet Logging**: Document all company-owned vehicles and equipment, including fuel type, monthly mileage/hours, and age. This data feeds directly into Aetherra's calculator for accurate transport emission estimates.",
                "- **Prioritize Quick Wins**: Identify your top 3 emission sources from the baseline data and prioritize them for immediate reduction. Focus on quick wins like eliminating idle equipment and fixing compressed air leaks.",
                "- **Assign Sustainability Champions**: Set up monthly emission reporting using Aetherra's calculator to track progress. Assign a sustainability champion in each department to own data collection and goal accountability."
            ]
        }

    # AI TIERED CALL
    
    def _call_groq():
        groq_key = os.environ.get("GROQ_API_KEY", "").strip()
        if not groq_client or not groq_key: return None
        import requests as gen_requests
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}
        data = {
            "messages": [
                {"role": "system", "content": "You are a professional sustainability analyst. Output RAW JSON ONLY."},
                {"role": "user", "content": prompt}
            ],
            "model": "llama-3.3-70b-versatile",
            "temperature": 0.7,
            "response_format": {"type": "json_object"}
        }
        try:
            r = gen_requests.post(url, headers=headers, json=data, timeout=20)
            if r.status_code == 200:
                return r.json()['choices'][0]['message']['content']
        except:
            pass
        return None

    def _call_gemini():
        gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not gemini_key or gemini_key == "your_ai_studio_key_here": return None
        import requests as gen_requests
        headers = {'Content-Type': 'application/json'}
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"response_mime_type": "application/json"}
        }
        for m_id in ['gemini-1.5-flash', 'gemini-1.5-pro', 'gemini-pro']:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{m_id}:generateContent?key={gemini_key}"
                r = gen_requests.post(url, headers=headers, json=payload, timeout=20)
                if r.status_code == 200:
                    return r.json()['candidates'][0]['content']['parts'][0]['text']
            except:
                continue
        return None


    def _call_openai():
        if not openai_client: return None
        response = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7
        )
        return response.choices[0].message.content

    calls = {
        'groq': _call_groq,
        'gemini': _call_gemini,
        'openai': _call_openai
    }

    return run_tiered_ai(calls, fallback_value=get_offline_goal())


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
                if ext in ['png', 'jpg', 'jpeg', 'svg']:
                    os.makedirs('static/uploads', exist_ok=True)
                    path = f"static/uploads/{session['company_id']}.{ext}"
                    file.save(path)
                    companies_col.update_one({"_id": ObjectId(session['company_id'])}, {"$set": {"logo_path": path}})
                    flash("Logo updated successfully!", "success")
                else:
                    flash("Invalid file format. Use PNG, JPG, or SVG.", "error")
                    
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

            # 0. DUPLICATE DETECTION (SHA256 Content Hash)
            import hashlib
            content_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
            existing_report = reports_col.find_one({
                "company_id": str(session['company_id']),
                "content_hash": content_hash
            })
            if existing_report:
                report_id = str(existing_report['_id'])
                print(f"DEBUG: Duplicate report detected. Hash: {content_hash}, ID: {report_id}")
                flash(f"Duplicate Report: This document has already been analyzed. <a href='/report/{report_id}' class='alert-link' style='color:#fff; text-decoration:underline; font-weight:bold;'>View Original Report</a>", "error")
                return redirect(request.url)

            comp = companies_col.find_one({"_id": ObjectId(session['company_id'])})
            user_domain = comp.get('domain', 'technology') if comp else 'technology'
            print(f"DEBUG: User Company Domain: {user_domain}")

            # 1. DOMAIN CLASSIFICATION (AI QUICK-PASS)
            from pdf_parser import detect_domain, parse_pdf_offline
            
            # Initial heuristic check
            heuristic_domain = detect_domain(text, fallback_domain=user_domain)
            
            # AI Verification pass (High Speed)
            def _groq_classify():
                groq_key = os.environ.get("GROQ_API_KEY", "").strip()
                if not groq_client or not groq_key: return None
                import requests as gen_requests
                prompt = f"Analyze this text snippet and return exactly one word representing the industry: 'technology', 'logistics', 'construction', or 'manufacturing'. Text: {text[:2000]}"
                url = "https://api.groq.com/openai/v1/chat/completions"
                headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}
                data = {
                    "messages": [{"role": "user", "content": prompt}],
                    "model": "llama-3.1-8b-instant",
                    "temperature": 0.0
                }
                try:
                    r = gen_requests.post(url, headers=headers, json=data, timeout=10)
                    if r.status_code == 200:
                        res = r.json()['choices'][0]['message']['content'].lower().strip()
                        for d in ['technology', 'logistics', 'construction', 'manufacturing']:
                            if d in res: return d
                except:
                    pass
                return None

            
            ai_domain = safe_api_call('groq', _groq_classify)
            detected_domain = ai_domain or heuristic_domain or user_domain
            print(f"DEBUG: Domain Detection (Heuristic: {heuristic_domain}, AI: {ai_domain}, Final: {detected_domain})")
            
            # Normalize for comparison
            norm_detected = detected_domain.replace("_and_IT", "").replace("_and_Transport", "").lower()
            norm_user = user_domain.replace("_and_IT", "").replace("_and_Transport", "").lower()

            if norm_detected != norm_user:
                flash(f"Invalid Report: Aetherra AI detected this as a {detected_domain.replace('_', ' ').title()} report, which does not match your workspace profile ({user_domain.replace('_', ' ').title()}).", "error")
                return redirect(request.url)

            # 2. AI EXTRACTION PIPELINE (3-LAYER)
            extracted = None
            prompt = f"""[CRITICAL: RETURN ONLY RAW JSON. NO MARKDOWN. NO PREAMBLE.]
Analyze this ESG report and extract EXACT operational data. 
DO NOT hallucinate or estimate. If a number or table is not explicitly present, return 0 or an empty array.
The document features granular asset tracking and operational metrics.
The title or filename will be a generic company name and will NOT tell you the industry. 
You MUST analyze the text content (keywords, assets, activities) to infer the domain.

Domains to detect:
- Technology: server, data center, cooling unit, IT, software, computing, network, digital, workstation, cloud infrastructure, latency, bandwidth.
- Logistics: warehouse, freight, delivery van, truck, shipping, transport, cargo, courier, supply chain, dispatch, last-mile.
- Construction: site work, hauling, excavator, temporary office, crane, concrete, civil engineering, earthmoving, structural, cement, scaffolding, contractor.
- Manufacturing: factory, machinery, cnc, shipments, production line, assembly, industrial, fabrication, shop floor, raw materials, throughput, machining, inventory.

[UNIQUE FEATURE ANALYSIS]:
- Identify and extract the 3-5 most significant operational metrics or emission sources unique to this specific document.
- Do NOT force data into "Scope 1/2/3" if they aren't explicitly used. Instead, use specific, identifiable categories found in the text.
- If the report uses custom terminology (e.g., "Fuel Consumption for On-Site Generators"), use that as a category name.

[IMPORTANT]: The user's company is officially registered in the {user_domain.replace('_', ' ').title()} sector. Favor this sector if the document contains supporting evidence.

- LAYER 0 (Scope Detect): Determine if the report is "monthly" or "yearly". Look for "1 January - 31 December", "Annual Report", etc.
  If YEARLY, you MUST extract scope_1, scope_2, scope_3 values from the text.
- LAYER 1 (Structural): Find the reporting period and total net emissions.
- LAYER 2 (Detailed Table): Extract up to 10 rows from the asset/vehicle/machine table.
  *CRITICAL ANTI-HALLUCINATION*: ONLY extract rows with EXACT quantitative metrics found in the text. DO NOT GUESS OR ESTIMATE ASSETS. If there is no asset list, return an empty array `[]`.
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
  "report_type": "string (monthly or yearly)",
  "detected_domain": "string",
  "reporting_period": "string",
  "scope_1": number,
  "scope_2": number,
  "scope_3": number,
  "breakdown_categories": {{"Category Name 1": number, "Category Name 2": number}},
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

            
            # TIERED AI EXTRACTION PIPELINE
            ai_clients = {
                'groq': groq_client,
                'gemini_key': os.environ.get("GEMINI_API_KEY"),
                'openai': openai_client
            }
            
            def _groq_ext():
                groq_key = os.environ.get("GROQ_API_KEY", "").strip()
                if not groq_client or not groq_key: return None
                import requests as gen_requests
                url = "https://api.groq.com/openai/v1/chat/completions"
                headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}
                data = {
                    "messages": [
                        {"role": "system", "content": "You are a carbond data analyst. Output RAW JSON ONLY. DO NOT guess data."},
                        {"role": "user", "content": prompt}
                    ],
                    "model": "llama-3.3-70b-versatile",
                    "temperature": 0.0,
                    "response_format": {"type": "json_object"}

                }
                try:
                    r = gen_requests.post(url, headers=headers, json=data, timeout=30)
                    if r.status_code == 200:
                        return r.json()['choices'][0]['message']['content']
                    else:
                        print(f"DEBUG: Groq API Error: {r.text}")
                except Exception as e:
                    print(f"DEBUG: Groq Request Error: {e}")
                return None

            def _gemini_ext():
                gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
                if not gemini_key: return None
                
                # LAYER 1: Raw API call (Most stable for Python 3.14/Windows)
                import requests as gen_requests
                headers = {'Content-Type': 'application/json'}
                payload = {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"response_mime_type": "application/json"}
                }
                
                for m_id in ['gemini-1.5-flash', 'gemini-1.5-pro', 'gemini-pro']:
                    try:
                        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m_id}:generateContent?key={gemini_key}"
                        r = gen_requests.post(url, headers=headers, json=payload, timeout=20)
                        if r.status_code == 200:
                            res_json = r.json()
                            return res_json['candidates'][0]['content']['parts'][0]['text']
                    except Exception as e:
                        print(f"DEBUG: Gemini raw fallback failed for {m_id}: {e}")
                
                # LAYER 2: SDK fallback (Only if raw fails)
                try:
                    import google.generativeai as genai
                    genai.configure(api_key=gemini_key)
                    model = genai.GenerativeModel('models/gemini-1.5-flash', generation_config={"response_mime_type": "application/json"})
                    response = model.generate_content(prompt)
                    return response.text
                except: return None

            def _openai_ext():
                if not openai_client: return None
                response = openai_client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[{"role": "system", "content": "You output JSON only."},
                              {"role": "user", "content": prompt}],
                    temperature=0.1
                )
                return response.choices[0].message.content

            calls = {'groq': _groq_ext, 'gemini': _gemini_ext, 'openai': _openai_ext}
            extracted = run_tiered_ai(calls)

            # TIER 3: OFFLINE PARSER (FINAL FALLBACK - STRONG)
            if not extracted:
                print("DEBUG: All AI engines failed or returned empty results. Invoking STRONG offline heuristic parser...")
                extracted = parse_pdf_offline(text, user_domain)
                print(f"DEBUG: Offline Parser Result Sum: {extracted.get('total_emissions', 0)} tCO2e")
                
                # Double-check the success flag we just added to pdf_parser.py
                if not extracted.get('success'):
                    # The offline parser also failed to find meaningful data
                    print(f"DEBUG: Offline Parser Failure: {extracted.get('message')}")
                    flash("AI could not read the document. Ensure it is a valid sustainability PDF or contains recognizable emission data.", "error")
                    return redirect(request.url)
                
                print("DEBUG: Offline Parser successful. Proceeding with heuristic data.")

            print(f"DEBUG: Final Extracted Data: {extracted}")

            # MONTHLY HANDLING BLOCK (Existing)
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
            import traceback
            error_trace = traceback.format_exc()
            print(f"CRITICAL SYSTEM FAILURE: {type(e).__name__}: {str(e)}")
            print(f"TRACEBACK: {error_trace}")
            flash(f"System Error: {type(e).__name__} - {str(e)}. Please check terminal for details.", "error")
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
                ai_clients = {
                    'groq': groq_client,
                    'gemini_key': os.environ.get("GEMINI_API_KEY"),
                    'openai': openai_client
                }
                ai_recs = get_ai_insights(domain, high_cats, ai_clients)

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


def get_chart_narrative(report):
    """
    Generates a professional AI analysis of the report's data visualizations.
    Focuses on explaining the 'why' behind the Pie and Bar charts.
    """
    from api_guard import run_tiered_ai
    
    # 1. Prepare data context
    total = report.get('total_emissions', 0)
    domain = report.get('domain', 'technology')
    
    if report.get('type') == 'yearly':
        breakdown = report.get('breakdown', [])
        breakdown_str = ", ".join([f"{b['name']}: {b['emissions']} tCO2e" for b in breakdown])
        context = f"Yearly report for {domain} sector. Total: {total} tCO2e. Breakdown: {breakdown_str}."
    else:
        cats = {
            "Transport": report.get('transport_emissions', 0),
            "Electricity": report.get('electricity_emissions', 0),
            "Logistics": report.get('logistics_emissions', 0),
            "Manufacturing": report.get('manufacturing_emissions', 0)
        }
        breakdown_str = ", ".join([f"{k}: {v} tCO2e" for k, v in cats.items() if v > 0])
        context = f"Monthly report for {domain} sector. Total: {total} tCO2e. Category contributions: {breakdown_str}."

    prompt = f"""You are a senior ESG Data Analyst. Analyze the following emission breakdown and provide a 'Narrative analysis' that would appear under a chart.
    DATA: {context}
    
    REQUIREMENTS:
    - Explain which category dominates and what that implies for the business operations.
    - Mention one potential 'Hidden opportunity' for reduction based on the domain ({domain}).
    - Keep it strictly professional, insightful, and under 100 words.
    - Return ONLY the narrative text.
    """

    def _call_groq():
        groq_key = os.environ.get("GROQ_API_KEY", "").strip()
        if not groq_client or not groq_key: return None
        import requests as gen_requests
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}
        data = {
            "messages": [{"role": "user", "content": prompt}],
            "model": "llama-3.1-7b-instant",
            "temperature": 0.7
        }
        try:
            r = gen_requests.post(url, headers=headers, json=data, timeout=15)
            if r.status_code == 200:
                return r.json()['choices'][0]['message']['content']
        except:
            pass
        return None

    def _call_gemini():
        gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not gemini_key: return None
        import google.generativeai as genai
        genai.configure(api_key=gemini_key)
        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content(prompt)
        return response.text

    def _call_openai():
        if not openai_client: return None
        response = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7
        )
        return response.choices[0].message.content

    calls = {'groq': _call_groq, 'gemini': _call_gemini, 'openai': _call_openai}
    
    fallback = f"The {domain} sector emissions are primarily driven by the reported assets. This chart visualizes the distribution of your {total} tCO2e impact."
    
    return run_tiered_ai(calls, fallback_value=fallback, is_json=False)

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

