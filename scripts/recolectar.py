#!/usr/bin/env python3
"""
Toma data/candidatas.json (salida de recolectar.py), le pide a Claude que se
quede solo con las notas de seguridad, las clasifique y las resuma en sus
propias palabras, y regenera index.html.

- Las notas se ACUMULAN durante el día en data/hoy.json y se reinician a
  medianoche (hora del centro de México).
- Cada titular se manda al modelo una sola vez al día: lo ya evaluado,
  se haya quedado o no, no se vuelve a pagar en la siguiente corrida.
- Si varios medios cuentan el mismo hecho, queda una nota con "+N medios".

Requiere el secreto ANTHROPIC_API_KEY (ver README).

    python scripts/curar.py            # corrida normal
    python scripts/curar.py --solo-render   # rehace index.html sin llamar a Claude
"""

from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import os
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

RAIZ = Path(__file__).resolve().parents[1]
CONFIG = RAIZ / "fuentes.yaml"
CANDIDATAS = RAIZ / "data" / "candidatas.json"
REPORTE = RAIZ / "data" / "estado_fuentes.json"
ACUMULADO = RAIZ / "data" / "hoy.json"
PLANTILLA = RAIZ / "templates" / "index_template.html"
SALIDA = RAIZ / "index.html"

TZ = ZoneInfo("America/Mexico_City")

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

# Orden en que aparecen en el panel.
CATEGORIAS = {
    "homicidios": "Homicidios y feminicidios",
    "crimen_organizado": "Crimen organizado",
    "delitos": "Robos, extorsión y otros delitos",
    "desaparecidos": "Desaparecidos y búsqueda",
    "justicia": "Justicia y sentencias",
    "politica_seguridad": "Política de seguridad",
    "accidentes": "Accidentes viales",
}

ESTADOS = [
    "Aguascalientes", "Baja California", "Baja California Sur", "Campeche", "Chiapas",
    "Chihuahua", "Ciudad de México", "Coahuila", "Colima", "Durango", "Estado de México",
    "Guanajuato", "Guerrero", "Hidalgo", "Jalisco", "Michoacán", "Morelos", "Nayarit",
    "Nuevo León", "Oaxaca", "Puebla", "Querétaro", "Quintana Roo", "San Luis Potosí",
    "Sinaloa", "Sonora", "Tabasco", "Tamaulipas", "Tlaxcala", "Veracruz", "Yucatán",
    "Zacatecas",
]


def normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", (texto or "").lower())
    return "".join(c for c in texto if not unicodedata.combining(c)).strip()


ALIAS_ESTADOS = {normalizar(e): e for e in ESTADOS}
ALIAS_ESTADOS.update({
    "cdmx": "Ciudad de México", "df": "Ciudad de México", "mexico": "Estado de México",
    "edomex": "Estado de México", "estado de mexico": "Estado de México",
    "edo. mex.": "Estado de México", "edo. mex": "Estado de México", "edo mex": "Estado de México",
    "ciudad de mexico (cdmx)": "Ciudad de México", "slp": "San Luis Potosí",
    "q. roo": "Quintana Roo", "qroo": "Quintana Roo",
    "coahuila de zaragoza": "Coahuila", "michoacan de ocampo": "Michoacán",
    "veracruz de ignacio de la llave": "Veracruz", "queretaro de arteaga": "Querétaro",
    "nl": "Nuevo León", "bc": "Baja California", "bcs": "Baja California Sur",
    "nacional": "Nacional", "mexico (nacional)": "Nacional",
})


def estado_canonico(valor: str, respaldo: str) -> str:
    return ALIAS_ESTADOS.get(normalizar(valor), respaldo if respaldo else "Nacional")


# --------------------------------------------------------------------------
# Instrucciones para el modelo
# --------------------------------------------------------------------------

