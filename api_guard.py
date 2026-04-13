# api_guard.py
# Centralized API usage tracking and rate limiting
# Prevents accidental overuse of external API keys
# Each API has a daily call limit - once hit, requests gracefully fall back to local data

import os
import json
from datetime import datetime, date
from threading import Lock

# file-based counter to persist across server restarts
COUNTER_FILE = os.path.join(os.path.dirname(__file__), '.api_usage.json')
_lock = Lock()

# ---------- DAILY LIMITS (set conservatively below free tier caps) ----------
# These are YOUR safety limits, not the provider's limits
DAILY_LIMITS = {
    "groq":             1000,  # Groq: primary, lightning fast, generous limits
    "gemini":           60,    # Gemini: secondary reliable fallback
    "openai":           20,    # OpenAI: tertiary fallback (expensive/strict limits)
    "climatiq":         25,    # Climatiq free tier: 1000/month
    "carbon_interface": 8,     # Carbon Interface free: 200/month
    "google_maps":      15,    # Google Maps: $200 free credit
    "sendgrid":         20,    # SendGrid free: 100/day
}


def _load_counters():
    """Load today's counters from disk."""
    try:
        with open(COUNTER_FILE, 'r') as f:
            data = json.load(f)
        # reset if it's a new day
        if data.get('date') != str(date.today()):
            return {'date': str(date.today()), 'counts': {}}
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {'date': str(date.today()), 'counts': {}}


def _save_counters(data):
    """Save counters to disk."""
    try:
        with open(COUNTER_FILE, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        print(f"Warning: could not save API counters: {e}")


def can_call(api_name):
    """
    Check if we're allowed to make another call to this API today.
    Returns True if under the daily limit, False if limit reached.
    """
    api_name = api_name.lower()
    limit = DAILY_LIMITS.get(api_name, 10)  # default 10 if unknown
    
    with _lock:
        data = _load_counters()
        current = data['counts'].get(api_name, 0)
        return current < limit


def record_call(api_name):
    """Record that we made one API call. Returns the new count."""
    api_name = api_name.lower()
    
    with _lock:
        data = _load_counters()
        current = data['counts'].get(api_name, 0)
        data['counts'][api_name] = current + 1
        _save_counters(data)
        return current + 1


def get_usage_summary():
    """Return a dict showing current usage vs limits for all APIs."""
    with _lock:
        data = _load_counters()
    
    summary = {}
    for api_name, limit in DAILY_LIMITS.items():
        used = data['counts'].get(api_name, 0)
        summary[api_name] = {
            'used': used,
            'limit': limit,
            'remaining': max(0, limit - used),
            'exhausted': used >= limit
        }
    return summary


def safe_api_call(api_name, call_fn, fallback_fn=None):
    """
    Safely execute an API call with automatic limit checking and fallback.
    """
    if not can_call(api_name):
        print(f"API GUARD: Daily limit reached for {api_name}. Using fallback.")
        if fallback_fn: return fallback_fn()
        return None
    
    try:
        result = call_fn()
        record_call(api_name)
        return result
    except Exception as e:
        print(f"API GUARD: {api_name} call failed ({e}). Using fallback.")
        if fallback_fn: return fallback_fn()
        return None


def clean_json_response(text):
    """
    Strips markdown backticks and other common AI noise from a JSON string.
    Finds the first '{' and last '}' to isolate the JSON object.
    """
    if not text or not isinstance(text, str): return text
    
    # Trace the boundaries of the JSON object
    start = text.find('{')
    end = text.rfind('}')
    
    if start != -1 and end != -1:
        text = text[start:end+1]
    
    # Basic cleanup of common AI formatting artifacts
    text = text.strip()
    
    # Remove markdown code blocks if they are still wrapping the result
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"): lines = lines[1:]
        if lines and lines[-1].startswith("```"): lines = lines[:-1]
        text = "\n".join(lines).strip()
        
    return text


def run_tiered_ai(calls_dict, fallback_value=None, is_json=True):
    """
    Executes AI calls in a strict hierarchical order: Groq -> Gemini -> OpenAI.
    """
    # 1. GROQ
    if 'groq' in calls_dict:
        res = safe_api_call('groq', calls_dict['groq'])
        if res:
            if is_json and isinstance(res, str):
                try: return json.loads(clean_json_response(res))
                except: pass
            else: return res
        
    # 2. GEMINI
    if 'gemini' in calls_dict:
        res = safe_api_call('gemini', calls_dict['gemini'])
        if res:
            if is_json and isinstance(res, str):
                try: return json.loads(clean_json_response(res))
                except: pass
            else: return res
        
    # 3. OPENAI
    if 'openai' in calls_dict:
        res = safe_api_call('openai', calls_dict['openai'])
        if res:
            if is_json and isinstance(res, str):
                try: return json.loads(clean_json_response(res))
                except: pass
            else: return res
        
    return fallback_value
