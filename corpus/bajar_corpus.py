#!/usr/bin/env python3
"""
Baja el archivo de posts de una cuenta de X usando twitterapi.io.

Sortea el tope de ~3.200 posts del timeline partiendo el tiempo en ventanas
mensuales y paginando cada ventana por cursor. Guarda los objetos crudos tal
como los devuelve la API: cuando cambies de idea sobre qué campo te importa,
no querés volver a pagar la bajada.

Uso tipico (los 1500 posts mas recientes, sin saber cuando empezo la cuenta):

    export TWITTERAPI_IO_KEY="tu_api_key"
    python bajar_corpus.py --user USUARIO --ultimos 1500

Archivo historico completo, en orden cronologico:

    python bajar_corpus.py --user USUARIO --desde 2011-03 --hasta 2026-07 --prueba
    python bajar_corpus.py --user USUARIO --desde 2011-03 --hasta 2026-07

Salida:
    corpus_<usuario>.jsonl        un objeto JSON por linea, crudo
    corpus_<usuario>.state.json   ventanas completadas (permite reanudar)

Si el proceso se corta, volve a lanzarlo con los mismos argumentos: saltea las
ventanas ya hechas y no duplica nada.

Requiere: pip install requests
"""

import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

import requests

ENDPOINT = "https://api.twitterapi.io/twitter/tweet/advanced_search"

# Tarifa publicada al momento de escribir esto. Confirmala en twitterapi.io/pricing:
# el script solo la usa para estimar el gasto, no para nada operativo.
PRECIO_POR_TWEET_USD = 0.00015

# Freno de seguridad por ventana. Un mes de una cuenta muy prolifica no deberia
# pasar de unas pocas decenas de paginas; si se dispara, algo anda mal.
MAX_PAGINAS_POR_VENTANA = 500

# Marzo de 2006: no hay posts anteriores a la existencia de la plataforma.
PISO_HISTORICO = "2006-03"

# Caminando hacia atras, cuantos meses vacios seguidos toleramos antes de asumir
# que llegamos al principio de la cuenta. Ajustable con --meses-vacios.
MESES_VACIOS_PARA_CORTAR = 36


def ventanas_mensuales(desde: str, hasta: str, orden: str = "asc"):
    """Genera pares (inicio, fin) mensuales. `fin` es exclusivo. Formato: YYYY-MM."""
    try:
        anio, mes = map(int, desde.split("-"))
        anio_f, mes_f = map(int, hasta.split("-"))
    except ValueError:
        sys.exit("Las fechas van en formato YYYY-MM (ejemplo: 2011-03)")

    if (anio, mes) > (anio_f, mes_f):
        sys.exit("--desde tiene que ser anterior o igual a --hasta")

    meses = []
    while (anio, mes) <= (anio_f, mes_f):
        inicio = date(anio, mes, 1)
        fin = date(anio + 1, 1, 1) if mes == 12 else date(anio, mes + 1, 1)
        meses.append((inicio, fin))
        anio, mes = (anio + 1, 1) if mes == 12 else (anio, mes + 1)

    if orden == "desc":
        meses.reverse()
    return meses


def pedir(sesion, headers, params, intentos=5):
    """GET con reintentos y backoff exponencial ante 429 y errores de servidor."""
    espera = 2
    for intento in range(intentos):
        try:
            r = sesion.get(ENDPOINT, headers=headers, params=params, timeout=30)
        except requests.RequestException as e:
            if intento == intentos - 1:
                raise
            print(f"\n    error de red ({e}); reintento en {espera}s", flush=True)
            time.sleep(espera)
            espera *= 2
            continue

        if r.status_code == 200:
            return r.json()

        if r.status_code in (429, 500, 502, 503, 504):
            if intento == intentos - 1:
                r.raise_for_status()
            print(f"\n    HTTP {r.status_code}; reintento en {espera}s", flush=True)
            time.sleep(espera)
            espera *= 2
            continue

        # 401, 403, 400: reintentar no arregla nada. Cortamos con el mensaje del server.
        print(f"\n    HTTP {r.status_code}: {r.text[:400]}", flush=True)
        r.raise_for_status()

    raise RuntimeError("se agotaron los reintentos")


def bajar_ventana(sesion, headers, consulta_base, inicio, fin, vistos, salida, tope=None):
    """Recorre una ventana completa paginando por cursor. Devuelve cuantos posts nuevos guardo."""
    consulta = f"{consulta_base} since:{inicio.isoformat()} until:{fin.isoformat()}"
    cursor = None
    nuevos = 0
    paginas = 0

    while True:
        params = {"query": consulta, "queryType": "Latest"}
        if cursor:
            params["cursor"] = cursor

        data = pedir(sesion, headers, params)
        lote = data.get("tweets") or []

        for t in lote:
            tid = t.get("id")
            if tid is None:
                continue
            tid = str(tid)
            if tid in vistos:
                continue
            vistos.add(tid)
            salida.write(json.dumps(t, ensure_ascii=False) + "\n")
            nuevos += 1

        paginas += 1

        # Con --ultimos no hace falta terminar la ventana si ya juntamos de sobra.
        if tope is not None and len(vistos) >= tope:
            break

        if not data.get("has_next_page") or not data.get("next_cursor"):
            break
        cursor = data["next_cursor"]

        if paginas >= MAX_PAGINAS_POR_VENTANA:
            print(f"\n    aviso: {inicio:%Y-%m} llego al tope de paginas", flush=True)
            break

        time.sleep(0.2)

    salida.flush()
    os.fsync(salida.fileno())
    return nuevos


