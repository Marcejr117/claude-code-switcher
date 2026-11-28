"""Spanish help and guided menus. Account operations stay in csw's core."""
import subprocess


QUICK_HELP = """csw — tus cuentas y modelos en Claude Code

LO DE CADA DÍA
  csw                  Menú guiado
  csw list             Tus cuentas, numeradas; > marca la seleccionada
  csw 2                Cambiar a la cuenta 2 (vale también su nombre: csw codex)
  /model               Dentro de Claude: elegir modelo de la cuenta seleccionada

CUENTAS
  csw add claude|codex|glm|deepseek|orcarouter|cursor <nombre>
                       Crear una cuenta; el login o la clave se piden una sola vez
  csw rename <cuenta> <nombre>    Cambiarle el nombre
  csw remove <cuenta>             Retirarla (queda copia recuperable)
  csw doctor                      Qué le falta a cada cuenta y cómo arreglarlo

A TENER EN CUENTA
  El cambio vale para todos los Claude abiertos: lo recargan solos en segundos.
  Para abrir una sesión con otra cuenta sin tocar las demás: csw run <cuenta>.
  Cambiar entre cuentas de Claude no pide login otra vez; Chrome, la extensión
  del editor y los conectores siguen funcionando.
  Con otros proveedores el navegador va como servidor MCP «chrome».
  Todos los comandos: csw help --all
"""

