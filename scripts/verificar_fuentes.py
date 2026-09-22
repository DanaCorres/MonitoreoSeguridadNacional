#!/usr/bin/env python3
"""Revisa uno por uno los medios de fuentes.yaml y dice cuáles responden.

No toca el panel: solo imprime un reporte por estado. Úsalo la primera vez y
cada vez que agregues medios.

    python scripts/verificar_fuentes.py
    python scripts/verificar_fuentes.py --estado Nayarit   # solo un estado

Qué significa cada marca:
  ✓  el medio responde por su propio sitio (RSS, sección o home)
  ~  el propio sitio no respondió, pero sí aparece en Google Noticias
  ✗  no se encontró nada por ninguna vía: revisa el dominio o quítalo
"""

from __future__ import annotations

import argparse
import concurrent.futures
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from recolectar import (CONFIG, armar_consultas, dominios_compartidos,  # noqa: E402
                        items_de_feed, leer_directo, modo_de, pedir_google,
                        repartir_google)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--estado", help="revisar solo un estado")
    args = parser.parse_args()

    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    ajustes = config["ajustes"]
    todas = config["fuentes"]
    compartidos = dominios_compartidos(todas)
    fuentes = [f for f in todas if not args.estado or f["estado"] == args.estado]

    resultado = {f["nombre"]: {"directas": 0, "google": 0, "via": "", "error": None,
                               "error_google": None, "modo": modo_de(f, compartidos)}
                 for f in fuentes}

    # 1. Sitio propio
    cache: dict = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=ajustes["hilos"]) as pool:
        tareas = {}
        for f in fuentes:
            modo = resultado[f["nombre"]]["modo"]
            if modo != "google":
                tareas[pool.submit(leer_directo, f, modo, ajustes, cache)] = f
        for tarea in concurrent.futures.as_completed(tareas):
            f = tareas[tarea]
            items, error, via = tarea.result()
            resultado[f["nombre"]].update(directas=len(items), via=via, error=error)

    # 2. Google Noticias para lo que no respondió. Aquí se busca SIN palabras
    #    de seguridad y en los últimos 7 días: la pregunta es si el medio
    #    existe en Google Noticias, no si publicó nota roja hoy.
    pendientes = [f for f in fuentes if not resultado[f["nombre"]]["directas"]]
    consultas = armar_consultas(pendientes, compartidos, ["noticias", "México"],
                                ajustes["medios_por_consulta_google"])
    consultas = [(u.replace("when%3A1d", "when%3A7d"), g) for u, g in consultas]
    medios_oem_vistos: set[str] = set()
    print(f"Consultando Google Noticias: {len(consultas)} consultas para "
          f"{len(pendientes)} medios...")

    def consultar(url):
        return items_de_feed(pedir_google(url, ajustes["timeout"]).content, 100)

    with concurrent.futures.ThreadPoolExecutor(max_workers=ajustes["hilos_google"]) as pool:
        tareas = {pool.submit(consultar, u): g for u, g in consultas}
        for tarea in concurrent.futures.as_completed(tareas):
            grupo = tareas[tarea]
            try:
                items = tarea.result()
            except Exception as e:  # noqa: BLE001
                detalle = (f"HTTP {e.response.status_code}" if hasattr(e, "response")
                           and e.response is not None else type(e).__name__)
                for f in grupo:
                    resultado[f["nombre"]]["error_google"] = detalle
                continue
            if all(f.get("oem") for f in grupo):
                medios_oem_vistos.update(it.get("origen_titulo", "") for it in items)
            for nombre, notas in repartir_google(items, grupo).items():
                resultado[nombre]["google"] += len(notas)

    # 3. Reporte por estado
    ok = respaldo = caidos = 0
    estado_actual = None
    for f in sorted(fuentes, key=lambda x: (x["estado"] != "Nacional", x["estado"], x["nombre"])):
        if f["estado"] != estado_actual:
            estado_actual = f["estado"]
            print(f"\n{estado_actual}")
        r = resultado[f["nombre"]]
        etiqueta = " (propuesta sin verificar)" if f.get("origen") == "nuevo" else ""
        if r["directas"]:
            ok += 1
            print(f"  ✓ {f['nombre']}{etiqueta}: {r['directas']} titulares vía {r['via']}")
        elif r["google"]:
            respaldo += 1
            if r["modo"] == "google":
                causa = "diario OEM" if f.get("oem") else (f.get("nota") or "dominio compartido")
            else:
                causa = r["error"] or "sin titulares en su sitio"
            print(f"  ~ {f['nombre']}{etiqueta}: {r['google']} vía Google Noticias ({causa})")
        else:
            caidos += 1
            partes = []
            if r["modo"] != "google":
                partes.append(f"sitio: {r['error'] or 'sin titulares'}")
            partes.append(f"Google: {r['error_google'] or 'sin notas en 7 días'}")
            print(f"  ✗ {f['nombre']}{etiqueta}: {'; '.join(partes)} — {f['sitio']}")

    if medios_oem_vistos:
        print("\nNombres de medio que Google reporta en oem.com.mx (para revisar la"
              " asignación de diarios OEM):")
        for m in sorted(filter(None, medios_oem_vistos)):
            print(f"  · {m}")

    print(f"\nTotal: {ok} por su sitio, {respaldo} solo por Google, {caidos} sin respuesta "
          f"(de {len(fuentes)}).")
    if caidos:
        print("En los ✗: si Google dice 'sin notas en 7 días', el medio no existe en Google"
              " Noticias o el dominio está mal. Si dice HTTP 429 o 503, Google limitó las"
              " consultas: vuelve a correr la verificación más tarde.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
