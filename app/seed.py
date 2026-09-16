"""Seeds the database with a default admin user and starter categories on first run.
Safe to call every startup - only inserts data if it doesn't already exist."""
from app.database import get_connection
from app.auth import hash_password
from app.config import (
    DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD, DEFAULT_CATEGORIES,
    ROLES, PERMISSION_KEYS, DEFAULT_ROLE_PERMISSIONS,
    DEFAULT_ASSET_TAG_RULES, DEFAULT_ASSET_REQUIRED_CATEGORIES,
)


def seed_if_needed():
    conn = get_connection()
    try:
        user_count = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        if user_count == 0:
            conn.execute(
                "INSERT INTO users (username, password_hash, full_name, role) "
                "VALUES (?, ?, 'Manager Account', 'manager')",
                (DEFAULT_ADMIN_USERNAME, hash_password(DEFAULT_ADMIN_PASSWORD)),
            )
            print(f"[setup] Created default manager user '{DEFAULT_ADMIN_USERNAME}' "
                  f"- change the password after first login!")

        cat_count = conn.execute("SELECT COUNT(*) c FROM categories").fetchone()["c"]
        if cat_count == 0:
            for name in DEFAULT_CATEGORIES:
                conn.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))

        cc_count = conn.execute("SELECT COUNT(*) c FROM cost_centers").fetchone()["c"]
        if cc_count == 0:
            conn.execute(
                "INSERT OR IGNORE INTO cost_centers (code, name) VALUES ('FACTORY', 'Factory Floor')"
            )

        # --- Asset module: tag rules + which categories demand an asset ---
        # Both are first-run only, exactly like the permission matrix below:
        # once a manager has edited a tag pattern or unticked a category,
        # re-seeding would silently undo their choice on the next restart.
        tag_rule_count = conn.execute(
            "SELECT COUNT(*) c FROM asset_tag_rules"
        ).fetchone()["c"]
        if tag_rule_count == 0:
            for category, (pattern, example) in DEFAULT_ASSET_TAG_RULES.items():
                conn.execute(
                    "INSERT OR IGNORE INTO asset_tag_rules (category, pattern, example) "
                    "VALUES (?, ?, ?)",
                    (category, pattern, example),
                )

        # Ticks the maintenance categories on a fresh install only. A name
        # that isn't already a category gets created (Machine/Office
        # maintenance aren't in DEFAULT_CATEGORIES); one that is, is ticked
        # in place. `requires_asset = 0` on every other category means the
        # picker stays hidden for Fuel, advances, snacks and the rest -
        # fuel is a general operating cost here, not an asset cost.
        ticked_count = conn.execute(
            "SELECT COUNT(*) c FROM categories WHERE requires_asset = 1"
        ).fetchone()["c"]
        if ticked_count == 0 and cat_count == 0:
            for name, asset_category in DEFAULT_ASSET_REQUIRED_CATEGORIES.items():
                conn.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
                conn.execute(
                    "UPDATE categories SET requires_asset = 1, asset_category = ? "
                    "WHERE name = ?",
                    (asset_category, name),
                )

        perm_count = conn.execute("SELECT COUNT(*) c FROM role_permissions").fetchone()["c"]
        if perm_count == 0:
            for role in ROLES:
                allowed_keys = DEFAULT_ROLE_PERMISSIONS.get(role, set())
                for key in PERMISSION_KEYS:
                    conn.execute(
                        "INSERT OR IGNORE INTO role_permissions (role, permission, allowed) VALUES (?, ?, ?)",
                        (role, key, 1 if key in allowed_keys else 0),
                    )
        conn.commit()
    finally:
        conn.close()
