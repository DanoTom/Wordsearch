#!/usr/bin/env python3
"""
Encuentra cuentas afines a una cuenta semilla, usando el grafo real de X.

El metodo: las cuentas que sigue la semilla son su vecindario declarado. Si
muchas de ellas siguen ademas a una misma cuenta X, entonces X es parte del
nucleo de ese nicho, aunque la semilla todavia no la siga. Se ordena por esa
co-ocurrencia, no por popularidad absoluta: asi no se cuelan las cuentas
masivas que sigue todo el mundo.

    export TWITTERAPI_IO_KEY="tu_api_key"
    python buscar_similares.py --user HANDLE --estimar     # cuanto costaria
    python buscar_similares.py --user HANDLE --top 100

Salida:
    similares_<handle>.md      listado ordenado, listo para leer
    cache_similares/           respuestas crudas de la API (evita repagar)

ATENCION: las rutas de los endpoints de seguidos/perfil NO estan verificadas
contra la documentacion de twitterapi.io (no era alcanzable al escribir esto).
Corre --verificar antes de nada: prueba las rutas candidatas con un pedido
minimo y te dice cual anda. Si ninguna responde, ajusta RUTAS_SEGUIDOS.

Requiere: pip install requests
"""

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

import requests

BASE = "https://api.twitterapi.io"

# Candidatas, en orden de probabilidad. --verificar decide cual queda.
RUTAS_SEGUIDOS = (
    "/twitter/user/followings",
    "/twitter/user/following",
    "/twitter/user/follows",
)
RUTAS_PERFIL = (
    "/twitter/user/info",
    "/twitter/user/profile",
)

PRECIO_POR_ITEM_USD = 0.00015

# Palabras funcionales que casi solo aparecen en castellano. Sirven para
# descartar bios en ingles o portugues sin traer una dependencia de idioma.
MARCAS_ES = re.compile(
    r"\b(de|que|para|con|una|los|las|del|por|como|sobre|entre|hacia|"
    r"divulgacion|divulgación|ciencia|historia|profesor|escritor|autor|"
    r"periodista|investigador|doctor|licenciado|cuenta|hilos|aqui|aquí)\b",
    re.IGNORECASE,
)
MARCAS_NO_ES = re.compile(
    r"\b(the|and|with|from|about|research|writer|author|host|founder|"
    r"professor|science|history|nao|não|voce|você|obrigado)\b",
    re.IGNORECASE,
)


def pedir(sesion, headers, url, params, intentos=4):
    espera = 2
    for intento in range(intentos):
        try:
            r = sesion.get(url, headers=headers, params=params, timeout=30)
        except requests.RequestException as e:
            if intento == intentos - 1:
                raise
            print(f"    error de red ({e}); reintento en {espera}s", flush=True)
            time.sleep(espera)
            espera *= 2
            continue

        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504):
            if intento == intentos - 1:
                r.raise_for_status()
            print(f"    HTTP {r.status_code}; reintento en {espera}s", flush=True)
            time.sleep(espera)
            espera *= 2
            continue
        return {"_error": r.status_code, "_cuerpo": r.text[:300]}
    raise RuntimeError("se agotaron los reintentos")


def verificar(sesion, headers, handle):
    """Prueba las rutas candidatas y reporta cual responde 200."""
    print("Probando rutas de SEGUIDOS:\n")
    buena_seguidos = None
    for ruta in RUTAS_SEGUIDOS:
        url = BASE + ruta
        for nombre_param in ("userName", "username", "screen_name"):
            data = pedir(sesion, headers, url, {nombre_param: handle})
            if "_error" not in data:
                print(f"  OK   {ruta}   (parametro: {nombre_param})")
                print("       claves de la respuesta: "
                      f"{sorted(data.keys())[:12]}")
                buena_seguidos = (ruta, nombre_param)
                break
            print(f"  {data['_error']:<4} {ruta}   (parametro: {nombre_param})")
        if buena_seguidos:
            break

    print("\nProbando rutas de PERFIL:\n")
    for ruta in RUTAS_PERFIL:
        url = BASE + ruta
        for nombre_param in ("userName", "username", "screen_name"):
            data = pedir(sesion, headers, url, {nombre_param: handle})
            if "_error" not in data:
                print(f"  OK   {ruta}   (parametro: {nombre_param})")
                print("       claves de la respuesta: "
                      f"{sorted(data.keys())[:12]}")
                return buena_seguidos
            print(f"  {data['_error']:<4} {ruta}   (parametro: {nombre_param})")
    return buena_seguidos


