import os, json, time, datetime
from urllib.parse import urlparse
import requests
import firebase_admin
from firebase_admin import credentials, firestore

MODEL = "gemini-flash-latest"
MAX_ITEMS = 30     # quotes challenged per run
SITE_CHARS = 4000  # homepage text sent to Gemini
TAVILY = {"Authorization": "Bearer " + os.environ.get("TAVILY_API_KEY", "")}
GEMINI = {"x-goog-api-key": os.environ.get("GEMINI_API_KEY", "")}
VERDICTS = {"keep", "downgrade", "reject"}


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def post(url, headers, body, tries=4):
    time.sleep(8)
    for i in range(tries):
        r = requests.post(url, headers=headers, json=body, timeout=90)
        if r.status_code in (429, 500, 503) and i < tries - 1:
            time.sleep(20 * (i + 1))
            continue
        r.raise_for_status()
        return r.json()


def domain_of(url):
    return urlparse(url).netloc.lower().replace("www.", "")


def site_text(domain):
    try:
        d = post("https://api.tavily.com/extract", TAVILY,
                 {"urls": ["https://" + domain]})
        res = d.get("results", [])
        if res:
            return " ".join(res[0].get("raw_content", "").split())[:SITE_CHARS]
    except Exception as e:
        print("Could not read", domain, type(e).__name__)
    return ""


def challenge(item, site):
    prompt = (
        "You are a skeptical fact-checker. Challenge this piece of evidence.\n"
        "TOPIC: " + item["topic"] + "\n"
        "CLAIM: " + item["claim"] + "\n"
        "QUOTE: " + item["quote"] + "\n"
        "URL: " + item["url"] + "\n"
        "SITE HOMEPAGE TEXT: " + (site or "(unavailable - do not guess)") + "\n\n"
        "Use ONLY the homepage text for facts about the site. Decide:\n"
        "- conflict_of_interest: true only if the homepage clearly shows the "
        "site sells a product or service that this quote promotes.\n"
        "- overreach: true if the claim goes beyond what the quote says.\n"
        "- verdict: 'keep' if reliable and on topic, 'downgrade' if usable "
        "only with caution, 'reject' if it should not be trusted.\n"
        "- concern: one short sentence giving your main doubt.\n"
        'Reply only as JSON: {"conflict_of_interest":false,"overreach":false,'
        '"verdict":"keep","concern":"..."}'
    )
    d = post("https://generativelanguage.googleapis.com/v1beta/models/"
             + MODEL + ":generateContent", GEMINI,
             {"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"responseMimeType": "application/json"}})
    return json.loads(d["candidates"][0]["content"]["parts"][0]["text"])


def main():
    db = firestore.client()
    todo = []
    for doc in db.collection("evidence").limit(300).stream():
        data = doc.to_dict()
        if "analysis" in data and "skeptic" not in data:
            data["id"] = doc.id
            todo.append(data)
    todo = todo[:MAX_ITEMS]
    cache = {}
    done = skipped = fails = 0
    for item in todo:
        dom = domain_of(item["url"])
        if dom not in cache:
            cache[dom] = site_text(dom)
        site = cache[dom]
        try:
            r = challenge(item, site)
        except Exception as e:
            print("Skipped one:", type(e).__name__,
                  getattr(getattr(e, "response", None), "status_code", ""))
            skipped += 1
            continue
        verdict = r.get("verdict") if r.get("verdict") in VERDICTS else "downgrade"
        db.collection("evidence").document(item["id"]).update({
            "skeptic": {
                "verdict": verdict,
                "conflict_of_interest": bool(r.get("conflict_of_interest")),
                "overreach": bool(r.get("overreach")),
                "site_checked": bool(site),
                "concern": str(r.get("concern", ""))[:300]},
            "skeptic_at": now()})
        done += 1
    db.collection("bot_runs").add({
        "bot": "skeptic", "challenged": done, "skipped": skipped, "at": now()})
    print("Challenged", done, "quotes. Skipped", skipped, ".")


if __name__ == "__main__":
    cred = credentials.Certificate(json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"]))
    firebase_admin.initialize_app(cred)
    main()
