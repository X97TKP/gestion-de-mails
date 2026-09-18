import functools
import os
import re
import sqlite3
import uuid
import json

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
import requests

# Imports locaux
from dotenv import load_dotenv
from mail_manager import get_account_list, get_latest_emails, send_email
from candidature_manager import (
    CV_PATH,
    get_cand_db,
    scraper_page,
    rediger_candidature_complete,
    generer_pdf_lettre,
    envoyer_candidature_avec_pj,
)
load_dotenv()


# ==============================================================================
# INITIALISATION DE L'APPLICATION
# ==============================================================================
app = Flask(__name__)

# Clé secrète pour chiffrer les cookies de session Flask
app.secret_key = os.getenv("APP_SECRET_KEY")
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True

# Mot de passe d'accès au site
ACCESS_PASSWORD = os.getenv("ACCESS_PASSWORD")

DB_PATH = os.getenv("DB_PATH")
OLLAMA_URL = os.getenv("OLLAMA_URL")
MODEL_NAME = os.getenv("MODEL_NAME")

# ==============================================================================
# CONFIGURATION DU PROFIL : 
# ==============================================================================
raw_persona = os.getenv("PERSONA")
PERSONA = json.loads(raw_persona) if raw_persona else {}

# ==============================================================================
# SÉCURITÉ ET BASES DE DONNÉES
# ==============================================================================

def login_required(view):
    """Décorateur pour verrouiller l'accès aux utilisateurs non connectés."""
    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Non authentifié"}), 401
            return redirect(url_for("login"))
        return view(**kwargs)
    return wrapped_view


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            )
        """)
        conn.commit()

init_db()

# ==============================================================================
# ROUTES D'AUTHENTIFICATION ET PUBLIQUES
# ==============================================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        entered_password = request.form.get("password")
        if entered_password == ACCESS_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("index"))
        else:
            error = "Mot de passe incorrect."

    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/mentions-legales")
def mentions_legales():
    return render_template("mentions_legales.html")

# ==============================================================================
# PAGE PRINCIPALE (PROTÉGÉE)
# ==============================================================================

@app.route("/")
@login_required
def index():
    return render_template("index.html")

# ==============================================================================
# APIS DU CHAT
# ==============================================================================

@app.route("/api/conversations", methods=["GET"])
@login_required
def get_conversations():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, title FROM conversations ORDER BY updated_at DESC")
        rows = cursor.fetchall()
    return jsonify([{"id": r["id"], "title": r["title"]} for r in rows])

@app.route("/api/conversations", methods=["POST"])
@login_required
def create_conversation():
    conv_id = str(uuid.uuid4())
    title = "Nouvelle discussion"
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO conversations (id, title) VALUES (?, ?)", (conv_id, title))
        conn.commit()
    return jsonify({"id": conv_id, "title": title})

@app.route("/api/conversations/<conv_id>/messages", methods=["GET"])
@login_required
def get_messages(conv_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY created_at ASC",
            (conv_id,),
        )
        rows = cursor.fetchall()
    return jsonify([{"role": r["role"], "content": r["content"]} for r in rows])

@app.route("/api/conversations/<conv_id>", methods=["DELETE"])
@login_required
def delete_conversation(conv_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
        cursor.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
        conn.commit()
    return jsonify({"success": True})

@app.route("/api/chat", methods=["POST"])
@login_required
def chat():
    data = request.json
    conv_id = data.get("conversation_id")
    user_message = data.get("message", "").strip()

    if not user_message or not conv_id:
        return jsonify({"error": "Paramètres manquants"}), 400

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM messages WHERE conversation_id = ?", (conv_id,))
        is_first = cursor.fetchone()["count"] == 0
        if is_first:
            title = (user_message[:30] + "...") if len(user_message) > 30 else user_message
            cursor.execute("UPDATE conversations SET title = ? WHERE id = ?", (title, conv_id))

        cursor.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'user', ?)",
            (conv_id, user_message),
        )
        cursor.execute("UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (conv_id,))
        conn.commit()

        cursor.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY created_at DESC LIMIT 16",
            (conv_id,),
        )
        history = cursor.fetchall()

    context = [{"role": r["role"], "content": r["content"]} for r in reversed(history)]
    payload = {"model": MODEL_NAME, "messages": context, "stream": False, "options": {"temperature": 0.7}}

    try:
        res = requests.post(OLLAMA_URL, json=payload)
        reply = res.json()["message"]["content"]
    except Exception as e:
        return jsonify({"error": f"Erreur Ollama: {str(e)}"}), 500

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'assistant', ?)",
            (conv_id, reply),
        )
        conn.commit()

    return jsonify({"reply": reply})

# ==============================================================================
# APIS DES E-MAILS (RÉPONSES CLASSIQUES)
# ==============================================================================

@app.route("/api/email-accounts", methods=["GET"])
@login_required
def list_email_accounts():
    return jsonify(get_account_list())

@app.route("/api/emails", methods=["GET"])
@login_required
def list_emails():
    account = request.args.get("account", "pro")
    emails = get_latest_emails(account_key=account, limit=20)
    return jsonify(emails)

@app.route("/api/emails/analyze", methods=["POST"])
@login_required
def analyze_email():
    data = request.json
    account = data.get("account", "pro")
    sender = data.get("from")
    subject = data.get("subject")
    body = data.get("body")
    instruction = data.get("instruction", "").strip()

    if account == "perso":
        return jsonify({
            "analysis": "### 📌 Résumé\nCe courriel a été reçu sur ton compte personnel.\n\n### ✍️ Note\n*Conformément à tes règles, tu ne réponds pas aux e-mails sur ton adresse personnelle.*"
        })

    if account == "pro":
        context_desc = f"Tu rédiges depuis ton adresse professionnelle ({PERSONA['nom']}). Ton numéro ({PERSONA['telephone']}) doit figurer dans la signature."
        signature = PERSONA["signature_pro"]
    else:
        context_desc = f"Tu rédiges depuis ton adresse universitaire ({PERSONA['nom']}). Le ton est très respectueux, direct et académique."
        signature = PERSONA["signature_univ"]

    system_prompt = f"""Tu es {PERSONA['nom']}, {PERSONA['statut']}.
{context_desc}
RÈGLES ABSOLUES :
1. Écris TOUJOURS à la première personne du singulier ('je', 'mon'). Tu ES {PERSONA['nom']}.
2. ZÉRO formule pompeuse ou vieillotte du 19e siècle.
3. Si un échange nécessite des créneaux, insère la balise [mes disponibilités : créneaux à préciser].
4. Termine obligatoirement par cette signature :
{signature}"""

    user_prompt = f"""Voici le mail reçu :