SISTEMA = """Eres editor de un panel de monitoreo de SEGURIDAD en México. Recibes titulares \
recientes de medios nacionales y locales. Cada línea trae un número, el medio, el estado que \
cubre ese medio y el titular (a veces con un extracto).

Tu trabajo: quedarte SOLO con notas de seguridad e inseguridad ocurridas en México, o que \
involucren a grupos criminales o autoridades mexicanas en el extranjero (extradiciones, \
juicios en EE.UU., detenciones de líderes). Clasifícalas en UNA de estas categorías:

- homicidios: asesinatos, feminicidios, ataques armados con víctimas, hallazgo de cuerpos, \
multihomicidios, cifras de homicidios.
- crimen_organizado: cárteles, células, enfrentamientos, narcobloqueos, aseguramientos de \
droga o armas, laboratorios, huachicol, detenciones de líderes u operadores.
- delitos: robo, asalto, extorsión, cobro de piso, secuestro, fraude, despojo, violencia \
sexual, trata, violencia familiar, riñas y detenciones por delitos comunes.
- desaparecidos: personas desaparecidas o no localizadas, fichas de búsqueda, fosas, \
colectivos y madres buscadoras, identificación de restos.
- justicia: sentencias, vinculaciones a proceso, audiencias, juicios, liberaciones, \
extradiciones, actuación de fiscalías y jueces en casos concretos.
- politica_seguridad: estrategias y operativos de gobierno, presupuesto de seguridad, \
nombramientos o destituciones en seguridad y fiscalías, Guardia Nacional y Fuerzas Armadas, \
cifras oficiales, reformas penales, condiciones y depuración de policías.
- accidentes: accidentes viales, choques, volcaduras, atropellamientos, percances de tránsito \
con lesionados o muertos.

DESCARTA: espectáculos, series o películas de crimen, deportes (salvo violencia real), clima \
y desastres naturales, incendios sin delito, ciberseguridad o "seguridad" de productos o \
apps, seguridad social/IMSS, columnas de opinión, notas internacionales sin vínculo con \
México, horóscopos y virales.

REGLAS:
1. Si varias líneas cuentan el mismo hecho, devuelve una sola (la del medio más local o la \
más clara) y pon en "repetidas" los números de las demás.
2. "estado": la entidad donde OCURRIÓ el hecho, con su nombre corto oficial ("Ciudad de \
México", "Estado de México", "Nuevo León", "Michoacán"...). Si el hecho es de alcance \
nacional o no hay un estado claro, "Nacional". No asumas que el hecho ocurrió en el estado \
del medio si el titular dice otro lugar.
3. "municipio": ciudad o municipio si el titular o el extracto lo dicen; si no, "".
4. "titulo": máximo 12 palabras. "resumen": máximo 25 palabras. SIEMPRE EN TUS PROPIAS \
PALABRAS: nunca copies el titular ni frases textuales de la fuente. No agregues datos que \
no estén en el titular o el extracto.
5. "relevancia": "alta" si hay varias víctimas, funcionarios o figuras públicas, un patrón o \
cifra, o una decisión de autoridad con impacto; "media" en los demás casos.
6. Responde ÚNICAMENTE con JSON válido, sin markdown ni texto adicional, sin saltos de línea \
dentro de los textos, con las comillas dobles internas escapadas.

Formato exacto:
{"notas": [{"n": 3, "categoria": "homicidios", "estado": "Sinaloa", "municipio": "Culiacán", \
"titulo": "...", "resumen": "...", "relevancia": "alta", "repetidas": [7, 12]}]}

Si ninguna línea es de seguridad: {"notas": []}"""


def prompt_de_lote(lote: list[dict]) -> str:
    lineas = []
    for n, it in enumerate(lote, start=1):
        linea = f"{n}. [{it['fuente']} / {it['estado_fuente']}] {it['titulo']}"
        extracto = (it.get("resumen") or "").strip()
        if extracto and normalizar(extracto[:60]) not in normalizar(it["titulo"]):
            linea += f" — {extracto[:180]}"
        lineas.append(linea)
    return "Titulares:\n" + "\n".join(lineas)


