import os, re, json, base64, smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         login_required, current_user)
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy.exc import IntegrityError
from apscheduler.schedulers.background import BackgroundScheduler

load_dotenv()
TZ = ZoneInfo(os.getenv("APP_TZ", "Asia/Dhaka"))
SLOTS = [("morning", "Morning", "🌅"), ("noon", "Noon", "☀️"), ("night", "Night", "🌙")]
SLOT_KEYS = [s[0] for s in SLOTS]
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-change-me")
db_url = os.getenv("DATABASE_URL") or "sqlite:///medtrack.db"
app.config["SQLALCHEMY_DATABASE_URI"] = db_url.replace("postgres://", "postgresql://", 1)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB uploads

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"


# ---------------------------------------------------------------- models
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    morning_time = db.Column(db.String(5), default="08:00")
    noon_time = db.Column(db.String(5), default="13:00")
    night_time = db.Column(db.String(5), default="21:00")
    email_notify = db.Column(db.Boolean, default=True)
    prescriptions = db.relationship("Prescription", backref="user", cascade="all, delete-orphan")


class Prescription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    doctor = db.Column(db.String(150))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(TZ))
    medicines = db.relationship("Medicine", backref="prescription", cascade="all, delete-orphan")


class Medicine(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    prescription_id = db.Column(db.Integer, db.ForeignKey("prescription.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    dosage = db.Column(db.String(80))
    morning = db.Column(db.Boolean, default=False)
    noon = db.Column(db.Boolean, default=False)
    night = db.Column(db.Boolean, default=False)
    meal_timing = db.Column(db.String(10), default="any")  # before | after | any
    duration_days = db.Column(db.Integer, default=7)
    start_date = db.Column(db.Date)

    def is_active(self, today):
        return self.start_date <= today < self.start_date + timedelta(days=self.duration_days or 7)


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    slot = db.Column(db.String(10), nullable=False)
    day = db.Column(db.Date, nullable=False)
    message = db.Column(db.Text, nullable=False)
    read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(TZ))
    __table_args__ = (db.UniqueConstraint("user_id", "slot", "day", name="one_per_slot_per_day"),)


class IntakeLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    slot = db.Column(db.String(10), nullable=False)
    day = db.Column(db.Date, nullable=False)
    __table_args__ = (db.UniqueConstraint("user_id", "slot", "day", name="one_intake_per_slot"),)


@login_manager.user_loader
def load_user(uid):
    return db.session.get(User, int(uid))


# ---------------------------------------------------------------- helpers
def today_local():
    return datetime.now(TZ).date()


def meds_for_slot(user_id, slot, day):
    meds = Medicine.query.filter_by(user_id=user_id).all()
    return [m for m in meds if getattr(m, slot) and m.is_active(day)]


def group_by_meal(meds):
    g = {"before": [], "after": [], "any": []}
    for m in meds:
        g[m.meal_timing if m.meal_timing in g else "any"].append(m)
    return g


def build_message(user, slot, meds):
    label = dict((k, l) for k, l, _ in SLOTS)[slot]
    lines = [f"Hi {user.name}, it's your {label.lower()} medicine time:"]
    titles = {"before": "Take BEFORE meal:", "after": "Take AFTER meal:", "any": "Take:"}
    for key, items in group_by_meal(meds).items():
        if items:
            lines.append("")
            lines.append(titles[key])
            lines += [f"  - {m.name} {m.dosage or ''}".rstrip() for m in items]
    return "\n".join(lines)


def send_email(to, subject, body):
    host = os.getenv("SMTP_HOST")
    if not host:
        return False
    try:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = os.getenv("SMTP_USER"), to, subject
        msg.set_content(body)
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=15) as s:
            s.starttls()
            s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"))
            s.send_message(msg)
        return True
    except Exception as e:
        app.logger.error("Email failed: %s", e)
        return False


def fire(user, slot, day):
    """Create ONE notification for this user+slot+day containing all medicines."""
    meds = meds_for_slot(user.id, slot, day)
    if not meds:
        return False
    msg = build_message(user, slot, meds)
    db.session.add(Notification(user_id=user.id, slot=slot, day=day, message=msg))
    try:
        db.session.commit()
    except IntegrityError:  # already sent today
        db.session.rollback()
        return False
    if user.email_notify:
        send_email(user.email, f"Medicine reminder – {slot}", msg)
    return True


def tick():
    """Runs every minute; fires reminders whose time passed within the last 5 minutes."""
    with app.app_context():
        now = datetime.now(TZ)
        for u in User.query.all():
            for slot in SLOT_KEYS:
                hh, mm = map(int, getattr(u, f"{slot}_time").split(":"))
                due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if 0 <= (now - due).total_seconds() < 300:
                    fire(u, slot, now.date())


PROMPT = """You are reading a medical prescription image (may be handwritten, English or Bangla).
Extract every prescribed medicine. Return ONLY valid JSON, no prose:
{"doctor": string|null,
 "medicines": [{"name": string, "dosage": string, "morning": bool, "noon": bool, "night": bool,
                "meal_timing": "before"|"after"|"any", "duration_days": int|null}]}
Rules: "1+0+1" means morning+night; "1+1+1" or TDS means morning+noon+night; BD/BID means
morning+night; OD/QD means morning only; HS means night. "AC" = before meal, "PC" = after meal.
If a field is unclear, make your best guess; the patient will review it. If nothing is
readable return {"doctor": null, "medicines": []}."""


def extract_prescription(image_bytes, media_type):
    import anthropic
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"),
        max_tokens=1500,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                         "data": base64.standard_b64encode(image_bytes).decode()}},
            {"type": "text", "text": PROMPT}]}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    return json.loads(re.search(r"\{.*\}", text, re.S).group(0))


