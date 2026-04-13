import os
import requests
from dotenv import load_dotenv

# Load keys
load_dotenv('.env.local')

GROQ_KEY = os.environ.get("GROQ_API_KEY")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

print("=== Aetherra AI Diagnostic Tool ===")
print(f"Groq Key Found: {'Yes' if GROQ_KEY else 'No'}")
print(f"Gemini Key Found: {'Yes' if GEMINI_KEY else 'No'}")
print("-" * 35)

def test_groq():
    if not GROQ_KEY: return "Skipped (No Key)"
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
    data = {
        "messages": [{"role": "user", "content": "Hello"}],
        "model": "llama-3.1-8b-instant"
    }
    try:
        r = requests.post(url, headers=headers, json=data, timeout=10)
        if r.status_code == 200: return "SUCCESS (HTTP)"
        return f"FAILED (HTTP {r.status_code}): {r.text[:100]}"
    except Exception as e:
        return f"ERROR: {str(e)}"

def test_gemini():
    if not GEMINI_KEY: return "Skipped (No Key)"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}"
    headers = {"Content-Type": "application/json"}
    data = {"contents": [{"parts": [{"text": "Hello"}]}]}
    try:
        r = requests.post(url, headers=headers, json=data, timeout=10)
        if r.status_code == 200: return "SUCCESS (HTTP)"
        return f"FAILED (HTTP {r.status_code}): {r.text[:100]}"
    except Exception as e:
        return f"ERROR: {str(e)}"

print(f"Testing Groq...   {test_groq()}")
print(f"Testing Gemini... {test_gemini()}")
print("-" * 35)
print("If both say SUCCESS, then Aetherra is ready.")
print("If they fail, check your keys or internet connection.")