def extraer_json(texto: str) -> dict:
    """Parsea la respuesta; si llegó cortada, la recorta al último objeto completo."""
    texto = re.sub(r"^```(json)?|```$", "", texto.strip()).strip()
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        pass
    ultimo = texto.rfind("}")
    if ultimo == -1:
        return {"notas": []}
    recortado = texto[:ultimo + 1]
    pila, en_cadena, escape = [], False, False
    for ch in recortado:
        if escape:
            escape = False
        elif ch == "\\":
            escape = True
        elif ch == '"':
            en_cadena = not en_cadena
        elif not en_cadena and ch in "{[":
            pila.append(ch)
        elif not en_cadena and ch in "}]" and pila:
            pila.pop()
    cierre = "".join("}" if c == "{" else "]" for c in reversed(pila))
    try:
        return json.loads(recortado + cierre)
    except json.JSONDecodeError:
        return {"notas": []}


def curar_lote(cliente, ajustes: dict, lote: list[dict]) -> tuple[list[dict], dict]:
    respuesta = cliente.messages.create(
        model=ajustes["modelo"],
        max_tokens=ajustes["max_tokens_ia"],
        system=SISTEMA,
        messages=[{"role": "user", "content": prompt_de_lote(lote)}],
    )
    texto = "".join(b.text for b in respuesta.content if getattr(b, "type", "") == "text")
    uso = {"entrada": respuesta.usage.input_tokens, "salida": respuesta.usage.output_tokens,
           "cortada": respuesta.stop_reason == "max_tokens"}

    notas = []
    for nota in extraer_json(texto).get("notas", []):
        try:
            item = lote[int(nota["n"]) - 1]
        except (KeyError, ValueError, TypeError, IndexError):
            continue
        categoria = nota.get("categoria", "")
        if categoria not in CATEGORIAS:
            continue
        otras = []
        for r in nota.get("repetidas") or []:
            try:
                otras.append(lote[int(r) - 1]["fuente"])
            except (ValueError, TypeError, IndexError):
                pass
        notas.append({
            "id": item["id"],
            "categoria": categoria,
            "estado": estado_canonico(nota.get("estado", ""), item["estado_fuente"]),
            "municipio": (nota.get("municipio") or "").strip(),
            "titulo": (nota.get("titulo") or "").strip(),
            "resumen": (nota.get("resumen") or "").strip(),
            "relevancia": "alta" if nota.get("relevancia") == "alta" else "media",
            "fuente": item["fuente"],
            "url": item["url"],
            "titulo_original": item["titulo"],
            "otras_fuentes": sorted(set(otras) - {item["fuente"]}),
        })
    return [n for n in notas if n["titulo"]], uso


# --------------------------------------------------------------------------
# Acumulado del día
# --------------------------------------------------------------------------

VACIAS = set("de la el los las del en y a un una por con para al se que su sus tras sin "
             "es son fue hay no o lo le les mas ya".split())


def palabras(titulo: str) -> set[str]:
    return {p for p in re.findall(r"[a-z0-9ñ]+", normalizar(titulo))
            if len(p) > 2 and p not in VACIAS}


def es_misma_historia(a: dict, b: dict) -> bool:
    """Dos notas del mismo estado cuyos titulares originales se parecen mucho."""
    if a["estado"] != b["estado"]:
        return False
    pa, pb = palabras(a["titulo_original"]), palabras(b["titulo_original"])
    if not pa or not pb:
        return False
    return len(pa & pb) / len(pa | pb) >= 0.5