def traer_seguidos(sesion, headers, handle, ruta, param, cache: Path, tope=None):
    """Trae a quien sigue `handle`, paginando. Cachea en disco para no repagar."""
    archivo = cache / f"seguidos_{handle.lower()}.json"
    if archivo.exists():
        return json.loads(archivo.read_text(encoding="utf-8"))

    url = BASE + ruta
    usuarios = []
    cursor = None
    while True:
        params = {param: handle}
        if cursor:
            params["cursor"] = cursor
        data = pedir(sesion, headers, url, params)
        if "_error" in data:
            print(f"    {handle}: HTTP {data['_error']}")
            break

        lote = (data.get("followings") or data.get("following")
                or data.get("users") or data.get("data") or [])
        usuarios.extend(lote)

        if tope and len(usuarios) >= tope:
            break
        if not data.get("has_next_page") or not data.get("next_cursor"):
            break
        cursor = data["next_cursor"]
        time.sleep(0.2)

    archivo.write_text(json.dumps(usuarios, ensure_ascii=False), encoding="utf-8")
    return usuarios


def handle_de(u):
    for k in ("userName", "screen_name", "screenName", "username"):
        if isinstance(u, dict) and u.get(k):
            return str(u[k]).lstrip("@")
    return None


def campo_u(u, *nombres):
    for n in nombres:
        if isinstance(u, dict) and u.get(n) not in (None, ""):
            return u[n]
    return None


def parece_hispano(u):
    """Heuristica sobre bio y nombre. No es deteccion de idioma seria."""
    texto = " ".join(str(campo_u(u, x) or "") for x in
                     ("description", "bio", "name", "location"))
    if not texto.strip():
        return None  # sin datos: que decida la persona
    if re.search(r"[ñáéíóúü¿¡]", texto, re.IGNORECASE):
        return True
    return len(MARCAS_ES.findall(texto)) > len(MARCAS_NO_ES.findall(texto))


