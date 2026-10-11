"""MediLife: privacy-minded personal health record prototype.

Uploaded files are private and are only served after ownership checks. AI is
strictly opt-in and unavailable without a server-side API key.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, g, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

load_dotenv()
ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv("DATABASE_PATH", ROOT / "data" / "medilife.sqlite3"))
UPLOADS = Path(os.getenv("UPLOAD_DIR", ROOT / "data" / "uploads"))
MAX_BYTES = int(os.getenv("MAX_UPLOAD_MB", "15")) * 1024 * 1024
ALLOWED = {"pdf", "png", "jpg", "jpeg"}
app = Flask(__name__, static_folder="static")
app.secret_key = os.getenv("FLASK_SECRET_KEY", "local-development-only-change-me")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                  SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1")
app.config["MAX_CONTENT_LENGTH"] = MAX_BYTES


def now():
    return datetime.now(timezone.utc).isoformat()


def db():
    if "db" not in g:
        DATA.parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(DATA)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response


def init_db():
    DATA.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DATA)
    con.executescript("""
    PRAGMA foreign_keys=ON;
    CREATE TABLE IF NOT EXISTS users (
      id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS profiles (
      user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
      display_name TEXT DEFAULT '', dob TEXT DEFAULT '', allergies TEXT DEFAULT '',
      history TEXT DEFAULT '', updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS documents (
      id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      filename TEXT NOT NULL, stored_name TEXT NOT NULL, category TEXT NOT NULL,
      uploaded_at TEXT NOT NULL, document_date TEXT, status TEXT NOT NULL,
      source_text TEXT DEFAULT '', error TEXT DEFAULT '', sha256 TEXT NOT NULL,
      UNIQUE(user_id, sha256)
    );
    CREATE INDEX IF NOT EXISTS documents_owner_date ON documents(user_id, uploaded_at DESC);
    CREATE TABLE IF NOT EXISTS observations (
      id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      name TEXT NOT NULL, value TEXT NOT NULL, unit TEXT DEFAULT '', reference_range TEXT DEFAULT '',
      flagged TEXT DEFAULT '', result_date TEXT DEFAULT '', source_excerpt TEXT DEFAULT '',
      verified INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS observations_owner_name ON observations(user_id, name);
    CREATE TABLE IF NOT EXISTS medications (
      id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      name TEXT NOT NULL, details TEXT DEFAULT '', prescription_date TEXT DEFAULT '',
      source_excerpt TEXT DEFAULT '', verified INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS conditions (
      id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      name TEXT NOT NULL, noted_date TEXT DEFAULT '', notes TEXT DEFAULT '', verified INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS conversations (
      id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS messages (
      id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
      role TEXT NOT NULL, content TEXT NOT NULL, sources TEXT DEFAULT '[]', created_at TEXT NOT NULL
    );
    """)
    con.commit(); con.close()


@app.before_request
def ensure_db():
    init_db()


def user_id():
    return session.get("user_id")


def auth_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not user_id():
            return jsonify(error="Please sign in to continue."), 401
        return fn(*args, **kwargs)
    return wrapped


def rowdict(row):
    return dict(row) if row else None


@app.get("/health")
def health():
    try:
        db().execute("SELECT 1")
        return jsonify(status="ok", database="connected", ai_configured=bool(os.getenv("OPENAI_API_KEY")))
    except sqlite3.Error:
        return jsonify(status="degraded", database="unavailable"), 503


@app.post("/api/auth/register")
def register():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(password) < 10:
        return jsonify(error="Enter a valid email and a password of at least 10 characters."), 400
    uid = str(uuid.uuid4())
    try:
        db().execute("INSERT INTO users(id,email,password_hash,created_at) VALUES(?,?,?,?)",
                     (uid, email, generate_password_hash(password), now()))
        db().execute("INSERT INTO profiles(user_id,updated_at) VALUES(?,?)", (uid, now()))
        db().commit()
    except sqlite3.IntegrityError:
        return jsonify(error="An account with that email already exists."), 409
    session.clear(); session["user_id"] = uid
    return jsonify(user={"email": email}), 201


@app.post("/api/auth/login")
def login():
    data = request.get_json(silent=True) or {}
    row = db().execute("SELECT * FROM users WHERE email=?", (str(data.get("email", "")).strip().lower(),)).fetchone()
    if not row or not check_password_hash(row["password_hash"], str(data.get("password", ""))):
        return jsonify(error="Email or password is incorrect."), 401
    session.clear(); session["user_id"] = row["id"]
    return jsonify(user={"email": row["email"]})


@app.post("/api/auth/logout")
def logout():
    session.clear(); return jsonify(ok=True)


@app.get("/api/auth/me")
def me():
    if not user_id(): return jsonify(user=None)
    row = db().execute("SELECT email FROM users WHERE id=?", (user_id(),)).fetchone()
    return jsonify(user={"email": row["email"]} if row else None)


def extract_document(path: Path, ext: str):
    """Extract text only when a real local parser/OCR engine succeeds."""
    if ext == "pdf":
        try:
            import pymupdf as fitz
            with fitz.open(path) as pdf:
                pages = []
                for page in pdf:
                    page_text = page.get_text("text").strip()
                    if not page_text:
                        try:
                            from PIL import Image
                            import pytesseract
                            import io
                            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                            page_text = pytesseract.image_to_string(Image.open(io.BytesIO(pix.tobytes("png")))).strip()
                        except ImportError:
                            return "", "This scanned PDF needs Tesseract OCR and Pillow, which are not installed."
                        except Exception:
                            return "", "A scanned page could not be read. Check that Tesseract OCR is installed and configured."
                    pages.append(page_text)
                return "\n\n".join(pages).strip()
        except ImportError:
            return "", "PDF text extraction is unavailable. Install PyMuPDF, then retry."
        except Exception:
            return "", "This PDF could not be read. Check that it is a valid, unencrypted PDF."
    try:
        from PIL import Image
        import pytesseract
        with Image.open(path) as image:
            return pytesseract.image_to_string(image).strip()
    except ImportError:
        return "", "Image OCR is unavailable. Install Pillow and pytesseract, and configure Tesseract OCR."
    except Exception:
        return "", "No readable text was extracted. Check OCR installation or upload a clearer image."


def parse_structured(text):
    """Conservative, review-required parsing of common printed report lines."""
    labs, meds, conditions = [], [], []
    patterns = [
      re.compile(r"(?im)^\s*([A-Za-z][A-Za-z0-9 /()-]{1,45})\s*[:\-]\s*([<>]?[\d.]+)\s*(mg/dL|g/dL|mmol/L|IU/L|U/L|%|mIU/L|ng/mL|10\^?\d*/?\w*)?\s*(?:\((?:ref(?:erence)?\.?\s*)?([^)]{1,30})\))?\s*(?:\b(HIGH|LOW|ABNORMAL|H|L)\b)?\s*$"),
    ]
    for line in text.splitlines():
        for pat in patterns:
            match = pat.match(line)
            if match:
                name, value, unit, ref, flag = match.groups()
                if any(k in name.lower() for k in ("glucose", "cholesterol", "hemoglobin", "creatinine", "platelet", "wbc", "rbc", "vitamin", "tsh", "sodium", "potassium", "blood pressure", "hba1c")):
                    labs.append({"name": name.strip(), "value": value, "unit": unit or "", "reference_range": ref or "", "flagged": (flag or "").upper(), "source_excerpt": line.strip(), "verified": False})
                break
    for line in text.splitlines():
        if re.search(r"(?i)\b(mg|mcg|µg|tablet|capsule|once daily|twice daily)\b", line) and re.search(r"(?i)\b(tab|tablet|cap|capsule|take|prescription|rx)\b", line):
            meds.append({"name": line.strip()[:100], "details": "", "prescription_date": "", "source_excerpt": line.strip(), "verified": False})
    return labs[:100], meds[:30], conditions


@app.post("/api/documents")
@auth_required
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="Choose a PDF or image to upload."), 400
    original = secure_filename(f.filename) or "medical-document"
    ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
    if ext not in ALLOWED:
        return jsonify(error="Supported file types are PDF, JPG, JPEG, and PNG."), 415
    content = f.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        return jsonify(error=f"File exceeds the {MAX_BYTES // (1024*1024)} MB limit."), 413
    if not content:
        return jsonify(error="The selected file is empty."), 400
    signatures = {"pdf": content.startswith(b"%PDF-"), "png": content.startswith(b"\x89PNG\r\n\x1a\n"),
                  "jpg": content.startswith(b"\xff\xd8\xff"), "jpeg": content.startswith(b"\xff\xd8\xff")}
    if not signatures.get(ext, False):
        return jsonify(error="The file contents do not match its extension, or the file is damaged."), 415
    digest = hashlib.sha256(content).hexdigest()
    existing = db().execute("SELECT id FROM documents WHERE user_id=? AND sha256=?", (user_id(), digest)).fetchone()
    if existing:
        return jsonify(error="This document was already uploaded.", document_id=existing["id"]), 409
    UPLOADS.mkdir(parents=True, exist_ok=True)
    stored = f"{uuid.uuid4().hex}.{ext}"
    path = UPLOADS / stored
    path.write_bytes(content)
    result = extract_document(path, ext)
    err = ""
    if isinstance(result, tuple): text, err = result
    else: text = result
    doc_id = str(uuid.uuid4())
    status = "needs_review" if text else "failed"
    category = request.form.get("category", "other")[:40]
    try:
        doc_date = request.form.get("document_date", "")[:10] or None
        if doc_date:
            try:
                datetime.fromisoformat(doc_date)
            except ValueError:
                path.unlink(missing_ok=True)
                return jsonify(error="Document date must be a valid calendar date."), 400
        db().execute("INSERT INTO documents(id,user_id,filename,stored_name,category,uploaded_at,document_date,status,source_text,error,sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (doc_id,user_id(),original,stored,category,now(),doc_date,status,text,err,digest))
        if text:
            labs, meds, conditions = parse_structured(text)
            for x in labs:
                db().execute("INSERT INTO observations(id,user_id,document_id,name,value,unit,reference_range,flagged,source_excerpt,verified,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()),user_id(),doc_id,x["name"],x["value"],x["unit"],x["reference_range"],x["flagged"],x["source_excerpt"],0,now()))
            for x in meds:
                db().execute("INSERT INTO medications(id,user_id,document_id,name,details,prescription_date,source_excerpt,verified) VALUES(?,?,?,?,?,?,?,0)",
                    (str(uuid.uuid4()),user_id(),doc_id,x["name"],x["details"],x["prescription_date"],x["source_excerpt"]))
        db().commit()
    except Exception:
        path.unlink(missing_ok=True); db().rollback(); raise
    return jsonify(document=document_json(doc_id), extracted={"observations":len(labs) if text else 0,"medications":len(meds) if text else 0}, message=(err or "Text extracted. Review the detected fields before relying on them.")), 201


def document_json(doc_id):
    row = db().execute("SELECT id,filename,category,uploaded_at,document_date,status,error,source_text FROM documents WHERE id=? AND user_id=?", (doc_id,user_id())).fetchone()
    if not row: return None
    result = dict(row); result["source_text"] = result["source_text"][:12000]
    return result


@app.get("/api/documents")
@auth_required
def documents():
    rows = db().execute("SELECT id,filename,category,uploaded_at,document_date,status,error FROM documents WHERE user_id=? ORDER BY uploaded_at DESC", (user_id(),)).fetchall()
    return jsonify(documents=[dict(r) for r in rows])


@app.get("/api/documents/<doc_id>")
@auth_required
def document_detail(doc_id):
    doc = document_json(doc_id)
    if not doc: return jsonify(error="Document not found."), 404
    doc["observations"] = [dict(r) for r in db().execute("SELECT * FROM observations WHERE document_id=? AND user_id=?",(doc_id,user_id()))]
    doc["medications"] = [dict(r) for r in db().execute("SELECT * FROM medications WHERE document_id=? AND user_id=?",(doc_id,user_id()))]
    return jsonify(document=doc)


@app.get("/api/documents/<doc_id>/file")
@auth_required
def document_file(doc_id):
    row = db().execute("SELECT stored_name,filename FROM documents WHERE id=? AND user_id=?", (doc_id,user_id())).fetchone()
    if not row: return jsonify(error="Document not found."), 404
    return send_from_directory(UPLOADS, row["stored_name"], as_attachment=False, download_name=row["filename"])


@app.post("/api/documents/<doc_id>/retry")
@auth_required
def retry_document(doc_id):
    row = db().execute("SELECT stored_name,filename FROM documents WHERE id=? AND user_id=?", (doc_id,user_id())).fetchone()
    if not row: return jsonify(error="Document not found."), 404
    path = UPLOADS / row["stored_name"]
    if not path.is_file(): return jsonify(error="The private source file is missing. Upload it again."), 410
    ext = row["filename"].rsplit(".",1)[-1].lower()
    result = extract_document(path,ext)
    if isinstance(result,tuple): text,err=result
    else: text,err=result,""
    if not text:
        db().execute("UPDATE documents SET status='failed',error=? WHERE id=? AND user_id=?",(err or "No readable text was extracted.",doc_id,user_id()));db().commit()
        return jsonify(error=err or "No readable text was extracted.",status="failed"),422
    db().execute("UPDATE documents SET source_text=?,error='',status='needs_review' WHERE id=? AND user_id=?",(text,doc_id,user_id()))
    labs,meds,_=parse_structured(text)
    for x in labs:
        db().execute("INSERT INTO observations(id,user_id,document_id,name,value,unit,reference_range,flagged,source_excerpt,verified,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (str(uuid.uuid4()),user_id(),doc_id,x["name"],x["value"],x["unit"],x["reference_range"],x["flagged"],x["source_excerpt"],0,now()))
    for x in meds:
        db().execute("INSERT INTO medications(id,user_id,document_id,name,details,prescription_date,source_excerpt,verified) VALUES(?,?,?,?,?,?,?,0)",
                     (str(uuid.uuid4()),user_id(),doc_id,x["name"],x["details"],x["prescription_date"],x["source_excerpt"]))
    db().commit()
    return jsonify(status="needs_review",message="Text extracted. Review detected fields before relying on them.")


@app.post("/api/documents/<doc_id>/review")
@auth_required
def mark_document_reviewed(doc_id):
    data=request.get_json(silent=True) or {}
    if not data.get("reviewed"):
        return jsonify(error="Confirm that you reviewed this document."),400
    cur=db().execute("UPDATE documents SET status='completed',error='' WHERE id=? AND user_id=? AND source_text<>''",(doc_id,user_id()))
    db().commit()
    if not cur.rowcount:return jsonify(error="Readable document not found."),404
    return jsonify(status="completed")


@app.patch("/api/observations/<obs_id>")
@auth_required
def update_observation(obs_id):
    data = request.get_json(silent=True) or {}
    allowed = {"name", "value", "unit", "reference_range", "flagged"}
    fields = {k:str(data[k])[:200] for k in allowed if k in data}
    if not fields: return jsonify(error="No editable fields provided."), 400
    fields["verified"] = 1 if data.get("verified") else 0
    db().execute("UPDATE observations SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=? AND user_id=?", (*fields.values(),obs_id,user_id())); db().commit()
    return jsonify(ok=True)


@app.patch("/api/medications/<med_id>")
@auth_required
def update_medication(med_id):
    data=request.get_json(silent=True) or {}; name=str(data.get("name",""))[:200]
    if not name: return jsonify(error="Medicine name is required."),400
    cur=db().execute("UPDATE medications SET name=?,details=?,verified=? WHERE id=? AND user_id=?", (name,str(data.get("details",""))[:300],1 if data.get("verified") else 0,med_id,user_id()));db().commit()
    if not cur.rowcount:return jsonify(error="Medicine not found."),404
    return jsonify(ok=True)


@app.get("/api/observations")
@auth_required
def observations():
    rows=db().execute("SELECT o.*,d.filename,d.document_date,d.uploaded_at FROM observations o JOIN documents d ON d.id=o.document_id WHERE o.user_id=? ORDER BY COALESCE(d.document_date,d.uploaded_at) DESC",(user_id(),)).fetchall()
    return jsonify(observations=[dict(r) for r in rows])


@app.get("/api/medications")
@auth_required
def medication_list():
    rows=db().execute("SELECT m.*,d.filename,d.document_date,d.uploaded_at FROM medications m JOIN documents d ON d.id=m.document_id WHERE m.user_id=? ORDER BY COALESCE(m.prescription_date,d.document_date,d.uploaded_at) DESC",(user_id(),)).fetchall()
    return jsonify(medications=[dict(r) for r in rows])


@app.route("/api/profile",methods=["GET","PUT"])
@auth_required
def profile():
    if request.method=="PUT":
        data=request.get_json(silent=True) or {}
        db().execute("UPDATE profiles SET display_name=?,dob=?,allergies=?,history=?,updated_at=? WHERE user_id=?",(*(str(data.get(k,""))[:500] for k in ("display_name","dob","allergies","history")),now(),user_id()));db().commit()
    row=rowdict(db().execute("SELECT display_name,dob,allergies,history,updated_at FROM profiles WHERE user_id=?",(user_id(),)).fetchone())
    meds=db().execute("SELECT name,verified FROM medications WHERE user_id=?",(user_id(),)).fetchall()
    return jsonify(profile=row,medications=[dict(x) for x in meds])


@app.get("/api/timeline")
@auth_required
def timeline():
    docs=db().execute("SELECT id,filename,category,uploaded_at,document_date,status FROM documents WHERE user_id=?",(user_id(),)).fetchall()
    events=[]
    for d in docs:
        events.append({"type":"document","date":d["document_date"] or d["uploaded_at"],"document_date":d["document_date"],"uploaded_at":d["uploaded_at"],"title":d["filename"],"detail":d["category"],"document_id":d["id"],"status":d["status"]})
    labs=db().execute("SELECT o.name,o.value,o.unit,o.flagged,o.document_id,o.created_at,d.filename,d.document_date,d.uploaded_at FROM observations o JOIN documents d ON d.id=o.document_id WHERE o.user_id=?",(user_id(),)).fetchall()
    for x in labs: events.append({"type":"lab","date":x["document_date"] or x["uploaded_at"],"title":x["name"],"detail":f"{x['value']} {x['unit']}".strip(),"flagged":x["flagged"],"document_id":x["document_id"],"source":x["filename"]})
    events.sort(key=lambda x:x["date"],reverse=True)
    return jsonify(events=events)


@app.post("/api/summary")
@auth_required
def summary():
    data=request.get_json(silent=True) or {}; doc_id=data.get("document_id"); language=data.get("language","en")
    doc=db().execute("SELECT id,filename,source_text FROM documents WHERE id=? AND user_id=?",(doc_id,user_id())).fetchone()
    if not doc:return jsonify(error="Choose one of your uploaded documents first."),404
    if not doc["source_text"]:return jsonify(error="This document has no extracted text to summarize."),422
    return ai_call("summary",doc["filename"],doc["source_text"],language)


def ai_call(kind,title,source,language="en",question=""):
    key=os.getenv("OPENAI_API_KEY")
    if not key:return jsonify(error="AI summaries are not configured. Add OPENAI_API_KEY on the server; extracted records remain available."),503
    try:
        from openai import OpenAI
        client=OpenAI(api_key=key,timeout=30)
        if kind=="summary":
            instruction="Explain this medical record in simple English." if language!="te" else "Explain this medical record in accessible Telugu (తెలుగు). Keep clinical names, numbers and units faithful to the source."
            prompt=f"{instruction} Use only source-supported facts. Separate extracted facts from explanation. Do not diagnose, prescribe, or infer missing reference ranges. Mention uncertainties and questions for a clinician. Cite the source filename. State the informational-only limitation.\nSOURCE FILE: {title}\nSOURCE TEXT:\n{source[:14000]}"
        else:
            instruction="Answer in accessible Telugu (తెలుగు)." if language=="te" else "Answer in simple English."
            prompt=f"You are a cautious health-record explainer. {instruction} Only use the retrieved records below. Never diagnose or recommend medication changes. Say when there is no relevant record, describe uncertainty, and cite source filenames. Add that this is informational, not medical advice.\nRETRIEVED RECORDS:\n{source[:14000]}\nQUESTION: {question[:1500]}"
        response=client.chat.completions.create(model=os.getenv("OPENAI_MODEL","gpt-4o-mini"),messages=[{"role":"system","content":"Be evidence-grounded and clinically cautious."},{"role":"user","content":prompt}])
        text=response.choices[0].message.content
        if not text: raise RuntimeError("Empty AI response")
        return jsonify(summary=text,source=title,language=language,generated_at=now())
    except Exception:
        app.logger.exception("AI provider request failed")
        return jsonify(error="The AI service could not complete this request. Your saved records are unchanged; try again later."),502


@app.post("/api/chat")
@auth_required
def chat():
    data=request.get_json(silent=True) or {}; question=str(data.get("message",""))[:1500].strip(); language=data.get("language","en")
    if not question:return jsonify(error="Type a question first."),400
    docs=db().execute("SELECT id,filename,source_text FROM documents WHERE user_id=? AND source_text<>'' ORDER BY uploaded_at DESC",(user_id(),)).fetchall()
    if not docs:return jsonify(error="No readable medical records are available yet. Upload a document, then ask again."),404
    terms={x.lower() for x in re.findall(r"[A-Za-z0-9]{3,}",question)}-{"the","and","for","with","from","what","when","where","does","this","that","about","explain","report","records","record","please","could","would","should","latest","previous","my","me","tell","show"}
    ranked=[]
    for d in docs:
        corpus=(d["filename"]+" "+d["source_text"]).lower()
        score=sum(1 for term in terms if term in corpus)
        if score:ranked.append((score,d))
    if ranked:
        ranked.sort(key=lambda pair:pair[0],reverse=True)
        selected=[d for _,d in ranked[:3]]
    elif re.search(r"(?i)\b(latest|history|all|summari[sz]e)\b",question):
        selected=docs[:3]
    else:
        return jsonify(error="I couldn't find a relevant phrase in your readable records for that question. Try naming the test, medicine, or document."),404
    context="\n\n".join(f"[Source: {d['filename']}]\n{d['source_text'][:5000]}" for d in selected)
    result=ai_call("chat","your uploaded records",context,language,question)
    return result


@app.get("/api/dashboard")
@auth_required
def dashboard():
    con=db();uid=user_id()
    total=con.execute("SELECT COUNT(*) n FROM documents WHERE user_id=?",(uid,)).fetchone()["n"]
    reviewed=con.execute("SELECT COUNT(*) n FROM documents WHERE user_id=? AND status='completed'",(uid,)).fetchone()["n"]
    recent=con.execute("SELECT id,filename,category,uploaded_at,document_date,status,error FROM documents WHERE user_id=? ORDER BY uploaded_at DESC LIMIT 5",(uid,)).fetchall()
    flagged=con.execute("SELECT o.*,d.filename FROM observations o JOIN documents d ON d.id=o.document_id WHERE o.user_id=? AND o.flagged<>'' ORDER BY o.created_at DESC LIMIT 5",(uid,)).fetchall()
    return jsonify(total_documents=total,processed_documents=reviewed,recent=[dict(x) for x in recent],flagged=[dict(x) for x in flagged])


@app.get("/api/fhir")
@auth_required
def fhir_export():
    rows=db().execute("SELECT id,email FROM users WHERE id=?",(user_id(),)).fetchone()
    entries=[{"resource":{"resourceType":"Patient","id":rows["id"],"identifier":[{"system":"urn:ietf:rfc:3986","value":"urn:uuid:"+rows["id"]}],"telecom":[{"system":"email","value":rows["email"]}]}}]
    for d in db().execute("SELECT id,filename,category,uploaded_at FROM documents WHERE user_id=?",(user_id(),)):
        entries.append({"resource":{"resourceType":"DocumentReference","id":d["id"],"status":"current","type":{"text":d["category"]},"date":d["uploaded_at"],"description":d["filename"]}})
    for o in db().execute("SELECT * FROM observations WHERE user_id=?",(user_id(),)):
        entries.append({"resource":{"resourceType":"Observation","id":o["id"],"status":"unknown","code":{"text":o["name"]},"valueString":f"{o['value']} {o['unit']}".strip(),"referenceRange":[{"text":o["reference_range"]}] if o["reference_range"] else [],"derivedFrom":[{"reference":"DocumentReference/"+o["document_id"]}]}})
    for m in db().execute("SELECT * FROM medications WHERE user_id=?",(user_id(),)):
        entries.append({"resource":{"resourceType":"MedicationRequest","id":m["id"],"status":"unknown","intent":"order","medicationCodeableConcept":{"text":m["name"]},"subject":{"reference":"Patient/"+rows["id"]},"authoredOn":m["prescription_date"] or None,"dosageInstruction":[{"text":m["details"]}] if m["details"] else [],"note":[{"text":"Extracted from a source document; verify before use."}]}})
    for c in db().execute("SELECT * FROM conditions WHERE user_id=?",(user_id(),)):
        entries.append({"resource":{"resourceType":"Condition","id":c["id"],"subject":{"reference":"Patient/"+rows["id"]},"code":{"text":c["name"]},"onsetDateTime":c["noted_date"] or None,"note":[{"text":"Recorded in source document; not independently verified."}] if c["notes"] else []}})
    return jsonify(fhirVersion="R4",type="collection",entry=entries,notice="Prototype FHIR-style export. Not an official ABDM connection.")


@app.delete("/api/account")
@auth_required
def delete_account():
    uid=user_id()
    names=[r["stored_name"] for r in db().execute("SELECT stored_name FROM documents WHERE user_id=?",(uid,))]
    db().execute("DELETE FROM users WHERE id=?",(uid,));db().commit();session.clear()
    for name in names:(UPLOADS/name).unlink(missing_ok=True)
    return jsonify(ok=True)


@app.errorhandler(413)
def too_large(_): return jsonify(error=f"File exceeds the {MAX_BYTES // (1024*1024)} MB limit."),413


@app.get("/")
def home(): return send_from_directory(app.static_folder,"index.html")


if __name__ == "__main__":
    app.run(host="127.0.0.1",port=int(os.getenv("PORT","5000")),debug=False)
