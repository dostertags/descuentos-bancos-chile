# Descuentos en restaurantes de bancos chilenos

Script en Python que junta en un solo lugar los descuentos de 7 bancos chilenos: BICE, BCI,
Banco de Chile, Santander, Security, Itaú y Scotiabank. Se enfoca en restaurantes (rutas
gourmet, "martes gourmet", etc.), aunque para algunos bancos trae todos sus beneficios.

**Cómo funciona:** cada banco publica sus descuentos en su sitio web. El script consulta las
mismas fuentes que usa cada sitio: una API JSON cuando existe, o el HTML de la página cuando
no. Luego convierte todo a un formato común (nombre, descuento, días, dirección, condiciones,
vigencia e imágenes) y guarda un archivo JSON por banco.

```bash
pip install -r requirements.txt
python fetch_benefits.py      # descarga todo a output/*.json (~3 min)
python to_excel.py            # crea output/beneficios.xlsx
```

Proyecto personal, sin relación con los bancos. Los datos pertenecen a cada banco y pueden
cambiar sin aviso.

---

## Detalle técnico

Descarga los descuentos/beneficios de **BICE, BCI, Banco de Chile, Santander, Banco Security
(restaurantes), Itaú (restaurantes) y Scotiabank (Ruta Gourmet)** desde los mismos endpoints públicos que usan sus sitios web
y los guarda en un esquema común. Sin navegador headless. Itaú es la única fuente que se obtiene
por **HTML** (fallback; ver abajo): no publica ningún endpoint JSON.

## Instalación

```bash
pip install curl_cffi tenacity python-dateutil beautifulsoup4
pip install pytest   # sólo para los tests
```

## Uso

```bash
python fetch_benefits.py --out ./output --sites all
python fetch_benefits.py --sites bci,santander      # uno o varios sitios
python fetch_benefits.py --sites itau                # sólo restaurantes Itaú (~2 min)
python fetch_benefits.py --sites scotia              # Ruta Gourmet Scotiabank (1 request)
python fetch_benefits.py --raw                      # además guarda la respuesta cruda en output/raw/
```

`--sites` acepta `all` o una lista separada por comas de `bice`, `bci`, `bancochile`, `santander`, `security`, `itau`, `scotia`.

Salida:

```
output/
  bice_beneficios.json
  bci_beneficios.json
  bancochile_personas_beneficios.json
  bancochile_seguros.json
  santander_promociones.json
  security_gourmet_restaurantes.json
  bancoitau_restaurantes.json
  scotia_ruta_gourmet.json
  _manifest.json   # timestamps, conteo y URL de origen por dataset, errores
  errors.log       # sólo errores; un sitio que falla no detiene a los demás
```

El script termina con código 1 si algún dataset falló (útil para el Programador de tareas).

## Esquema unificado

```json
{
  "source": "bice|bci|bancochile|santander|security|itau|scotia",
  "id": "...", "title": "...", "description": "...", "url": "...",
  "category": "...", "tags": ["..."], "images": ["..."], "conditions": "...",
  "merchant": "...", "discount": "20%", "valid_from": "ISO-8601|null", "valid_to": "ISO-8601|null",
  "location": "...", "days": "..."
}
```

HTML se convierte a texto plano. `merchant`, `discount`, `valid_from`, `valid_to`, `location`, `days` son extras
sobre el esquema pedido; quedan vacíos cuando la fuente no los entrega (p. ej. Santander no
publica fechas, y las ofertas de cuotas sin interés no tienen %).

## Endpoints

| Sitio | Endpoint | Paginación | Notas |
|---|---|---|---|
| BICE | `https://banco.bice.cl/api/content/spaces/beneficios-bice/types/beneficios/entries` | `page`, `per_page=100` | Modyo. 403 a `requests` → requiere curl_cffi |
| BCI | `https://api.bciplus.cl/bff-loyalty-beneficios/v1/offers` | `pagina`, `itemsPorPagina=100` | Header `Ocp-Apim-Subscription-Key` (clave pública que la web envía desde el navegador) |
| Banco de Chile | `https://sitiospublicos.bancochile.cl/api/content/spaces/personas/types/beneficios/entries` | `page`, `per_page=100` | Modyo. Filtrar por categoría con `tags=<slug>` |
| Banco de Chile (seguros) | `.../spaces/personas/types/seguro/entries` | igual | Productos de seguro, no descuentos |
| Santander | `https://banco.santander.cl/beneficios/promociones.json` | `page`, `per_page=500` | 403 a `requests` → requiere curl_cffi |
| Itaú (restaurantes) — **HTML fallback** | Páginas de listado `ITAU_LISTING_PAGES` + detalle de cada beneficio en `https://itaubeneficios.cl` | Sin paginación; se unen y deduplican por URL | Filtro por categoría con `ITAU_RESTAURANT_KEYWORDS` |
| Scotiabank (Ruta Gourmet) | `GET https://www.scotiarewards.cl/scclubfront/categoria/platosycomida/rutagourmet` | Sin paginación (1 página) | Datos embebidos en el HTML como arreglos JS `sitiosSantiago` / `sitiosRegiones` |
| Security (Gourmet → Restaurantes) | `POST https://personas.bancosecurity.cl/views/ajax` | `page` en el query string, 12 por página, hasta que no haya `a[rel=next]` | Drupal Views AJAX: HTML dentro de `commands[].data`, parseado con BeautifulSoup |

