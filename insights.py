# insights.py
# domain-specific sustainability insights engine
# primary: uses openai api key for dynamic AI-generated insights
# fallback: prebaked tips (15 per category per domain) when api is unavailable

import random
import json
import os

# ========== prebaked insights (fallback) ==========

DOMAIN_INSIGHTS = {
    # ----- TECHNOLOGY / IT -----
    "technology": [
        "Migrate on-premise servers to green cloud providers (AWS, Azure, GCP have carbon-neutral data centers).",
        "Encourage remote work policies to cut employee commute emissions by up to 40%.",
        "Upgrade server room cooling with hot/cold aisle containment to save 20-30% power.",
        "Set server room temperature to 27C instead of 20C (ASHRAE recommends up to 27C).",
        "Virtualize servers to consolidate workloads and cut physical machine count.",
        "Replace company cars with electric vehicles (EVs) for all direct fleet operations.",
        "Use video conferencing instead of flying to meetings — saves ~1.1 tonnes CO2 per round trip.",
        "Replace fluorescent workplace lights with LED panels to save 50-75% lighting energy.",
        "Install smart power strips that automatically cut phantom power from unused peripherals.",
        "Deploy serverless cloud architectures to minimize idle idle infrastructure consumption.",
        "Consolidate hardware orders into larger shipments to reduce overall vendor transport emissions.",
        "Implement a 'Buy-back' or hardware refurbishing program for old off-cycle devices to extend product lifecycles.",
        "Schedule non-urgent high-compute batch processing during off-peak (cleaner grid) hours.",
        "Use laptop docking stations instead of traditional desktops — laptops use 70% less energy.",
        "Require your Top 10 software/hardware suppliers to report their carbon data annually.",
        "Audit contract manufacturers for zero-emission assembly lines.",
        "Offer green-transit incentives (bike-to-work or public rail passes) to staff.",
        "Specify recycled aluminum and plastics in any custom physical product designs.",
        "Digital delivery eliminates physical shipping emissions entirely – prioritize SaaS over hard-tokens.",
        "Install high-efficiency occupancy sensors in all meeting rooms to auto-switch HVAC and lighting."
    ],

    # ----- LOGISTICS & TRANSPORT -----
    "logistics": [
        "Consolidate warehouse shipments to improve truck load utilization above 85%.",
        "Install GPS tracking on every vehicle to identify and eliminate inefficient routes in real time.",
        "Transition last-mile delivery fleets to electric vans or cargo e-bikes.",
        "Upgrade warehouse lighting to LED with high-bay motion sensors to save 60% lighting costs.",
        "Use predictive analytics to optimize route planning and reduce empty-haul miles.",
        "Regularly service diesel engines — a well-tuned engine uses 4-10% less fuel.",
        "Use intermodal transport (truck + rail) for all freight shipments over 500km.",
        "Negotiate with your primary carriers for certified carbon-neutral delivery options.",
        "Use smart climate controls in distribution centers to prevent over-cooling stored goods.",
        "Install aerodynamic kits (cab fairings, side skirts) on heavy trucks to save 5-8% fuel per trip.",
        "Use returnable and reusable packaging containers to eliminate single-use cardboard waste.",
        "Implement dynamic cross-docking to reduce warehousing times and double-handling energy.",
        "Use AI demand forecasting to pre-position stock and significantly reduce rush air-shipments.",
        "Enforce a strict speed limit policy for fleet vehicles; speeds over 90km/h exponentially tanks fuel efficiency.",
        "Use lightweight pallets (plastic or composite) instead of heavy hardwood to cut transport weight.",
        "Track fuel consumption per vehicle per month to spot and correct mechanical anomalies early.",
        "Deploy robust tire pressure monitoring systems (TPMS) — correct pressure reliably saves 3% fuel.",
        "Install transparent dock door seals at warehouses to drastically reduce HVAC temperature loss.",
        "Turn off automated conveyor belts and sorting machines dynamically during off-hours.",
        "Digitize proof-of-delivery completely to eliminate paper-based return trips."
    ],

    # ----- CONSTRUCTION -----
    "construction": [
        "Switch to low-carbon concrete mixes (slag or fly ash blends) for a 30-50% materials emission reduction.",
        "Use strictly sustainably sourced timber (FSC certified) for all structural elements.",
        "Pre-fabricate structural components off-site to reduce on-site machinery waste and transport runs.",
        "Replace older haul trucks with Tier 4 or fully electric models for massive exhaust reductions.",
        "Use solar-powered temporary LED tower lighting on all night-shift construction sites.",
        "Use recycled structural steel, which produces 75% less CO2 during fabrication than virgin steel.",
        "Schedule aggregate crew carpooling to substantially reduce the number of personal vehicles on site.",
        "Implement strict anti-idling policies for all heavy site vehicles to save 3-5% fuel daily.",
        "Deploy large-scale battery energy storage systems instead of diesel generators for small loads.",
        "Use electrically powered mini excavators for smaller trenching tasks instead of diesel variants.",
        "Connect the site to main grid power as early as possible instead of running temporary diesel generators.",
        "Stockpile excavated soil on-site for grading reuse instead of trucking it away.",
        "Source heavy aggregates from local quarries strictly within 50km to minimize haul emissions.",
        "Use automated machine control (GPS grading) on excavators to reduce passes and fuel consumption.",
        "Install equipment fuel-flow tracking meters to monitor and restrict real-time consumption.",
        "Reuse formwork and metal scaffolding strictly across all projects to reduce new material ordering.",
        "Deploy electric heavy-duty forklifts on site instead of traditional propane-powered models.",
        "Use digital BIM takeoff tools to precisely estimate material needs and eliminate over-ordering waste.",
        "Service heavy lift machinery strictly every month to maintain absolute peak fuel efficiency.",
        "Use bio-lubricants in hydraulic systems to radically reduce environmental hazard impact."
    ],

    # ----- MANUFACTURING -----
    "manufacturing": [
        "Install smart digital sub-meters on every major machine to immediately identify energy hogs.",
        "Recover waste heat from ovens, kilns or compressors to pre-heat water or nearby facilities.",
        "Optimize delivery schedules to consolidate all outbound manufactured shipments.",
        "Use variable frequency drives (VFDs) on all pumps and fans to match motor speed to exact load.",
        "Implement a rigorous compressed air leak detection program — leaks account for 20% of air energy waste.",
        "Locate new facilities geographically closer to key tier-1 suppliers to minimize inbound transport.",
        "Perform preventive maintenance consistently to keep high-load equipment running efficiently.",
        "Audit packaging suppliers exclusively for sustainable and renewable material sourcing practices.",
        "Eliminate over-packaging by right-sizing automated parcel boxes directly to product dimensions.",
        "Use electric internal forklifts and automated warehouse vehicles instead of propane variants.",
        "Track manufacturing emissions granularly per-delivery-mile and set strict annual reduction targets.",
        "Use rail siding connections directly at major factories for bulk raw material delivery.",
        "Negotiate with outbound logistics partners for certified carbon-neutral shipping options.",
        "Deploy automated predictive maintenance AI via IoT sensors to prevent inefficient machine failures.",
        "Design your physical products for modular disassembly so materials can be strictly recycled.",
        "Use high-ratio recycled material content (80%+) for all corrugated cardboard boxing.",
        "Insulate high-temperature industrial equipment (like injection molders or boilers) to prevent thermal loss.",
        "Use digital twin AI simulations to constantly optimize supply chain and factory floor transport flows.",
        "Schedule the most energy-intensive manufacturing processes during off-peak renewable grid hours.",
        "Partner selectively with upstream suppliers who actively use 100% renewable energy in their own production."
    ]
}

