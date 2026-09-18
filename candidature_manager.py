import os
import re
import json
import sqlite3
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

from bs4 import BeautifulSoup
import requests
from dotenv import load_dotenv

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm

load_dotenv()

# Chemins et bases de données
DB_CANDIDATURES = os.getenv("DB_CANDIDATURES", "candidatures.db")
CV_PATH = os.getenv("CV_PATH", "cv.pdf")


def get_cand_db():
    conn = sqlite3.connect(DB_CANDIDATURES)
    conn.row_factory = sqlite3.Row
    return conn


def init_cand_db():
    with get_cand_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS candidatures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entreprise TEXT NOT NULL,
                site_web TEXT,
                email_contact TEXT NOT NULL,
                sujet TEXT,
                corps TEXT,
                lettre_pdf_path TEXT,
                date_envoi TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                statut TEXT DEFAULT 'en_attente'
            )
        """)
        conn.commit()


init_cand_db()


def scraper_page(url):
    """Extrait le texte brut et les e-mails d'une page web."""
    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        soup = BeautifulSoup(res.text, "html.parser")
        
        # Supprimer les balises superflues
        for tag in soup(["script", "style", "noscript", "header", "footer"]):
            tag.decompose()

        texte = soup.get_text(separator=" ", strip=True)
        emails = re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", res.text)
        propres = [e for e in set(emails) if not e.endswith((".png", ".jpg", ".webp", ".gif"))]
        
        return {"texte": texte[:3000], "emails": propres}
    except Exception as e:
        return {"texte": "", "emails": [], "error": str(e)}


def rediger_candidature_complete(nom_entreprise, contact_nom, contexte, ollama_url, model_name, persona_cfg):
    """
    Demande au LLM de produire à la fois le mail d'accompagnement et
    les 3 paragraphes argumentés de la lettre de motivation (Vous / Moi / Nous).
    """
    nom = persona_cfg.get("nom", "Le Candidat")
    formation = os.getenv("FORMATION_CANDIDAT", persona_cfg.get("statut", "étudiant en informatique"))
    duree = os.getenv("ALTERNANCE_DUREE", "24 mois")
    debut = os.getenv("ALTERNANCE_DEBUT", "la rentrée")
    rythme = os.getenv("ALTERNANCE_RYTHME", "alterné")

    prompt = f"""Tu rédiges une candidature en tant que {nom}, {formation}.
Alternance recherchée : {duree} dès {debut}. Rythme : {rythme}.
Entreprise : {nom_entreprise}.
Interlocuteur ciblé : {contact_nom if contact_nom else "la Direction des Ressources Humaines"}.
Éléments récupérés sur l'entreprise : "{contexte}"

Génère un JSON strict avec ces 5 champs :
1. "sujet_mail" : Objet percutant pour le courriel.
2. "corps_mail" : Court texte d'accompagnement (5-6 lignes) pour le courriel, annonçant le CV et la lettre de motivation en pièces jointes.
3. "lettre_p1" : Paragraphe 1 de la lettre (VOUS). Montre une vraie connaissance de l'entreprise ({nom_entreprise}), de ses projets ou innovations récents, et fais le lien avec l'informatique.
4. "lettre_p2" : Paragraphe 2 de la lettre (MOI). Présente les compétences techniques du candidat ({nom}) : bases de données SQL, modélisation, développement logiciel, environnements Linux/Windows, autonomie et adaptabilité.
5. "lettre_p3" : Paragraphe 3 de la lettre (NOUS). Ce que le candidat va concrètement apporter à {nom_entreprise}, sa motivation et son envie de s'investir durablement.

Réponds UNIQUEMENT avec l'objet JSON :
{{
  "sujet_mail": "...",
  "corps_mail": "...",
  "lettre_p1": "...",
  "lettre_p2": "...",
  "lettre_p3": "..."
}}"""

    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.4},
    }

    try:
        res = requests.post(ollama_url, json=payload).json()
        raw = res["message"]["content"].strip()
        raw = re.sub(r"^```json\s*|^```\s*|```\s*$", "", raw, flags=re.MULTILINE).strip()
        return json.loads(raw)
    except Exception:
        return {
            "sujet_mail": f"Candidature alternance Développement Informatique - {nom}",
            "corps_mail": f"Bonjour,\n\nActuellement {formation}, je vous adresse ma candidature pour une alternance de {duree} au sein de {nom_entreprise}.\nVous trouverez ci-joints mon curriculum vitae ainsi que ma lettre de motivation détaillée.\n\nCordialement,\n{persona_cfg.get('signature_pro', nom)}",
            "lettre_p1": f"Acteur dynamique dans son domaine, {nom_entreprise} place les solutions numériques au cœur de son fonctionnement et de ses innovations.",
            "lettre_p2": f"Étudiant passionné par l'informatique, j'ai développé de solides compétences en conception logicielle, gestion de bases de données et maîtrise des environnements d'exploitation.",
            "lettre_p3": f"Intégrer {nom_entreprise} représente pour moi l'opportunité d'apporter mon autonomie, ma rigueur technique et ma motivation pour contribuer activement à vos projets."
        }


