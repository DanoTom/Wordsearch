#!/usr/bin/env python3
"""
Baja una muestra mas honda de las candidatas que mejor puntuaron.

    python profundizar_muestras.py --candidatos candidatos_USUARIO.jsonl \
                                   --referencia corpus_USUARIO.jsonl --top 250

Veinte posts alcanzan para descartar, no para juzgar. Una cuenta que escribe
como la referencia pero tuvo una semana de futbol o de elecciones queda mal
clasificada por puro azar de muestreo, y una cuenta de charla que justo posteo
tres reflexiones seguidas se cuela. Con cien posts el registro se estabiliza.

Se baja solo para las mejores del ranking flojo: profundizar las 2.000 costaria
diez veces mas y nueve de cada diez ya estan bien descartadas.

Las muestras hondas se anexan al mismo cache que usa rankear_cuentas.py. Al
leerlo, la ultima linea de cada handle es la que vale, asi que la muestra honda
pisa a la corta sin borrar nada: el archivo sigue siendo el registro completo
de lo que se pago.

Requiere: pip install requests
"""

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rankear_cuentas import (  # noqa: E402
    PRECIO_POR_TWEET_USD, afinidad_firma, campo_perfil, densidad_academica,
    densidad_jerga, largo_mediano, pedir, perfil_lexico, pesos_distintivos,
    prefiltrar, tokenizar,
)

POSTS_HONDOS = 100


def main():
    ap = argparse.ArgumentParser(
        description="Baja muestras mas hondas de las mejores candidatas.")
    ap.add_argument("--candidatos", required=True)
    ap.add_argument("--referencia", required=True)
    ap.add_argument("--user")
    ap.add_argument("--dir", default=".")
    ap.add_argument("--top", type=int, default=250,
                    help="cuantas candidatas profundizar (default: 250)")
    ap.add_argument("--posts", type=int, default=POSTS_HONDOS,
                    help=f"posts por cuenta (default: {POSTS_HONDOS})")
    args = ap.parse_args()

    key = os.environ.get("TWITTERAPI_IO_KEY")
    if not key:
        sys.exit("Falta la variable de entorno TWITTERAPI_IO_KEY")

    ruta_ref = Path(args.referencia)
    usuario = args.user or ruta_ref.stem.replace("corpus_", "")
    carpeta = Path(args.dir)
    cache_ruta = carpeta / f"muestras_{usuario}.jsonl"

    textos_ref = []
    for linea in ruta_ref.open(encoding="utf-8"):
        try:
            o = json.loads(linea)
        except json.JSONDecodeError:
            continue
        if not o.get("retweeted_tweet"):
            textos_ref.append(o.get("text") or "")
    lex_ref = perfil_lexico(textos_ref)
    jerga_ref = densidad_jerga(textos_ref)
    acad_ref = densidad_academica(textos_ref)
    largo_ref = largo_mediano(textos_ref)

    candidatos = []
    for linea in Path(args.candidatos).open(encoding="utf-8"):
        try:
            candidatos.append(json.loads(linea))
        except json.JSONDecodeError:
            continue
    pasan, _ = prefiltrar(candidatos, usuario)

    cache, hondas = {}, set()
    if cache_ruta.exists():
        for linea in cache_ruta.open(encoding="utf-8"):
            try:
                o = json.loads(linea)
            except json.JSONDecodeError:
                continue
            previo = cache.get(o["_handle"])
            if previo is None or len(o["tweets"]) > len(previo):
                cache[o["_handle"]] = o["tweets"]
            if o.get("_hondo") and o["tweets"]:
                hondas.add(o["_handle"])
    print(f"Cache: {len(cache)} cuentas, {len(hondas)} ya profundizadas")

    lex_pool = Counter()
    for c in pasan:
        for t in cache.get(c["_handle"], []):
            if not t.get("esRT") and t.get("text"):
                lex_pool.update(tokenizar(t["text"]))
    pesos = pesos_distintivos(lex_ref, lex_pool)

    # Ranking flojo: solo afinidad y registro, sin las aduanas. Las aduanas se
    # aplican despues, sobre la muestra honda, que es cuando son confiables.
    puntajes = []
    for c in pasan:
        h = c["_handle"]
        propios = [t for t in cache.get(h, []) if not t.get("esRT") and t.get("text")]
        if len(propios) < 5:
            continue
        langs = Counter(t.get("lang") for t in propios)
        if langs.get("es", 0) / len(propios) < 0.5:
            continue
        textos = [t["text"] for t in propios]
        lex = perfil_lexico(textos)
        if not lex:
            continue
        import math
        afinidad = afinidad_firma(lex, pesos)
        pena_jerga = min(1.0, max(0.0, densidad_jerga(textos) - jerga_ref) / 0.004)
        pena_acad = min(1.0, max(0.0, densidad_academica(textos) - acad_ref) / 0.15)
        largo = largo_mediano(textos) or 1
        pena_largo = min(1.0, abs(math.log(largo / max(largo_ref, 1))) / 1.2)
        registro = max(0.0, 1.0 - (0.30 * pena_jerga + 0.20 * pena_acad + 0.30 * pena_largo))
        puntajes.append((afinidad * registro, h))

    puntajes.sort(reverse=True)
    elegidas = [h for _, h in puntajes[:args.top] if h not in hondas]
    print(f"A profundizar: {len(elegidas)} cuentas x {args.posts} posts")
    print(f"Costo estimado: USD {len(elegidas) * args.posts * PRECIO_POR_TWEET_USD:.2f}\n")

    headers = {"X-API-Key": key}
    sesion = requests.Session()
    bajados = 0
    with cache_ruta.open("a", encoding="utf-8") as f:
        for i, h in enumerate(elegidas, 1):
            tweets, cursor, paginas = [], None, 0
            # last_tweets pagina de a ~20; hay que caminar el cursor.
            while len(tweets) < args.posts and paginas < 8:
                params = {"userName": h}
                if cursor:
                    params["cursor"] = cursor
                data = pedir(sesion, headers, "user/last_tweets", params)
                if not data:
                    break
                bloque = ((data or {}).get("data") or {}).get("tweets") or []
                if not bloque:
                    break
                tweets.extend(bloque)
                paginas += 1
                cursor = data.get("next_cursor")
                if not data.get("has_next_page") or not cursor:
                    break
                time.sleep(0.12)

            livianos = [{"text": t.get("text") or "",
                         "createdAt": t.get("createdAt"),
                         "lang": t.get("lang"),
                         "id": t.get("id"),
                         "isReply": t.get("isReply"),
                         "esRT": bool(t.get("retweeted_tweet")),
                         "likeCount": t.get("likeCount")}
                        for t in tweets[:args.posts]]
            bajados += len(livianos)
            f.write(json.dumps({"_handle": h, "_hondo": True,
                                "tweets": livianos}, ensure_ascii=False) + "\n")
            if i % 20 == 0:
                f.flush()
                print(f"  {i}/{len(elegidas)}  (@{h}, {len(livianos)} posts)", flush=True)
            time.sleep(0.12)

    print(f"\nProfundizadas: {len(elegidas)} cuentas, {bajados} posts")
    print(f"Costo estimado de esta corrida: USD {bajados * PRECIO_POR_TWEET_USD:.2f}")


if __name__ == "__main__":
    main()
