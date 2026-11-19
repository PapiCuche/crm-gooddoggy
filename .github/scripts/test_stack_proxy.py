"""Proxy de confianza del stack local (F2-03D): el backend solo acepta `X-Forwarded-For` del
proxy, que tiene una dirección fija fuera del rango que Docker reparte. Análisis estático con
la stdlib: `docker compose config` valida la sintaxis, no que estas piezas encajen."""

import ipaddress
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = (ROOT / "infra/docker/compose.yaml").read_text()
PROD = (ROOT / "infra/prod/compose.yaml").read_text()
CADDYFILES = ("infra/docker/proxy/Caddyfile", "infra/prod/Caddyfile")  # local y producción
DEFAULT = r"\$\{%s:-([^}]+)\}"


def defaults(name, compose=COMPOSE):
    return re.findall(DEFAULT % name, compose)


def caddyfiles():
    return ((path, (ROOT / path).read_text()) for path in CADDYFILES)


class StackProxy(unittest.TestCase):
    def test_backend_trusts_only_the_fixed_proxy_address(self):
        trusted = re.findall(r"^\s+FORWARDED_ALLOW_IPS: (.+)$", COMPOSE, re.M)
        fixed = re.findall(r"^\s+ipv4_address: (.+)$", COMPOSE, re.M)
        self.assertEqual(trusted, fixed)  # la misma expresión, una vez cada una
        (proxy,) = set(defaults("STACK_PROXY_IP"))  # y el mismo valor por defecto
        self.assertEqual(trusted, ["${STACK_PROXY_IP:-%s}" % proxy])
        runtime = COMPOSE[COMPOSE.index("x-runtime:") : COMPOSE.index("services:")]
        self.assertIn("FORWARDED_ALLOW_IPS", runtime)  # lo heredan backend, ws, worker y beat

    def test_proxy_address_is_in_the_subnet_and_outside_the_dynamic_range(self):
        (proxy,), (subnet,), (dynamic,) = (
            set(defaults(name)) for name in ("STACK_PROXY_IP", "STACK_SUBNET", "STACK_IP_RANGE")
        )
        address = ipaddress.ip_address(proxy)
        self.assertIn(address, ipaddress.ip_network(subnet))
        self.assertNotIn(address, ipaddress.ip_network(dynamic))  # nadie más la recibe
        self.assertTrue(ipaddress.ip_network(dynamic).subnet_of(ipaddress.ip_network(subnet)))
        example = (ROOT / "infra/env/.env.example").read_text()
        for name, value in (("PROXY_IP", proxy), ("SUBNET", subnet), ("IP_RANGE", dynamic)):
            self.assertIn(f"# STACK_{name}={value}", example)

    def test_caddy_replaces_the_header_a_client_sends(self):
        for path, caddyfile in caddyfiles():
            with self.subTest(path):
                self.assertNotIn("trusted_proxies", caddyfile)  # conservaría la del cliente
                self.assertNotRegex(caddyfile, r"(?i)header_up\s+\S*X-Forwarded-For")  # ni + ni -

    def test_the_webhook_body_cap_covers_the_view_cap_with_a_small_margin(self):
        view = (ROOT / "backend/apps/conversations/meta_webhook.py").read_text()
        # Sin `ast`: el backend usa sintaxis de Python 3.14 y esta prueba corre con el del runner.
        ((count, shift),) = re.findall(r"^BODY_MAX = (\d+) << (\d+)\b", view, re.M)
        body_max = int(count) << int(shift)
        for path, caddyfile in caddyfiles():
            with self.subTest(path):
                block = re.search(r"handle /webhooks/\* \{(.*?)\n\t\}", caddyfile, re.S).group(1)
                (size,) = re.findall(r"request_body \{\s*max_size (\d+)MB\s*\}", block)
                cap = int(size) * 10**6  # en Caddy, MB es 10^6 y MiB 2^20
                self.assertGreater(cap, body_max)  # un lote válido de Meta nunca se corta
                self.assertLess(cap, 2 * body_max)  # y uno abusivo no llega a Django
                self.assertLess(block.index("request_body"), block.index("reverse_proxy"))

    def test_caddy_caps_an_api_body_just_above_the_largest_upload(self):
        for path, caddyfile in caddyfiles():
            with self.subTest(path):
                api = caddyfile[caddyfile.index("handle /api/*") : caddyfile.index("handle /webhooks/*")]
                (size,) = re.findall(r"request_body \{\s+max_size (\d+)MB\b", api)  # MB: 10**6 B
                self.assertTrue((10 << 20) + (64 << 10) <= int(size) * 10**6 <= 12 * 10**6)  # 10 MiB + form

    def test_caddy_keeps_the_same_routes_and_hides_the_health_probes(self):
        for path, caddyfile in caddyfiles():
            with self.subTest(path):
                self.assertIn("admin off", caddyfile)
                routes = dict(re.findall(r"handle (\S+) \{.*?\n\t\t(?:respond|reverse_proxy) (\S+)", caddyfile, re.S))
                self.assertEqual(routes, {"/api/*": "backend:8000", "/webhooks/*": "backend:8000",
                                          "/ws/*": "ws:8000", "/health/*": "404"})
                self.assertRegex(caddyfile, r"\n\thandle \{\n(?:\t\t.*\n)*?\t\treverse_proxy frontend:3000\n")