Expéditeur : {sender}
Sujet : {subject}
Contenu :
{body}

Consigne de {PERSONA['nom']} : {instruction if instruction else 'Répondre positivement et efficacement'}

Format attendu :
### 📌 Résumé
[Résumé clair en 2 points clés]

### ✍️ Proposition de réponse
[Texte complet prêt à l'envoi]"""

    payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        "stream": False,
        "options": {"temperature": 0.4},
    }

    try:
        res = requests.post(OLLAMA_URL, json=payload)
        return jsonify({"analysis": res.json()["message"]["content"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/emails/send", methods=["POST"])
@login_required
def trigger_send_email():
    data = request.json
    success, msg = send_email(data.get("account", "pro"), data.get("to"), data.get("subject"), data.get("body"))
    return jsonify({"success": True}) if success else (jsonify({"error": msg}), 500)

# ==============================================================================
# APIS DES CANDIDATURES (DÉMARCHAGE)
# ==============================================================================

@app.route("/api/candidatures/generate", methods=["POST"])
@login_required
def api_cand_generate():
    data = request.json
    entreprise = data.get("entreprise", "").strip()
    contact = data.get("contact", "").strip()
    url = data.get("url", "").strip()
    email_direct = data.get("email", "").strip()

    if not entreprise:
        return jsonify({"error": "Le nom de l'entreprise est obligatoire."}), 400

    with get_cand_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, date_envoi, statut FROM candidatures WHERE LOWER(entreprise) = LOWER(?)", (entreprise,))
        existant = cur.fetchone()
        if existant:
            return jsonify({"error": f"Entreprise déjà contactée le {existant['date_envoi']}."}), 409

    scraped = scraper_page(url) if url else {"texte": "", "emails": []}
    contact_email = email_direct or (scraped["emails"][0] if scraped["emails"] else "")

    redaction = rediger_candidature_complete(
        nom_entreprise=entreprise,
        contact_nom=contact,
        contexte=scraped["texte"],
        ollama_url=OLLAMA_URL,
        model_name=MODEL_NAME,
        persona_cfg=PERSONA
    )

    # --- CORRECTION DU MAIL ---
    # On nettoie le texte brut généré par l'IA
    corps_brut = redaction.get("corps_mail", "").replace("Bonjour,", "").replace("Bonjour", "").strip()
    
    # On force la structure parfaite : Bonjour + IA + Signature
    corps_propre = f"Bonjour,\n\n{corps_brut}\n\n{PERSONA['signature_pro']}"

    return jsonify({
        "entreprise": entreprise,
        "contact": contact,
        "url": url,
        "email": contact_email,
        "sujet_mail": redaction.get("sujet_mail", ""),
        "corps_mail": corps_propre,
        "lettre_p1": redaction.get("lettre_p1", ""),
        "lettre_p2": redaction.get("lettre_p2", ""),
        "lettre_p3": redaction.get("lettre_p3", ""),
        "cv_found": os.path.exists(CV_PATH)
    })

@app.route("/api/candidatures/send", methods=["POST"])
@login_required
def api_cand_send():
    data = request.json
    entreprise = data.get("entreprise", "").strip()
    contact = data.get("contact", "").strip()
    url = data.get("url", "").strip()
    email_dest = data.get("email", "").strip()
    sujet = data.get("sujet_mail", "").strip()
    corps = data.get("corps_mail", "").strip()
    lettre_p1 = data.get("lettre_p1", "").strip()
    lettre_p2 = data.get("lettre_p2", "").strip()
    lettre_p3 = data.get("lettre_p3", "").strip()

    if not email_dest or not sujet or not corps:
        return jsonify({"error": "Champs obligatoires manquants."}), 400

    # Création du dossier pour les PDF générés
    os.makedirs("generated_letters", exist_ok=True)
    clean_nom = re.sub(r'[^a-zA-Z0-9_-]', '_', entreprise)
    pdf_path = os.path.join("generated_letters", f"Lettre_Motivation_{clean_nom}.pdf")

    try:
        # 1. Génération du PDF
        generer_pdf_lettre(entreprise, contact, lettre_p1, lettre_p2, lettre_p3, output_path=pdf_path)
        # 2. Envoi par mail (via le dictionnaire ACCOUNTS qui doit être récupéré du mail_manager)
        from mail_manager import ACCOUNTS
        envoyer_candidature_avec_pj(ACCOUNTS["pro"], email_dest, sujet, corps, pdf_path)
        statut = "en_attente"
    except Exception as e:
        return jsonify({"error": f"Erreur traitement : {str(e)}"}), 500

    with get_cand_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO candidatures (entreprise, site_web, email_contact, sujet, corps, lettre_pdf_path, statut)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (entreprise, url, email_dest, sujet, corps, pdf_path, statut))
        conn.commit()

    return jsonify({"success": True})

@app.route("/api/candidatures/list", methods=["GET"])
@login_required
def api_cand_list():
    with get_cand_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, entreprise, site_web, email_contact, sujet, date_envoi, statut,
                   CAST(julianday('now') - julianday(date_envoi) AS INT) as jours_ecoules
            FROM candidatures
            ORDER BY date_envoi DESC
        """)
        rows = cur.fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/candidatures/<int:cid>/status", methods=["PATCH"])
@login_required
def api_cand_update_status(cid):
    data = request.json
    nouveau_statut = data.get("statut", "repondu")
    with get_cand_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE candidatures SET statut = ? WHERE id = ?", (nouveau_statut, cid))
        conn.commit()
    return jsonify({"success": True})

@app.route("/api/candidatures/<int:cid>", methods=["DELETE"])
@login_required
def api_cand_delete(cid):
    with get_cand_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM candidatures WHERE id = ?", (cid,))
        conn.commit()
    return jsonify({"success": True})

# ==============================================================================
# LANCEMENT DU SERVEUR
# ==============================================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)