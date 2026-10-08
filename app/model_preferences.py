"""Durable model defaults belonging to verified people, never session owners."""


def account_id(conn, user_id):
    linked = conn.execute("""SELECT linked.id FROM users u JOIN users linked
        ON linked.id=u.linked_user_id AND linked.kind='google'
        WHERE u.id=? AND u.kind='slack'""", (user_id,)).fetchone()
    return linked['id'] if linked else user_id


def preferred_model(conn, settings, user_id):
    canonical_id = account_id(conn, user_id)
    row = conn.execute('SELECT model FROM user_model_preferences WHERE user_id=?',
                       (canonical_id,)).fetchone()
    if row is None:
        # Linking may happen after a Slack choice was saved. Both web and Slack
        # must see that choice until the person saves a canonical preference.
        row = conn.execute("""SELECT p.model FROM user_model_preferences p
            JOIN users u ON u.id=p.user_id
            JOIN users target ON target.id=u.linked_user_id AND target.kind='google'
            WHERE u.kind='slack' AND target.id=?
            ORDER BY u.id LIMIT 1""", (canonical_id,)).fetchone()
    if row:
        try:
            return settings.resolve_model(row['model'])
        except ValueError:
            pass  # Removed models must not prevent starting a new session.
    return settings.resolve_model()


def save_model(conn, user_id, model):
    # Legacy runs without a surviving account can still switch this session.
    conn.execute("""INSERT INTO user_model_preferences(user_id,model)
        SELECT id,? FROM users WHERE id=?
        ON CONFLICT(user_id) DO UPDATE SET model=excluded.model""",
                 (model, account_id(conn, user_id)))
