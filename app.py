import os
import csv
import json
import string
import random
from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify, send_file, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.sql import func
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm

# KONFIGURACJA
DATABASE_URL = os.getenv("DATABASE_URL")
TEACHER_KEY = os.getenv("TEACHER_KEY", "change_this_teacher_key")
BACKUP_TO_FILES = False  # Render ephemeral FS

# TWORZENIE APLIKACJI
app = Flask(__name__, static_folder='static', static_url_path='')
app.secret_key = os.getenv("FLASK_SECRET_KEY", "zmien_ten_klucz")
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://") \
    if DATABASE_URL else 'sqlite:///local.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# INICJALIZACJA BAZY
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
    payload = db.Column(db.Text)
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

# PDF GENERATOR
def generate_pdf_bytes(session_obj):
    votes = Vote.query.filter_by(session_id=session_obj.id).order_by(Vote.ts).all()
    candidates = Candidate.query.filter_by(session_id=session_obj.id).all()
    counts = {c.id: {'name': c.name, 'count': 0} for c in candidates}

    for v in votes:
        if v.candidate_id in counts:
            counts[v.candidate_id]['count'] += 1

    total_votes = sum(info['count'] for info in counts.values())

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
        percent = (info['count']/total_votes*100) if total_votes > 0 else 0
        c.drawString(margin, y, f"{info['name']}: {info['count']} głosów ({percent:.1f}%)")
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

# TRASY API
@app.route("/")
def index():
    return app.send_static_file('index.html')

@app.route('/api/health')
def health():
    return jsonify({'ok': True})

@app.route('/api/session/open', methods=['POST'])
def open_session():
    key = request.headers.get('X-TEACHER-KEY') or request.headers.get('x-teacher-key')
    if key != TEACHER_KEY:
        return jsonify({'error':'Forbidden'}), 403

    data = request.get_json() or {}
    class_name = data.get('class_name','Unknown')
    candidates = data.get('candidates',[])

    code = gen_code(4)
    while Session.query.filter_by(code=code).first():
        code = gen_code(4)

    s = Session(class_name=class_name, code=code, status='OPEN', start_ts=now_ts())
    db.session.add(s)
    db.session.commit()

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
    db.session.add(s)
    db.session.commit()

    # Generowanie PDF i zapis do katalogu reports
    pdf_bytes = generate_pdf_bytes(s)
    os.makedirs('reports', exist_ok=True)
    with open(f'reports/session_{code}.pdf', 'wb') as f:
        f.write(pdf_bytes)

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
    total_votes = 0
    for v in votes:
        if v.candidate_id in counts:
            counts[v.candidate_id]['count'] += 1
            total_votes +=1
    # Dodanie procentów
    for info in counts.values():
        info['percent'] = (info['count']/total_votes*100) if total_votes>0 else 0
    return jsonify({'session_code': s.code, 'class': s.class_name, 'status': s.status, 'counts': counts})

@app.route('/api/sessions/summary', methods=['POST'])
def sessions_summary():
    key = request.headers.get('x-teacher-key')
    if key != TEACHER_KEY:
        return jsonify({'error':'Forbidden'}), 403

    data = request.get_json() or {}
    session_codes = data.get('session_codes', [])

    if not session_codes:
        return jsonify({'error': 'Podaj kody sesji'}), 400

    all_counts = {}
    total_votes = 0

    for code in session_codes:
        s = Session.query.filter_by(code=code, status='CLOSED').first()
        if not s:
            continue
        votes = Vote.query.filter_by(session_id=s.id).all()
        cands = Candidate.query.filter_by(session_id=s.id).all()
        for c in cands:
            if c.name not in all_counts:
                all_counts[c.name] = 0
        for v in votes:
            cand = Candidate.query.get(v.candidate_id)
            if cand:
                all_counts[cand.name] +=1
                total_votes +=1
@app.route('/api/session/<session_code>/report', methods=['GET'])
def session_report(session_code):
    key = request.headers.get('X-TEACHER-KEY')
    if key != TEACHER_KEY:
        return jsonify({'error': 'Forbidden'}), 403
    
    s = Session.query.filter_by(code=session_code).first()
    if not s:
        return jsonify({'error': 'Session not found'}), 404
    
    pdf_bytes = generate_pdf_bytes(s)
    return send_file(BytesIO(pdf_bytes), mimetype='application/pdf', as_attachment=True,
                     download_name=f'session_{session_code}.pdf')
@app.route('/api/sessions/summary-report', methods=['POST'])
def sessions_summary_report():
    key = request.headers.get('X-TEACHER-KEY')
    if key != TEACHER_KEY:
        return jsonify({'error':'Forbidden'}), 403

    data = request.get_json() or {}
    session_codes = data.get('session_codes', [])

    if not session_codes:
        return jsonify({'error': 'Podaj kody sesji'}), 400

    summary_counts = {}
    total_votes = 0

    for code in session_codes:
        s = Session.query.filter_by(code=code).first()
        if not s:
            continue
        votes = Vote.query.filter_by(session_id=s.id).all()
        candidates = Candidate.query.filter_by(session_id=s.id).all()
        for c in candidates:
            if c.name not in summary_counts:
                summary_counts[c.name] = 0
        for v in votes:
            cand = Candidate.query.get(v.candidate_id)
            if cand:
                summary_counts[cand.name] += 1
        total_votes += len(votes)

    # generowanie PDF
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    margin = 20*mm
    y = 800

    c.setFont('Helvetica-Bold', 14)
    c.drawString(margin, y, "Zbiorcze podsumowanie sesji")
    y -= 20

    for name, count in summary_counts.items():
        percent = (count/total_votes*100) if total_votes > 0 else 0
        c.setFont('Helvetica', 11)
        c.drawString(margin, y, f"{name}: {count} głosów ({percent:.1f}%)")
        y -= 15

    c.setFont('Helvetica', 10)
    y -= 20
    c.drawString(margin, y, f"Łączna liczba głosów: {total_votes}")

    c.save()
    buf.seek(0)
    return send_file(buf, mimetype='application/pdf', as_attachment=True,
                     download_name="summary_report.pdf")


    # Generowanie PDF zbiorczego
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    margin = 15*mm
    y = h - margin

    c.setFont('Helvetica-Bold', 14)
    c.drawString(margin, y, f"Raport zbiorczy sesji: {', '.join(session_codes)}")
    y -= 10*mm
    c.setFont('Helvetica', 11)
    for name, count in all_counts.items():
        percent = (count/total_votes*100) if total_votes>0 else 0
        c.drawString(margin, y, f"{name}: {count} głosów ({percent:.1f}%)")
        y -= 6*mm
        if y < margin:
            c.showPage()
            y = h - margin
    c.save()
    buf.seek(0)

    # Zapis do reports/
    os.makedirs('reports', exist_ok=True)
    with open(f"reports/summary_{'_'.join(session_codes)}.pdf", 'wb') as f:
        f.write(buf.getvalue())

    return send_file(buf, mimetype='application/pdf', as_attachment=True,
                     download_name=f"summary_{'_'.join(session_codes)}.pdf")

# TWORZENIE TABEL
with app.app_context():
    db.create_all()

# URUCHOMIENIE
if __name__ == "__main__":
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