def formater_contact(contact_brut):
    """Met une majuscule au prénom et conserve le nom de famille en majuscule."""
    if not contact_brut:
        return ""
    mots = contact_brut.split()
    res = []
    for m in mots:
        if m.isupper():
            res.append(m)
        else:
            res.append(m.capitalize())
    return " ".join(res)


def extraire_titre_nom(contact_brut):
    """Génère une salutation propre selon la présence de civilité et du nom de famille."""
    if not contact_brut:
        return "Madame, Monsieur,"
    
    contact_lower = contact_brut.lower()
    titre = ""
    
    if "madame" in contact_lower or "mme" in contact_lower:
        titre = "Madame"
    elif "monsieur" in contact_lower or "m." in contact_lower or "mr" in contact_lower:
        titre = "Monsieur"

    mots = contact_brut.split()
    noms_maj = [m for m in mots if m.isupper() and len(m) > 1]
    
    if titre and noms_maj:
        return f"{titre} {noms_maj[-1]},"
    elif titre:
        return f"{titre},"
    else:
        return "Madame, Monsieur,"


def generer_pdf_lettre(nom_entreprise, contact_nom, lettre_p1, lettre_p2, lettre_p3, output_path="lettre_motivation.pdf"):
    """Génère un PDF A4 d'une seule page à partir des variables d'environnement."""
    # Récupération dynamique du persona depuis l'environnement
    raw_persona = os.getenv("PERSONA")
    try:
        persona = json.loads(raw_persona) if raw_persona else {}
    except Exception:
        persona = {}

    nom_candidat = persona.get("nom", "")
    tel_candidat = persona.get("telephone", "")

    # Récupération de l'e-mail pro configuré
    raw_accounts = os.getenv("ACCOUNTS")
    email_candidat = ""
    try:
        accounts = json.loads(raw_accounts) if raw_accounts else {}
        email_candidat = accounts.get("pro", {}).get("email", "")
    except Exception:
        email_candidat = ""

    ville = os.getenv("VILLE_CANDIDAT", "")
    objet_texte = os.getenv("ALTERNANCE_OBJET", "Candidature Alternance Informatique")
    duree_texte = os.getenv("ALTERNANCE_DUREE", "24 mois")
    debut_texte = os.getenv("ALTERNANCE_DEBUT", "Septembre")
    rythme_texte = os.getenv("ALTERNANCE_RYTHME", "2 semaines entreprise / 2 semaines école")

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=2.0 * cm,
        rightMargin=2.0 * cm,
        topMargin=2.0 * cm,
        bottomMargin=2.0 * cm
    )

    styles = getSampleStyleSheet()

    style_exp = ParagraphStyle(
        'Expediteur',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        leading=14,
        textColor=colors.HexColor('#111827')
    )
    style_date = ParagraphStyle(
        'DateStyle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        leading=14,
        alignment=2,
        textColor=colors.HexColor('#374151')
    )
    style_dest = ParagraphStyle(
        'Destinataire',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=10,
        leading=14,
        textColor=colors.HexColor('#1f2937')
    )
    style_cadre = ParagraphStyle(
        'CadreObjet',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9.5,
        leading=14,
        textColor=colors.HexColor('#111827')
    )
    style_salutation = ParagraphStyle(
        'Salutation',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=10.5,
        leading=15,
        textColor=colors.HexColor('#000000'),
        spaceAfter=8
    )
    style_body = ParagraphStyle(
        'CorpsLettre',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        leading=15,
        alignment=4,
        textColor=colors.HexColor('#1f2937'),
        spaceAfter=10
    )

    story = []

    # 1. En-tête : Expéditeur & Date dynamique
    jours = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
    vrais_mois = ["", "Janvier", "Février", "Mars", "Avril", "Mai", "Juin", "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre"]
    now = datetime.now()
    
    date_texte = f"{jours[now.weekday()]} {now.day} {vrais_mois[now.month]} {now.year}"
    if ville:
        date_texte += f"<br/>{ville}"

    texte_expediteur = f"<b>{nom_candidat}</b>"
    if tel_candidat:
        texte_expediteur += f"<br/>Téléphone: {tel_candidat}"
    if email_candidat:
        texte_expediteur += f"<br/>Mail: {email_candidat}"

    header_table = Table([
        [
            Paragraph(texte_expediteur, style_exp),
            Paragraph(date_texte, style_date)
        ]
    ], colWidths=[10 * cm, 7 * cm])
    
    header_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('LEFTPADDING', (0,0), (-1,-1), 0),
        ('RIGHTPADDING', (0,0), (-1,-1), 0),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 14))

    # 2. Destinataire
    contact_propre = formater_contact(contact_nom)
    titre_dest = f"À l'attention de {contact_propre} - {nom_entreprise}" if contact_propre else f"À l'attention de la Direction des Ressources Humaines - {nom_entreprise}"
    story.append(Paragraph(titre_dest, style_dest))
    story.append(Spacer(1, 12))

    # 3. Encadré Objet / Durée / Rythme
    cadre_contenu = [
        [Paragraph(
            f"<b>Objet:</b> {objet_texte}<br/>"
            f"<b>Durée:</b> {duree_texte} - À partir de {debut_texte}.<br/>"
            f"<b>Rythme:</b> {rythme_texte}.",
            style_cadre
        )]
    ]
    cadre_table = Table(cadre_contenu, colWidths=[17 * cm])
    cadre_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#f8fafc')),
        ('BOX', (0,0), (-1,-1), 0.5, colors.HexColor('#cbd5e1')),
        ('TOPPADDING', (0,0), (-1,-1), 7),
        ('BOTTOMPADDING', (0,0), (-1,-1), 7),
        ('LEFTPADDING', (0,0), (-1,-1), 10),
        ('RIGHTPADDING', (0,0), (-1,-1), 10),
    ]))
    story.append(cadre_table)
    story.append(Spacer(1, 14))

    # 4. Salutation
    appel = extraire_titre_nom(contact_nom)
    story.append(Paragraph(appel, style_salutation))

    # 5. Corps de texte (Vous / Moi / Nous)
    story.append(Paragraph(lettre_p1, style_body))
    story.append(Paragraph(lettre_p2, style_body))
    story.append(Paragraph(lettre_p3, style_body))

    # 6. Formule de politesse
    appel_sans_virgule = appel.rstrip(',')
    politesse = f"Je vous prie d'agréer, {appel_sans_virgule}, l'expression de mes salutations distinguées."
    
    story.append(Spacer(1, 4))
    story.append(Paragraph(politesse, style_body))

    doc.build(story)
    return output_path


