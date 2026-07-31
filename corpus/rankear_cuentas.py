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
borromeo borromeano sinthome sinthoma metapsicologia topica epistemologia
hermeneutica fenomenologico fenomenologica ontologico ontologica
esquizoanalisis grafo automaton tyche agalma extimidad extimo parletre
holofrase discordancial semblante semblantes retroaccion aprescoup
significantizacion simbolizacion imaginarizacion pulsional pulsionales
metonimia metonimico condensacion desplazamiento supervision cartel carteles
psicopatologia nosografia nosologia diacronico sincronico epistemico praxis
teleologico dialectica dialectico libidica libidinal catexis anaclitico
""".split())

# Vocabulario del campo. No define el ranking (eso lo hace el parecido con el
# corpus real): sirve de aduana, para que no entre una cuenta de politica que
# comparte con la referencia cuatro palabras emotivas y nada mas.
CAMPO = set("""
psicoanalisis psicoanalitico psicoanalista analizante analista inconsciente
deseo sujeto goce pulsion transferencia sintoma freud lacan jung winnicott
psicologia psicologo psicologa psicoterapia terapia terapeuta paciente
angustia duelo trauma apego subjetividad psiquico psiquica psiquismo
neurosis melancolia narcisismo represion libido
filosofia filosofico filosofa ontologia metafisica existencial
""".split())

# Autoayuda y pseudociencia. Comparten el tema con la referencia (el amor, el
# dolor, el vinculo) pero no el genero: prometen resultados en vez de abrir
# preguntas. Es exactamente lo que hay que dejar afuera.
AUTOAYUDA = (
    re.compile(r"\bel\s+(secreto|truco)\b", re.I),
    re.compile(r"\b\d+\s+(claves|razones|pasos|habitos|senales|tips|frases)\b", re.I),
    re.compile(r"\b(deja|empieza|comienza)\s+de?\s+\w+", re.I),
    re.compile(r"\btu\s+vida\s+(cambia|cambiara)\b", re.I),
    re.compile(r"\b(abro|va)\s+hilo\b", re.I),
    re.compile(r"\b(guarda|comparte|sigueme|siguelo)\b", re.I),
    re.compile(r"\b(biodescodificacion|descodificacion|epigenetic|cuantic|"
               r"vibracion|manifestar|abundancia|ley\s+de\s+atraccion|reiki|"
               r"chakra|alma\s+gemela|el\s+universo\s+conspira|sanacion|"
               r"holistic|frecuencia\s+vibratoria)", re.I),
    re.compile(r"^[^a-z\n]{14,}$", re.M),   # titular en mayusculas sostenidas
)

# Cuentas que no son una voz: editoriales, medios, catedras, instituciones.
INSTITUCION = re.compile(
    r"\b(editorial|revista|universidad|facultad|instituto|colegio|libreria|"
    r"radio|diario|periodico|noticias|redaccion|agencia|fundacion|asociacion|"
    r"congreso|jornadas|seminario\s+de|maestria|licenciatura|posgrado|catedra|"
    r"academia|festival|feria|coordinadora|colectivo|sindicato|partido|"
    r"portal|podcast|canal\s+de|oficial|somos\s+un|somos\s+una)\b", re.I)

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


def densidad_autoayuda(textos):
    if not textos:
        return 0.0
    con_marca = sum(1 for t in textos if any(r.search(t) for r in AUTOAYUDA))
    return con_marca / len(textos)


def topicalidad(textos):
    """Fraccion de posts que tocan el campo. Es la aduana, no el ranking."""
    if not textos:
        return 0.0
    dentro = sum(1 for t in textos if any(w in CAMPO for w in tokenizar(t)))
    return dentro / len(textos)


def densidad_link(textos):
    """
    Fraccion de posts con enlace. Separa la voz de la marca mejor que cualquier
    palabra clave: quien escribe postea texto, quien promociona postea enlaces.
    La referencia esta en 0,03; los medios y las marcas, entre 0,50 y 1,00.
    """
    if not textos:
        return 0.0
    return sum(1 for t in textos if "http" in t) / len(textos)


def pesos_distintivos(lex_ref, lex_pool, minimo=40, tope=400):
    """
    Que palabras distinguen a la referencia del monton, por log-odds con prior
    informativo (Monroe, Colaresi y Quinn). El coseno plano no alcanza: "amor" y
    "vida" las usa tanto ella como cualquier cuenta de autoayuda, y son
    justamente las que inflaban el puntaje equivocado.
    """
    n_ref = sum(lex_ref.values())
    n_pool = sum(lex_pool.values())
    if not n_ref or not n_pool:
        return {}
    a0 = 0.01
    z = {}
    for w in set(lex_ref) | set(lex_pool):
        r, p = lex_ref.get(w, 0), lex_pool.get(w, 0)
        if r + p < minimo or r == 0:
            continue
        lr = math.log((r + a0) / (n_ref - r + a0))
        lp = math.log((p + a0) / (n_pool - p + a0))
        var = 1 / (r + a0) + 1 / (p + a0)
        valor = (lr - lp) / math.sqrt(var)
        if valor > 0:
            z[w] = valor
    return dict(sorted(z.items(), key=lambda kv: -kv[1])[:tope])


def afinidad_firma(lex_cand, pesos):
    """
    Cuanto de la firma lexica de la referencia reaparece en el candidato.
    Se pondera por el peso de cada termino y se satura por termino, para que
    repetir una sola palabra mil veces no simule parecido.
    """
    total = sum(pesos.values())
    if not total or not lex_cand:
        return 0.0
    n_cand = sum(lex_cand.values()) or 1
    acumulado = 0.0
    for w, peso in pesos.items():
        tasa = lex_cand.get(w, 0) / n_cand
        # 0.002 = dos apariciones cada mil palabras alcanza para dar el termino
        # por presente; mas que eso no suma.
        acumulado += peso * min(1.0, tasa / 0.002)
    return acumulado / total


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
    """
    Etiqueta tematica gruesa, por el vocabulario que domina.

    Las familias llevan solo terminos propios del campo. Con palabras genericas
    ("mundo", "sentido", "vida") la etiqueta de filosofia se comia a todas.
    """
    familias = {
        "psicoanálisis": "psicoanalisis psicoanalitico analista inconsciente goce "
                         "pulsion transferencia lacan freud sintoma significante "
                         "sujeto deseo falta".split(),
        "filosofía": "filosofia filosofico nietzsche heidegger spinoza deleuze foucault "
                     "ontologia metafisica etica epistemologia existencialismo "
                     "estoicismo platon aristoteles".split(),
        "psicología / clínica": "terapia terapeuta paciente psicologia psicologo psicologa "
                                "ansiedad emocional emociones trauma apego conducta "
                                "consulta psiquiatria diagnostico".split(),
        "literatura / escritura": "poesia poema literatura novela cuento verso escritura "
                                  "escritor escritora narrativa lectura prosa".split(),
    }
    total = sum(lexico.values()) or 1
    puntajes = {nombre: sum(lexico.get(w, 0) for w in palabras) / total
                for nombre, palabras in familias.items()}
    mejor, valor = max(puntajes.items(), key=lambda kv: kv[1])
    return mejor if valor > 0.003 else "reflexión general"


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
    ap.add_argument("--largo-min", type=int, default=50,
                    help="largo mediano minimo de post, en caracteres (default: 50)")
    ap.add_argument("--largo-max", type=int, default=700,
                    help="largo mediano maximo; arriba de eso es otro genero (default: 700)")
    ap.add_argument("--max-autoayuda", type=float, default=0.25,
                    help="fraccion maxima de posts con marcas de autoayuda (default: 0.25)")
    ap.add_argument("--min-topicalidad", type=float, default=0.15,
                    help="fraccion minima de posts que tocan el campo (default: 0.15)")
    ap.add_argument("--max-links", type=float, default=0.45,
                    help="fraccion maxima de posts con enlace; arriba de eso es una "
                         "marca o un medio, no una voz (default: 0.45)")
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

    # ---- Firma lexica de la referencia contra el pool ----
    lex_pool = Counter()
    for c in pasan:
        for t in cache.get(c["_handle"], []):
            if not t.get("esRT") and t.get("text"):
                lex_pool.update(tokenizar(t["text"]))
    pesos = pesos_distintivos(lex_ref, lex_pool)
    print(f"\nFirma lexica: {len(pesos)} terminos distintivos. Los 12 primeros: "
          + ", ".join(list(pesos)[:12]))

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

        # ---- Aduanas: esto no descuenta puntos, deja afuera ----
        # Un ensayo motivacional de 3.000 caracteres no es "un poco menos
        # parecido" que un post de 160: es otro genero. Descontarle un cuarto
        # de punto, como hacia la version anterior, lo dejaba primero igual.
        largo = largo_mediano(textos)
        if largo > args.largo_max or largo < args.largo_min:
            descartes2[f"largo mediano fuera de {args.largo_min}-{args.largo_max} car."] += 1
            continue

        autoayuda = densidad_autoayuda(textos)
        if autoayuda > args.max_autoayuda:
            descartes2["registro de autoayuda o pseudociencia"] += 1
            continue

        topico = topicalidad(textos)
        if topico < args.min_topicalidad:
            descartes2["no habla del campo (tema ajeno)"] += 1
            continue

        perfil_texto = ((campo_perfil(c, "description", defecto="") or "") + " "
                        + (campo_perfil(c, "name", defecto="") or ""))
        if INSTITUCION.search(normalizar(perfil_texto)):
            descartes2["institucion o medio, no una voz"] += 1
            continue

        links = densidad_link(textos)
        if links > args.max_links:
            descartes2["postea mas enlaces que texto (marca o medio)"] += 1
            continue

        # ---- Ejes ----
        afinidad = afinidad_firma(lex, pesos)

        # Registro: la jerga por encima de la referencia penaliza; por debajo, no.
        jerga = densidad_jerga(textos)
        exceso_jerga = max(0.0, jerga - jerga_ref)
        pena_jerga = min(1.0, exceso_jerga / 0.004)

        acad = densidad_academica(textos)
        pena_acad = min(1.0, max(0.0, acad - acad_ref) / 0.15)

        # Distancia de largo en escala log: 60 vs 240 caracteres es otro genero.
        pena_largo = min(1.0, abs(math.log((largo or 1) / max(largo_ref, 1))) / 1.2)
        pena_autoayuda = min(1.0, autoayuda / max(args.max_autoayuda, 0.01))

        registro = 1.0 - (0.30 * pena_jerga + 0.20 * pena_acad
                          + 0.30 * pena_largo + 0.20 * pena_autoayuda)
        registro = max(0.0, registro)
        # Sin piso: una cuenta con el registro equivocado no conserva medio
        # puntaje por hablar del mismo tema.
        puntaje = afinidad * registro

        filas.append({
            "autoayuda": autoayuda,
            "topicalidad": topico,
            "links": links,
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
