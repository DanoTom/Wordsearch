#!/usr/bin/env python3
"""
Puntua los candidatos de descubrir_cuentas.py contra el corpus de referencia y
arma el listado final en .md.

    python rankear_cuentas.py --candidatos candidatos_USUARIO.jsonl \
                              --referencia corpus_USUARIO.jsonl --top 80

La pregunta no es "quien habla de psicoanalisis" sino "quien escribe COMO esta
cuenta": mismo tema y mismo registro divulgativo. Por eso se puntua contra el
corpus real de la referencia y no contra una lista de palabras clave inventada.

Dos ejes, que son cosas distintas:

    afinidad    solapamiento de vocabulario con el corpus de referencia,
                pesado por IDF para que las palabras comunes no inflen el
                puntaje. Mide el tema.

    registro    distancia en densidad de jerga tecnica, largo de post y marcas
                academicas (citas, paginas, "Seminario", DOIs). Mide el tono.
                La densidad de jerga de la propia referencia es el blanco: no
                se penaliza usar terminos del campo, se penaliza escribir para
                colegas en vez de para cualquiera.

Las muestras de posts se cachean: bajar cuesta plata, repuntuar no. Volve a
correr el script con otros pesos o otro --top sin gastar un centavo.

Salida:
    muestras_<usuario>.jsonl          posts de muestra crudos (cache)
    cuentas_similares_<usuario>.md    el listado final

Requiere: pip install requests
"""

import argparse
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = "https://api.twitterapi.io/twitter"
PRECIO_POR_TWEET_USD = 0.00015

POSTS_POR_MUESTRA = 20   # una pagina de last_tweets

# Palabras vacias del espanol. No pretenden ser exhaustivas: solo sacan del
# medio lo que no distingue a una cuenta de otra.
STOP = set("""
a al algo alguien alguna algunas alguno algunos ante antes aqui aquel aquella
aquello asi aun aunque bajo bien cada casi como con contra cual cuales cuando
cuanto de del desde donde dos el ella ellas ello ellos en entre era eran eres
es esa esas ese eso esos esta estan estas este esto estos estoy fue fueron
gracias ha hace hacen hacer hacia han hasta hay incluso la las le les lo los
mas me menos mi mientras muy nada ni no nos nunca o otra otras otro otros para
pero poco por porque puede pueden que quien se segun sea ser si sin sino sobre
solo son su sus tambien tan tanto te tiene tienen todo todos tu un una uno unos
vez y ya
""".split())

# Marcas de escritura para colegas, no para cualquiera. Cuenta la densidad, no
# la presencia: una cita suelta no vuelve academica a una cuenta.
JERGA = set("""
significante significantes forclusion forclusiva matema matemas lalengua
borromeo borromeano sinthome sinthoma metapsicologia topica toposica
epistemologia hermeneutica fenomenologico fenomenologica ontologico ontologica
esquizoanalisis grafo automaton tyche agalma extimidad extimo parletre
holofrase discordancial semblante semblantes retroaccion aprescoup
significantizacion simbolizacion imaginarizacion pulsional pulsionales
metonimia metonimico condensacion desplazamiento supervision cartel carteles
psicopatologia nosografia nosologia estructural estructurales diacronico
sincronico epistemico praxis teleologico dialectica dialectico
""".split())

MARCAS_ACADEMICAS = (
    re.compile(r"\(\d{4}\)"),                 # (1975)
    re.compile(r"\bp[aá]g?s?\.\s*\d", re.I),  # pág. 34
    re.compile(r"\bop\.\s*cit", re.I),
    re.compile(r"\bcf\.", re.I),
    re.compile(r"\bet\s+al\b", re.I),
    re.compile(r"\bvol\.\s*\d", re.I),
    re.compile(r"\bSeminario\s+[IVXL0-9]", re.I),
    re.compile(r"\bdoi\.org\b", re.I),
    re.compile(r"\bISBN\b", re.I),
)


def normalizar(texto):
    """Minusculas y sin tildes, solo para comparar. El texto original no se toca."""
    t = texto.lower()
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"),
                 ("ú", "u"), ("ü", "u"), ("ñ", "n")):
        t = t.replace(a, b)
    return t


def tokenizar(texto):
    """Palabras de contenido, sin URLs ni menciones."""
    t = re.sub(r"https?://\S+", " ", texto)
    t = re.sub(r"@\w+", " ", t)
    t = normalizar(t)
    return [w for w in re.findall(r"[a-z]{4,}", t) if w not in STOP]