def cargar_vistos(ruta: Path):
    """Levanta los IDs ya guardados para no duplicar al reanudar."""
    vistos = set()
    if not ruta.exists():
        return vistos
    with ruta.open(encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea:
                continue
            try:
                tid = json.loads(linea).get("id")
            except json.JSONDecodeError:
                continue
            if tid is not None:
                vistos.add(str(tid))
    return vistos


def mes_actual() -> str:
    hoy = date.today()
    return f"{hoy.year:04d}-{hoy.month:02d}"


def main():
    ap = argparse.ArgumentParser(
        description="Baja el corpus de posts de una cuenta publica de X."
    )
    ap.add_argument("--user", required=True, help="handle sin @")
    ap.add_argument("--desde", help="YYYY-MM del primer post (default: 2006-03)")
    ap.add_argument("--hasta", help="YYYY-MM final, inclusive (default: mes actual)")
    ap.add_argument("--ultimos", type=int, metavar="N",
                    help="baja los N posts mas recientes caminando hacia atras mes a mes; "
                         "no necesita saber cuando empezo la cuenta")
    ap.add_argument("--meses-vacios", type=int, default=MESES_VACIOS_PARA_CORTAR,
                    help="con --ultimos: corta tras esta cantidad de meses vacios seguidos "
                         f"(default: {MESES_VACIOS_PARA_CORTAR})")
    ap.add_argument("--sin-respuestas", action="store_true",
                    help="excluye respuestas (por defecto vienen incluidas)")
    ap.add_argument("--dir", default=".", help="carpeta de salida")
    ap.add_argument("--prueba", action="store_true",
                    help="baja solo la primera ventana pendiente y corta")
    args = ap.parse_args()

    key = os.environ.get("TWITTERAPI_IO_KEY")
    if not key:
        sys.exit("Falta la variable de entorno TWITTERAPI_IO_KEY")

    usuario = args.user.lstrip("@")
    consulta_base = f"from:{usuario}"
    if args.sin_respuestas:
        consulta_base += " -filter:replies"

    desde = args.desde or PISO_HISTORICO
    hasta = args.hasta or mes_actual()
    orden = "desc" if args.ultimos else "asc"
    tope = args.ultimos

    carpeta = Path(args.dir)
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta_jsonl = carpeta / f"corpus_{usuario}.jsonl"
    ruta_estado = carpeta / f"corpus_{usuario}.state.json"

    completadas = set()
    if ruta_estado.exists():
        try:
            completadas = set(json.loads(ruta_estado.read_text(encoding="utf-8"))["ventanas"])
        except (json.JSONDecodeError, KeyError):
            print("aviso: archivo de estado ilegible, empiezo de cero")

    vistos = cargar_vistos(ruta_jsonl)
    if vistos or completadas:
        print(f"Reanudando: {len(vistos)} posts guardados, "
              f"{len(completadas)} ventanas completas.\n")

    if tope and len(vistos) >= tope:
        print(f"Ya hay {len(vistos)} posts guardados, que cubre el tope de {tope}. "
              f"No hago ningun pedido a la API.")
        print(f"\nArchivo: {ruta_jsonl}")
        return

    headers = {"X-API-Key": key}
    sesion = requests.Session()
    nuevos_totales = 0
    vacios_seguidos = 0

    try:
        with ruta_jsonl.open("a", encoding="utf-8") as salida:
            for inicio, fin in ventanas_mensuales(desde, hasta, orden):
                etiqueta = inicio.strftime("%Y-%m")
                if etiqueta in completadas:
                    continue

                print(f"  {etiqueta} ...", end=" ", flush=True)
                n = bajar_ventana(sesion, headers, consulta_base,
                                  inicio, fin, vistos, salida, tope)
                nuevos_totales += n
                print(f"{n:>5} posts   (total {len(vistos)})", flush=True)

                completadas.add(etiqueta)
                ruta_estado.write_text(
                    json.dumps({"ventanas": sorted(completadas)}, indent=2),
                    encoding="utf-8",
                )

                if args.prueba:
                    print("\nModo prueba: corto aca. Revisa el JSONL antes de seguir.")
                    break

                if tope is not None and len(vistos) >= tope:
                    print(f"\nLlegue al tope de {tope} posts. Corto aca.")
                    break

                # Caminando hacia atras, una racha larga de meses vacios significa
                # que pasamos el primer post de la cuenta.
                if tope is not None:
                    vacios_seguidos = vacios_seguidos + 1 if n == 0 else 0
                    if vacios_seguidos >= args.meses_vacios:
                        print(f"\n{vacios_seguidos} meses seguidos sin posts: "
                              f"asumo que llegue al principio de la cuenta. Corto aca.")
                        break

                time.sleep(0.3)

    except KeyboardInterrupt:
        print("\n\nInterrumpido. El progreso quedo guardado: "
              "volve a lanzarlo con los mismos argumentos para seguir.")

    print(f"\nArchivo: {ruta_jsonl}")
    print(f"Posts totales: {len(vistos)}")
    print(f"Bajados en esta corrida: {nuevos_totales}")
    print(f"Costo estimado de esta corrida: USD {nuevos_totales * PRECIO_POR_TWEET_USD:.2f}")


if __name__ == "__main__":
    main()
