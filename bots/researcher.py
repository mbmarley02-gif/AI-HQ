import os, json, time, hashlib, datetime, re
import requests
import firebase_admin
from firebase_admin import credentials, firestore

TOPIC = os.environ.get("TOPIC", "").strip()
MODEL = "gemini-flash-latest"
MAX_SOURCES = 5      # pages read per run
MAX_CHARS = 12000    # text per page sent to Gemini
TAVILY = {"Authorization": "Bearer " + os.environ.get("TAVILY_API_KEY", "")}
GEMINI = {"x-goog-api-key": os.environ.get("GEMINI_API_KEY", "")}


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def post(url, headers, body, tries=3):
    for i in range(tries):
        r = requests.post(url, headers=headers, json=body, timeout=90)
        if r.status_code in (429, 500, 503) and i < tries - 1:
            time.sleep(10 * (i + 1))
            continue
        r.raise_for_status()
        return r.json()


def norm(t):
    t = t.replace("\u2019", "'").replace("\u2018", "'")
    t = t.replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"\s+", " ", t).strip()


def find_pages(topic):
    d = post("https://api.tavily.com/search", TAVILY,
             {"query": topic, "max_results": MAX_SOURCES})
    urls = [x["url"] for x in d.get("results", [])]
    d = post("https://api.tavily.com/extract", TAVILY, {"urls": urls})
    return [(x["url"], norm(x.get("raw_content", ""))[:MAX_CHARS])
            for x in d.get("results", [])]


def ask_gemini(topic, page):
    prompt = (
        "You are a research assistant. Topic: " + topic + "\n"
        "From the PAGE TEXT below, pick up to 4 facts useful for this topic. "
        "For each, copy an EXACT quote from the page (max 300 characters) "
        "and write a one-sentence claim. Never invent or edit quotes. "
        'Reply only as JSON: [{"quote": "...", "claim": "..."}]. '
        "If nothing is useful, reply [].\n\nPAGE TEXT:\n" + page
    )
    d = post("https://generativelanguage.googleapis.com/v1beta/models/"
             + MODEL + ":generateContent", GEMINI,
             {"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"responseMimeType": "application/json"}})
    text = d["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def main():
    if not TOPIC:
        raise SystemExit("No topic given.")
    db = firestore.client()
    run_id = now().strftime("%Y%m%d-%H%M%S")
    saved = dropped = 0
    for url, page in find_pages(TOPIC):
        if len(page) < 200:
            continue
        try:
            items = ask_gemini(TOPIC, page)
        except Exception as e:
            print("Skipped a page:", type(e).__name__)
            continue
        for it in items:
            quote = norm(str(it.get("quote", "")))
            if not quote or quote not in page:
                dropped += 1
                continue
            key = hashlib.sha1((url + quote).encode()).hexdigest()[:20]
            db.collection("evidence").document(key).set({
                "topic": TOPIC, "run_id": run_id, "url": url,
                "quote": quote, "claim": str(it.get("claim", "")),
                "verified": True, "fetched_at": now()})
            saved += 1
    db.collection("bot_runs").add({
        "bot": "researcher", "topic": TOPIC, "run_id": run_id,
        "saved": saved, "dropped_unverified": dropped, "at": now()})
    print("Saved", saved, "verified quotes. Dropped", dropped, "unverified.")


if __name__ == "__main__":
    cred = credentials.Certificate(json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"]))
    firebase_admin.initialize_app(cred)
    main()
  
