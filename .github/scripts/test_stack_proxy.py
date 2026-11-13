"""Proxy de confianza del stack local (F2-03D): el backend solo acepta `X-Forwarded-For` del
proxy, que tiene una dirección fija fuera del rango que Docker reparte. Análisis estático con
la stdlib: `docker compose config` valida la sintaxis, no que estas piezas encajen."""

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

    def test_caddy_caps_an_api_body_just_above_the_largest_upload(self):
        caddyfile = (ROOT / "infra/docker/proxy/Caddyfile").read_text()
        api = caddyfile[caddyfile.index("handle /api/*") : caddyfile.index("handle /webhooks/*")]
        (size,) = re.findall(r"request_body \{\s+max_size (\d+)MB\b", api)  # MB: 10**6 bytes
        self.assertTrue((10 << 20) + (64 << 10) <= int(size) * 10**6 <= 12 * 10**6)  # 10 MiB + form


if __name__ == "__main__":
    unittest.main()
