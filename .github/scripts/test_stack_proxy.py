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


if __name__ == "__main__":
    unittest.main()