FULL_HELP = """csw — referencia completa

MENÚ Y AYUDA
  csw | csw menu                  Menú guiado (sin terminal: lista de cuentas)
  csw help | csw -h | csw --help   Ayuda rápida
  csw help --all                   Esta referencia
  csw hot help                    Guía del cambio en caliente

SELECCIÓN GLOBAL — no abre ni reinicia Claude
  csw <perfil|número|archivo>       Cambiar proveedor, cuenta y catálogo en settings.json
  claude                          Leer esa configuración sin una copia que la fije
  csw launch [-- opciones]         Lo mismo sin depender de la función del shell
  /model                          Dentro de Claude: elegir modelo del proveedor actual
  Cambia todas las sesiones que leen el archivo, incluso durante turnos, agentes
  o goals. Una petición ya enviada no se redirige; las nuevas usan la cuenta elegida
  después de la recarga. Un modelo incompatible exige /model antes de continuar.
  Los procesos antiguos con --settings o csw run conservan su conexión original.

SESIONES AISLADAS OPCIONALES — no necesarias para seleccionar settings.json
  csw hot start [perfil] [-- opciones de Claude]
  csw hot                         Atajo de csw hot start (sin perfil)
  csw hot menu [sesión]            Elegir proveedor → cuenta; después /model
  csw hot use <perfil> [sesión]     Cambiar una cuenta directamente
  csw hot list                     Ver ID, cuenta, modelo y estado
  Si hay varias sesiones, el menú pregunta cuál cambiar.
  Los cambios afectan solo a la sesión elegida. Al continuar, su historial
  se enviará al nuevo destino. Espera a que terminen turnos, agentes y goals.
  Una sesión fijada con --settings/csw run no se puede convertir en este modo.

CUENTA ACTUAL Y ARRANQUE AISLADO
  csw list | csw l                 Cuentas y números de selección
  csw list --all                   También snapshots históricos
  csw active                      Ver perfil global, modelo y contexto configurado
  csw run [perfil] [-- opciones]   Abrir Claude con conexión fija e independiente
  Úsalo solo si quieres que csw NO cambie la conexión de esa sesión.
  csw 2 usa el número de csw list, NO la opción 2 del menú principal.

MODELOS
  csw models [perfil] [--refresh]  Listado completo; sin perfil usa el global
  csw model <perfil> [id]          Guardar modelo; sin ID abre buscador y páginas
  /model                          Dentro de Claude: cambiar el modelo de esa sesión
  Los agentes/workflows creados después heredan ese modelo y el /effort activo.
  No cambia retroactivamente agentes que ya estén ejecutándose.
  Puede usarse con un goal activo entre turnos, sin cancelar el objetivo.
  Espera a que termine el turno y los subagentes; el nuevo modelo debe ser de esa cuenta.
  csw model guarda el modelo para nuevos arranques. En una sesión abierta usa /model.
  El contexto mostrado es documentado/configurado; depende del modelo y cuenta.
  [1m] no garantiza ni fuerza una ventana de 1M si el proveedor no la admite.

CUENTAS — perfil = proveedor o proveedor:nombre
  csw add codex <nombre>             Código de dispositivo por defecto
  csw add codex <nombre> --browser   Alternativa: enlace OAuth con callback local
  csw add cursor|claude|glm|deepseek|orcarouter <nombre>
                                  Nueva cuenta; nombre corto, como trabajo o marce3
  csw login <perfil>               Renovar; Codex muestra URL corta + código
  csw login <perfil> --browser     Usar navegador en Codex; Claude/Cursor ya lo usan
  csw rename <perfil> <nombre>     Renombrar la cuenta (Claude, GLM, DeepSeek, OrcaRouter)
  csw name <perfil> <etiqueta>     Cambiar el nombre visible, no el login
                                  En Codex principal, un nombre corto añade codex:nombre
                                  como identificador; csw codex sigue funcionando
  csw remove <perfil>             Retirar con copia recuperable; alias: csw rm
                                  No permite retirar cuentas seleccionadas/en uso
  csw restore <id-copia>          Recuperar usando el ID que mostró csw remove
  csw save <slug> [etiqueta]       Guardar el ajuste actual como otro perfil
  Retirar no cancela suscripciones ni revoca claves. La copia guarda credenciales.

REVISIÓN
  csw doctor                      Revisar configuración y conexión local
  csw monitor                     Abrir monitor del proxy seleccionado
  csw statusline                  Uso interno: lee JSON de Claude por stdin
  csw shared-hook                 Uso interno: protección y contexto de sesiones globales
  Claude/Max, GLM y OrcaRouter conectan directamente; Codex y Cursor usan el proxy.
  DeepSeek usa conexión directa o el adaptador local de historial cuando está habilitado.
  Se conservan permisos, hooks y plugins compartidos. Cursor: herramientas,
  agentes y MCP no garantizados; texto comprobado. Ver límites en PROVIDERS.md.

EJEMPLOS (las cuentas adicionales deben existir)
  csw add claude trabajo
  csw claude:trabajo               Seleccionar sin abrir otra sesión
  csw glm2                        En otra terminal; después /model en Claude
  csw model codex gpt-daybreak-blue-latest
  csw run codex:trabajo -- --resume
  csw models cursor --refresh

Guía detallada: ~/.claude/PROVIDERS.md
"""

HOT_HELP = """Modo hot aislado (opcional) — mismo Claude, misma conversación

Para modificar settings.json usa csw → opción 1. No necesitas este modo.
Hot sirve para mantener sesiones independientes del archivo global.

  csw hot start [perfil] [-- opciones de Claude]
      Abrir Claude. Sin perfil: elegir proveedor y cuenta.
  csw hot menu [sesión]
      En otra terminal: elegir una cuenta para un Claude que sigue abierto.
  csw hot use <perfil> [sesión]
      Lo mismo, indicando la cuenta; ejemplo: csw hot use glm2.
  csw hot list
      Ver ID, cuenta, modelo y estado. Si hay varias, el menú pregunta cuál.

Después de cambiar de cuenta, espera un par de segundos y usa /model en Claude.
El historial se enviará al nuevo destino al continuar. No cambia otras sesiones.
Cambiar modelo con /model admite un goal activo entre turnos, sin subagentes trabajando.
Cambiar proveedor/cuenta sí requiere terminar el goal; /goal clear lo cancela.
Este comando controla solo sesiones iniciadas con csw hot start.
Un Claude normal sigue settings.json; csw run fija la conexión al arrancar.
Para ver todas las opciones: csw help --all.
"""


