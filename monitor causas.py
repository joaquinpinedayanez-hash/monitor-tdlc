# monitor_causas.py
"""
Bot de monitoreo de escritos del TDLC.
Diseñado para correr en GitHub Actions cada 20 minutos.
Las credenciales se leen desde variables de entorno (GitHub Secrets).
El estado de escritos ya vistos se guarda en estado_escritos.json,
que se persiste entre ejecuciones mediante un artefacto de GitHub Actions.
"""

import os
import sys
import json
import smtplib
import datetime as dt
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from playwright.sync_api import sync_playwright

# ── Rutas ────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
CAUSAS_FILE = BASE_DIR / "causas.json"
ESTADO_FILE = BASE_DIR / "estado_escritos.json"

# ── Credenciales desde variables de entorno ───────────────────────────────────
EMAIL_FROM     = os.environ["EMAIL_FROM"]
GMAIL_PASSWORD = os.environ["GMAIL_PASSWORD"]
EMAIL_TO_DEFAULT = [e.strip() for e in os.environ["EMAIL_TO_DEFAULT"].split(",") if e.strip()]

URL_BASE = "https://consultas.tdlc.cl"

# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg):
    print(f"[{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

# ── Hora Chile ────────────────────────────────────────────────────────────────
def chile_offset_hours():
    mes = dt.datetime.utcnow().month
    return 4 if 4 <= mes <= 9 else 3

def ahora_chile():
    return dt.datetime.utcnow() - dt.timedelta(hours=chile_offset_hours())

def rango_hoy_chile_ms():
    offset = chile_offset_hours()
    ahora = ahora_chile()
    inicio_chile = dt.datetime(ahora.year, ahora.month, ahora.day)
    inicio_utc = inicio_chile + dt.timedelta(hours=offset)
    fin_utc    = inicio_utc + dt.timedelta(days=1)
    return int(inicio_utc.timestamp() * 1000), int(fin_utc.timestamp() * 1000)

def fmt_fecha_ms(ms):
    if not isinstance(ms, (int, float)):
        return "N/A"
    t = dt.datetime.utcfromtimestamp(ms / 1000) - dt.timedelta(hours=chile_offset_hours())
    return t.strftime("%d-%m-%Y %H:%M")

# ── Estado ────────────────────────────────────────────────────────────────────
def cargar_estado():
    if ESTADO_FILE.exists():
        try:
            return json.loads(ESTADO_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"⚠️ Error leyendo estado, se reinicia: {e}")
    return {}

def guardar_estado(estado):
    ESTADO_FILE.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")

# ── Causas ────────────────────────────────────────────────────────────────────
def cargar_causas():
    return json.loads(CAUSAS_FILE.read_text(encoding="utf-8"))

def destinatarios_para(causa):
    """
    Lee EMAIL_TO_CAUSAS, que es un JSON con este formato:
      {"42572": ["a@mail.cl", "b@mail.cl"], "42509": ["c@mail.cl"]}
    Si el idCausa no aparece, usa EMAIL_TO_DEFAULT.
    """
    raw = os.environ.get("EMAIL_TO_CAUSAS", "").strip()
    if raw:
        try:
            mapa = json.loads(raw)
            key = str(causa["idCausa"])
            if key in mapa and mapa[key]:
                return mapa[key]
        except Exception as e:
            log(f"⚠️ Error leyendo EMAIL_TO_CAUSAS: {e}")
    return EMAIL_TO_DEFAULT

# ── Scraping ──────────────────────────────────────────────────────────────────
def obtener_escritos(page, causa):
    api = f"{URL_BASE}/rest/escrito/pendientes/{causa['idCausa']}/true"
    try:
        resp_raw = page.evaluate(f"""() => {{
            var xhr = new XMLHttpRequest();
            xhr.open('GET', '{api}', false);
            xhr.setRequestHeader('Accept', 'application/json');
            try {{ xhr.send(); return xhr.status + '|' + xhr.responseText; }}
            catch(e) {{ return 'ERR|' + e.toString(); }}
        }}""")
        status, _, body = resp_raw.partition("|")
        if status != "200":
            log(f"  ⚠️ Status {status} para idCausa={causa['idCausa']}")
            return None
        data = json.loads(body)
        lista = data.get("results", data) if isinstance(data, dict) else data
        return lista if isinstance(lista, list) else []
    except Exception as e:
        log(f"  ⚠️ Error REST/JSON: {e}")
        return None

def revisar_causas(causas, estado):
    inicio_ms, fin_ms = rango_hoy_chile_ms()
    log(f"Rango HOY Chile: [{inicio_ms} .. {fin_ms}]")
    nuevos_por_causa = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page    = browser.new_context().new_page()

        try:
            page.goto(f"{URL_BASE}/estadoDiario", wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(3000)
        except Exception as e:
            log(f"⚠️ Bootstrap falló: {e}")
            browser.close()
            return nuevos_por_causa

        for causa in causas:
            log(f"📂 {causa['alias']} (idCausa={causa['idCausa']})")
            escritos = obtener_escritos(page, causa)
            if escritos is None:
                continue

            escritos_hoy = [
                e for e in escritos
                if isinstance(e, dict)
                and isinstance(e.get("fechaIngreso"), (int, float))
                and inicio_ms <= e["fechaIngreso"] < fin_ms
            ]

            key = str(causa["idCausa"])
            ids_vistos = set(estado.get(key, {}).get("ids_vistos", []))
            nuevos = [e for e in escritos_hoy if e.get("id") not in ids_vistos]

            log(f"  → {len(escritos)} total, {len(escritos_hoy)} hoy, {len(nuevos)} nuevo(s)")

            if nuevos:
                nuevos_por_causa[causa["alias"]] = {"causa": causa, "escritos": nuevos}

            ids_actualizados = ids_vistos | {e["id"] for e in escritos_hoy if e.get("id") is not None}
            estado[key] = {
                "alias": causa["alias"],
                "ultimo_check": ahora_chile().strftime("%Y-%m-%d %H:%M:%S"),
                "ids_vistos": sorted(ids_actualizados),
            }

        browser.close()

    return nuevos_por_causa

# ── Formateo ──────────────────────────────────────────────────────────────────
def _safe_get(obj, *claves, default=""):
    cur = obj
    for k in claves:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur if cur is not None else default

def formatear_mensaje(alias, data):
    hoy   = ahora_chile().strftime("%d-%m-%Y")
    causa = data["causa"]
    escritos = data["escritos"]
    url   = f"{URL_BASE}/do_search?proc={causa['proc']}&idCausa={causa['idCausa']}"
    primer   = escritos[0] if escritos else {}
    rol      = primer.get("rolCausa", "")
    caratula = primer.get("caratulaCausa", "")

    msg  = f"TDLC — Nuevos escritos al {hoy}\n"
    msg += "=" * 60 + "\n\n"
    msg += f"Causa: {alias}\n"
    if rol:       msg += f"Rol: {rol}\n"
    if caratula:  msg += f"Caratula: {caratula}\n"
    msg += f"URL: {url}\n"
    msg += f"{len(escritos)} escrito(s) nuevo(s):\n\n"

    for e in escritos:
        tipo   = _safe_get(e, "tipoEscrito", "name") or e.get("referencia", "(sin tipo)")
        parte  = e.get("parteQuePresenta", "") or _safe_get(e, "origen", "persona", "nombres")
        cuad   = _safe_get(e, "cuaderno", "name")
        foja_i = e.get("fojaInicioDoc")
        foja_s = f"{foja_i}-{e.get('fojaTerminoDoc')}" if foja_i is not None else "N/A"

        msg += f"  * {tipo}\n"
        msg += f"    Referencia: {e.get('referencia','')}\n"
        if parte: msg += f"    Presenta: {parte}\n"
        msg += f"    Fecha: {fmt_fecha_ms(e.get('fechaIngreso'))}\n"
        if cuad:  msg += f"    Cuaderno: {cuad}\n"
        msg += f"    Foja: {foja_s}\n"
        firmado = e.get("nombreUsuarioIngreso", "")
        if firmado: msg += f"    Firmado por: {firmado}\n"
        msg += "\n"

    return msg

# ── Email ─────────────────────────────────────────────────────────────────────
def enviar_email(mensaje, asunto, destinatarios):
    msg = MIMEMultipart()
    msg["From"]    = EMAIL_FROM
    msg["To"]      = ", ".join(destinatarios)
    msg["Subject"] = asunto
    msg.attach(MIMEText(mensaje, "plain", "utf-8"))
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(EMAIL_FROM, GMAIL_PASSWORD)
        server.sendmail(EMAIL_FROM, destinatarios, msg.as_string())
    log(f"📧 Email enviado a: {', '.join(destinatarios)}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    force = "--force" in sys.argv
    init  = "--init"  in sys.argv

    log(f"=== Revisión {ahora_chile().strftime('%d-%m-%Y %H:%M')} ===")

    causas = cargar_causas()
    estado = cargar_estado()
    nuevos = revisar_causas(causas, estado)
    guardar_estado(estado)

    total = sum(len(v["escritos"]) for v in nuevos.values())

    if init:
        log(f"Modo --init: {total} escrito(s) registrado(s) sin alertar.")
        return

    if total == 0:
        log("Sin escritos nuevos.")
        if force:
            hoy = ahora_chile().strftime("%d-%m-%Y")
            enviar_email(
                f"TDLC — Test. Sin novedades al {hoy}",
                f"TDLC — Test bot {hoy}",
                EMAIL_TO_DEFAULT,
            )
        return

    log(f"¡{total} escrito(s) nuevo(s)!")
    hoy = ahora_chile().strftime("%d-%m-%Y")

    for alias, data in nuevos.items():
        causa = data["causa"]
        n = len(data["escritos"])
        destinatarios = destinatarios_para(causa)
        try:
            enviar_email(
                formatear_mensaje(alias, data),
                f"TDLC — {n} escrito(s) nuevo(s) | {alias[:50]} | {hoy}",
                destinatarios,
            )
        except Exception as e:
            log(f"❌ Error email para {alias}: {e}")

if __name__ == "__main__":
    main()