class ProdStack(unittest.TestCase):
    """infra/prod (F1-13, ADR-033). infra/prod/validate.sh comprueba el `docker compose config`."""

    def test_backend_trusts_only_the_fixed_proxy_address_of_the_edge(self):
        (trusted,) = re.findall(r"^\s+FORWARDED_ALLOW_IPS: (.+)$", PROD, re.M)
        self.assertEqual(re.findall(r"^\s+ipv4_address: (.+)$", PROD, re.M), [trusted])
        (proxy,), (subnet,), (dynamic,) = (
            set(defaults(name, PROD)) for name in ("STACK_PROXY_IP", "STACK_SUBNET", "STACK_IP_RANGE")
        )
        self.assertIn(ipaddress.ip_address(proxy), ipaddress.ip_network(subnet))
        self.assertNotIn(ipaddress.ip_address(proxy), ipaddress.ip_network(dynamic))

    def test_only_the_proxy_publishes_ports_and_nothing_is_built(self):
        self.assertEqual(PROD.count("ports:"), 1)
        proxy = PROD[PROD.index("\n  proxy:") : PROD.index("\nnetworks:")]
        self.assertEqual(re.findall(r'^\s+- "(.+?)"', proxy, re.M), ["0.0.0.0:80:80", "0.0.0.0:443:443"])
        text = PROD + (ROOT / "infra/prod/Caddyfile").read_text()
        code = "\n".join(line for line in text.lower().splitlines() if not line.lstrip().startswith("#"))
        for word in ("build:", "garage", "mailpit", "auto_https"):
            self.assertNotIn(word, code)
        images = re.findall(r"^\s+image: (.+)$", PROD, re.M)
        self.assertTrue(images and all(re.fullmatch(r"\$\{[A-Z]+_IMAGE:\?.+\}", i) for i in images))

    def test_postgres_logs_no_customer_values_and_the_superuser_stays_local(self):
        for setting in ("log_min_error_statement=panic", "log_statement=none",
                        "log_parameter_max_length_on_error=0", "log_error_verbosity=terse",
                        "log_min_messages=log", "hba_file=/etc/postgresql/pg_hba.conf"):
            self.assertRegex(PROD, r"\n\s+- -c\n\s+- %s\n" % setting)
        self.assertIn("./postgres/pg_hba.conf:/etc/postgresql/pg_hba.conf:ro", PROD)
        hba = (ROOT / "infra/prod/postgres/pg_hba.conf").read_text()
        rules = [line.split() for line in hba.splitlines() if line and not line.startswith("#")]
        self.assertEqual(rules, [["local", "all", "all", "trust"], ["host", "all", "postgres", "all", "reject"],
                                 ["host", "all", "all", "all", "scram-sha-256"]])  # el primero que casa gana

    def test_the_rest_of_the_routes_cap_the_body_too(self):
        caddyfile = (ROOT / "infra/prod/Caddyfile").read_text()
        for route in ("/ws/* ", ""):
            block = re.search(r"\n\thandle %s\{(.*?)\n\t\}" % re.escape(route), caddyfile, re.S).group(1)
            self.assertRegex(block, r"^\s+request_body \{\s+max_size 1MB\b")
            self.assertLess(block.index("request_body"), block.index("reverse_proxy"))

    def test_webhooks_are_rate_limited_per_ip_and_queries_never_logged(self):
        caddyfile = (ROOT / "infra/prod/Caddyfile").read_text()
        block = re.search(r"handle /webhooks/\* \{(.*?)\n\t\}", caddyfile, re.S).group(1)
        self.assertRegex(block, r"rate_limit \{\s+zone \w+ \{\s+key \{remote_host\}\s+events \d+\s+window")
        self.assertLess(block.index("rate_limit"), block.index("reverse_proxy"))
        self.assertIn("order rate_limit", caddyfile)
        self.assertRegex((ROOT / "infra/prod/caddy/Dockerfile").read_text(),
                         r"--with github\.com/mholt/caddy-ratelimit@v\d+\.\d+\.\d+\n")
        self.assertIn("request>uri regexp \\?.* ?redacted", caddyfile)  # la query entera
        # El log por omisión (errores) y el de acceso del sitio usan el filtro.
        self.assertEqual(caddyfile.count("import redacted_fields"), 2)
        self.assertEqual(caddyfile.count("log {") + caddyfile.count("log default {"), 2)

    def test_the_env_template_names_every_variable_production_needs_and_no_value(self):
        text = (ROOT / "infra/prod/.env.production.example").read_text()
        sections, current = {}, None
        for line in text.splitlines():
            header = re.match(r"# =+ ([a-z]+\.env) ", line)
            if header:
                current = sections.setdefault(header.group(1), set())
            elif line and not line.startswith("#"):
                self.assertRegex(line, r"^[A-Z][A-Z0-9_]*=$")  # ningún valor, ni de ejemplo
                current.add(line[:-1])
        settings = ROOT / "backend/config/settings"
        base, production = (settings / "base.py").read_text(), (settings / "production.py").read_text()
        env_of = dict(re.findall(r'^(\w+)(?::[^=]+)? = env\.\w+\("(\w+)"', base, re.M))
        imported = re.search(r"from config\.settings\.base import \((.*?)\)", production, re.S).group(1)
        never = {"DJANGO_DEBUG", "DJANGO_TEST_ACCOUNTS_ORGANIZATION", "SANDBOX_INBOUND_ENABLED"}
        defaulted = {"MAIL_BACKEND"}  # production solo admite su valor por omisión (smtp)
        needed = (set(re.findall(r'env\.required\("(\w+)"', base))
                  | set(re.findall(r'env\.\w+\("(\w+)"', production))
                  | {env_of[name] for name in re.findall(r"\w+", imported) if name in env_of}) - never - defaulted
        runtime_block = PROD[PROD.index("x-runtime:") : PROD.index("  depends_on:")]
        set_by_compose = set(re.findall(r"^    (\w+): ", runtime_block, re.M))
        self.assertEqual(set_by_compose, {"DJANGO_SETTINGS_MODULE", "FORWARDED_ALLOW_IPS"})
        runtime = sections["runtime.env"]
        self.assertLessEqual(needed - set_by_compose, runtime)
        self.assertFalse((never | {"DATABASE_MIGRATOR_URL", "CRM_MIGRATOR_PASSWORD"}) & runtime)
        self.assertEqual(sections["migrate.env"], {"DATABASE_MIGRATOR_URL"})
        for name, file in (("proxy", "proxy.env"), ("redis", "redis.env"), ("migrate", "migrate.env")):
            self.assertIn("${CRM_ENV_DIR:-/etc/crm}/" + file, PROD[PROD.index(f"\n  {name}:") :])

    def test_the_role_bootstrap_matches_adr_002_without_local_conveniences(self):
        script = (ROOT / "infra/prod/postgres/bootstrap-roles.sh").read_text()
        roles = dict(re.findall(r"^ALTER ROLE (crm_\w+) (.+) PASSWORD :'\w+';$", script, re.M))
        self.assertEqual(roles["crm_migrator"].split(), "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION BYPASSRLS".split())
        self.assertEqual(roles["crm_app"].split(), "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS".split())
        self.assertNotRegex(script, r"(?i)password\s+'|crm_test")  # sin contraseñas ni BD de tests
        self.assertIn("\\getenv migrator_pw CRM_MIGRATOR_PASSWORD", script)  # ni en los argumentos
        self.assertIn("WHERE NOT EXISTS", script)  # repetible