def envoyer_candidature_avec_pj(smtp_cfg, destinataire, sujet, corps, pdf_lettre_path):
    """Envoie le courriel avec le CV et la lettre de motivation."""
    msg = MIMEMultipart()
    msg["From"] = smtp_cfg["email"]
    msg["To"] = destinataire
    msg["Subject"] = sujet
    msg.attach(MIMEText(corps, "plain", "utf-8"))

    # Pièce jointe 1 : CV
    if os.path.exists(CV_PATH):
        with open(CV_PATH, "rb") as f:
            part_cv = MIMEBase("application", "pdf")
            part_cv.set_payload(f.read())
        encoders.encode_base64(part_cv)
        nom_cv = os.getenv("SORTIE_CV_PDF", "CV.pdf")
        part_cv.add_header("Content-Disposition", f'attachment; filename="{nom_cv}"')
        msg.attach(part_cv)

    # Pièce jointe 2 : Lettre de motivation
    if pdf_lettre_path and os.path.exists(pdf_lettre_path):
        with open(pdf_lettre_path, "rb") as f:
            part_lettre = MIMEBase("application", "pdf")
            part_lettre.set_payload(f.read())
        encoders.encode_base64(part_lettre)
        nom_lm = os.getenv("SORTIE_LM_PDF", "Lettre_Motivation.pdf")
        part_lettre.add_header("Content-Disposition", f'attachment; filename="{nom_lm}"')
        msg.attach(part_lettre)

    # Envoi SMTP sécurisé
    with smtplib.SMTP_SSL(smtp_cfg["smtp_server"], smtp_cfg["smtp_port"]) as server:
        server.login(smtp_cfg["email"], smtp_cfg["password"])
        server.send_message(msg)