def to_int(v, default=7):
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- auth
@app.route("/")
def index():
    return redirect(url_for("dashboard" if current_user.is_authenticated else "login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        pw = request.form["password"]
        if not name or "@" not in email:
            flash("Please enter a valid name and email.")
        elif len(pw) < 6:
            flash("Password must be at least 6 characters.")
        elif User.query.filter_by(email=email).first():
            flash("That email is already registered.")
        else:
            u = User(name=name, email=email, password_hash=generate_password_hash(pw))
            db.session.add(u)
            db.session.commit()
            login_user(u)
            return redirect(url_for("dashboard"))
    return render_template("auth.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = User.query.filter_by(email=request.form["email"].strip().lower()).first()
        if u and check_password_hash(u.password_hash, request.form["password"]):
            login_user(u)
            return redirect(url_for("dashboard"))
        flash("Wrong email or password.")
    return render_template("auth.html", mode="login")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- pages
@app.route("/dashboard")
@login_required
def dashboard():
    today = today_local()
    schedule, has = {}, {}
    for slot in SLOT_KEYS:
        meds = meds_for_slot(current_user.id, slot, today)
        schedule[slot], has[slot] = group_by_meal(meds), bool(meds)
    taken = {l.slot for l in IntakeLog.query.filter_by(user_id=current_user.id, day=today)}
    notes = (Notification.query.filter_by(user_id=current_user.id)
             .order_by(Notification.created_at.desc()).limit(3).all())
    rxs = (Prescription.query.filter_by(user_id=current_user.id)
           .order_by(Prescription.created_at.desc()).all())
    times = {s: getattr(current_user, f"{s}_time") for s in SLOT_KEYS}
    return render_template("dashboard.html", today=today, slots=SLOTS, schedule=schedule,
                           has=has, taken=taken, notes=notes, rxs=rxs, times=times)


@app.route("/scan")
@login_required
def scan():
    return render_template("scan.html")


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        for s in SLOT_KEYS:
            v = request.form.get(f"{s}_time", "")
            if TIME_RE.match(v):
                setattr(current_user, f"{s}_time", v)
        current_user.email_notify = "email_notify" in request.form
        db.session.commit()
        flash("Settings saved.")
    return render_template("settings.html")


# ---------------------------------------------------------------- API
@app.post("/api/scan")
@login_required
def api_scan():
    f = request.files.get("image")
    if not f or f.mimetype not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        return jsonify(error="Upload a JPG, PNG or WEBP image."), 400
    try:
        return jsonify(extract_prescription(f.read(), f.mimetype))
    except Exception as e:
        app.logger.error("Scan failed: %s", e)
        return jsonify(error="Could not read the prescription. Try a clearer photo."), 502


@app.post("/api/prescriptions")
@login_required
def api_save_prescription():
    data = request.get_json(force=True)
    rx = Prescription(user_id=current_user.id, doctor=(data.get("doctor") or "")[:150])
    db.session.add(rx)
    db.session.flush()
    count = 0
    for m in data.get("medicines", []):
        name = (m.get("name") or "").strip()
        if not name:
            continue
        meal = m.get("meal_timing") if m.get("meal_timing") in ("before", "after") else "any"
        db.session.add(Medicine(
            prescription_id=rx.id, user_id=current_user.id, name=name[:150],
            dosage=(m.get("dosage") or "")[:80], morning=bool(m.get("morning")),
            noon=bool(m.get("noon")), night=bool(m.get("night")), meal_timing=meal,
            duration_days=to_int(m.get("duration_days")), start_date=today_local()))
        count += 1
    if not count:
        db.session.rollback()
        return jsonify(error="No medicines to save."), 400
    db.session.commit()
    return jsonify(ok=True, saved=count)


@app.post("/api/prescriptions/<int:rid>/delete")
@login_required
def api_delete_prescription(rid):
    rx = Prescription.query.filter_by(id=rid, user_id=current_user.id).first_or_404()
    db.session.delete(rx)
    db.session.commit()
    return jsonify(ok=True)


@app.post("/api/taken/<slot>")
@login_required
def api_taken(slot):
    if slot not in SLOT_KEYS:
        return jsonify(error="bad slot"), 400
    try:
        db.session.add(IntakeLog(user_id=current_user.id, slot=slot, day=today_local()))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
    return jsonify(ok=True)


@app.get("/api/notifications/pending")
@login_required
def api_pending():
    rows = Notification.query.filter_by(user_id=current_user.id, read=False).all()
    return jsonify([{"id": n.id, "slot": n.slot, "message": n.message} for n in rows])


@app.post("/api/notifications/<int:nid>/read")
@login_required
def api_read(nid):
    n = Notification.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
    n.read = True
    db.session.commit()
    return jsonify(ok=True)


@app.post("/api/notify-now/<slot>")
@login_required
def api_notify_now(slot):
    """Demo helper: fire a reminder immediately (replaces today's one for that slot)."""
    if slot not in SLOT_KEYS:
        return jsonify(error="bad slot"), 400
    day = today_local()
    Notification.query.filter_by(user_id=current_user.id, slot=slot, day=day).delete()
    db.session.commit()
    ok = fire(current_user, slot, day)
    return jsonify(ok=ok, message=None if ok else "No active medicines for that slot.")


# ---------------------------------------------------------------- boot
with app.app_context():
    db.create_all()

if os.getenv("RUN_SCHEDULER", "1") == "1":
    scheduler = BackgroundScheduler(timezone=TZ)
    scheduler.add_job(tick, "interval", seconds=60, id="tick", max_instances=1)
    scheduler.start()

if __name__ == "__main__":
    app.run(debug=True, use_reloader=False, port=5000)