def help_text(full=False):
    print(FULL_HELP if full else QUICK_HELP)


def confirm(message):
    return input(message + " [s/N]: ").strip().lower() in ("s", "si", "sí")


def pick_model(api, path, target, catalog):
    query, page, size = "", 0, 15
    print("\nGuardar modelo para FUTUROS arranques. En Claude abierto usa /model.")
    print(f"Cuenta: {api.profile_label(path)} · Guardado: {api.effective_model(target)}")
    while True:
        rows = [(i, row) for i, row in enumerate(catalog["models"], 1)
                if query.casefold() in (row["model"] + " " + row["label"]).casefold()]
        pages = max(1, (len(rows) + size - 1) // size)
        page = min(page, pages - 1)
        print(f"\n{catalog['source']} · {len(rows)} modelo{'s' if len(rows) != 1 else ''} · página {page + 1}/{pages}"
              + (f" · búsqueda: {query}" if query else ""))
        for index, row in rows[page * size:(page + 1) * size]:
            model = api.preferred_context_model(row["model"], target)
            print(f"  {index:3}. {model} — {row['label']} · contexto {api.context_label(model, target)}")
        if not rows:
            print("Sin coincidencias. Usa / para quitar el filtro.")
        print("Número o ID = elegir · /texto = buscar · / = ver todos")
        print("n = siguiente · p = anterior · r = actualizar catálogo · 0/Enter = cancelar")
        choice = input("Modelo: ").strip()
        if choice.lower() in ("", "0", "q"):
            print("Cancelado: modelo guardado sin cambios.")
            return None, catalog
        if choice.startswith("/"):
            query, page = choice[1:].strip(), 0
        elif choice.lower() == "n":
            page = min(page + 1, pages - 1)
        elif choice.lower() == "p":
            page = max(page - 1, 0)
        elif choice.lower() == "r":
            print("Consultando el catálogo del proveedor...", flush=True)
            catalog = api.model_catalog(path, target, refresh=True)
            page = 0
        elif choice.isdecimal():
            selected = next((row["model"] for i, row in rows if i == int(choice)), None)
            if selected is not None:
                return selected, catalog
            print("Elige un número de la lista filtrada o usa / para ver todos.")
        else:
            return choice, catalog


def restore_menu(api, hot):
    rows = []
    for directory in sorted((api.ROOT / "removed-accounts").glob("*")):
        if directory.is_symlink() or not (directory / "profile.json").is_file():
            continue
        metadata = directory / "metadata.json"
        if metadata.is_symlink():
            continue
        try:
            label = api.read_json(metadata).get("label", directory.name)
        except (OSError, ValueError):
            continue
        rows.append((f"{label} · copia {directory.name}", directory.name))
    if not rows:
        print("No hay cuentas retiradas para recuperar.")
        return
    identifier = hot.choose_number("Cuenta que quieres recuperar", rows)
    if identifier and confirm(f"¿Restaurar la copia {identifier}? No cambia la cuenta predeterminada"):
        api.main(["restore", identifier])


def accounts_menu(api, hot):
    while True:
        choice = hot.choose_number("MIS CUENTAS", [
            ("Ver cuentas guardadas", "list"),
            ("Añadir otra cuenta (login o clave API)", "add"),
            ("Renovar login (Codex usa código de dispositivo)", "login"),
            ("Cambiar nombre visible de una cuenta", "name"),
            ("Retirar una cuenta (con copia recuperable)", "remove"),
            ("Recuperar una cuenta retirada", "restore"),
            ("Cambiar proveedor y cuenta en settings.json (sin abrir Claude)", "default"),
            ("Revisar configuración y conexión local", "doctor"),
        ], cancel_label="Volver al menú principal")
        if choice is None:
            return
        try:
            if choice in ("list", "doctor"):
                api.main([choice])
            elif choice == "restore":
                restore_menu(api, hot)
            elif choice == "add":
                backend = hot.choose_number("Proveedor de la nueva cuenta", [
                    (hot.LABELS[k], "claude" if k == "Claude sub" else k.lower()) for k in hot.ORDER
                ])
                if backend is None:
                    continue
                slug = input("Nombre corto (ej. trabajo o marce3; vacío = cancelar): ").strip()
                if slug:
                    if backend == "codex":
                        print("Codex mostrará una URL corta y un código de un solo uso. No abrirá un callback local.")
                    api.main(["add", backend, slug])
            else:
                allowed = ("Claude sub", "Codex", "Cursor") if choice == "login" else None
                path = hot.choose_profile(api, allowed=allowed)
                if path is None:
                    continue
                label = api.profile_label(path)
                if choice == "name":
                    name = input(f"Nuevo nombre visible de {label} (vacío = cancelar): ").strip()
                    if name:
                        api.main(["name", label, name])
                        print(f"Nombre guardado. Identificador: {api.profile_label(path)}.")
                elif choice == "remove":
                    print("No cancela la suscripción ni revoca la clave. La copia conserva sus credenciales.")
                    if confirm(f"¿Retirar {label} de csw?"):
                        api.main(["remove", label])
                    else:
                        print("Cancelado: cuenta conservada.")
                elif choice == "default":
                    print("Afecta a los Claude que leen settings.json, incluso si tienen turnos, agentes o goals activos.")
                    print("Lo ya enviado termina en la cuenta anterior; las llamadas nuevas usarán la elegida tras recargar.")
                    if confirm(f"¿Aplicar {label} a settings.json?"):
                        api.switch(label)
                else:
                    if api.provider(api.selected_data(path)) == "Codex":
                        print("Codex mostrará una URL corta y un código de un solo uso.")
                    api.main(["login", label])
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print(f"No se pudo completar: {error}")


def home(api):
    hot = api.hot_module()
    while True:
        selected = api.active()
        print("\ncsw — cuentas y modelos de Claude Code")
        print("Cuenta en settings.json: " + (api.profile_label(api.ROOT / selected) if selected else "sin seleccionar"))
        print("Este menú no abre Claude. El modelo se elige con /model dentro de Claude.")
        choice = hot.choose_number("¿QUÉ QUIERES HACER?", [
            ("Cambiar proveedor y cuenta en settings.json", "global"),
            ("Controlar una sesión hot aislada (solo si ya usas ese modo)", "switch"),
            ("Guardar modelo predeterminado de una cuenta (opcional)", "model"),
            ("Mis cuentas: añadir, renovar login, renombrar o retirar", "accounts"),
            ("Ayuda rápida y ejemplos", "help"),
        ], cancel_label="Salir")
        if choice is None:
            return 0
        try:
            if choice == "global":
                path = hot.choose_profile(api)
                if path is None:
                    continue
                print("Cambia todas las sesiones que leen settings.json. No cambia sesiones aisladas.")
                print("Se publica aunque haya turnos, agentes o goals activos.")
                print("Lo ya enviado termina con la cuenta anterior; las llamadas nuevas usarán la elegida tras recargar.")
                if confirm(f"¿Aplicar {api.profile_label(path)} a settings.json?"):
                    api.switch(api.profile_label(path))
                    return 0
            if choice == "switch":
                if not hot.sessions(api):
                    print("No hay sesiones hot aisladas. Para cambiar settings.json usa la opción 1; no necesitas abrir otra sesión.")
                    continue
                return hot.main(api, ["menu"])
            if choice == "model":
                path = hot.choose_profile(api)
                if path is not None:
                    api.select_model(api.profile_label(path))
            elif choice == "accounts":
                accounts_menu(api, hot)
            elif choice == "help":
                help_text()
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print(f"No se pudo completar: {error}")