NAME = r"[A-Za-z0-9_][A-Za-z0-9_.-]*"  # un nombre de servicio o de ancla de compose


def networks_of(block):
    """Las redes de un bloque con sangría de 4: `networks: [a, b]` o `networks:` y una por línea."""
    listed = re.search(r"^    networks: \[([^\]]*)\]", block, re.M)
    if listed:
        return {n.strip() for n in listed.group(1).split(",") if n.strip()}
    nested = re.search(r"^    networks:\n((?:      .*\n)+)", block, re.M)
    return set(re.findall(rf"^      ({NAME}):", nested.group(1), re.M)) if nested else None


def services(compose=COMPOSE):
    """Cada servicio con sus redes, del bloque `services:`. Sin `PyYAML` (estas pruebas corren con
    el Python del runner, solo con la stdlib): sin `networks:` propio, las de su ancla
    (`<<: *ancla`, `x-…: &ancla` arriba) y, sin ninguna, `default`."""
    head = compose[: compose.index("\nservices:")]
    anchors = {
        name: block
        for name, block in re.findall(rf"^x-{NAME}: &({NAME})\n((?:(?:  .*|\s*)\n)*)", head, re.M)
    }
    body = compose[compose.index("\nservices:") : compose.index("\nnetworks:")] + "\n"
    found = {}
    for name, block in re.findall(rf"^  ({NAME}):[^\n]*\n((?:(?:    .*|\s*)\n)*)", body, re.M):
        own = networks_of(block)
        if own is None:
            for anchor in re.findall(rf"^    <<: \*({NAME})", block, re.M):
                inherited = "\n".join("  " + line for line in anchors[anchor].splitlines()) + "\n"
                own = own or networks_of(inherited)
        found[name] = own or {"default"}
    return found


