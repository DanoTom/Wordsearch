#!/usr/bin/env python3
"""
Descubre cuentas parecidas a una cuenta de referencia, por tres canales.

    python descubrir_cuentas.py --user USUARIO --jsonl corpus_USUARIO.jsonl

Los tres canales, porque ninguno alcanza solo:

    seguidos    a quien sigue la cuenta de referencia. Señal fuerte de afinidad,
                pero mezcla su vida entera (amigos, prensa, humor, futbol).
    menciones   con quien conversa realmente, sacado del corpus ya bajado.
                Gratis: no cuesta un solo pedido a la API.
    busqueda    autores que escriben sobre los mismos temas SIN estar en su red.
                Es el unico canal que sale de la burbuja, y por eso el que mas
                aporta cuando se buscan cuentas "parecidas pero distintas".

Guarda los perfiles crudos tal como los devuelve la API, con el canal por el que
aparecio cada uno. El ranking se hace despues, en rankear_cuentas.py: descubrir
cuesta plata, puntuar no.

Salida:
    candidatos_<usuario>.jsonl        un perfil crudo por linea

Requiere: pip install requests
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

BASE = "https://api.twitterapi.io/twitter"

# Tarifas publicadas al momento de escribir esto (confirmalas en
# twitterapi.io/pricing). Solo se usan para estimar el gasto.
PRECIO_POR_TWEET_USD = 0.00015
PRECIO_POR_PERFIL_USD = 0.00018

MAX_PAGINAS = 60


def pedir(sesion, headers, ruta, params, intentos=5):
    """GET con reintentos y backoff ante 429 y errores de servidor."""
    espera = 2
    for intento in range(intentos):
        try:
            r = sesion.get(f"{BASE}/{ruta}", headers=headers, params=params, timeout=30)
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

        print(f"\n    HTTP {r.status_code}: {r.text[:300]}", flush=True)
        r.raise_for_status()

    raise RuntimeError("se agotaron los reintentos")


def traer_seguidos(sesion, headers, usuario):
    """Todas las cuentas que sigue la cuenta de referencia, paginando por cursor."""
    perfiles, cursor, paginas = [], None, 0
    while True:
        params = {"userName": usuario, "pageSize": 200}
        if cursor:
            params["cursor"] = cursor
        data = pedir(sesion, headers, "user/followings", params)
        lote = data.get("followings") or []
        perfiles.extend(lote)
        paginas += 1
        print(f"    pagina {paginas}: {len(lote)} (total {len(perfiles)})", flush=True)
        if not data.get("has_next_page") or not data.get("next_cursor"):
            break
        cursor = data["next_cursor"]
        if paginas >= MAX_PAGINAS:
            print("    aviso: tope de paginas", flush=True)
            break
        time.sleep(0.2)
    return perfiles


def handles_mencionados(ruta_jsonl):
    """Handles con los que conversa la cuenta, sacados del corpus ya bajado."""
    from collections import Counter
    cuenta = Counter()
    if not ruta_jsonl or not Path(ruta_jsonl).exists():
        return cuenta
    with open(ruta_jsonl, encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea:
                continue
            try:
                o = json.loads(linea)
            except json.JSONDecodeError:
                continue
            for m in (o.get("entities") or {}).get("user_mentions") or []:
                if m.get("screen_name"):
                    cuenta[m["screen_name"].lower()] += 1
            if o.get("inReplyToUsername"):
                cuenta[o["inReplyToUsername"].lower()] += 1
    return cuenta


def autores_por_busqueda(sesion, headers, consultas, paginas_por_consulta):
    """
    Autores que escriben sobre los temas, esten o no en la red de la cuenta.

    Guardamos el perfil que viene incrustado en cada tweet: la API ya lo manda
    completo, asi que no hace falta un pedido aparte por cuenta.
    """
    perfiles, tweets_vistos = {}, 0
    for consulta in consultas:
        cursor, paginas = None, 0
        print(f"  {consulta[:60]:<62}", end=" ", flush=True)
        nuevos = 0
        while paginas < paginas_por_consulta:
            params = {"query": consulta, "queryType": "Latest"}
            if cursor:
                params["cursor"] = cursor
            data = pedir(sesion, headers, "tweet/advanced_search", params)
            lote = data.get("tweets") or []
            tweets_vistos += len(lote)
            for t in lote:
                autor = t.get("author") or {}
                h = autor.get("userName")
                if h and h.lower() not in perfiles:
                    perfiles[h.lower()] = autor
                    nuevos += 1
            paginas += 1
            if not data.get("has_next_page") or not data.get("next_cursor"):
                break
            cursor = data["next_cursor"]
            time.sleep(0.2)
        print(f"{nuevos:>4} autores nuevos", flush=True)
        time.sleep(0.3)
    return perfiles, tweets_vistos


def traer_perfiles(sesion, headers, handles):
    """Perfil completo de handles sueltos (los que salieron de menciones)."""
    perfiles = {}
    for i, h in enumerate(handles, 1):
        try:
            data = pedir(sesion, headers, "user/info", {"userName": h})
        except requests.HTTPError:
            continue
        p = data.get("data")
        if p and p.get("userName"):
            perfiles[p["userName"].lower()] = p
        if i % 25 == 0:
            print(f"    {i}/{len(handles)}", flush=True)
        time.sleep(0.15)
    return perfiles


# Consultas armadas con el vocabulario real de la cuenta de referencia, no con
# terminos genericos: buscamos el registro, no solo el tema.
CONSULTAS_DEFECTO = [
    'psicoanálisis lang:es -filter:replies min_faves:15',
    'psicoanalista lang:es -filter:replies min_faves:10',
    '"el deseo" "el sujeto" lang:es -filter:replies min_faves:15',
    '"la falta" deseo lang:es -filter:replies min_faves:15',
    'Lacan lang:es -filter:replies min_faves:15',
    'Freud lang:es -filter:replies min_faves:20',
    '"el goce" lang:es -filter:replies min_faves:10',
    '"el inconsciente" lang:es -filter:replies min_faves:15',
    '"lazo social" lang:es -filter:replies min_faves:10',
    'transferencia analista lang:es -filter:replies min_faves:10',
    '"la palabra" silencio escucha lang:es -filter:replies min_faves:15',
    'duelo pérdida elaborar lang:es -filter:replies min_faves:15',
    'angustia síntoma lang:es -filter:replies min_faves:15',
    'subjetividad deseo lang:es -filter:replies min_faves:10',
    'psicología clínica lang:es -filter:replies min_faves:15',
    'terapia psicológica lang:es -filter:replies min_faves:20',
    'filosofía "la vida" lang:es -filter:replies min_faves:25',
    'Byung-Chul Han lang:es -filter:replies min_faves:15',
    'Winnicott OR Bion OR Klein lang:es -filter:replies min_faves:5',
    '"salud mental" lang:es -filter:replies min_faves:25',
    'vínculo apego lang:es -filter:replies min_faves:15',
    'amor "el otro" lang:es -filter:replies min_faves:25',
]

# Segunda tanda: pares de terminos que la cuenta de referencia usa junta, sacados
# de su firma lexica real (log-odds contra el pool). Las consultas de arriba
# encuentran el tema; estas encuentran el registro, que es lo dificil. El umbral
# de likes va bajo a proposito: las cuentas que mejor pegan son chicas.
CONSULTAS_REGISTRO = [
    '"el deseo" "la falta" lang:es -filter:replies min_faves:5',
    '"el lazo" "el otro" lang:es -filter:replies min_faves:3',
    'sostener "el deseo" lang:es -filter:replies min_faves:5',
    '"la escucha" analista lang:es -filter:replies min_faves:3',
    '"el síntoma" "el sujeto" lang:es -filter:replies min_faves:5',
    '"lo real" "lo simbólico" lang:es -filter:replies min_faves:3',
    '"la angustia" "el deseo" lang:es -filter:replies min_faves:5',
    '"el duelo" elaboración lang:es -filter:replies min_faves:3',
    'transferencia "el analista" lang:es -filter:replies min_faves:3',
    '"la palabra" "el silencio" lang:es -filter:replies min_faves:5',
    '"posición subjetiva" lang:es -filter:replies min_faves:2',
    '"el malestar" cultura lang:es -filter:replies min_faves:5',
    '"un analizante" OR "en análisis" lang:es -filter:replies min_faves:3',
    '"el amor" "el deseo" psicoanálisis lang:es -filter:replies min_faves:5',
    '"hacer lazo" lang:es -filter:replies min_faves:2',
    '"eso que insiste" OR "lo que insiste" lang:es -filter:replies min_faves:3',
    '"el cuerpo" "la palabra" lang:es -filter:replies min_faves:5',
    'melancolía duelo Freud lang:es -filter:replies min_faves:3',
    '"no hay relación sexual" OR "no todo" Lacan lang:es -filter:replies min_faves:3',
    '"la neurosis" OR "lo inconsciente" lang:es -filter:replies min_faves:5',
]


def traer_co_seguidos(sesion, headers, semillas, paginas_max):
    """
    A quien siguen las cuentas que ya sabemos que pegan.

    Es el canal que mejor encuentra el racimo: si diez cuentas afines siguen a
    la misma persona, esa persona casi seguro pertenece al mismo mundo. Se
    limita a las primeras paginas por semilla porque el costo escala rapido.
    """
    from collections import Counter
    veces = Counter()
    perfiles = {}
    for i, semilla in enumerate(semillas, 1):
        cursor, paginas = None, 0
        vistos_semilla = set()
        while paginas < paginas_max:
            params = {"userName": semilla, "pageSize": 200}
            if cursor:
                params["cursor"] = cursor
            data = pedir(sesion, headers, "user/followings", params)
            lote = data.get("followings") or []
            for p in lote:
                h = (p.get("userName") or p.get("screen_name") or "").lower()
                if not h or h in vistos_semilla:
                    continue
                vistos_semilla.add(h)
                veces[h] += 1
                perfiles.setdefault(h, p)
            paginas += 1
            if not data.get("has_next_page") or not data.get("next_cursor"):
                break
            cursor = data["next_cursor"]
            time.sleep(0.2)
        print(f"  {i}/{len(semillas)} @{semilla}: {len(vistos_semilla)} seguidos", flush=True)
        time.sleep(0.3)
    return veces, perfiles


def main():
    ap = argparse.ArgumentParser(
        description="Descubre cuentas parecidas a una cuenta de referencia."
    )
    ap.add_argument("--user", required=True, help="handle de referencia, sin @")
    ap.add_argument("--jsonl", help="corpus ya bajado, para el canal de menciones")
    ap.add_argument("--dir", default=".", help="carpeta de salida")
    ap.add_argument("--sin-seguidos", action="store_true",
                    help="saltea el canal de seguidos")
    ap.add_argument("--sin-busqueda", action="store_true",
                    help="saltea el canal de busqueda tematica")
    ap.add_argument("--paginas-por-consulta", type=int, default=2,
                    help="paginas de busqueda por consulta (default: 2, ~20 tweets c/u)")
    ap.add_argument("--min-menciones", type=int, default=2,
                    help="minimo de menciones para traer el perfil (default: 2)")
    ap.add_argument("--semillas",
                    help="handles separados por coma cuyos seguidos se agregan al pool; "
                         "usalos despues de una primera ronda, con las cuentas que ya validaste")
    ap.add_argument("--paginas-co", type=int, default=3,
                    help="paginas de seguidos por semilla (default: 3, o sea 600 cuentas)")
    ap.add_argument("--min-co", type=int, default=2,
                    help="cuantas semillas tienen que seguir a una cuenta para admitirla "
                         "(default: 2)")
    ap.add_argument("--registro", action="store_true",
                    help="usa tambien las consultas de registro, mas finas que las de tema")
    args = ap.parse_args()

    key = os.environ.get("TWITTERAPI_IO_KEY")
    if not key:
        sys.exit("Falta la variable de entorno TWITTERAPI_IO_KEY")

    usuario = args.user.lstrip("@")
    carpeta = Path(args.dir)
    carpeta.mkdir(parents=True, exist_ok=True)
    salida = carpeta / f"candidatos_{usuario}.jsonl"

    headers = {"X-API-Key": key}
    sesion = requests.Session()

    candidatos = {}   # handle en minusculas -> perfil crudo
    origenes = {}     # handle en minusculas -> set de canales

    # Lo ya descubierto en corridas anteriores se conserva: cada candidato del
    # archivo costo un pedido, y volver a pedirlo no lo mejora.
    if salida.exists():
        for linea in salida.open(encoding="utf-8"):
            try:
                o = json.loads(linea)
            except json.JSONDecodeError:
                continue
            h = o.get("_handle")
            if h:
                candidatos[h] = o
                origenes[h] = set(o.get("_origen", ()))
        print(f"Reanudando: {len(candidatos)} candidatos ya descubiertos.")
    tweets_vistos = 0
    perfiles_pedidos = 0

    def sumar(handle, perfil, canal):
        h = handle.lower()
        if h == usuario.lower():
            return
        if h not in candidatos:
            candidatos[h] = perfil
        origenes.setdefault(h, set()).add(canal)

    if not args.sin_seguidos:
        print(f"\n[1/3] Seguidos de @{usuario}")
        for p in traer_seguidos(sesion, headers, usuario):
            h = p.get("userName") or p.get("screen_name")
            if h:
                sumar(h, p, "seguidos")
                perfiles_pedidos += 1

    print(f"\n[2/3] Menciones en el corpus")
    menciones = handles_mencionados(args.jsonl)
    faltantes = [h for h, c in menciones.items()
                 if c >= args.min_menciones and h not in candidatos
                 and h != usuario.lower()]
    print(f"  {len(menciones)} handles mencionados; "
          f"{len(faltantes)} con >={args.min_menciones} menciones y sin perfil todavia")
    if faltantes:
        for h, p in traer_perfiles(sesion, headers, faltantes).items():
            sumar(h, p, "menciones")
            perfiles_pedidos += 1
    for h in menciones:
        if h in candidatos:
            origenes.setdefault(h, set()).add("menciones")

    if not args.sin_busqueda:
        consultas = list(CONSULTAS_DEFECTO)
        if args.registro:
            consultas += CONSULTAS_REGISTRO
        print(f"\n[3/4] Busqueda ({len(consultas)} consultas)")
        perfiles, vistos = autores_por_busqueda(
            sesion, headers, consultas, args.paginas_por_consulta)
        tweets_vistos += vistos
        for h, p in perfiles.items():
            sumar(h, p, "busqueda")

    if args.semillas:
        semillas = [s.strip().lstrip("@") for s in args.semillas.split(",") if s.strip()]
        print(f"\n[4/4] Seguidos en comun de {len(semillas)} semillas validadas")
        veces, perfiles_co = traer_co_seguidos(sesion, headers, semillas, args.paginas_co)
        admitidos = 0
        for h, n in veces.items():
            if n >= args.min_co:
                sumar(h, perfiles_co[h], "co-seguidos")
                perfiles_pedidos += 1
                admitidos += 1
        print(f"  {len(veces)} cuentas vistas; {admitidos} seguidas por >={args.min_co} semillas")

    with salida.open("w", encoding="utf-8") as f:
        for h, perfil in candidatos.items():
            registro = dict(perfil)
            registro["_handle"] = h
            registro["_origen"] = sorted(origenes.get(h, ()))
            registro["_menciones"] = menciones.get(h, 0)
            f.write(json.dumps(registro, ensure_ascii=False) + "\n")

    costo = tweets_vistos * PRECIO_POR_TWEET_USD + perfiles_pedidos * PRECIO_POR_PERFIL_USD
    print(f"\nArchivo: {salida}")
    print(f"Candidatos unicos: {len(candidatos)}")
    from collections import Counter
    por_canal = Counter(c for cs in origenes.values() for c in cs)
    for canal, n in por_canal.most_common():
        print(f"  {canal:<12} {n:>5}")
    print(f"Costo estimado de esta corrida: USD {costo:.2f}")


if __name__ == "__main__":
    main()