Por qué `curl_cffi` y no `requests`: BICE y Santander bloquean por huella TLS; `requests`
recibe 403 aunque se envíe un User-Agent de Chrome. `curl_cffi` imita el TLS de Chrome y tiene
la misma API que `requests`. Reintentos: `tenacity` con backoff exponencial (5 intentos),
respetando `Retry-After` en 429/5xx. Pausa aleatoria de 1–2 s entre páginas.

## Si un sitio cambia

Todos los endpoints se definen en la lista `DATASETS` de `fetch_benefits.py`; cada uno tiene su
URL, su forma de paginar y su función `norm_*`. Para redescubrir un endpoint:

1. **Sitios Modyo (BICE, Banco de Chile, BCI):** abrir la página de beneficios y buscar en el HTML
   `/api/content/spaces/` o los `widget-definition" id="modyo-<uuid>/<hash>"`. Descargar
   `<sitio>/widget_manager/<uuid>/<hash>.js` y buscar `api/content/spaces/<space>/types/<type>`,
   `baseURL` o `axios`. El nombre del space/type aparece también en cada entrada (`meta.space`, `meta.type`).
2. **BCI:** el JS del widget de `https://www.bci.cl/beneficios/beneficios-bci` define
   `baseURL = 'https://api.bciplus.cl/...'` y la `Ocp-Apim-Subscription-Key`. Si la clave rota,
   copiarla de ahí a `BCI_SUBSCRIPTION_KEY`.
3. **Santander:** DevTools → Network → filtrar por `.json` en la página de beneficios; buscar URLs
   con `promociones` o `beneficios`.
4. Si cambia la forma de los campos, ajustar la función `norm_*` correspondiente. Correr con
   `--raw` para ver la respuesta original.

El endpoint `widget_manager/...json` de la página "Sabores" de Banco de Chile que venía en la
especificación original devuelve 404; no se usa (la API de beneficios ya incluye esos registros).

### Banco Security

Configuración al inicio de `fetch_benefits.py`: `SECURITY_BASE_URL`, `SECURITY_CATEGORY_ID`
(121 = Restaurantes; en la misma página: 206 Comida Rápida, 241 Saludable, 171 Vinos y Licores,
231 Supermercado), `SECURITY_MAX_PAGES`, `SECURITY_TIMEOUT`.

Ojo: Drupal lee `page` y los filtros expuestos (`field_categorias_beneficio_target_id[121]`)
**del query string**. Si sólo van en el cuerpo del POST se ignoran en silencio y cada página
devuelve las mismas 12 tarjetas de todo Gourmet. Si el sitio cambia, revisar el formulario
`views-exposed-form-beneficios-block-2` en `/beneficios/gourmet` (nombres de filtros y
`view_display_id`/`view_path` en `drupalSettings.views`) y el HTML de las tarjetas
(`article[data-history-node-id]`) en `parse_security_cards`.

### Itaú — fallback HTML (limitaciones)

**Descubrimiento:** `itaubeneficios.cl` es WordPress, pero la REST API está desactivada: `/wp-json/`,
`/wp-json/wp/v2/types`, `/wp-json/wp/v2/beneficios` y `?rest_route=/...` devuelven la portada en HTML.
Tampoco hay `wp-sitemap.xml`/`sitemap_index.xml` (404), feed, ni `admin-ajax.php` público (403).
Por eso `fetch_itau` es un **fallback HTML** (marcado así en el código):

1. Descarga las páginas de `ITAU_LISTING_PAGES` y parsea las tarjetas `a.beneficio__item`
   (título, comuna, Presencial/Online, % de descuento, categoría, imagen y logo).
   Ninguna página pagina; el listado general (`/beneficios/beneficios-y-descuentos/`) **no trae todo**
   (le faltan p. ej. algunos de `/restaurantes/` y de Ruta Gourmet), así que se unen las tres y se
   deduplica por URL. Las páginas `/lunes-gourmet/` ... `/domingo-gourmet/` no se usan: el filtro por
   día se hace en el navegador y todas devuelven las mismas 13 tarjetas.
