"""Word Editor user profile service.

Manages display names, phones, access notes, and location assignments.
Does NOT touch passwords, system roles, or account enable/disable.
Every change logged to audit_log.
"""
from app.database import get_db
from app.services.errors import NotFoundError, ValidationError
from app.database import get_connection

__all__ = ["list_wd_users", "get_wd_user", "update_wd_user",
           "assign_locations", "get_user_locations"]


def list_wd_users() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT u.id, u.username, u.full_name, u.role, u.active,
                      COALESCE(p.display_name, u.full_name) as display_name,
                      COALESCE(p.phone, '') as phone,
                      COALESCE(p.access_note, '') as access_note,
                      GROUP_CONCAT(cc.name, ', ') as locations
               FROM users u
               LEFT JOIN wd_user_profiles p ON p.user_id = u.id
               LEFT JOIN wd_user_locations wl ON wl.user_id = u.id
               LEFT JOIN cost_centers cc ON cc.id = wl.cost_center_id
               WHERE u.active = 1
               GROUP BY u.id ORDER BY display_name"""
        ).fetchall()
        return [dict(r) for r in rows]


def get_wd_user(user_id: int) -> dict:
    with get_db() as conn:
        row = conn.execute(
            """SELECT u.id, u.username, u.full_name, u.role,
                      COALESCE(p.display_name, u.full_name) as display_name,
                      COALESCE(p.phone, '') as phone,
                      COALESCE(p.access_note, '') as access_note
               FROM users u
               LEFT JOIN wd_user_profiles p ON p.user_id = u.id
               WHERE u.id = ?""", (user_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("User not found.")
        u = dict(row)
        locs = conn.execute(
            """SELECT cc.id, cc.name FROM wd_user_locations wl
               JOIN cost_centers cc ON cc.id = wl.cost_center_id
               WHERE wl.user_id = ?""", (user_id,)
        ).fetchall()
        u["location_ids"] = [r["id"] for r in locs]
        u["location_names"] = [r["name"] for r in locs]
        return u


def update_wd_user(user_id: int, *, display_name: str, phone: str,
                   access_note: str, updated_by: int):
    with get_db() as conn:
        exists = conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
        if not exists:
            raise NotFoundError("User not found.")
        conn.execute(
            """INSERT INTO wd_user_profiles (user_id, display_name, phone, access_note, updated_by)
               VALUES (?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 display_name=excluded.display_name,
                 phone=excluded.phone,
                 access_note=excluded.access_note,
                 updated_by=excluded.updated_by,
                 updated_at=datetime('now')""",
            (user_id, display_name.strip(), phone.strip(),
             access_note.strip(), updated_by)
        )
        conn.execute(
            """INSERT INTO audit_log (user_id, action, details)
               VALUES (?, 'wd_user_profile_update', ?)""",
            (updated_by, f"Updated WD profile for user_id={user_id}: display_name={display_name!r}")
        )


def assign_locations(user_id: int, cost_center_ids: list[int], *, assigned_by: int):
    with get_db() as conn:
        conn.execute("DELETE FROM wd_user_locations WHERE user_id=?", (user_id,))
        for cc_id in cost_center_ids:
            conn.execute(
                "INSERT OR IGNORE INTO wd_user_locations (user_id, cost_center_id, assigned_by) VALUES (?,?,?)",
                (user_id, cc_id, assigned_by)
            )
        conn.execute(
            """INSERT INTO audit_log (user_id, action, details)
               VALUES (?, 'wd_user_locations_update', ?)""",
            (assigned_by, f"Updated WD locations for user_id={user_id}: cc_ids={cost_center_ids!r}")
        )


def get_user_locations(user_id: int) -> list[int]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT cost_center_id FROM wd_user_locations WHERE user_id=?", (user_id,)
        ).fetchall()
        return [r["cost_center_id"] for r in rows]