class SharedStack(unittest.TestCase):
    """Modo de servidor compartido (ADR-046): Caddyfile.shared detrás del borde HTTPS de otra aplicación."""

    caddyfile = (ROOT / "infra/prod/Caddyfile.shared").read_text()
    prod = (ROOT / "infra/prod/Caddyfile").read_text()

    def test_only_the_fixed_edge_address_may_set_the_client_ip(self):
        self.assertRegex(self.caddyfile, r"\n\t\ttrusted_proxies static \{\$CRM_EDGE_PROXY_IP\}\n")
        self.assertRegex(self.caddyfile, r"\n\t\tclient_ip_headers X-Forwarded-For\n")
        self.assertRegex(self.caddyfile, r"\n\t\ttrusted_proxies_strict\n")
        self.assertEqual(self.caddyfile.count("trusted_proxies static"), 1)
        shared = (ROOT / "infra/prod/compose.shared.yaml").read_text()
        self.assertRegex(shared, r"CRM_EDGE_PROXY_IP: \$\{CRM_EDGE_PROXY_IP:-[0-9.]+\}")

    def test_every_upstream_gets_the_client_ip_and_https_replaced_not_chained(self):
        self.assertRegex(self.caddyfile, r"\(upstream_identity\) \{\n\theader_up X-Forwarded-For \{client_ip\}\n"
                                         r"\theader_up X-Forwarded-Proto https\n\}")
        upstreams = re.findall(r"reverse_proxy (\S+) \{\n\t+import upstream_identity\n", self.caddyfile)
        self.assertEqual(upstreams, ["backend:8000", "backend:8000", "ws:8000", "frontend:3000"])
        self.assertEqual(self.caddyfile.count("reverse_proxy "), 4)

    def test_webhooks_are_limited_per_client_not_per_edge(self):
        block = re.search(r"handle /webhooks/\* \{(.*?)\n\t\}", self.caddyfile, re.S).group(1)
        self.assertRegex(block, r"key \{client_ip\}")
        code = re.sub(r"#.*", "", self.caddyfile)  # los comentarios lo nombran para explicar por qué no
        self.assertNotIn("{remote_host}", code)

    def test_no_tls_no_admin_and_other_hosts_are_refused(self):
        for text in ("admin off", "auto_https off", "http://crm.{$CRM_DOMAIN} {"):
            self.assertIn(text, self.caddyfile)
        self.assertRegex(self.caddyfile, r"\n:80 \{\n\tabort\n\}\n?$")

    def test_same_caps_routes_redaction_and_stripped_headers_as_the_dedicated_edge(self):
        def rows(text):
            return re.findall(r"^\t*(handle \S+ \{|handle \{|max_size \S+|respond \d+|events \d+|window \S+|"
                              r"request_header -\S+|request>uri .*|request>headers>Referer delete)", text, re.M)
        self.assertEqual(rows(self.caddyfile), rows(self.prod))
        self.assertEqual(self.caddyfile.count("import redacted_fields"), 2)


