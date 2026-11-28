import os, json, time, datetime
import requests
import firebase_admin
from firebase_admin import credentials, firestore

MODEL = "gemini-flash-latest"
BATCH = 8          # quotes judged per Gemini call
MAX_ITEMS = 40     # quotes judged per run
GEMINI = {"x-goog-api-key": os.environ.get("GEMINI_API_KEY", "")}
SUPPORT = {"supported", "partial", "unsupported"}
SOURCES = {"neutral", "promotional", "forum", "news", "official", "unknown"}


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


def judge(batch):
    items = [{"id": b["id"], "topic": b["topic"], "claim": b["claim"],
              "quote": b["quote"], "url": b["url"]} for b in batch]
    prompt = (
        "You are a strict research analyst. For each item, judge it using "
        "ONLY the topic, claim, quote and url given. Do not invent facts "
        "about the website.\n"
        "- relevance: integer 0-10, how well the quote answers the topic.\n"
        "- support: 'supported' if the quote alone proves the claim, "
        "'partial' if it only hints at it, 'unsupported' if the claim "
        "goes beyond the quote.\n"
        "- source_type: neutral, promotional (the site sells what it is "
        "recommending), forum, news, official, or unknown. If unsure, "
        "use unknown.\n"
        "- note: one short sentence explaining your judgement.\n"
        'Reply only as JSON: [{"id":"...","relevance":0,"support":"...",'
        '"source_type":"...","note":"..."}]\n\nITEMS:\n' + json.dumps(items)
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
        if "analysis" not in data:
            data["id"] = doc.id
            todo.append(data)
    todo = todo[:MAX_ITEMS]
    done = skipped = 0
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        ids = {b["id"] for b in batch}
        try:
            results = judge(batch)
        except Exception as e:
            print("Skipped a batch:", type(e).__name__,
                  getattr(getattr(e, "response", None), "status_code", ""))
            skipped += len(batch)
            continue
        for r in results:
            if r.get("id") not in ids:
                continue
            try:
                rel = max(0, min(10, int(r.get("relevance", 0))))
            except (TypeError, ValueError):
                continue
            sup = r.get("support") if r.get("support") in SUPPORT else "unsupported"
            src = r.get("source_type") if r.get("source_type") in SOURCES else "unknown"
            db.collection("evidence").document(r["id"]).update({
                "analysis": {"relevance": rel, "support": sup,
                             "source_type": src,
                             "note": str(r.get("note", ""))[:300]},
                "analyzed_at": now()})
            done += 1
    db.collection("bot_runs").add({
        "bot": "analyst", "analyzed": done, "skipped": skipped, "at": now()})
    print("Analyzed", done, "quotes. Skipped", skipped, ".")


if __name__ == "__main__":
    cred = credentials.Certificate(json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"]))
    firebase_admin.initialize_app(cred)
    main()
