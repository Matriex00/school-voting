import os
import csv
import json
import string
import random
from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify, send_file, abort
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.sql import func

# Konfiguracja
import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.sql import func

# Konfiguracja
DATABASE_URL = os.getenv("DATABASE_URL")  # Render environment variable
TEACHER_KEY = os.getenv("TEACHER_KEY", "change_this_teacher_key")
BACKUP_TO_FILES = False  # UWAGA: Render web services mają ephemeral FS — NIE polegaj na plikach

# Tworzymy aplikację Flask
app = Flask(__name__, static_folder='static', static_url_path='')

# Konfiguracja SQLAlchemy
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Inicjalizacja bazy danych
db = SQLAlchemy(app)

# MODELS
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
    payload = db.Column(db.Text)  # JSON string of the raw vote (acts as immediate backup)
    ts = db.Column(db.DateTime(timezone=True), server_default=func.now())

class Audit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    action = db.Column(db.String(128))
    details = db.Column(db.Text)
    ts = db.Column(db.DateTime(timezone=True), server_default=func.now())

# Helpers
def gen_code(length=4):
    chars = string.ascii_uppercase + string.digits
    return ''.join(random.choices(chars, k=length))

def now_ts():
    return datetime.now(ZoneInfo('Europe/Warsaw'))

def append_backup_file(session_code, row):
    # Uwaga: na Render pliki web service są ephemeral — zapis może zniknąć przy redeploy.
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

# Routes
@app.route('/')
def index():
    return app.send_static_file('index.html')

@app.route('/admin.html')
def admin_ui():
    return app.send_static_file('admin.html')

@app.route('/api/session/open', methods=['POST'])
def open_session():
    key = request.headers.get('X-TEACHER-KEY') or request.headers.get('x-teacher-key')
    if key != TEACHER_KEY:
        return jsonify({'error':'Forbidden'}), 403
    data = request.get_json() or {}
    class_name = data.get('class_name','Unknown')
    candidates = data.get('candidates',[])
    code = gen_code(4)
    # ensure uniqueness
    while Session.query.filter_by(code=code).first():
        code = gen_code(4)
    s = Session(class_name=class_name, code=code, status='OPEN', start_ts=now_ts())
    db.session.add(s); db.session.commit()
    for c in candidates:
        db.session.add(Candidate(session_id=s.id, name=c))
    db.session.add(Audit(action='OPEN_SESSION', details=f'session {code} opened'))
    db.session.commit()
    return jsonify({'session_code': code, 'session_id': s.id})

@app.route('/api/session/<session_code>/candidates', methods=['GET'])
def get_candidates(session_code):
    s = Session.query.filter_by(code=session_code).first()
    if not s:
        return jsonify({'error':'Session not found'}), 404
    cands = Candidate.query.filter_by(session_id=s.id).all()
    return jsonify([{'id':c.id,'name':c.name} for c in cands])

@app.route('/api/session/join', methods=['POST'])
def join_session():
    data = request.get_json() or {}
    code = data.get('session_code')
    tablet_id = data.get('tablet_id') or request.remote_addr
    s = Session.query.filter_by(code=code, status='OPEN').first()
    if not s:
        return jsonify({'error':'Session not open or not found'}), 404
    t = Tablet.query.filter_by(tablet_id=tablet_id, session_id=s.id).first()
    if not t:
        t = Tablet(tablet_id=tablet_id, session_id=s.id)
        db.session.add(t)
        db.session.commit()
    db.session.add(Audit(action='TABLET_JOIN', details=f'tablet {tablet_id} joined {code}'))
    db.session.commit()
    return jsonify({'ok':True, 'session_code': code})

@app.route('/api/vote', methods=['POST'])
def vote():
    data = request.get_json() or {}
    code = data.get('session_code')
    candidate_id = data.get('candidate_id')
    tablet_id = data.get('tablet_id') or request.remote_addr
    s = Session.query.filter_by(code=code, status='OPEN').first()
    if not s:
        return jsonify({'error':'Session not open or not found'}), 404
    candidate = Candidate.query.filter_by(id=candidate_id, session_id=s.id).first()
    if not candidate:
        return jsonify({'error':'Candidate not found'}), 404
    v = Vote(session_id=s.id, candidate_id=candidate.id, tablet_id=tablet_id, ts=now_ts())
    db.session.add(v)
    db.session.commit()
    # immediate backup into DB (safe, persistent)
    payload = {
        'vote_id': v.id,
        'session_code': s.code,
        'candidate_id': candidate.id,
        'candidate_name': candidate.name,
        'tablet_id': tablet_id,
        'ts': v.ts.isoformat()
    }
    b = Backup(session_code=s.code, payload=json.dumps(payload, ensure_ascii=False), ts=now_ts())
    db.session.add(b)
    db.session.commit()
    # optional file backup (not persistent on Render)
    append_backup_file(s.code, [v.id, s.code, candidate.id, candidate.name, tablet_id, v.ts.isoformat()])
    db.session.add(Audit(action='VOTE', details=f'vote {v.id} for {candidate.name} from {tablet_id}'))
    db.session.commit()
    return jsonify({'ok':True, 'vote_id': v.id, 'timestamp': v.ts.isoformat()})

@app.route('/api/session/close', methods=['POST'])
def close_session():
    key = request.headers.get('X-TEACHER-KEY') or request.headers.get('x-teacher-key')
    if key != TEACHER_KEY:
        return jsonify({'error':'Forbidden'}), 403
    data = request.get_json() or {}
    code = data.get('session_code')
    s = Session.query.filter_by(code=code, status='OPEN').first()
    if not s:
        return jsonify({'error':'Session not open or not found'}), 404
    s.status = 'CLOSED'
    s.end_ts = now_ts()
    db.session.add(s); db.session.commit()
    # generate PDF in-memory and return file (teacher should download it)
    pdf_bytes = generate_pdf_bytes(s)
    db.session.add(Audit(action='CLOSE_SESSION', details=f'session {code} closed'))
    db.session.commit()
    return send_file(BytesIO(pdf_bytes), mimetype='application/pdf', as_attachment=True,
                     download_name=f'session_{code}.pdf')

@app.route('/api/session/<session_code>/results', methods=['GET'])
def session_results(session_code):
    key = request.headers.get('X-TEACHER-KEY') or request.headers.get('x-teacher-key')
    if key != TEACHER_KEY:
        return jsonify({'error':'Forbidden'}), 403
    s = Session.query.filter_by(code=session_code).first()
    if not s:
        return jsonify({'error':'Session not found'}), 404
    votes = Vote.query.filter_by(session_id=s.id).all()
    cands = Candidate.query.filter_by(session_id=s.id).all()
    counts = {c.id: {'name': c.name, 'count': 0} for c in cands}
    for v in votes:
        if v.candidate_id in counts:
            counts[v.candidate_id]['count'] += 1
    return jsonify({'session_code': s.code, 'class': s.class_name, 'status': s.status, 'counts': counts})

@app.route('/api/health')
def health():
    return jsonify({'ok': True})

# PDF generation using reportlab
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm

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

# App init
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
