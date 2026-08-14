"""
in'li Monitor — surveillance des nouvelles annonces in'li via GitHub Actions.

Principe :
  1. Appelle l'URL de recherche in'li (filtres appliques cote serveur)
  2. Parse les annonces du HTML
  3. Compare avec les references deja vues (inli_state.json)
  4. Envoie un mail si nouveaute

Config via variables d'environnement (GitHub Secrets) :
  GMAIL_ADDRESS       adresse Gmail expeditrice
  GMAIL_APP_PASSWORD  mot de passe d'application Gmail (16 caracteres)
  RECIPIENT_EMAIL     adresse de reception des alertes
  SEARCH_URL          (optionnel) URL de recherche in'li, sinon valeur par defaut
"""

import os
import re
import json
import smtplib
import logging
from pathlib import Path
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
DEFAULT_SEARCH_URL = (
    "https://www.inli.fr/locations/offres/ile-de-france-region_r:11"
    "?price_max=900&area_min=40&room_min=2"
)
SEARCH_URL = os.environ.get("SEARCH_URL", DEFAULT_SEARCH_URL)
BASE = "https://www.inli.fr"
STATE_FILE = "inli_state.json"

GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
RECIPIENT_EMAIL = os.environ["RECIPIENT_EMAIL"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# SCRAPING
# ─────────────────────────────────────────────
def fetch_offers() -> list:
    """Recupere et parse les annonces correspondant aux filtres de SEARCH_URL."""
    r = requests.get(SEARCH_URL, headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Nombre d'annonces annonce par le site ("149 logements")
    total = None
    m = re.search(r"(\d+)\s+logements?", soup.get_text(" ", strip=True))
    if m:
        total = int(m.group(1))
        log.info(f"in'li annonce {total} logement(s) pour ces filtres")

    offers = {}
    for a in soup.select('a[href*="/locations/offre/"]'):
        href = a.get("href", "")
        if not href:
            continue

        ref = href.rstrip("/").split("/")[-1]  # ex: PRV-312199
        text = " ".join(a.get_text(" ", strip=True).split())

        price = re.search(r"([\d\s]+)\s*€", text)
        rooms = re.search(r"(\d+)\s*pi[eè]ces?", text)
        area = re.search(r"([\d.,]+)\s*m²", text)

        try:
            city = href.split("/locations/offre/")[1].split("/")[0]
        except IndexError:
            city = "?"

        offer = {
            "id": ref,
            "link": href if href.startswith("http") else BASE + href,
            "city": city.replace("-", " ").title(),
            "price": price.group(1).replace(" ", "") if price else "?",
            "rooms": rooms.group(1) if rooms else "?",
            "area": area.group(1).replace(",", ".") if area else "?",
        }

        # La meme annonce apparait plusieurs fois (liste + carte + image), et
        # certaines occurrences ne contiennent aucun detail. On garde la plus
        # riche, pas la derniere rencontree.
        def richness(o):
            return sum(1 for k in ("price", "rooms", "area") if o[k] != "?")

        if ref not in offers or richness(offer) > richness(offers[ref]):
            offers[ref] = offer

    parsed = list(offers.values())

    # Garde-fou : le site annonce des logements mais on n'en parse aucun
    # => la structure HTML a probablement change, il faut le savoir.
    if total and total > 0 and not parsed:
        raise RuntimeError(
            f"{total} annonce(s) annoncee(s) par in'li mais 0 parsee. "
            "La structure HTML du site a probablement change."
        )

    log.info(f"{len(parsed)} annonce(s) parsee(s)")
    return parsed


# ─────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────
def load_state() -> set:
    p = Path(STATE_FILE)
    if not p.exists():
        log.info("Pas de state existant : premier run.")
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        seen = set(data.get("seen_ids", []))
        log.info(f"State charge : {len(seen)} reference(s) connue(s)")
        return seen
    except Exception as e:
        log.warning(f"State illisible, reinitialisation : {e}")
        return set()


def save_state(seen: set) -> None:
    payload = {
        "seen_ids": sorted(seen),
        "updated": datetime.now().isoformat(timespec="seconds"),
    }
    Path(STATE_FILE).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info(f"State sauvegarde : {len(seen)} reference(s)")


# ─────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────
def send_email(new_offers: list) -> None:
    rows = "".join(
        f"""
        <tr>
          <td style="padding:12px;border-bottom:1px solid #eee;">
            <a href="{o['link']}" style="font-weight:bold;color:#1a73e8;text-decoration:none;">
              {o['city']} — {o['price']} € cc
            </a><br>
            <span style="color:#666;font-size:13px;">
              {o['rooms']} pièces &nbsp;|&nbsp; {o['area']} m² &nbsp;|&nbsp; réf. {o['id']}
            </span>
          </td>
        </tr>"""
        for o in new_offers
    )

    body = f"""<html>
  <body style="font-family:Arial,Helvetica,sans-serif;max-width:600px;margin:auto;">
    <h2 style="color:#2c3e50;">🏠 {len(new_offers)} nouvelle(s) annonce(s) in'li</h2>
    <p>Ces logements viennent d'apparaître avec vos critères :</p>
    <table width="100%" style="border-collapse:collapse;border:1px solid #ddd;border-radius:8px;">
      {rows}
    </table>
    <p style="margin-top:24px;">
      <a href="{SEARCH_URL}" style="background:#1a73e8;color:#fff;padding:12px 20px;
         text-decoration:none;border-radius:6px;display:inline-block;">
        Voir la recherche sur in'li
      </a>
    </p>
    <small style="color:#999;">
      Alerte generee le {datetime.now().strftime('%d/%m/%Y à %H:%M:%S')}
    </small>
  </body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🏠 in'li — {len(new_offers)} nouvelle(s) annonce(s)"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT_EMAIL
    msg.attach(MIMEText(body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_ADDRESS, GMAIL_PASSWORD)
        s.sendmail(GMAIL_ADDRESS, RECIPIENT_EMAIL, msg.as_string())

    log.info(f"Email envoye a {RECIPIENT_EMAIL}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main() -> None:
    log.info("=== in'li Monitor demarre ===")
    log.info(f"URL surveillee : {SEARCH_URL}")

    offers = fetch_offers()
    seen = load_state()
    new = [o for o in offers if o["id"] not in seen]

    if new:
        log.info(f"{len(new)} nouvelle(s) annonce(s) detectee(s)")
        for o in new:
            log.info(
                f"  NEW -> {o['city']} | {o['price']} EUR | "
                f"{o['rooms']}p | {o['area']} m2 | {o['link']}"
            )
        send_email(new)
    else:
        log.info(f"Aucune nouveaute ({len(offers)} annonce(s) deja connue(s))")

    save_state(seen | {o["id"] for o in offers})
    log.info("=== Termine ===")


if __name__ == "__main__":
    main()