# legacy flat fallback
INSIGHTS = DOMAIN_INSIGHTS["technology"]

def get_random_insight(category=None, domain=None):
    """Get a random prebaked insight for dashboard display strictly tied to domain."""
    if domain and domain in DOMAIN_INSIGHTS:
        pool = DOMAIN_INSIGHTS[domain]
    else:
        pool = DOMAIN_INSIGHTS["technology"]
    
    return random.choice(pool)


def get_ai_insights(domain, high_cats, ai_clients):
    """
    Primary path: use Tiered AI (Groq -> Gemini -> OpenAI) to generate domain-aware sustainability tips.
    Falls back to prebaked insights if all API calls fail or daily limits are reached.
    """
    from api_guard import run_tiered_ai
    
    prompt = (
        f"You are a sustainability expert. A {domain} company has high emissions "
        f"in these categories: {', '.join(high_cats)}. "
        f"For each category, provide 3 specific, actionable sustainability tips "
        f"tailored to the {domain} industry. "
        f"Return ONLY raw JSON with category names as keys and arrays of 3 tip strings as values."
    )

    def _call_groq():
        groq_key = os.environ.get("GROQ_API_KEY", "").strip()
        if not ai_clients.get('groq') or not groq_key: return None
        import requests as gen_requests
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}
        data = {
            "messages": [
                {"role": "system", "content": "You are a professional sustainability analyst. Output RAW JSON ONLY."},
                {"role": "user", "content": prompt}
            ],
            "model": "llama-3.1-8b-instant",
            "temperature": 0.7,
            "response_format": {"type": "json_object"}
        }
        try:
            r = gen_requests.post(url, headers=headers, json=data, timeout=15)
            if r.status_code == 200:
                return r.json()['choices'][0]['message']['content']
        except:
            pass
        return None


    def _call_gemini():
        gemini_key = ai_clients.get('gemini_key')
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
            except Exception:
                continue
        return None

    def _call_openai():
        # Decommissioned/Obsolete
        return None
    
    def _fallback():
        result = {}
        pool = DOMAIN_INSIGHTS.get(domain, DOMAIN_INSIGHTS["technology"])
        for cat in high_cats:
            # We no longer have category sub-pools, we just grab 3 unique recommendations from the domain pool
            result[cat] = random.sample(pool, min(3, len(pool)))
        return result

    calls = {
        'groq': _call_groq,
        'gemini': _call_gemini,
        'openai': _call_openai
    }
    
    result = run_tiered_ai(calls)
    if result:
        return result
    
    return _fallback()


def get_insights_for_report(cats, domain=None):
    """Legacy helper for basic insight retrieval."""
    result = {}
    for cat in cats:
        result[cat] = get_random_insight(cat, domain)
    return result
