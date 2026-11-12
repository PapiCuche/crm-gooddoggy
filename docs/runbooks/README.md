# Runbooks

Procedimientos operativos. Se escriben a partir de la Fase 1, conforme exista lo que operar:

- [Stack local en limpio](local-stack.md) (F1-10).
- [Vencimiento automático de reservas de inventario](inventory-reservation-expiry.md) (F3-34).

- Restauración de backups (BD + object storage) y prueba mensual.
- Rotación de secretos (roles de BD, KEK de credenciales, `DJANGO_SECRET_KEY`, secretos de proveedores).
- Recuperación de acceso MFA.
- Incidente de fuga de datos / secreto expuesto.
- Reproceso de webhooks fallidos.
- Activación del kill switch de IA.
