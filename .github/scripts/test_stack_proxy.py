"""Proxy de confianza del stack local (F2-03D): el backend solo acepta `X-Forwarded-For` del
proxy, que tiene una dirección fija fuera del rango que Docker reparte. Análisis estático con
la stdlib: `docker compose config` valida la sintaxis, no que estas piezas encajen."""

import ast
import ipaddress
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = (ROOT / "infra/docker/compose.yaml").read_text()
DEFAULT = r"\$\{%s:-([^}]+)\}"


def defaults(name):
    return re.findall(DEFAULT % name, COMPOSE)


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
        caddyfile = (ROOT / "infra/docker/proxy/Caddyfile").read_text()
        self.assertNotIn("trusted_proxies", caddyfile)  # con ella conservaría la del cliente
        self.assertNotRegex(caddyfile, r"(?i)header_up\s+\S*X-Forwarded-For")  # ni `+` ni `-`

    def test_the_webhook_body_cap_covers_the_view_cap_with_a_small_margin(self):
        caddyfile = (ROOT / "infra/docker/proxy/Caddyfile").read_text()
        block = re.search(r"handle /webhooks/\* \{(.*?)\n\t\}", caddyfile, re.S).group(1)
        (size,) = re.findall(r"request_body \{\s*max_size (\d+)MB\s*\}", block)
        view = (ROOT / "backend/apps/conversations/meta_webhook.py").read_text()
        (node,) = [n.value for n in ast.parse(view).body if isinstance(n, ast.Assign)
                   and getattr(n.targets[0], "id", "") == "BODY_MAX"]  # fmt: skip
        body_max = eval(compile(ast.Expression(node), "BODY_MAX", "eval"), {"__builtins__": {}})
        cap = int(size) * 10**6  # en Caddy, MB es 10^6 y MiB 2^20
        self.assertGreater(cap, body_max)  # un lote válido de Meta nunca se corta en el proxy
        self.assertLess(cap, 2 * body_max)  # y uno abusivo no llega a Django
        self.assertLess(block.index("request_body"), block.index("reverse_proxy"))


if __name__ == "__main__":
    unittest.main()
