#!/usr/bin/env python3
"""
Recolecta titulares de seguridad de todos los medios de fuentes.yaml.

Camino de cada medio, del más confiable al de respaldo:
  1. "feed":   RSS ya confirmado.
  2. "oem":    sección policiaca del diario en oem.com.mx.
  3. "pagina": una sección concreta del medio (policiaca, justicia...).
  4. Solo "sitio": se abre el home, se busca su RSS (la etiqueta que los
     sitios publican para los lectores de noticias, o /feed/ y /rss/) y, si
     no hay, se leen los titulares del propio home.
  5. Respaldo por Google Noticias: si lo anterior no trajo nada, o el medio
     bloquea a GitHub, se busca "site:dominio + palabras de seguridad".

Después vienen dos filtros:
  - Fecha: solo entra lo publicado hoy (hora del centro de México).
  - Palabras clave (fuentes.yaml > filtro): solo entra lo que suena a
    seguridad. Así Claude recibe candidatas y no la portada entera del país.

Salida:
  data/candidatas.json     titulares que pasaron los filtros
  data/estado_fuentes.json qué medio respondió, por qué vía y cuántas notas
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

RAIZ = Path(__file__).resolve().parents[1]
CONFIG = RAIZ / "fuentes.yaml"
DATOS = RAIZ / "data"
CACHE_FEEDS = DATOS / "feeds_descubiertos.json"
HISTORIAL = DATOS / "urls_vistas.json"
SALIDA = DATOS / "candidatas.json"
REPORTE = DATOS / "estado_fuentes.json"

TZ = ZoneInfo("America/Mexico_City")

# Headers de navegador: varios medios rechazan a quien se anuncia como bot.
# Solo se leen titulares públicos, igual que cualquier lector de RSS.
NAVEGADOR = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
}

RASTREADORES = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                "utm_content", "fbclid", "gclid", "cmpid", "ref"}

# Secciones cuyo contenido ya es de seguridad: no pasan por el filtro de
# palabras (una nota de la sección policiaca no necesita decir "homicidio").
SECCION_SEGURIDAD = re.compile(r"polic|justicia|seguridad|nota-?roja|sucesos", re.I)

# Enlaces del home que no son notas.
BASURA = re.compile(
    r"aviso de privacidad|t[eé]rminos y condiciones|pol[ií]tica de (privacidad|cookies)|"
    r"suscr[ií]b|reg[ií]strate|inicia sesi[oó]n|contacto|qui[eé]nes somos|directorio|"
    r"publicidad|newsletter|todos los derechos|men[uú] principal|ver m[aá]s|"
    r"lee tambi[eé]n|leer m[aá]s|clasificados|hor[oó]scopo", re.I)


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------

def normalizar(texto: str) -> str:
    """Minúsculas y sin acentos, para comparar palabras."""
    texto = unicodedata.normalize("NFKD", (texto or "").lower())
    return "".join(c for c in texto if not unicodedata.combining(c))


def limpiar_texto(texto: str) -> str:
    if not texto:
        return ""
    if "<" in texto:
        texto = BeautifulSoup(texto, "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", unescape(texto)).strip()


def limpiar_url(url: str) -> str:
    """Quita parámetros de rastreo (utm_...) sin tocar los que identifican la nota."""
    if not url:
        return ""
    partes = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(partes.query) if k.lower() not in RASTREADORES]
    return urlunsplit((partes.scheme, partes.netloc, partes.path, urlencode(query), ""))


def huella(url: str, titulo: str) -> str:
    base = url.rstrip("/") if url else normalizar(titulo)
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]


def dominio(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


def parece_nota(texto: str) -> bool:
    """Filtro mínimo para scraping: descarta menús, avisos y botones."""
    if not 25 <= len(texto) <= 220:
        return False
    if BASURA.search(texto) or len(texto.split()) < 5 or texto.isupper():
        return False
    return True


def pedir(url: str, timeout: int) -> requests.Response:
    r = requests.get(url, headers=NAVEGADOR, timeout=timeout)
    r.raise_for_status()
    return r


def cargar_json(ruta: Path, defecto):
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defecto


def guardar_json(ruta: Path, datos) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")


# --------------------------------------------------------------------------
# Lectura de feeds y de páginas
# --------------------------------------------------------------------------

def fecha_de_entrada(entrada) -> str | None:
    """Fecha de publicación de una entrada de feed, en hora del centro."""
    for campo in ("published_parsed", "updated_parsed"):
        t = entrada.get(campo)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc).astimezone(TZ).isoformat()
            except (TypeError, ValueError):
                pass
    return None


def items_de_feed(contenido: bytes, maximo: int) -> list[dict]:
    feed = feedparser.parse(contenido)
    items = []
    for e in feed.entries[:maximo]:
        titulo = limpiar_texto(e.get("title", ""))
        url = limpiar_url(e.get("link", ""))
        if len(titulo) < 15 or not url.startswith("http"):
            continue
        fuente_google = e.get("source") or {}
        items.append({
            "titulo": titulo,
            "url": url,
            "resumen": limpiar_texto(e.get("summary", ""))[:300],
            "fecha": fecha_de_entrada(e),
            # Solo Google Noticias trae esto: el sitio real que publicó la nota.
            "origen_href": fuente_google.get("href", "") if hasattr(fuente_google, "get") else "",
            "origen_titulo": fuente_google.get("title", "") if hasattr(fuente_google, "get") else "",
        })
    return items


def items_de_html(html: str, base: str, maximo: int, contiene: str | None = None) -> list[dict]:
    """Titulares de una página: los enlaces que parecen nota, del mismo sitio."""
    soup = BeautifulSoup(html, "html.parser")
    propio = dominio(base)
    vistos, items = set(), []
    for a in soup.find_all("a", href=True):
        texto = a.get_text(" ", strip=True)
        if not parece_nota(texto):
            continue
        href = limpiar_url(urljoin(base, a["href"]))
        if not href.startswith("http") or dominio(href) != propio:
            continue
        if contiene and contiene not in href:
            continue
        clave = (href.rstrip("/"), normalizar(texto))
        if clave[0] in vistos or clave[1] in vistos:
            continue
        vistos.update(clave)
        items.append({"titulo": texto, "url": href, "resumen": "", "fecha": None})
        if len(items) >= maximo:
            break
    return items


def rss_anunciado(html: str, base: str) -> str | None:
    """La etiqueta <link rel="alternate" type="application/rss+xml"> del home."""
    soup = BeautifulSoup(html, "html.parser")
    for link in soup.find_all("link", href=True):
        tipo = (link.get("type") or "").lower()
        rel = " ".join(link.get("rel") or []).lower()
        if "alternate" in rel and ("rss" in tipo or "atom" in tipo):
            href = urljoin(base, link["href"])
            if "comments" not in href:  # WordPress anuncia también el feed de comentarios
                return href
    return None


# --------------------------------------------------------------------------
# Cómo se lee cada medio
# --------------------------------------------------------------------------

def dominios_compartidos(fuentes: list[dict]) -> set[str]:
    """Dominios que usan varios medios de la lista (milenio.com, poresto.net...).

    Para esos, leer el home traería la portada nacional del grupo, no la del
    estado. Si no tienen página o feed propio, se leen por Google Noticias
    con el nombre del estado.
    """
    conteo: dict[str, int] = {}
    for f in fuentes:
        conteo[f["sitio"]] = conteo.get(f["sitio"], 0) + 1
    return {d for d, n in conteo.items() if n > 1 and d != "oem.com.mx"}


def modo_de(fuente: dict, compartidos: set[str]) -> str:
    # oem.com.mx bloquea a GitHub en todas sus páginas (verificado el
    # 22/09/2026: 36 de 36 diarios con HTTP 403). Sus diarios se leen por
    # Google Noticias.
    if fuente.get("oem") or fuente.get("nota") in ("bloqueado-403", "sin-titulares"):
        return "google"
    if fuente.get("feed"):
        return "feed"
    if fuente.get("pagina"):
        return "pagina"
    if fuente["sitio"] in compartidos:
        return "google"
    return "sitio"


def leer_directo(fuente: dict, modo: str, ajustes: dict, cache: dict) -> tuple[list, str | None, str]:
    """Trae titulares del propio medio. Devuelve (items, error, via)."""
    timeout, maximo = ajustes["timeout"], ajustes["max_por_fuente"]
    try:
        if modo == "feed":
            return items_de_feed(pedir(fuente["feed"], timeout).content, maximo), None, "rss"

        if modo == "oem":
            url = f"https://oem.com.mx/{fuente['oem']}/policiaca"
            html = pedir(url, timeout).text
            filtro = f"/{fuente['oem']}/policiaca/"
            return items_de_html(html, url, maximo, contiene=filtro), None, "oem"

        if modo == "pagina":
            url = fuente["pagina"]
            return items_de_html(pedir(url, timeout).text, url, maximo), None, "pagina"

        # modo "sitio": RSS ya descubierto en otra corrida, o descubrirlo ahora.
        nombre = fuente["nombre"]
        if cache.get(nombre):
            return items_de_feed(pedir(cache[nombre], timeout).content, maximo), None, "rss"

        try:
            respuesta = pedir(f"https://{fuente['sitio']}/", timeout)
        except (requests.exceptions.ConnectionError, requests.exceptions.SSLError):
            # Algunos sitios pequeños solo responden sin candado (http://).
            respuesta = pedir(f"http://{fuente['sitio']}/", timeout)
        if nombre not in cache:
            candidatos = [rss_anunciado(respuesta.text, respuesta.url)]
            candidatos += [urljoin(respuesta.url, r) for r in ("feed/", "rss/")]
            cache[nombre] = None
            for candidato in filter(None, candidatos):
                try:
                    items = items_de_feed(pedir(candidato, min(timeout, 8)).content, maximo)
                except Exception:  # noqa: BLE001 - un candidato que falla no es error
                    continue
                if len(items) >= 3:  # 1 o 2 entradas suele ser un falso positivo
                    cache[nombre] = candidato
                    return items, None, "rss"
        return items_de_html(respuesta.text, respuesta.url, maximo), None, "home"

    except requests.exceptions.HTTPError as e:
        return [], f"HTTP {e.response.status_code}", modo
    except requests.exceptions.Timeout:
        return [], f"sin respuesta en {timeout}s", modo
    except Exception as e:  # noqa: BLE001
        return [], f"{type(e).__name__}", modo


# --------------------------------------------------------------------------
# Respaldo por Google Noticias
# --------------------------------------------------------------------------

def url_google(sitios: list[str], palabras: list[str], termino: str | None = None) -> str:
    consulta = "(" + " OR ".join(f"site:{s}" for s in sitios) + ")"
    if termino:
        consulta += f' "{termino}"'
    consulta += " (" + " OR ".join(palabras) + ") when:1d"
    return ("https://news.google.com/rss/search?q=" + quote_plus(consulta)
            + "&hl=es-419&gl=MX&ceid=MX:es-419")


def armar_consultas(pendientes: list[dict], compartidos: set[str],
                    palabras: list[str], por_consulta: int) -> list[tuple[str, list[dict]]]:
    """Agrupa los medios pendientes en consultas a Google Noticias.

    - Dominio propio: se juntan varios en una consulta (site:a OR site:b...).
    - Dominio compartido (milenio.com, poresto.net): uno por consulta, con
      el nombre del estado, para no traer la portada nacional del grupo.
    - Diarios OEM: todos viven en oem.com.mx, y Google Noticias no acepta
      rutas en site:. Se hace una consulta por estado ("site:oem.com.mx
      Puebla") y cada nota se asigna al diario por el nombre del medio que
      reporta Google ("El Sol de Puebla").
    """
    consultas, sueltos = [], []
    oem_por_estado: dict[str, list[dict]] = {}
    for f in pendientes:
        if f.get("oem"):
            oem_por_estado.setdefault(f["estado"], []).append(f)
        elif f["sitio"] in compartidos:
            termino = None if f["estado"] == "Nacional" else f["estado"]
            consultas.append((url_google([f["sitio"]], palabras, termino), [f]))
        else:
            sueltos.append(f)
    for i in range(0, len(sueltos), por_consulta):
        grupo = sueltos[i:i + por_consulta]
        consultas.append((url_google([f["sitio"] for f in grupo], palabras), grupo))
    for estado, grupo in oem_por_estado.items():
        termino = None if estado in ("Nacional", "Ciudad de México") else estado
        consultas.append((url_google(["oem.com.mx"], palabras, termino), grupo))
    return consultas


def pedir_google(url: str, timeout: int) -> requests.Response:
    """Consulta a Google Noticias con pausa y reintentos.

    Si se le hacen muchas consultas seguidas, Google responde 429 o 503
    ("demasiadas solicitudes"). Se espera y se reintenta hasta dos veces.
    """
    for espera in (0, 6, 20):
        time.sleep(espera or 0.7)
        try:
            return pedir(url, timeout)
        except requests.exceptions.HTTPError as e:
            if e.response.status_code not in (429, 503) or espera == 20:
                raise
    raise RuntimeError("inalcanzable")


def repartir_google(items: list[dict], grupo: list[dict]) -> dict[str, list]:
    """Asigna cada nota de Google al medio del grupo que la publicó."""
    por_medio: dict[str, list] = {f["nombre"]: [] for f in grupo}
    es_oem = all(f.get("oem") for f in grupo)
    for it in items:
        # Google manda el titular como "Titular - Medio": se separa.
        if " - " in it["titulo"]:
            recorte, _, sufijo = it["titulo"].rpartition(" - ")
            if len(sufijo) < 50 and len(recorte) > 20:
                it["titulo"] = recorte
        it["resumen"] = ""  # el "resumen" de Google es solo el titular repetido
        if es_oem and len(grupo) > 1:
            # Varios diarios OEM en un grupo: se intenta por nombre. Ojo: Google
            # suele reportar todo oem.com.mx como "El Sol de México" (verificado
            # el 22/09/2026), por eso fuentes.yaml deja un solo diario OEM por
            # estado y este caso casi no ocurre.
            medio = normalizar(it.get("origen_titulo", ""))
            for f in grupo:
                if medio and normalizar(f["nombre"]) in medio:
                    por_medio[f["nombre"]].append(it)
                    break
            continue
        if len(grupo) == 1:
            por_medio[grupo[0]["nombre"]].append(it)
            continue
        href = it.get("origen_href", "")
        for f in grupo:
            if f["sitio"] in href:
                por_medio[f["nombre"]].append(it)
                break
        # Si no se sabe de qué medio es, se descarta: no se puede asignar estado.
    return por_medio


# --------------------------------------------------------------------------
# Filtros
# --------------------------------------------------------------------------

FECHA_EN_URL = [
    re.compile(r"/(20\d{2})/(\d{1,2})/(\d{1,2})/"),          # /2026/09/22/
    re.compile(r"/(20\d{2})-(\d{1,2})-(\d{1,2})"),            # /2026-09-22
    re.compile(r"[-_](\d{1,2})[-_](\d{1,2})[-_](20\d{2})"),   # -22-09-2026
]


def fecha_de_url(url: str) -> date | None:
    for i, patron in enumerate(FECHA_EN_URL):
        m = patron.search(url or "")
        if not m:
            continue
        try:
            a, b, c = (int(x) for x in m.groups())
            return date(c, b, a) if i == 2 else date(a, b, c)
        except ValueError:
            return None
    return None


def filtrar_fecha(nombre: str, items: list[dict], historial: dict, hoy: date) -> list[dict]:
    """Deja solo lo de hoy.

    Tres criterios: la fecha del feed; la fecha escrita en la URL; y, para lo
    que no trae fecha (scraping), el historial de ligas ya vistas.

    Arranque en frío: la primera vez que un medio sin fechas responde, todo
    lo que trae su página se da por visto y no entra. Así el primer día no se
    llena el panel con notas de la semana pasada; a partir de la segunda
    corrida entra solo lo que aparece nuevo.
    """
    hoy_iso, ayer_iso = hoy.isoformat(), (hoy - timedelta(days=1)).isoformat()
    primera_vez = nombre not in historial["fuentes"]
    urls = historial["urls"]
    salida = []
    for it in items:
        fecha = None
        if it.get("fecha"):
            fecha = datetime.fromisoformat(it["fecha"]).date()
        fecha = fecha or fecha_de_url(it["url"])
        if fecha is not None:
            if fecha == hoy:
                salida.append(it)
            continue
        vista = urls.get(it["url"])
        if vista and vista < hoy_iso:
            continue
        if primera_vez:
            urls[it["url"]] = ayer_iso
            continue
        salida.append(it)
    if items:
        historial["fuentes"].append(nombre)
    for it in salida:
        urls.setdefault(it["url"], hoy_iso)
    return salida


def compilar_filtro(config: dict):
    incluir = re.compile("|".join(f"(?:{p})" for p in config["filtro"]["incluir"]))
    excluir_lista = config["filtro"].get("excluir") or []
    excluir = re.compile("|".join(f"(?:{p})" for p in excluir_lista)) if excluir_lista else None

    def pasa(it: dict) -> bool:
        texto = normalizar(f"{it['titulo']} {it.get('resumen', '')}")
        if excluir and excluir.search(texto):
            return False
        return bool(incluir.search(texto))
    return pasa


def ya_filtrado(fuente: dict, via: str) -> bool:
    """Lo que viene de una sección policiaca o de Google ya es de seguridad."""
    if via in ("oem", "google"):
        return True
    return via == "pagina" and bool(SECCION_SEGURIDAD.search(fuente.get("pagina", "")))


def intercalar(por_fuente: dict[str, list]) -> list[dict]:
    """Una nota de cada medio por turno, para que ninguno acapare los lotes."""
    mezcla = []
    vueltas = max((len(v) for v in por_fuente.values()), default=0)
    for i in range(vueltas):
        for notas in por_fuente.values():
            if i < len(notas):
                mezcla.append(notas[i])
    return mezcla


# --------------------------------------------------------------------------
# Principal
# --------------------------------------------------------------------------

def main() -> int:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    ajustes, fuentes = config["ajustes"], config["fuentes"]
    compartidos = dominios_compartidos(fuentes)
    cache = cargar_json(CACHE_FEEDS, {})
    hoy = datetime.now(TZ).date()

    # 1. Lectura directa, en paralelo.
    crudos: dict[str, list] = {}
    reporte: dict[str, dict] = {}
    directas = [(f, modo_de(f, compartidos)) for f in fuentes]
    with concurrent.futures.ThreadPoolExecutor(max_workers=ajustes["hilos"]) as pool:
        tareas = {pool.submit(leer_directo, f, m, ajustes, cache): (f, m)
                  for f, m in directas if m != "google"}
        for tarea in concurrent.futures.as_completed(tareas):
            f, m = tareas[tarea]
            items, error, via = tarea.result()
            crudos[f["nombre"]] = [dict(it, via=via) for it in items]
            reporte[f["nombre"]] = {"estado": f["estado"], "modo": m, "via": via,
                                    "directas": len(items), "google": 0, "error": error}
    for f, m in directas:
        if m == "google":
            crudos[f["nombre"]] = []
            reporte[f["nombre"]] = {"estado": f["estado"], "modo": m, "via": "google",
                                    "directas": 0, "google": 0, "error": None}
    guardar_json(CACHE_FEEDS, cache)

    # 2. Respaldo por Google Noticias para lo que no trajo nada.
    if ajustes.get("google_respaldo", True):
        pendientes = [f for f in fuentes if not crudos[f["nombre"]]]
        consultas = armar_consultas(pendientes, compartidos, config["google_palabras"],
                                    ajustes["medios_por_consulta_google"])
        print(f"Google Noticias: {len(consultas)} consultas para {len(pendientes)} medios")

        def consultar(url):
            return items_de_feed(pedir_google(url, ajustes["timeout"]).content, 100)

        with concurrent.futures.ThreadPoolExecutor(max_workers=ajustes["hilos_google"]) as pool:
            tareas = {pool.submit(consultar, url): grupo for url, grupo in consultas}
            for tarea in concurrent.futures.as_completed(tareas):
                grupo = tareas[tarea]
                try:
                    items = tarea.result()
                except Exception as e:  # noqa: BLE001
                    for f in grupo:
                        previo = reporte[f["nombre"]]["error"]
                        reporte[f["nombre"]]["error"] = (f"{previo}; " if previo else "") + \
                            f"Google: {type(e).__name__}"
                    continue
                for nombre, notas in repartir_google(items, grupo).items():
                    ya = {it["url"] for it in crudos[nombre]}
                    nuevas = [dict(it, via="google") for it in notas if it["url"] not in ya]
                    crudos[nombre] = (crudos[nombre] + nuevas)[:ajustes["max_por_fuente"]]
                    reporte[nombre]["google"] = len(crudos[nombre])

    # 3. Filtros de fecha y de palabras.
    por_nombre = {f["nombre"]: f for f in fuentes}
    historial = cargar_json(HISTORIAL, {"urls": {}, "fuentes": []})
    historial.setdefault("urls", {})
    historial.setdefault("fuentes", [])
    pasa = compilar_filtro(config)

    candidatas: dict[str, list] = {}
    vistos: set[str] = set()
    total_hoy = 0
    for nombre, items in crudos.items():
        f = por_nombre[nombre]
        de_hoy = filtrar_fecha(nombre, items, historial, hoy)
        total_hoy += len(de_hoy)
        elegidas = []
        for it in de_hoy:
            if not (ya_filtrado(f, it["via"]) or pasa(it)):
                continue
            it_id = huella(it["url"], it["titulo"])
            if it_id in vistos:
                continue
            vistos.add(it_id)
            elegidas.append({"id": it_id, "fuente": nombre, "estado_fuente": f["estado"],
                             "titulo": it["titulo"], "resumen": it.get("resumen", ""),
                             "url": it["url"], "via": it["via"]})
        candidatas[nombre] = elegidas
        reporte[nombre]["candidatas"] = len(elegidas)

    # Historial: se guardan las fuentes una sola vez y se olvidan las ligas viejas.
    limite = (hoy - timedelta(days=ajustes["dias_historial"])).isoformat()
    historial["urls"] = {u: d for u, d in historial["urls"].items() if d >= limite}
    historial["fuentes"] = sorted(set(historial["fuentes"]))
    guardar_json(HISTORIAL, historial)

    lista = intercalar(candidatas)
    guardar_json(SALIDA, {"generado": datetime.now(TZ).isoformat(),
                          "fecha": hoy.isoformat(), "items": lista})
    guardar_json(REPORTE, {"fecha": hoy.isoformat(), "fuentes": reporte})

    # 4. Resumen para el log de GitHub Actions.
    vivas = [n for n, r in reporte.items() if r["directas"] or r["google"]]
    muertas = sorted(n for n in reporte if n not in vivas)
    por_google = sum(1 for r in reporte.values() if r["google"])
    print(f"\nMedios que respondieron: {len(vivas)} de {len(reporte)} "
          f"({por_google} por Google Noticias)")
    print(f"Titulares de hoy: {total_hoy}. Pasaron el filtro de seguridad: {len(lista)}")
    if muertas:
        print(f"Sin notas en esta corrida ({len(muertas)}):")
        for n in muertas:
            print(f"  ✗ {n} [{reporte[n]['estado']}] {reporte[n]['error'] or 'sin titulares'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