def perfil_lexico(textos):
    """Frecuencia de palabras de contenido sobre un conjunto de textos."""
    c = Counter()
    for t in textos:
        c.update(tokenizar(t))
    return c


def coseno(a, b, idf):
    """Coseno entre dos bolsas de palabras, pesadas por IDF y suavizadas por log."""
    if not a or not b:
        return 0.0
    va = {w: (1 + math.log(n)) * idf.get(w, 1.0) for w, n in a.items()}
    vb = {w: (1 + math.log(n)) * idf.get(w, 1.0) for w, n in b.items()}
    na = math.sqrt(sum(v * v for v in va.values()))
    nb = math.sqrt(sum(v * v for v in vb.values()))
    if not na or not nb:
        return 0.0
    comunes = set(va) & set(vb)
    return sum(va[w] * vb[w] for w in comunes) / (na * nb)


def densidad_jerga(textos):
    total = jerga = 0
    for t in textos:
        toks = tokenizar(t)
        total += len(toks)
        jerga += sum(1 for w in toks if w in JERGA)
    return (jerga / total) if total else 0.0


def densidad_academica(textos):
    if not textos:
        return 0.0
    con_marca = sum(1 for t in textos if any(r.search(t) for r in MARCAS_ACADEMICAS))
    return con_marca / len(textos)


def largo_mediano(textos):
    if not textos:
        return 0
    largos = sorted(len(t) for t in textos)
    return largos[len(largos) // 2]


FORMATOS_FECHA = ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ")


def parsear_fecha(valor):
    if not valor:
        return None
    s = str(valor).strip()
    for fmt in FORMATOS_FECHA:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def pedir(sesion, headers, ruta, params, intentos=4):
    espera = 2
    for intento in range(intentos):
        try:
            r = sesion.get(f"{BASE}/{ruta}", headers=headers, params=params, timeout=30)
        except requests.RequestException:
            if intento == intentos - 1:
                return None
            time.sleep(espera)
            espera *= 2
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504):
            if intento == intentos - 1:
                return None
            time.sleep(espera)
            espera *= 2
            continue
        return None   # 401/403/404: la cuenta no existe, es privada o esta suspendida
    return None


def campo_perfil(p, *nombres, defecto=None):
    for n in nombres:
        if p.get(n) not in (None, ""):
            return p[n]
    return defecto


# Pistas de bio para el canal de seguidos. No definen el tema (eso lo decide el
# puntaje contra el corpus): solo evitan pagar una muestra por el dentista y el
# club de futbol que la cuenta de referencia tambien sigue.
PISTAS_BIO = """
psicoanal psicolog psiquiatr terapeut terapia clinic freud lacan winnicott
analista inconsciente deseo subjetiv filosof pensamiento escritura escritor
poeta poesia literatura letras salud mental emocion duelo vinculo apego
humanidades docente catedra ensayo reflexion palabra
""".split()


def bio_promete(c):
    texto = normalizar((campo_perfil(c, "description", defecto="") or "") + " "
                       + (campo_perfil(c, "name", defecto="") or ""))
    return any(k in texto for k in PISTAS_BIO)


def prefiltrar(candidatos, referencia_handle):
    """
    Descarta lo que no puede ser una cuenta de divulgacion hispanohablante viva,
    antes de gastar un pedido de muestras por cuenta.
    """
    pasan, descartes = [], Counter()
    for c in candidatos:
        h = c["_handle"]
        if h == referencia_handle.lower():
            descartes["es la referencia"] += 1
            continue
        if campo_perfil(c, "protected", defecto=False):
            descartes["protegida"] += 1
            continue

        seguidores = campo_perfil(c, "followers", "followers_count", defecto=0) or 0
        if seguidores < 300:
            descartes["menos de 300 seguidores"] += 1
            continue

        posts = campo_perfil(c, "statusesCount", "statuses_count", defecto=0) or 0
        if posts < 200:
            descartes["menos de 200 posts"] += 1
            continue

        # Los canales de busqueda y menciones ya llegaron por el tema o por la
        # conversacion. El de seguidos es el ruidoso: ahi si pedimos senal en la bio.
        origen = set(c.get("_origen", ()))
        if origen == {"seguidos"} and not bio_promete(c):
            descartes["seguida sin senal tematica en la bio"] += 1
            continue

        pasan.append(c)
    return pasan, descartes