def main():
    ap = argparse.ArgumentParser(
        description="Encuentra cuentas afines por co-ocurrencia en el grafo de seguidos."
    )
    ap.add_argument("--user", required=True, help="handle semilla, sin @")
    ap.add_argument("--top", type=int, default=100, help="cuantas devolver (default 100)")
    ap.add_argument("--max-semillas", type=int, default=60,
                    help="cuantas cuentas del vecindario expandir (default 60). "
                         "Es lo que gobierna el costo.")
    ap.add_argument("--tope-por-cuenta", type=int, default=400,
                    help="maximo de seguidos a traer por cuenta (default 400)")
    ap.add_argument("--min-seguidores", type=int, default=2000)
    ap.add_argument("--solo-hispano", action="store_true",
                    help="descarta las que la heuristica marca como no hispanas")
    ap.add_argument("--dir", default=".", help="carpeta de salida")
    ap.add_argument("--estimar", action="store_true",
                    help="calcula el costo y sale, sin bajar nada")
    ap.add_argument("--verificar", action="store_true",
                    help="prueba las rutas candidatas de la API y sale")
    args = ap.parse_args()

    key = os.environ.get("TWITTERAPI_IO_KEY")
    if not key:
        sys.exit("Falta la variable de entorno TWITTERAPI_IO_KEY")

    semilla = args.user.lstrip("@")
    headers = {"X-API-Key": key}
    sesion = requests.Session()

    if args.verificar:
        verificar(sesion, headers, semilla)
        return

    carpeta = Path(args.dir)
    cache = carpeta / "cache_similares"
    cache.mkdir(parents=True, exist_ok=True)

    ruta, param = RUTAS_SEGUIDOS[0], "userName"

    print(f"1. Traigo a quien sigue @{semilla} ...")
    vecinos = traer_seguidos(sesion, headers, semilla, ruta, param, cache,
                             tope=args.tope_por_cuenta)
    if not vecinos:
        sys.exit("No pude traer los seguidos de la semilla. "
                 "Corre --verificar para chequear las rutas de la API.")
    print(f"   sigue a {len(vecinos)} cuentas\n")

    # Expandimos las mas chicas primero: una cuenta de nicho con 20k seguidores
    # define el nicho mucho mejor que un medio masivo que sigue todo el mundo.
    def seguidores(u):
        return campo_u(u, "followers", "followersCount", "followers_count") or 0

    candidatas = [v for v in vecinos if handle_de(v)]
    candidatas.sort(key=seguidores)
    a_expandir = candidatas[:args.max_semillas]

    costo = (len(a_expandir) * args.tope_por_cuenta) * PRECIO_POR_ITEM_USD
    print(f"2. Voy a expandir {len(a_expandir)} cuentas "
          f"(hasta {args.tope_por_cuenta} seguidos c/u)")
    print(f"   Costo maximo estimado: USD {costo:.2f}\n")
    if args.estimar:
        print("Modo estimacion: no bajo nada. Sacá --estimar para correrlo en serio.")
        return

    votos = Counter()
    perfiles = {}
    ya_sigue = {handle_de(v).lower() for v in vecinos if handle_de(v)}

    for i, v in enumerate(a_expandir, 1):
        h = handle_de(v)
        print(f"   [{i}/{len(a_expandir)}] @{h} ...", end=" ", flush=True)
        suyos = traer_seguidos(sesion, headers, h, ruta, param, cache,
                               tope=args.tope_por_cuenta)
        print(f"{len(suyos)} seguidos")
        for u in suyos:
            hu = handle_de(u)
            if not hu or hu.lower() == semilla.lower():
                continue
            votos[hu] += 1
            perfiles.setdefault(hu, u)
        time.sleep(0.2)

    print(f"\n3. {len(votos)} cuentas distintas aparecieron en el vecindario.\n")

    filas = []
    for h, n in votos.most_common():
        u = perfiles[h]
        segs = seguidores(u)
        if segs < args.min_seguidores:
            continue
        hispano = parece_hispano(u)
        if args.solo_hispano and hispano is False:
            continue
        filas.append({
            "handle": h,
            "votos": n,
            "seguidores": segs,
            "nombre": campo_u(u, "name") or "",
            "bio": (campo_u(u, "description", "bio") or "").replace("\n", " "),
            "hispano": hispano,
            "ya_sigue": h.lower() in ya_sigue,
        })
        if len(filas) >= args.top:
            break

    salida = carpeta / f"similares_{semilla}.md"
    lineas = [
        f"# Cuentas afines a @{semilla}\n",
        f"\n{len(filas)} cuentas, ordenadas por cuantas del vecindario de "
        f"@{semilla} las siguen. Expandidas {len(a_expandir)} cuentas.\n",
        "\nLa columna **afinidad** es esa cuenta: cuanto mas alta, mas central "
        "es la cuenta en este nicho. `ya la sigue` marca las que @"
        f"{semilla} ya sigue.\n",
        "\nEl idioma sale de una heuristica sobre la bio, no de deteccion "
        "seria: revisa antes de confiar.\n",
        "\n| # | cuenta | afinidad | seguidores | ¿hispana? | ¿ya la sigue? | bio |\n",
        "|---|---|---|---|---|---|---|\n",
    ]
    for i, f in enumerate(filas, 1):
        marca = {True: "sí", False: "no", None: "?"}[f["hispano"]]
        lineas.append(
            f"| {i} | [@{f['handle']}](https://x.com/{f['handle']}) | {f['votos']} | "
            f"{f['seguidores']:,} | {marca} | {'sí' if f['ya_sigue'] else ''} | "
            f"{f['bio'][:160].replace('|', '/')} |\n"
        )
    salida.write_text("".join(lineas), encoding="utf-8")

    print(f"Archivo: {salida}")
    print(f"Cuentas en el listado: {len(filas)}")
    print(f"De esas, ya seguidas por @{semilla}: "
          f"{sum(1 for f in filas if f['ya_sigue'])}")
    marcadas = sum(1 for f in filas if f["hispano"] is False)
    if marcadas:
        print(f"Marcadas como no hispanas: {marcadas} "
              f"(usa --solo-hispano para sacarlas)")


if __name__ == "__main__":
    main()
