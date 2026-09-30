# MedTrack AI – Prescription Scanner + Patient Dashboard + Smart Reminders

## What it does
1. Patient registers / logs in (data saved per user, many users at once).
2. Uploads a prescription photo → a vision AI (Claude) extracts medicines, dose, morning/noon/night, before/after meal, duration.
3. Patient reviews & corrects the result, then saves it.
4. Dashboard shows today's schedule, prescription history, active/completed medicines, "mark as taken".
5. Exactly **one reminder per period** (morning / noon / night) that lists **all** medicines, grouped Before meal / After meal. Delivered as in-app banner + browser notification + optional email.

## Tools
Python 3.11+, Flask, Flask-SQLAlchemy, Flask-Login, APScheduler, Anthropic API (vision), SQLite (dev) / PostgreSQL (prod), Tailwind CDN, Gunicorn, Render (hosting), Git/GitHub.

## Step-by-step development plan (hackathon timeline)
| Step | Time | Task |
|---|---|---|
| 1 | 30 min | Get an Anthropic API key; create repo; venv; `pip install -r requirements.txt` |
| 2 | 45 min | Models + registration/login (done in `app.py`) |
| 3 | 60 min | AI scan endpoint `/api/scan` – tune the prompt with 5–10 real prescriptions |
| 4 | 45 min | Review-and-save screen (`scan.html`) – never trust OCR blindly, patient confirms |
| 5 | 60 min | Dashboard: schedule, history, mark-taken |
| 6 | 45 min | Scheduler (`tick()`), grouped message, email + browser notification |
| 7 | 30 min | Test with 2–3 accounts at once, use Settings → "Try it now" for demos |
| 8 | 45 min | Deploy to Render, prepare demo + slides |

## Run locally
```bash
python -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                 # add ANTHROPIC_API_KEY and SECRET_KEY
python app.py                                        # http://127.0.0.1:5000
```

## Deploy on Render (free)
1. Push the folder to GitHub.
2. Render → **New + → PostgreSQL** (free). Copy the *Internal Database URL*.
3. Render → **New + → Web Service** → pick your repo.
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app --workers 1 --threads 4 --bind 0.0.0.0:$PORT`
4. Environment variables: `SECRET_KEY`, `ANTHROPIC_API_KEY`, `DATABASE_URL` (the Postgres URL), `APP_TZ=Asia/Dhaka`, optional `SMTP_*`.
5. Deploy. Tables are created automatically on first start.
6. Free instances sleep when idle, so reminders only fire while awake. For the demo, open the site first, or ping it with UptimeRobot every 5 min.

**Keep `--workers 1`**: the scheduler runs inside the app; more workers = duplicate schedulers (the DB unique constraint still prevents duplicate reminders).

## Known limits / next steps (mention these to judges)
- Add CSRF protection (Flask-WTF) and rate-limit `/api/scan` before real use.
- Not a medical device: the patient must confirm extracted data.
- Better delivery: SMS (Twilio), Telegram bot, or web-push with a service worker.
- Drug-interaction warnings, refill reminders, caregiver accounts, Bangla UI.
