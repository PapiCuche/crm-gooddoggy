# Runbooks

Procedimientos operativos. Se escriben a partir de la Fase 1, conforme exista lo que operar:

- [Stack local en limpio](local-stack.md) (F1-10).
- [Vencimiento automático de reservas de inventario](inventory-reservation-expiry.md) (F3-34).
- [Activación de WhatsApp (Cloud API de Meta) con un número de prueba](whatsapp-activation.md) (F7-12a).
- [Backups, restauración y simulacro de producción](backup-restore.md) (F1-16).
- [Primer arranque de producción](production-first-boot.md) (F1-17).
- [Alta de un miembro en producción](add-member.md) (F2-127).

- Rotación de secretos (roles de BD, KEK de credenciales, `DJANGO_SECRET_KEY`, secretos de proveedores).
- Recuperación de acceso MFA.
- Incidente de fuga de datos / secreto expuesto.
- Reproceso de webhooks fallidos.
- Activación del kill switch de IA.