def traer_muestras(sesion, headers, handles, cache_ruta):
    """Trae los ultimos posts de cada handle. Cachea: bajar cuesta, repuntuar no."""
    cache = {}
    if cache_ruta.exists():
        with cache_ruta.open(encoding="utf-8") as f:
            for linea in f:
                try:
                    o = json.loads(linea)
                except json.JSONDecodeError:
                    continue
                cache[o["_handle"]] = o["tweets"]

    faltan = [h for h in handles if h not in cache]
    print(f"  muestras en cache: {len(cache)}; faltan {len(faltan)}")

    bajados = 0
    if faltan:
        with cache_ruta.open("a", encoding="utf-8") as f:
            for i, h in enumerate(faltan, 1):
                data = pedir(sesion, headers, "user/last_tweets",
                             {"userName": h, "count": POSTS_POR_MUESTRA})
                tweets = ((data or {}).get("data") or {}).get("tweets") or []
                # Solo lo que necesita el puntaje. El resto es peso muerto.
                livianos = [{"text": t.get("text") or "",
                             "createdAt": t.get("createdAt"),
                             "lang": t.get("lang"),
                             "id": t.get("id"),
                             "isReply": t.get("isReply"),
                             "esRT": bool(t.get("retweeted_tweet")),
                             "likeCount": t.get("likeCount")}
                            for t in tweets]
                cache[h] = livianos
                bajados += len(livianos)
                f.write(json.dumps({"_handle": h, "tweets": livianos},
                                   ensure_ascii=False) + "\n")
                if i % 25 == 0:
                    f.flush()
                    print(f"    {i}/{len(faltan)}  (@{h})", flush=True)
                time.sleep(0.12)
    return cache, bajados


def etiquetar(lexico):
    """Etiqueta tematica gruesa, por el vocabulario que domina."""
    familias = {
        "psicoanálisis": "deseo sujeto goce inconsciente analisis pulsion transferencia "
                         "lacan freud sintoma falta significante analista".split(),
        "filosofía": "pensamiento filosofia verdad existencia ser tiempo etica politica "
                     "nietzsche heidegger sentido mundo".split(),
        "psicología / clínica": "terapia paciente ansiedad emocional emociones trauma "
                                "psicologia mental conducta apego duelo".split(),
        "literatura / escritura": "libro leer escribir poesia poema literatura novela "
                                  "autor lectura escritura".split(),
    }
    total = sum(lexico.values()) or 1
    puntajes = {nombre: sum(lexico.get(w, 0) for w in palabras) / total
                for nombre, palabras in familias.items()}
    mejor, valor = max(puntajes.items(), key=lambda kv: kv[1])
    return mejor if valor > 0.004 else "general"


