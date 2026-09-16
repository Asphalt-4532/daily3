"""Business logic for the trusted-hostname allowlist: which Host header
values this app will accept requests for.

Concern: this is deliberately narrow. It does NOT configure DNS, firewalls,
port forwarding, or anything about how traffic actually reaches this
machine - it only controls which Host header value(s) app.main's
TrustedHostFromDBMiddleware will accept once at least one is configured.
That's the one piece of "network configuration" a web app can safely and
meaningfully self-manage; everything else (setting up a Cloudflare Tunnel,
a reverse proxy, port forwarding) happens outside the app - see README.md's
"Exposing this beyond your LAN" section.

An empty list means "accept any Host header" (today's default, unrestricted
behavior - adding this feature must not change existing installs that never
touch it). localhost/127.0.0.1 are always implicitly accepted by the
middleware regardless of what's configured here, specifically so a manager
can't lock themselves out of local access by mis-configuring this list.

Shared/app-wide, not owned by any one program - matches app.services.users
and app.services.audit.
Depends on: app.database only.
Used by: app.routes.network_routes (management UI) and app.main
(TrustedHostFromDBMiddleware, which calls get_active_hostnames() per
request).

No FastAPI imports on purpose - see app/services/errors.py.
"""
import re

from app.database import get_db, log_action
from app.services.errors import ValidationError, NotFoundError

__all__ = [
    "list_network_configs", "get_active_hostnames", "add_network_config",
    "update_network_config", "toggle_network_config", "delete_network_config",
]

# A hostname, optionally with a :port. No protocol, no path - if someone
# pastes "https://expense.example.com/" we want a clear error, not a
# hostname that silently never matches any real Host header.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.[A-Za-z0-9-]{1,63})*(:\d{1,5})?$"
)


def _clean_hostname(raw: str) -> str:
    value = (raw or "").strip().lower()
    if not value:
        raise ValidationError("Hostname is required.")
    if "://" in value:
        raise ValidationError("Enter just the hostname (e.g. expense.example.com), not a full URL.")
    if "/" in value:
        raise ValidationError("Enter just the hostname, without a path.")
    if not _HOSTNAME_RE.match(value):
        raise ValidationError(f"'{raw}' doesn't look like a valid hostname.")
    return value


def list_network_configs():
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT nc.*, u.full_name created_by_name FROM network_configs nc "
            "JOIN users u ON u.id = nc.created_by ORDER BY nc.created_at"
        ).fetchall()]


def get_active_hostnames():
    """Returns the set of currently-active configured hostnames (lowercase,
    no scheme/path). Does NOT include the always-safe localhost/testserver
    defaults - see app.main.TrustedHostFromDBMiddleware for those."""
    with get_db() as conn:
        rows = conn.execute("SELECT hostname FROM network_configs WHERE active=1").fetchall()
        return {r["hostname"] for r in rows}


def add_network_config(name, hostname, notes, acting_user_id):
    name = (name or "").strip()
    if not name:
        raise ValidationError("A label is required (e.g. 'Cloudflare Tunnel').")
    hostname = _clean_hostname(hostname)
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM network_configs WHERE hostname=?", (hostname,)
        ).fetchone()
        if existing:
            raise ValidationError(f"'{hostname}' is already in the list.")
        conn.execute(
            "INSERT INTO network_configs (name, hostname, notes, created_by) VALUES (?, ?, ?, ?)",
            (name, hostname, (notes or "").strip(), acting_user_id),
        )
        log_action(conn, acting_user_id, "add_network_config", f"{name} ({hostname})")


def update_network_config(config_id, name, hostname, notes, acting_user_id):
    name = (name or "").strip()
    if not name:
        raise ValidationError("A label is required (e.g. 'Cloudflare Tunnel').")
    hostname = _clean_hostname(hostname)
    with get_db() as conn:
        row = conn.execute("SELECT id FROM network_configs WHERE id=?", (config_id,)).fetchone()
        if not row:
            raise NotFoundError("Network entry not found.")
        clash = conn.execute(
            "SELECT id FROM network_configs WHERE hostname=? AND id!=?", (hostname, config_id)
        ).fetchone()
        if clash:
            raise ValidationError(f"'{hostname}' is already in the list.")
        conn.execute(
            "UPDATE network_configs SET name=?, hostname=?, notes=? WHERE id=?",
            (name, hostname, (notes or "").strip(), config_id),
        )
        log_action(conn, acting_user_id, "update_network_config", f"{name} ({hostname})")


def toggle_network_config(config_id, acting_user_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM network_configs WHERE id=?", (config_id,)).fetchone()
        if not row:
            raise NotFoundError("Network entry not found.")
        conn.execute("UPDATE network_configs SET active = 1 - active WHERE id=?", (config_id,))
        log_action(conn, acting_user_id, "toggle_network_config",
                   f"{row['name']} ({row['hostname']}) -> {'inactive' if row['active'] else 'active'}")


def delete_network_config(config_id, acting_user_id):
    """A real delete, not soft-delete - this is operational configuration,
    not a financial record, so there's nothing that needs preserving
    forever. Still logged, since who removed a trusted hostname matters
    for the app's security posture."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM network_configs WHERE id=?", (config_id,)).fetchone()
        if not row:
            raise NotFoundError("Network entry not found.")
        conn.execute("DELETE FROM network_configs WHERE id=?", (config_id,))
        log_action(conn, acting_user_id, "delete_network_config", f"{row['name']} ({row['hostname']})")