2. Filtra restaurantes comparando, sin tildes y por palabra completa, la **categoría de la tarjeta**
   (p. ej. "Restaurantes", "Martes Gourmet") y el primer segmento de la URL contra
   `ITAU_RESTAURANT_KEYWORDS`. Editar esa lista para ampliar/restringir. Las páginas de listado
   mezclan destacados de otras categorías (p. ej. Cabify en `/restaurantes/`); el filtro los descarta.
3. Para cada restaurante descarga la página de detalle (1–2 s de pausa entre cada una, ~2 min en
   total) y extrae: ID de WordPress (`postid-NNN` en `<body>`), secciones "Beneficio" + "Descripción"
   → `description`, "Restricciones" → `conditions`, fechas "Válido desde/hasta" (`dd-mm-yyyy`) y
   galería. Si un detalle falla, se registra en `errors.log` y se conserva el registro con los datos
   de la tarjeta (el `id` pasa a ser la URL). `ITAU_FETCH_DETAILS = False` omite este paso.

Mapeo al esquema: `category` = categoría de la tarjeta, `tags` = [Presencial|Online],
`images` = [fondo, logo, galería...], `discount` = % de la tarjeta, `location` = comuna/región.
En `_manifest.json`, `source_url` lista las páginas de listado usadas.

Limitaciones: depende de las clases CSS del tema (`beneficio__item`, `beneficio__sidebar`,
`beneficio__information__texto`); si Itaú rediseña el sitio hay que ajustar `parse_itau_listing` /
`parse_itau_detail`. Si aparece un restaurante en una página de listado que no está en
`ITAU_LISTING_PAGES`, no se descarga: revisar el menú de `https://itaubeneficios.cl/` al actualizar.
Si en el futuro Itaú habilita `/wp-json/wp/v2/<tipo>`, conviene reemplazar el fallback por ese endpoint
(mismo patrón que `paginate`).

### Scotiabank — Ruta Gourmet

No hay API ni AJAX: la página trae los restaurantes en dos arreglos JS dentro de un `<script>`
(`const sitiosSantiago = [...]` y `const sitiosRegiones = [...]`, que la página luego concatena).
`extract_js_array` localiza cada `const <nombre> = [` y recorre el texto balanceando corchetes
(ignorando los que están dentro de strings) hasta el `]` de cierre, y lo parsea con `json.loads`
(son JSON válido). Configuración: `SCOTIA_URL`, `SCOTIA_ARRAYS`, `CHILE_REGIONS`.

Si Scotiabank rediseña la página y un arreglo desaparece o deja de ser JSON, el scraper **falla de
forma explícita** (`ValueError` con el nombre del arreglo): queda en `errors.log` y en
`_manifest.json`, y el resto de los bancos sigue. No se devuelve una lista vacía silenciosa.

Mapeo:

| Campo Scotiabank | Esquema unificado |
|---|---|
| `id_sitio` | `id` |
| `nombre` | `title`, `merchant` (sin espacios sobrantes) |
| `telefono` ("50% Dcto.") | `discount` = "50%" (el campo se llama teléfono pero trae el descuento) |
| `especialidad` ("Lunes a jueves") | `days` = "Lunes, Martes, Miércoles, Jueves" (rangos expandidos) + un tag por día |
| `direccion` (varias separadas por `\|`) + `id_region` | `location` = "dir1 \| dir2 \| Región" |
| `id_region` | región con la numeración oficial de Chile (13 = Metropolitana, 8 = Biobío, ...), en `tags` |
| `web` | `url` (absoluta, sin espacios/NBSP; si falta, la página de Ruta Gourmet) |
| `imagen` | `images` (absoluta con `https://www.scotiarewards.cl`) |
| `descripcion` sección 3 (especificaciones) | `description` |
| `descripcion` sección 4 (texto legal) | `conditions` |
| `descripcion` secciones 2, 6, 7 | tags: comuna, "Pet friendly", "Ruta temática" |
| fechas en secciones 0 y 4 | `valid_to` (la más tardía) |

`descripcion` viene como HTML con secciones separadas por `|`: 0 vigencia, 1 descuento, 2 comuna,
3 especificaciones, 4 legal, 5 código de tarjetas (`focovisa`/`todas`), 6 `pet-friendly`/`x`,
7 ruta especial (`rutatematica`). `valid_to` toma la fecha más tardía entre la vigencia y el
texto legal porque hay fichas con la vigencia desactualizada (p. ej. vigencia "30 de junio" y
legal "30 de septiembre" para un restaurante aún listado).

No hay página de detalle por restaurante, así que `url` apunta al sitio del restaurante.

## Tests

```bash
python -m pytest tests -q               # unitarios + integración con HTTP simulado
RUN_LIVE=1 python -m pytest tests -k live   # smoke test contra el sitio real
```

## Ejecución diaria (Windows)

```bash
schtasks /Create /SC DAILY /ST 08:00 /TN "Beneficios bancos" /TR "python C:\ruta\al\repo\fetch_benefits.py --out C:\ruta\al\repo\output"
```
