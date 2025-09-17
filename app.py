import os
import csv
import json
import string
import random
from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify, send_file, abort, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.sql import func
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm

# Konfiguracja
DATABASE_URL = os.getenv("DATABASE_URL")  # zmienna środowiskowa Render
TEACHER_KEY = os.getenv("TEACHER_KEY", "change_this_teacher_key")
BACKUP_TO_FILES = False  # Render web services mają ephemeral FS — NIE polegaj na plikach

# Tworzymy aplikację Flask
app = Flask(__name__, static_folder='static', static_url_path='')
app.secret_key = os.getenv("FLASK_SECRET_KEY", "zmien_ten_klucz")
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://") \
    if DATABASE_URL else 'sqlite:///local.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Inicjalizacja bazy danych
db = SQLAlchemy(app)

# MODELE
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)

class Session(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    class_name = db.Column(db.String(64))
    code = db.Column(db.String(16), unique=True, index=True)
    start_ts = db.Column(db.DateTime(timezone=True), server_default=func.now())
    end_ts = db.Column(db.DateTime(timezone=True), nullable=True)
    status = db.Column(db.String(16), default='OPEN')

class Candidate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('session.id', ondelete='CASCADE'))
    name = db.Column(db.String(128))

class Tablet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tablet_id = db.Column(db.String(128), index=True)
    session_id = db.Column(db.Integer, db.ForeignKey('session.id', ondelete='CASCADE'))
    joined_ts = db.Column(db.DateTime(timezone=True), server_default=func.now())

class Vote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('session.id', ondelete='CASCADE'))
    candidate_id = db.Column(db.Integer, db.ForeignKey('candidate.id'))
    tablet_id = db.Column(db.String(128))
    ts = db.Column(db.DateTime(timezone=True), server_default=func.now())

class Backup(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_code = db.Column(db.String(16), index=True)
    payload = db.Column(db.Text)  # JSON string of the raw vote
    ts = db.Column(db.DateTime(timezone=True), server_default=func.now())

class Audit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    action = db.Column(db.String(128))
    details = db.Column(db.Text)
    ts = db.Column(db.DateTime(timezone=True), server_default=func.now())

# HELPERS
def gen_code(length=4):
    chars = string.ascii_uppercase + string.digits
    return ''.join(random.choices(chars, k=length))

def now_ts():
    return datetime.now(ZoneInfo('Europe/Warsaw'))

def append_backup_file(session_code, row):
    if not BACKUP_TO_FILES:
        return
    os.makedirs('backups', exist_ok=True)
    fn = f'backups/votes_{session_code}.csv'
    first = not os.path.exists(fn)
    with open(fn, 'a', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        if first:
            writer.writerow(['vote_id','session_code','candidate_id','candidate_name','tablet_id','timestamp'])
        writer.writerow(row)

# TRASY
@app.route("/")
def index():
    return app.send_static_file('index.html')

@app.route("/login", methods=["GET", "POST"])
def login():
    # TODO: dodaj logikę logowania
    pass

@app.route("/dashboard")
def dashboard():
    if "user_id" in session:
        user = User.query.get(session["user_id"])
        return f"Witaj {user.username}!"
    return redirect(url_for("login"))

@app.route('/db-test')
def db_test():
    try:
        db.session.execute("SELECT 1")
        return "DB OK"
    except Exception as e:
        return f"DB ERROR: {e}"

# UTWORZENIE TABEL AUTOMATYCZNIE PRZY STARCIU APLIKACJI
with app.app_context():
    db.create_all()

# PDF GENERATION
def generate_pdf_bytes(session_obj):
    votes = Vote.query.filter_by(session_id=session_obj.id).order_by(Vote.ts).all()
    candidates = Candidate.query.filter_by(session_id=session_obj.id).all()
    counts = {c.id: {'name': c.name, 'count': 0} for c in candidates}
    for v in votes:
        if v.candidate_id in counts:
            counts[v.candidate_id]['count'] += 1
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    margin = 15*mm
    y = h - margin
    c.setFont('Helvetica-Bold', 14)
    c.drawString(margin, y, f"Podsumowanie sesji: {session_obj.code} — klasa: {session_obj.class_name}")
    y -= 10*mm
    c.setFont('Helvetica', 10)
    c.drawString(margin, y, f"Start: {session_obj.start_ts}")
    y -= 6*mm
    c.drawString(margin, y, f"Koniec: {session_obj.end_ts}")
    y -= 10*mm
    c.setFont('Helvetica-Bold', 12)
    c.drawString(margin, y, "Wyniki:")
    y -= 8*mm
    c.setFont('Helvetica', 11)
    for cid, info in counts.items():
        c.drawString(margin, y, f"{info['name']}: {info['count']} głosów")
        y -= 6*mm
    y -= 6*mm
    c.setFont('Helvetica-Bold', 12)
    c.drawString(margin, y, "Szczegóły głosów:")
    y -= 8*mm
    c.setFont('Helvetica', 9)
    for v in votes:
        ts = v.ts.isoformat()
        txt = f"id={v.id}, candidate_id={v.candidate_id}, tablet={v.tablet_id}, ts={ts}"
        c.drawString(margin, y, txt[:110])
        y -= 5*mm
        if y < margin:
            c.showPage()
            y = h - margin
    c.save()
    buf.seek(0)
    return buf.read()

# URUCHOMIENIE APLIKACJI
if __name__ == "__main__":
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
