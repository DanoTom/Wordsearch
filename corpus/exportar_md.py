#!/usr/bin/env python3
"""
Lee el JSONL crudo que dejo bajar_corpus.py y arma un documento .md ordenado
cronologicamente, con los hilos reconstruidos como bloque unico.

    python exportar_md.py --jsonl corpus_USUARIO.jsonl

Salida por defecto: corpus_USUARIO.md

REGLA QUE NO SE NEGOCIA
    El texto va exactamente como vino de la API. No se normalizan comillas ni
    apostrofes, no se colapsan espacios ni saltos de linea, no se sacan URLs,
    emoji, hashtags ni menciones, no se corrige ortografia. Lo que a un script
    le parece suciedad es, aca, el objeto de estudio.

    El JSONL crudo no se toca nunca: es la fuente de verdad, y volver a bajarlo
    cuesta plata.

La API puede nombrar los campos de mas de una manera segun version. Este script
prueba las variantes conocidas y, si no encuentra alguna, lo reporta en vez de
inventar. Corre --inspeccionar para ver el mapeo real contra tus datos.
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Variantes de nombre por campo, en orden de preferencia. La API de twitterapi.io
# usa camelCase; dejamos las formas snake_case y las de la API oficial por las dudas.
ALIAS = {
    "id":            ("id", "id_str", "tweetId", "tweet_id"),
    "fecha":         ("createdAt", "created_at", "date", "time"),
    "texto":         ("text", "full_text", "fullText", "rawContent"),
    "conversacion":  ("conversationId", "conversation_id", "conversationIdStr"),
    "responde_a":    ("inReplyToId", "in_reply_to_id", "inReplyToStatusId",
                      "in_reply_to_status_id_str", "in_reply_to_status_id"),
    "responde_a_usuario": ("inReplyToUserId", "in_reply_to_user_id",
                           "inReplyToUserIdStr"),
    "responde_a_handle":  ("inReplyToUsername", "in_reply_to_screen_name",
                           "inReplyToScreenName"),
    "idioma":        ("lang", "language"),
    "citado":        ("quoted_tweet", "quotedTweet", "quoted_status", "quotedStatus"),
    "retweet":       ("retweeted_tweet", "retweetedTweet", "retweeted_status",
                      "retweetedStatus"),
    "autor":         ("author", "user"),
}

ALIAS_METRICAS = {
    "likes":     ("likeCount", "favorite_count", "favoriteCount", "like_count"),
    "retweets":  ("retweetCount", "retweet_count"),
    "respuestas": ("replyCount", "reply_count"),
    "citas":     ("quoteCount", "quote_count"),
    "vistas":    ("viewCount", "view_count", "views"),
    "marcadores": ("bookmarkCount", "bookmark_count"),
}

ALIAS_HANDLE = ("userName", "screen_name", "screenName", "username", "handle")


def campo(obj, clave, defecto=None):
    """Devuelve el primer alias presente de `clave`. No inventa valores."""
    if not isinstance(obj, dict):
        return defecto
    for nombre in ALIAS.get(clave, ()):
        if nombre in obj and obj[nombre] not in (None, ""):
            return obj[nombre]
    return defecto


def metrica(obj, clave):
    for nombre in ALIAS_METRICAS.get(clave, ()):
        if nombre in obj and obj[nombre] is not None:
            try:
                return int(obj[nombre])
            except (TypeError, ValueError):
                return None
    return None


def handle_de(autor):
    if not isinstance(autor, dict):
        return None
    for nombre in ALIAS_HANDLE:
        if autor.get(nombre):
            return str(autor[nombre]).lstrip("@")
    return None


# "Tue Dec 10 07:00:30 +0000 2024" (formato clasico de Twitter) e ISO 8601.
FORMATOS_FECHA = (
    "%a %b %d %H:%M:%S %z %Y",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d %H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S",
)


def parsear_fecha(valor):
    """Devuelve datetime en UTC, o None si no se pudo interpretar."""
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        try:
            return datetime.fromtimestamp(float(valor), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    s = str(valor).strip()
    for fmt in FORMATOS_FECHA:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    try:  # ultimo intento: ISO con offset raro
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def leer_jsonl(ruta: Path):
    """Levanta el JSONL deduplicando por id. Devuelve (registros, lineas_malas)."""
    por_id = {}
    malas = 0
    with ruta.open(encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea:
                continue
            try:
                obj = json.loads(linea)
            except json.JSONDecodeError:
                malas += 1
                continue
            tid = campo(obj, "id")
            if tid is None:
                malas += 1
                continue
            por_id[str(tid)] = obj
    return por_id, malas


def clasificar(obj, autor_corpus):
    """original / hilo / respuesta / cita / retweet."""
    if campo(obj, "retweet"):
        return "retweet"

    responde_a = campo(obj, "responde_a")
    if responde_a:
        handle_destino = campo(obj, "responde_a_handle")
        if handle_destino and autor_corpus and \
                str(handle_destino).lstrip("@").lower() == autor_corpus.lower():
            return "hilo"
        # Sin handle de destino resolvemos mas adelante, mirando si el post
        # respondido esta en el propio corpus.
        return "respuesta"

    if campo(obj, "citado"):
        return "cita"
    return "original"


def reconstruir_hilos(registros, autor_corpus):
    """
    Agrupa por conversacion los casos donde el autor se responde a si mismo y
    resuelve el orden interno encadenando por el campo de respuesta.

    Un hilo es una unidad de composicion, no N posts sueltos.
    """
    ids_propios = set(registros)

    # Paso 1: afinar la clasificacion usando el propio corpus. Si el post al que
    # responde esta en el corpus, es una respuesta a si mismo, o sea: hilo.
    tipos = {}
    for tid, obj in registros.items():
        tipo = clasificar(obj, autor_corpus)
        if tipo == "respuesta":
            responde_a = campo(obj, "responde_a")
            if responde_a and str(responde_a) in ids_propios:
                tipo = "hilo"
        tipos[tid] = tipo

    # Paso 2: agrupar por conversacion. Solo cuentan como hilo las conversaciones
    # con dos o mas posts propios encadenados.
    por_conversacion = defaultdict(list)
    for tid, obj in registros.items():
        conv = campo(obj, "conversacion") or tid
        por_conversacion[str(conv)].append(tid)

    hilos = {}
    for conv, ids in por_conversacion.items():
        if len(ids) < 2:
            continue
        if not any(tipos[t] == "hilo" for t in ids):
            continue
        hilos[conv] = ordenar_hilo(ids, registros)

    return tipos, hilos


def ordenar_hilo(ids, registros):
    """Encadena por el campo de respuesta; lo que quede suelto va por fecha."""
    conjunto = set(ids)
    hijo_de = {}
    for tid in ids:
        padre = campo(registros[tid], "responde_a")
        padre = str(padre) if padre is not None else None
        if padre in conjunto and padre != tid:
            hijo_de.setdefault(padre, []).append(tid)

    def clave_fecha(tid):
        dt = parsear_fecha(campo(registros[tid], "fecha"))
        return (dt or datetime.max.replace(tzinfo=timezone.utc), tid)

    raices = [t for t in ids
              if not (campo(registros[t], "responde_a")
                      and str(campo(registros[t], "responde_a")) in conjunto)]
    raices.sort(key=clave_fecha)

    orden = []
    visto = set()

    def bajar(tid):
        if tid in visto:
            return
        visto.add(tid)
        orden.append(tid)
        for h in sorted(hijo_de.get(tid, []), key=clave_fecha):
            bajar(h)

    for r in raices:
        bajar(r)
    # Cualquier resto (ciclos raros, cadenas rotas) entra por fecha al final.
    for t in sorted(conjunto - visto, key=clave_fecha):
        orden.append(t)
    return orden


def url_de(obj, autor_corpus):
    tid = campo(obj, "id")
    autor = handle_de(campo(obj, "autor")) or autor_corpus or "i"
    return f"https://x.com/{autor}/status/{tid}"


def render_texto(texto, modo_bloque):
    """
    El texto va verbatim. En modo bloque lo envolvemos en una cerca de codigo
    lo bastante larga como para no colisionar con backticks del propio post,
    lo que garantiza que se renderice byte a byte y respetando los saltos.
    """
    if not modo_bloque:
        return texto
    maxima = 0
    for corrida in re.findall(r"`+", texto):
        maxima = max(maxima, len(corrida))
    cerca = "`" * max(3, maxima + 1)
    return f"{cerca}\n{texto}\n{cerca}"


def escribir_md(ruta, autor_corpus, entradas, registros, modo_bloque, resumen):
    """entradas: lista de (datetime, tipo, [ids]) ya ordenada cronologicamente."""
    partes = []
    partes.append(f"# Corpus de @{autor_corpus}\n")
    partes.append(
        f"- Posts: **{resumen['total_en_md']}** "
        f"(de {resumen['total_leidos']} bajados)\n"
        f"- Rango: **{resumen['rango']}**\n"
        f"- Hilos reconstruidos: **{resumen['hilos']}**\n"
        f"- Generado: {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC\n"
    )
    partes.append(
        "\n> El texto de cada post esta exactamente como lo devolvio la API: "
        "sin normalizar comillas, sin colapsar espacios ni saltos de linea, "
        "sin tocar URLs, emoji, hashtags, menciones ni ortografia.\n"
    )

    anio_actual = None
    for dt, tipo, ids in entradas:
        anio = dt.year if dt else "sin fecha"
        if anio != anio_actual:
            partes.append(f"\n---\n\n## {anio}\n")
            anio_actual = anio

        sello = f"{dt:%Y-%m-%d %H:%M}" if dt else "fecha desconocida"
        if tipo == "hilo":
            partes.append(f"\n### {sello} · hilo ({len(ids)} posts)\n")
        else:
            partes.append(f"\n### {sello} · {tipo}\n")

        for i, tid in enumerate(ids):
            obj = registros[tid]
            texto = campo(obj, "texto", "")
            if i > 0:
                partes.append("\n")
            partes.append("\n" + render_texto(texto, modo_bloque) + "\n")

            # La cita se resuelve por post, no por entrada: un hilo puede
            # contener un post que cita a un tercero.
            citado = campo(obj, "citado")
            if isinstance(citado, dict):
                autor_cit = handle_de(campo(citado, "autor")) or "?"
                texto_cit = campo(citado, "texto", "")
                partes.append(
                    f"\n<sub>cita a @{autor_cit}:</sub>\n\n"
                    + "\n".join("> " + l for l in texto_cit.split("\n")) + "\n"
                )

        partes.append(f"\n<sub>[{ids[0]}]({url_de(registros[ids[0]], autor_corpus)})</sub>\n")

    ruta.write_text("".join(partes), encoding="utf-8")


def meses_del_rango(inicio, fin):
    meses = []
    a, m = inicio.year, inicio.month
    while (a, m) <= (fin.year, fin.month):
        meses.append(f"{a:04d}-{m:02d}")
        a, m = (a + 1, 1) if m == 12 else (a, m + 1)
    return meses


def inspeccionar(registros):
    """Muestra la estructura real de un registro, para verificar el mapeo."""
    print("=" * 70)
    print("ESTRUCTURA DE UN REGISTRO (el mas largo de la muestra)")
    print("=" * 70)

    mas_largo = max(registros.values(), key=lambda o: len(campo(o, "texto", "") or ""))
    print(json.dumps(mas_largo, ensure_ascii=False, indent=2)[:4000])

    print("\n" + "=" * 70)
    print("CLAVES PRESENTES EN EL CORPUS (nivel superior)")
    print("=" * 70)
    conteo = Counter()
    for obj in registros.values():
        conteo.update(obj.keys())
    total = len(registros)
    for clave, n in sorted(conteo.items(), key=lambda kv: -kv[1]):
        print(f"  {clave:<32} {n:>6}/{total}")

    print("\n" + "=" * 70)
    print("MAPEO RESUELTO")
    print("=" * 70)
    # Se escanea todo el corpus, no un solo registro: los campos opcionales
    # (responde_a, citado, retweet) faltan en la mayoria de los posts.
    for tabla in (ALIAS, ALIAS_METRICAS):
        for clave, nombres in tabla.items():
            encontrado = next((n for n in nombres if conteo.get(n)), None)
            if encontrado:
                print(f"  {clave:<22} -> {encontrado:<24} "
                      f"presente en {conteo[encontrado]}/{total}")
            else:
                print(f"  {clave:<22} -> NO ENCONTRADO  (probe: "
                      f"{', '.join(nombres)})")


def main():
    ap = argparse.ArgumentParser(
        description="Arma un .md ordenado a partir del JSONL crudo."
    )
    ap.add_argument("--jsonl", required=True, help="ruta al corpus_<usuario>.jsonl")
    ap.add_argument("--salida", help="ruta del .md (default: junto al jsonl)")
    ap.add_argument("--incluir-retweets", action="store_true",
                    help="incluye retweets (por defecto se excluyen: no son texto propio)")
    ap.add_argument("--modo-bloque", action="store_true",
                    help="envuelve cada post en una cerca de codigo, para que el "
                         "renderizado respete los saltos de linea byte a byte")
    ap.add_argument("--inspeccionar", action="store_true",
                    help="muestra la estructura cruda y el mapeo de campos, y sale")
    args = ap.parse_args()

    ruta = Path(args.jsonl)
    if not ruta.exists():
        sys.exit(f"No existe {ruta}")

    registros, malas = leer_jsonl(ruta)
    if not registros:
        sys.exit(f"{ruta} no tiene registros legibles")
    if malas:
        print(f"aviso: {malas} lineas ilegibles salteadas\n")

    if args.inspeccionar:
        inspeccionar(registros)
        return

    # El autor del corpus es el handle mayoritario entre los registros.
    handles = Counter()
    for obj in registros.values():
        h = handle_de(campo(obj, "autor"))
        if h:
            handles[h] += 1
    autor_corpus = handles.most_common(1)[0][0] if handles else ruta.stem.replace("corpus_", "")

    tipos, hilos = reconstruir_hilos(registros, autor_corpus)

    # El post que abre un hilo tambien es hilo: la unidad de composicion es la
    # cadena entera, no cada eslabon por separado.
    for ids in hilos.values():
        for tid in ids:
            if tipos[tid] != "retweet":
                tipos[tid] = "hilo"

    # Los posts que forman parte de un hilo se emiten una sola vez, en su bloque.
    en_hilo = {tid: conv for conv, ids in hilos.items() for tid in ids}

    excluidos_rt = 0
    entradas = []
    conversaciones_hechas = set()

    for tid, obj in registros.items():
        if tipos[tid] == "retweet" and not args.incluir_retweets:
            excluidos_rt += 1
            continue

        conv = en_hilo.get(tid)
        if conv is not None:
            if conv in conversaciones_hechas:
                continue
            conversaciones_hechas.add(conv)
            ids = [t for t in hilos[conv]
                   if args.incluir_retweets or tipos[t] != "retweet"]
            if not ids:
                continue
            dt = parsear_fecha(campo(registros[ids[0]], "fecha"))
            entradas.append((dt, "hilo", ids))
        else:
            dt = parsear_fecha(campo(obj, "fecha"))
            entradas.append((dt, tipos[tid], [tid]))

    sin_fecha = sum(1 for dt, _, _ in entradas if dt is None)
    entradas.sort(key=lambda e: (e[0] is None,
                                 e[0] or datetime.max.replace(tzinfo=timezone.utc)))

    fechas = [dt for dt, _, _ in entradas if dt]
    rango = (f"{min(fechas):%Y-%m-%d} a {max(fechas):%Y-%m-%d}"
             if fechas else "sin fechas legibles")
    total_en_md = sum(len(ids) for _, _, ids in entradas)

    salida = Path(args.salida) if args.salida else ruta.with_suffix(".md")
    escribir_md(salida, autor_corpus, entradas, registros, args.modo_bloque, {
        "total_en_md": total_en_md,
        "total_leidos": len(registros),
        "rango": rango,
        "hilos": len(hilos),
    })

    # ---- Reporte ----
    print(f"Archivo: {salida}")
    print(f"Autor detectado: @{autor_corpus}\n")

    print("Posts por tipo:")
    cuenta_tipos = Counter(tipos.values())
    for t, n in cuenta_tipos.most_common():
        marca = "  (excluidos del .md)" if t == "retweet" and not args.incluir_retweets else ""
        print(f"  {t:<12} {n:>6}{marca}")
    print(f"  {'TOTAL':<12} {len(registros):>6}")
    print(f"\nEn el .md: {total_en_md} posts en {len(entradas)} entradas")
    if sin_fecha:
        print(f"aviso: {sin_fecha} entradas sin fecha legible, van al final")

    print(f"\nRango real cubierto: {rango}")

    if fechas:
        presentes = {f"{dt:%Y-%m}" for dt in fechas}
        huecos = [m for m in meses_del_rango(min(fechas), max(fechas))
                  if m not in presentes]
        if huecos:
            print(f"\nMeses sin ningun post ({len(huecos)}), posibles huecos de la bajada:")
            for i in range(0, len(huecos), 12):
                print("  " + "  ".join(huecos[i:i + 12]))
        else:
            print("\nNo hay meses vacios dentro del rango.")

    largos = sorted(registros.values(),
                    key=lambda o: len(campo(o, "texto", "") or ""))
    print("\nLos 5 mas largos (para confirmar que no hay truncamiento):")
    for obj in reversed(largos[-5:]):
        texto = campo(obj, "texto", "") or ""
        print(f"  {len(texto):>5} chars  {campo(obj, 'id')}  "
              f"{texto[:60].replace(chr(10), ' / ')}...")
    print("\nLos 5 mas cortos:")
    for obj in largos[:5]:
        texto = campo(obj, "texto", "") or ""
        print(f"  {len(texto):>5} chars  {campo(obj, 'id')}  "
              f"{texto[:60].replace(chr(10), ' / ')}")

    if hilos:
        largos_hilo = [len(ids) for ids in hilos.values()]
        print(f"\nHilos reconstruidos: {len(hilos)}")
        print(f"Largo promedio: {sum(largos_hilo) / len(largos_hilo):.1f} posts")
        print(f"Hilo mas largo: {max(largos_hilo)} posts")
    else:
        print("\nNo se detectaron hilos.")

    if excluidos_rt:
        print(f"\n{excluidos_rt} retweets excluidos. "
              f"Usa --incluir-retweets si los queres en el .md.")


if __name__ == "__main__":
    main()
