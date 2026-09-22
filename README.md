# Panel de seguridad — México

Panel que muestra solo notas de seguridad e inseguridad de todo el país, con
medios nacionales y locales de las 32 entidades. Mismo funcionamiento que el
panel de Baja California: un GitHub Action recolecta titulares cada 3 horas,
Claude se queda con los de seguridad y los resume, y se regenera `index.html`.
Las notas se acumulan durante el día y el panel se reinicia a medianoche,
hora del centro de México.

**Categorías:** homicidios y feminicidios · crimen organizado · robos,
extorsión y otros delitos · desaparecidos y búsqueda · justicia y sentencias ·
política de seguridad · accidentes viales. Arriba hay un selector para ver un
solo estado; la liga se puede compartir ya filtrada (por ejemplo
`.../#estado=Sinaloa`).

---

## Puesta en marcha

### 1. Súbelo a GitHub

Crea un repositorio nuevo (por ejemplo `PanelSeguridadMX`), público para que
Actions y Pages no cuesten. Luego, desde esta carpeta:

```bash
git init
git add .
git commit -m "Panel nacional de seguridad"
git branch -M main
git remote add origin https://github.com/DanaCorres/PanelSeguridadMX.git
git push -u origin main
```

O desde el navegador: *Add file → Upload files* y arrastra todo, incluida la
carpeta `.github` (en Mac se muestra con `Cmd + Shift + .`).

### 2. Activa GitHub Pages

*Settings → Pages → Deploy from a branch* → rama `main`, carpeta `/ (root)`.

### 3. Agrega la API key de Claude

*Settings → Secrets and variables → Actions → New repository secret*.
Nombre: `ANTHROPIC_API_KEY`. Valor: tu llave de console.anthropic.com. Puedes
usar la misma de tus otros paneles.

### 4. Permiso de escritura

*Settings → Actions → General → Workflow permissions → Read and write
permissions*. Sin esto el Action no puede guardar el panel actualizado.

### 5. Verifica las fuentes (importante la primera vez)

*Actions → Verificar fuentes → Run workflow*. Tarda unos minutos y no cambia
nada del panel: solo imprime en el log, estado por estado, qué medio responde:

- `✓` responde por su propio sitio.
- `~` su sitio no responde (bloqueo, caído), pero sí aparece en Google Noticias.
  Funciona, solo que la liga pasa por news.google.com.
- `✗` no se encontró por ninguna vía: el dominio está mal o el medio ya no
  existe. Corrígelo o quítalo de `fuentes.yaml`.

Los medios marcados "(propuesta sin verificar)" son los que se agregaron sin
haberlos probado antes en tus paneles. Son los primeros a revisar.

### 6. Primera actualización

*Actions → Actualizar panel de seguridad → Run workflow*. A partir de ahí
corre solo a las 7:00, 10:00, 13:00, 16:00, 19:00 y 22:00 (hora del centro).

**Ojo con el primer día.** Muchos medios se leen de su página (sin fecha en
cada nota). Para que el panel no se llene de notas de la semana pasada, la
primera vez que se lee un medio así todo lo que trae se da por visto. Desde la
segunda corrida entra solo lo que aparece nuevo. Por eso el primer día el
panel se ve más flaco de lo normal.

---

## Cómo está armado

```
fuentes.yaml                 medios, filtro de palabras y ajustes (lo único que editas)
scripts/recolectar.py        descarga en paralelo, filtra por fecha y palabras -> data/candidatas.json
scripts/curar.py             Claude clasifica y resume, acumula el día -> index.html
scripts/verificar_fuentes.py reporte de qué medio responde
templates/index_template.html  diseño de la página
.github/workflows/           las dos tareas: actualizar (automática) y verificar (a mano)
data/                        memoria del panel: notas del día, ligas vistas, RSS descubiertos
```

**De dónde salen los titulares de cada medio**, en este orden:

1. `feed`: el RSS ya confirmado en tus paneles de BC, NL y nacional.
2. `oem`: la sección policiaca del diario en oem.com.mx (los "El Sol de…").
3. `pagina`: una sección concreta (policiaca, justicia, la del estado).
4. Solo `sitio`: se abre el home y se busca su RSS; si no tiene, se leen los
   titulares del home. El RSS que encuentra se guarda en
   `data/feeds_descubiertos.json` para no volver a buscarlo.
5. Respaldo: si nada de lo anterior trajo notas, se busca en Google Noticias
   `site:dominio` más palabras de seguridad. Así se recuperan los medios que
   bloquean a GitHub, como Canal 66 o Reporte Índigo. Para medios que
   comparten dominio (Milenio, Por Esto!, La Jornada Maya) se busca con el
   nombre del estado, para no traer la portada nacional del grupo.

**Los dos filtros:**

1. *Palabras clave* (gratis): en `fuentes.yaml > filtro`. Solo los titulares
   que traen alguna palabra de seguridad llegan a Claude. Es amplio a
   propósito; si notas que algo importante no llega, agrega la palabra ahí
   (sin acentos). Las secciones policiacas y lo que viene de Google Noticias
   se saltan este filtro porque ya vienen filtrados de origen.
2. *Curaduría con Claude*: descarta lo que suena a seguridad pero no lo es
   (series de crimen, "seguridad" de apps, IMSS, deportes), clasifica, asigna
   el estado donde ocurrió el hecho (no el del medio) y junta notas repetidas:
   si varios medios cuentan lo mismo, queda una con "+N medios".

Cada titular se le manda a Claude una sola vez al día. Lo ya evaluado no se
vuelve a pagar en la siguiente corrida.

## Costo

Con el modelo Haiku, el costo depende de cuántas notas de seguridad haya en el
día. Como referencia, del orden de 10 a 20 dólares al mes. Cada corrida
imprime en el log los tokens usados y el costo aproximado en dólares
(paso "Curar con Claude"), así que en la primera semana sabrás la cifra real.

Si quieres bajarlo: quita horarios del `cron` en
`.github/workflows/actualizar.yml`, o cierra el filtro de palabras.

## Ajustes útiles en `fuentes.yaml`

- `max_por_estado`: notas máximas por estado en el día (30). Si se llena, se
  quedan primero las de relevancia alta y luego las más recientes.
- `lote_ia`: titulares por llamada a Claude (60). Si en el log aparece
  "una respuesta se cortó", bájalo a 40.
- `google_respaldo: false` apaga Google Noticias por completo.

## Limitaciones

- Facebook e Instagram no se pueden leer por script. Los medios que solo
  existen ahí quedan fuera.
- Reforma, El Norte y Mural tienen muro de pago: se ven sus titulares, pero al
  abrir la nota topas con el muro.
- El scraping de páginas es frágil: si un medio rediseña su sitio, puede
  dejar de traer notas hasta que se ajuste. El log de cada corrida lista los
  medios sin notas ("Sin notas en esta corrida").
- Hay estados donde la cobertura local es escasa por autocensura
  (Tamaulipas, partes de Michoacán, Guerrero y Sinaloa). El panel no puede
  mostrar lo que los medios no publican.
- Los títulos y resúmenes los escribe un modelo de lenguaje: puede equivocarse
  en el estado, la categoría o un matiz. Trátalo como un primer vistazo y
  verifica en la fuente original.
