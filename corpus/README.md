# Corpus de X → documento .md

Baja los posts de una cuenta pública de X con la API de
[twitterapi.io](https://twitterapi.io) y los arma en un `.md` ordenado
cronológicamente, con los hilos reconstruidos como bloque único.

## Los scripts

```
bajar_corpus.py       baja los posts y los guarda crudos en un .jsonl
exportar_md.py        lee el .jsonl y arma el .md
buscar_similares.py   arma un listado de cuentas afines por grafo de seguidos
```

El `.jsonl` crudo es **la fuente de verdad** y no se toca nunca: volver a
bajarlo cuesta plata. El `.md` se puede regenerar todas las veces que quieras
sin gastar un centavo.

## Uso

Cuenta en curso: **@ePerezJandette** (psicoanálisis, filosofía del lenguaje;
activa al menos entre abril 2024 y octubre 2025).

```bash
export TWITTERAPI_IO_KEY="tu_api_key"

# 1. Prueba: baja un solo mes, para ver que la key funciona y qué trae
python bajar_corpus.py --user ePerezJandette --ultimos 1500 --sin-respuestas --prueba

# 2. Ver la estructura real de los datos y el mapeo de campos
python exportar_md.py --jsonl corpus_ePerezJandette.jsonl --inspeccionar

# 3. Bajada completa (reanudable: si se corta, relanzá lo mismo)
python bajar_corpus.py --user ePerezJandette --ultimos 1500 --sin-respuestas

# 4. Armar el documento
python exportar_md.py --jsonl corpus_ePerezJandette.jsonl

# 5. Cuentas afines. Primero verificar las rutas de la API, después estimar,
#    y recién ahí correrlo en serio.
python buscar_similares.py --user ePerezJandette --verificar
python buscar_similares.py --user ePerezJandette --estimar
python buscar_similares.py --user ePerezJandette --top 100 --solo-hispano
```

`--ultimos N` camina mes a mes hacia atrás desde hoy hasta juntar N posts, así
que **no necesitás saber cuándo empezó la cuenta**. Si querés el archivo
histórico completo en vez de los más recientes, usá `--desde AAAA-MM --hasta
AAAA-MM`.

### Opciones que quizás quieras

| Flag | Qué hace |
|---|---|
| `--sin-respuestas` | excluye las respuestas a terceros de la bajada |
| `--incluir-retweets` | incluye retweets en el `.md` (por defecto se excluyen: no son texto propio) |
| `--modo-bloque` | envuelve cada post en una cerca de código, para que el renderizado respete los saltos de línea exactamente |
| `--prueba` | baja una sola ventana mensual y corta |

## Costo

USD 0,15 por cada 1.000 posts (confirmá la tarifa vigente en
twitterapi.io/pricing). 1.500 posts ≈ **USD 0,22**. Cada script imprime el
costo estimado de la corrida al terminar.

## La regla que no se negocia

El texto de cada post va **exactamente como vino de la API**: no se normalizan
comillas ni apóstrofes, no se colapsan espacios ni saltos de línea, no se sacan
URLs, emoji, hashtags ni menciones, no se corrige ortografía ni tipeos.

Lo que a un script le parece suciedad es, acá, el objeto de estudio: cómo
puntúa esta persona, dónde corta el renglón, si usa mayúsculas, cómo abrevia.

## Qué reporta al terminar

- posts por tipo (`original` / `hilo` / `respuesta` / `cita` / `retweet`)
- rango de fechas real cubierto
- **meses sin ningún post**, para detectar huecos de la bajada
- los 5 posts más largos y los 5 más cortos, para confirmar que no hay truncamiento
- cantidad de hilos reconstruidos y su largo promedio

## Nota sobre los nombres de campo

La API puede nombrar los campos de más de una manera según la versión.
`exportar_md.py` prueba las variantes conocidas de cada uno y, si no encuentra
alguno, lo reporta en vez de inventar un valor. Corré `--inspeccionar` para ver
el mapeo real contra tus datos antes de confiar en la clasificación.