def cargar_acumulado(hoy: str) -> dict:
    try:
        datos = json.loads(ACUMULADO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        datos = {}
    if datos.get("fecha") != hoy:
        if datos:
            print(f"Nuevo día ({datos.get('fecha')} -> {hoy}): se reinicia el panel.")
        return {"fecha": hoy, "corridas": 0, "evaluadas": [], "notas": []}
    return datos


def integrar(acumulado: dict, nuevas: list[dict], ahora: str, max_por_estado: int) -> int:
    notas = acumulado["notas"]
    por_id = {n["id"] for n in notas}
    agregadas = 0
    for nueva in nuevas:
        if nueva["id"] in por_id:
            continue
        gemela = next((n for n in notas if es_misma_historia(n, nueva)), None)
        if gemela:
            fuentes = set(gemela.get("otras_fuentes", [])) | {nueva["fuente"]}
            fuentes |= set(nueva.get("otras_fuentes", []))
            gemela["otras_fuentes"] = sorted(fuentes - {gemela["fuente"]})
            if nueva["relevancia"] == "alta":
                gemela["relevancia"] = "alta"
            continue
        nueva["agregada"] = ahora
        notas.append(nueva)
        por_id.add(nueva["id"])
        agregadas += 1

    # Tope por estado: primero relevancia alta, luego lo más reciente.
    notas.sort(key=lambda n: (n["relevancia"] == "alta", n.get("agregada", "")), reverse=True)
    conteo: dict[str, int] = {}
    recortadas = []
    for n in notas:
        conteo[n["estado"]] = conteo.get(n["estado"], 0) + 1
        if conteo[n["estado"]] <= max_por_estado:
            recortadas.append(n)
    # En el panel, lo más reciente arriba.
    recortadas.sort(key=lambda n: n.get("agregada", ""), reverse=True)
    acumulado["notas"] = recortadas
    return agregadas


# --------------------------------------------------------------------------
# Página
# --------------------------------------------------------------------------

def fecha_legible(momento: datetime) -> str:
    return (f"{momento.day} de {MESES[momento.month - 1]} de {momento.year}, "
            f"{momento:%H:%M} (hora del centro de México)")


def render(acumulado: dict, ahora: datetime) -> str:
    e = html.escape
    notas = acumulado["notas"]

    stats = "\n".join(
        f'<div class="stat" data-cat="{cat}"><p>{e(etq)}</p>'
        f'<p class="num">{sum(1 for n in notas if n["categoria"] == cat)}</p></div>'
        for cat, etq in CATEGORIAS.items())

    conteo_estados: dict[str, int] = {}
    for n in notas:
        conteo_estados[n["estado"]] = conteo_estados.get(n["estado"], 0) + 1
    opciones = [f'<option value="">Todo el país ({len(notas)})</option>']
    for estado in ["Nacional"] + ESTADOS:
        if estado in conteo_estados:
            etiqueta = "Alcance nacional" if estado == "Nacional" else estado
            opciones.append(f'<option value="{e(estado)}">{e(etiqueta)} '
                            f'({conteo_estados[estado]})</option>')

    bloques = []
    for cat, etiqueta in CATEGORIAS.items():
        tarjetas = []
        for n in (x for x in notas if x["categoria"] == cat):
            lugar = ", ".join(p for p in (n["municipio"], n["estado"]) if p and p != "Nacional")
            lugar = lugar or "Alcance nacional"
            extra = ""
            if n.get("otras_fuentes"):
                k = len(n["otras_fuentes"])
                extra = (f' · <span title="{e(", ".join(n["otras_fuentes"]))}">'
                         f'+{k} medio{"s" if k > 1 else ""}</span>')
            marca = ' <span class="alta-tag">Relevante</span>' if n["relevancia"] == "alta" else ""
            tarjetas.append(
                f'<div class="card {cat}" data-estado="{e(n["estado"])}">\n'
                f'  <h3><a href="{e(n["url"])}" target="_blank" rel="noopener">'
                f'{e(n["titulo"])}</a>{marca}</h3>\n'
                f'  <p>{e(n["resumen"])}</p>\n'
                f'  <p class="src">{e(n["fuente"])} · {e(lugar)}{extra}</p>\n'
                f'</div>')
        bloques.append(
            f'<section data-cat="{cat}">\n<div class="section-title">'
            f'<span class="dot {cat}-bg"></span><h2>{e(etiqueta)}</h2></div>\n'
            + "\n".join(tarjetas)
            + '\n<p class="vacio">Sin notas por ahora.</p>\n</section>')

    reporte = {}
    try:
        reporte = json.loads(REPORTE.read_text(encoding="utf-8")).get("fuentes", {})
    except (OSError, ValueError):
        pass
    vivas = sum(1 for r in reporte.values() if r.get("directas") or r.get("google"))
    cobertura = f"{vivas} de {len(reporte)} medios respondieron en la última revisión." \
        if reporte else ""

    pagina = PLANTILLA.read_text(encoding="utf-8")
    return (pagina.replace("{{FECHA}}", e(fecha_legible(ahora)))
                  .replace("{{STATS}}", stats)
                  .replace("{{OPCIONES}}", "\n".join(opciones))
                  .replace("{{CARDS}}", "\n".join(bloques))
                  .replace("{{COBERTURA}}", e(cobertura)))


# --------------------------------------------------------------------------
# Principal
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--solo-render", action="store_true",
                        help="rehace index.html con lo acumulado, sin llamar a Claude")
    args = parser.parse_args()

    ajustes = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))["ajustes"]
    ahora = datetime.now(TZ)
    acumulado = cargar_acumulado(ahora.date().isoformat())

    todo_fallo = None
    if not args.solo_render:
        candidatas = json.loads(CANDIDATAS.read_text(encoding="utf-8"))["items"]
        evaluadas = set(acumulado["evaluadas"])
        pendientes = [c for c in candidatas if c["id"] not in evaluadas]
        print(f"Candidatas: {len(candidatas)}; nuevas para evaluar: {len(pendientes)}")

        if pendientes:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise SystemExit("Falta el secreto ANTHROPIC_API_KEY (ver README, paso 3).")
            import anthropic
            cliente = anthropic.Anthropic(max_retries=3)
            tam = ajustes["lote_ia"]
            lotes = [pendientes[i:i + tam] for i in range(0, len(pendientes), tam)]

            nuevas, fallidos, ultimo_error = [], 0, ""
            tokens_in = tokens_out = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=ajustes["hilos_ia"]) as pool:
                tareas = {pool.submit(curar_lote, cliente, ajustes, lote): lote for lote in lotes}
                for tarea in concurrent.futures.as_completed(tareas):
                    lote = tareas[tarea]
                    try:
                        notas, uso = tarea.result()
                    except Exception as e:  # noqa: BLE001
                        # Ese lote no se marca como evaluado: se reintenta en la próxima corrida.
                        print(f"[aviso] falló un lote de {len(lote)}: {type(e).__name__}: {e}")
                        fallidos += 1
                        ultimo_error = f"{type(e).__name__}: {e}"
                        continue
                    tokens_in += uso["entrada"]
                    tokens_out += uso["salida"]
                    if uso["cortada"]:
                        print("[aviso] una respuesta se cortó; baja lote_ia en fuentes.yaml")
                    nuevas.extend(notas)
                    evaluadas.update(c["id"] for c in lote)

            agregadas = integrar(acumulado, nuevas, ahora.isoformat(), ajustes["max_por_estado"])
            acumulado["evaluadas"] = sorted(evaluadas)
            acumulado["corridas"] += 1
            costo = tokens_in / 1e6 * 1 + tokens_out / 1e6 * 5  # tarifa de Haiku 4.5, USD
            print(f"Claude eligió {len(nuevas)} notas de seguridad; nuevas en el panel: "
                  f"{agregadas}. Tokens: {tokens_in:,} entrada / {tokens_out:,} salida "
                  f"(~{costo:.3f} USD). Lotes fallidos: {fallidos}")
            if fallidos == len(lotes):
                todo_fallo = ultimo_error

    acumulado["actualizado"] = ahora.isoformat()
    ACUMULADO.parent.mkdir(parents=True, exist_ok=True)
    ACUMULADO.write_text(json.dumps(acumulado, ensure_ascii=False, indent=1), encoding="utf-8")
    SALIDA.write_text(render(acumulado, ahora), encoding="utf-8")

    for cat, etiqueta in CATEGORIAS.items():
        print(f"  {etiqueta}: {sum(1 for n in acumulado['notas'] if n['categoria'] == cat)}")
    print(f"index.html regenerado: {len(acumulado['notas'])} notas acumuladas hoy.")
    if todo_fallo:
        # Sale en rojo para que se note en la pestaña Actions. Los titulares no
        # se marcaron como evaluados: se reintentan en la siguiente corrida.
        print("\nERROR: fallaron TODAS las llamadas a Claude, el panel no recibió notas nuevas.")
        print(f"Motivo: {todo_fallo}")
        print("Revisa el secreto ANTHROPIC_API_KEY, el saldo y los límites en console.anthropic.com.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