class StackAntivirus(unittest.TestCase):
    """F7-15c: clamd analiza archivos no confiables; si lo comprometen, no alcanza ningún listener
    HTTP (`backend:8000` se saltaría el tope del cuerpo de Caddy). Solo comparte red con su relay."""

    def test_the_parser_follows_anchors_and_takes_any_service_name(self):
        compose = (
            "x-run: &run\n  image: x\n  networks: [av]\nx-plain: &plain\n  image: y\n"
            "services:\n  worker_2:\n    <<: *run\n  web.1:\n    <<: *plain\n"
            "  edge:\n    networks:\n      default:\n        ipv4_address: 1.2.3.4\n      av:\n"
            "  own:\n    <<: *run\n    networks: [default]\nnetworks:\n  av: {}\n"
        )
        self.assertEqual(services(compose), {"worker_2": {"av"}, "web.1": {"default"},
                                             "edge": {"default", "av"}, "own": {"default"}})

    def test_clamd_shares_its_networks_only_with_its_relay(self):
        found = services()
        self.assertEqual(found["clamd"], {"av"})
        self.assertEqual({name for name, nets in found.items() if nets & found["clamd"]},
                         {"clamd", "clamd-relay"})
        self.assertIn("av: { internal: true }", COMPOSE)
        self.assertIn("av-client: { internal: true }", COMPOSE)

    def test_the_scanners_reach_clamd_only_through_the_relay(self):
        found = services()
        clients = {name for name, nets in found.items() if "av-client" in nets}
        self.assertEqual(clients, {"clamd-relay", "backend", "worker-media"})
        self.assertEqual(re.findall(r"CLAMD_ADDRESS: (\S+)", COMPOSE), ["clamd-relay:3310"])
        relay = COMPOSE[COMPOSE.index("  clamd-relay:") : COMPOSE.index("  freshclam:")]
        (command,) = re.findall(r"command: (\[.*\])", relay)
        self.assertRegex(command, r'"TCP-LISTEN:3310,[^"]*", "TCP:clamd:3310"\]$')  # solo a clamd
        self.assertNotIn("ports:", relay)

    def test_the_graph_simulator_lives_only_in_the_development_stack(self):
        """ADR-041: el servicio y su variable, solo en el compose de desarrollo y tras un perfil;
        el simulador se monta desde `tests/`, que ninguna imagen copia."""
        for path in ("infra/prod/compose.yaml", "infra/prod/Caddyfile", "backend/Dockerfile"):
            text = (ROOT / path).read_text()
            for needle in ("meta-sim", "meta_sim", "META_GRAPH_SIMULATOR_URL", "tests"):
                self.assertNotIn(needle, text, f"{needle} en {path}")
        self.assertIn("tests/", (ROOT / "backend/.dockerignore").read_text().split())
        sim = COMPOSE[COMPOSE.index("  meta-sim:") : COMPOSE.index("  mailpit:")]
        for line in ("profiles: [meta-sim]", 'user: "65534:65534"', "read_only: true",
                     "cap_drop: [ALL]", 'security_opt: ["no-new-privileges:true"]'):
            self.assertIn(f"\n    {line}\n", sim)
        self.assertEqual(sim.count("ports:"), 1)  # una sola lista, en la forma de bloque
        entries = []
        for line in sim[sim.index("\n    ports:\n"):].split("\n")[2:]:
            if not line.startswith("      - "):
                break
            entries.append(line[8:].split(" #")[0].strip().strip('"'))
        self.assertEqual(entries, ["127.0.0.1:${META_SIM_HOST_PORT:-8090}:8090"])  # solo loopback
        self.assertNotIn("privileged", sim)
        self.assertIn("../../backend/tests/meta_sim:/sim/tests/meta_sim:ro", sim)
        runtime = COMPOSE[COMPOSE.index("x-runtime:") : COMPOSE.index("services:")]
        self.assertIn("DJANGO_SETTINGS_MODULE: config.settings.local", runtime)
        self.assertEqual(re.findall(r"META_GRAPH_SIMULATOR_URL: (\S+)", COMPOSE),
                         ["${META_GRAPH_SIMULATOR_URL:-}"])  # solo el runtime local, vacía


if __name__ == "__main__":
    unittest.main()