def main():
    ap = argparse.ArgumentParser(description="Puntua y rankea cuentas candidatas.")
    ap.add_argument("--candidatos", required=True)
    ap.add_argument("--referencia", required=True, help="corpus .jsonl de referencia")
    ap.add_argument("--user", help="handle de referencia (default: del nombre del archivo)")
    ap.add_argument("--top", type=int, default=80, help="cuantas cuentas listar")
    ap.add_argument("--salida")
    ap.add_argument("--dir", default=".")
    ap.add_argument("--sin-bajar", action="store_true",
                    help="usa solo lo que ya esta en cache, no pide nada a la API")
    args = ap.parse_args()

    key = os.environ.get("TWITTERAPI_IO_KEY")
    if not key and not args.sin_bajar:
        sys.exit("Falta la variable de entorno TWITTERAPI_IO_KEY")

    ruta_ref = Path(args.referencia)
    usuario = args.user or ruta_ref.stem.replace("corpus_", "")
    carpeta = Path(args.dir)

    # ---- Perfil de la referencia ----
    textos_ref = []
    for linea in ruta_ref.open(encoding="utf-8"):
        try:
            o = json.loads(linea)
        except json.JSONDecodeError:
            continue
        if not o.get("retweeted_tweet"):
            textos_ref.append(o.get("text") or "")
    if not textos_ref:
        sys.exit("El corpus de referencia no tiene textos legibles")

    lex_ref = perfil_lexico(textos_ref)
    jerga_ref = densidad_jerga(textos_ref)
    acad_ref = densidad_academica(textos_ref)
    largo_ref = largo_mediano(textos_ref)
    print(f"Referencia @{usuario}: {len(textos_ref)} posts, "
          f"largo mediano {largo_ref}, jerga {jerga_ref*1000:.2f}‰, "
          f"marcas academicas {acad_ref*100:.1f}%\n")

    # ---- Candidatos ----
    candidatos = []
    for linea in Path(args.candidatos).open(encoding="utf-8"):
        try:
            candidatos.append(json.loads(linea))
        except json.JSONDecodeError:
            continue
    print(f"Candidatos crudos: {len(candidatos)}")

    pasan, descartes = prefiltrar(candidatos, usuario)
    print(f"Pasan el prefiltro: {len(pasan)}")
    for motivo, n in descartes.most_common():
        print(f"  descartados por {motivo}: {n}")

    headers = {"X-API-Key": key} if key else {}
    sesion = requests.Session()
    cache_ruta = carpeta / f"muestras_{usuario}.jsonl"
    handles = [c["_handle"] for c in pasan]
    if args.sin_bajar:
        cache, bajados = {}, 0
        if cache_ruta.exists():
            for linea in cache_ruta.open(encoding="utf-8"):
                o = json.loads(linea)
                cache[o["_handle"]] = o["tweets"]
        print(f"  modo sin-bajar: {len(cache)} muestras en cache")
    else:
        print("\nMuestras de posts:")
        cache, bajados = traer_muestras(sesion, headers, handles, cache_ruta)

    # ---- IDF sobre el conjunto de candidatos: castiga lo que dicen todos ----
    docs = {}
    for c in pasan:
        h = c["_handle"]
        textos = [t["text"] for t in cache.get(h, [])
                  if not t.get("esRT") and t.get("text")]
        if textos:
            docs[h] = textos
    n_docs = len(docs) + 1
    apariciones = Counter()
    for h, textos in docs.items():
        apariciones.update(set(tokenizar(" ".join(textos))))
    apariciones.update(set(lex_ref))
    idf = {w: math.log(n_docs / (1 + n)) + 1.0 for w, n in apariciones.items()}

    # ---- Puntaje ----
    hace_seis_meses = datetime.now(timezone.utc) - timedelta(days=180)
    filas, descartes2 = [], Counter()

    for c in pasan:
        h = c["_handle"]
        muestra = cache.get(h, [])
        propios = [t for t in muestra if not t.get("esRT") and t.get("text")]
        if len(propios) < 5:
            descartes2["muestra insuficiente o cuenta vacia"] += 1
            continue

        langs = Counter(t.get("lang") for t in propios)
        if langs.get("es", 0) / len(propios) < 0.5:
            descartes2["no escribe mayoritariamente en espanol"] += 1
            continue

        fechas = [parsear_fecha(t.get("createdAt")) for t in propios]
        fechas = [f for f in fechas if f]
        if not fechas or max(fechas) < hace_seis_meses:
            descartes2["inactiva hace mas de 6 meses"] += 1
            continue

        textos = [t["text"] for t in propios]
        lex = perfil_lexico(textos)
        if not lex:
            descartes2["sin texto util"] += 1
            continue

        afinidad = coseno(lex_ref, lex, idf)

        # Registro: la jerga por encima de la referencia penaliza; por debajo, no.
        jerga = densidad_jerga(textos)
        exceso_jerga = max(0.0, jerga - jerga_ref)
        pena_jerga = min(1.0, exceso_jerga / 0.004)

        acad = densidad_academica(textos)
        pena_acad = min(1.0, max(0.0, acad - acad_ref) / 0.15)

        largo = largo_mediano(textos)
        # Distancia de largo en escala log: 60 vs 240 caracteres es otro genero.
        pena_largo = min(1.0, abs(math.log((largo or 1) / max(largo_ref, 1))) / 1.6)

        registro = 1.0 - (0.45 * pena_jerga + 0.30 * pena_acad + 0.25 * pena_largo)
        puntaje = afinidad * (0.55 + 0.45 * registro)

        filas.append({
            "handle": campo_perfil(c, "userName", "screen_name", defecto=h),
            "nombre": campo_perfil(c, "name", defecto=""),
            "bio": (campo_perfil(c, "description", defecto="") or "").replace("\n", " ").strip(),
            "seguidores": campo_perfil(c, "followers", "followers_count", defecto=0) or 0,
            "posts": campo_perfil(c, "statusesCount", "statuses_count", defecto=0) or 0,
            "origen": c.get("_origen", []),
            "menciones": c.get("_menciones", 0),
            "afinidad": afinidad,
            "registro": registro,
            "puntaje": puntaje,
            "jerga": jerga,
            "largo": largo,
            "ultimo": max(fechas),
            "etiqueta": etiquetar(lex),
            "ejemplo": max(textos, key=len).replace("\n", " ").strip(),
        })

    for motivo, n in descartes2.most_common():
        print(f"  descartados por {motivo}: {n}")

    filas.sort(key=lambda r: -r["puntaje"])
    top = filas[:args.top]

    # ---- Documento ----
    salida = Path(args.salida) if args.salida else carpeta / f"cuentas_similares_{usuario}.md"
    p = []
    p.append(f"# Cuentas afines a @{usuario}\n\n")
    p.append(f"{len(top)} cuentas hispanohablantes, ordenadas por parecido con el corpus "
             f"de @{usuario} (tema **y** registro divulgativo).\n\n")
    p.append(f"- Candidatas evaluadas: **{len(filas)}** (de {len(candidatos)} descubiertas)\n")
    p.append(f"- Generado: {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC\n\n")
    p.append("**Cómo leer las columnas.** *Afinidad* es el solapamiento de vocabulario "
             "con el corpus de referencia, pesado por IDF: mide el tema. *Registro* mide "
             "cuánto se parece el tono al de la referencia — baja con la jerga técnica, "
             "las marcas académicas (citas, páginas, seminarios) y los largos de post muy "
             "distintos. Una cuenta con afinidad alta y registro bajo habla de lo mismo "
             "pero escribe para colegas.\n\n")

    por_etiqueta = defaultdict(list)
    for r in top:
        por_etiqueta[r["etiqueta"]].append(r)
    p.append("| Grupo | Cuentas |\n|---|---|\n")
    for et, rs in sorted(por_etiqueta.items(), key=lambda kv: -len(kv[1])):
        p.append(f"| {et} | {len(rs)} |\n")
    p.append("\n---\n\n## Listado\n\n")
    p.append("| # | Cuenta | Seguidores | Grupo | Afin. | Reg. | Bio |\n")
    p.append("|---:|---|---:|---|---:|---:|---|\n")
    for i, r in enumerate(top, 1):
        bio = r["bio"][:110].replace("|", "\\|")
        p.append(f"| {i} | [@{r['handle']}](https://x.com/{r['handle']}) | "
                 f"{r['seguidores']:,} | {r['etiqueta']} | {r['afinidad']:.3f} | "
                 f"{r['registro']:.2f} | {bio} |\n")

    p.append("\n---\n\n## Fichas\n")
    for i, r in enumerate(top, 1):
        p.append(f"\n### {i}. [@{r['handle']}](https://x.com/{r['handle']}) — {r['nombre']}\n\n")
        p.append(f"{r['seguidores']:,} seguidores · {r['posts']:,} posts · "
                 f"último {r['ultimo']:%Y-%m-%d} · {r['etiqueta']}  \n")
        p.append(f"afinidad {r['afinidad']:.3f} · registro {r['registro']:.2f} · "
                 f"largo mediano {r['largo']} car. · descubierta por "
                 f"{', '.join(r['origen']) or 'n/d'}")
        if r["menciones"]:
            p.append(f" · {r['menciones']} menciones en el corpus")
        p.append("\n")
        if r["bio"]:
            p.append(f"\n> {r['bio']}\n")
        ejemplo = r["ejemplo"]
        if len(ejemplo) > 400:
            ejemplo = ejemplo[:400] + "…"
        p.append(f"\n<sub>post de muestra:</sub> {ejemplo}\n")

    salida.write_text("".join(p), encoding="utf-8")

    print(f"\nArchivo: {salida}")
    print(f"Evaluadas: {len(filas)} | En el listado: {len(top)}")
    print("\nPor grupo:")
    for et, rs in sorted(por_etiqueta.items(), key=lambda kv: -len(kv[1])):
        print(f"  {et:<24} {len(rs):>3}")
    print("\nPrimeras 15:")
    for i, r in enumerate(top[:15], 1):
        print(f"  {i:>2}. @{r['handle']:<22} {r['seguidores']:>8,}  "
              f"afin {r['afinidad']:.3f}  reg {r['registro']:.2f}  {r['etiqueta']}")
    if bajados:
        print(f"\nCosto estimado de esta corrida: USD {bajados * PRECIO_POR_TWEET_USD:.2f}")
    else:
        print("\nSin pedidos a la API: se uso el cache. Costo: USD 0.00")


if __name__ == "__main__":
    main()
