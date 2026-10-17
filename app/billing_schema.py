"""Recognize the reviewed billing compatibility view during offline migration."""

from .database import DatabaseError, identifier


def consolidated_billing_view(connection, schema):
    kind = connection.execute('''SELECT c.relkind::text
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=? AND c.relname='model_requests' ''', (schema,)).fetchone()
    if kind is None or kind[0] != 'v':
        return False
    targets = connection.execute('''SELECT DISTINCT n.nspname
        FROM pg_rewrite r JOIN pg_depend d ON d.objid=r.oid AND d.classid='pg_rewrite'::regclass
        JOIN pg_class target ON target.oid=d.refobjid AND d.refclassid='pg_class'::regclass
        JOIN pg_namespace n ON n.oid=target.relnamespace
        WHERE r.ev_class=to_regclass(?) AND target.relname='moyai_billing_records'
          AND target.relkind='r' ''', (identifier(schema) + '.model_requests',)).fetchall()
    if len(targets) != 1:
        raise DatabaseError('The billing compatibility view has no unique consolidated store.')
    native_schema = targets[0][0]
    state = connection.execute(f'''SELECT phase,source_schema,native_schema,view_manifest
        FROM {identifier(native_schema)}.moyai_billing_storage WHERE id=1''').fetchone()
    definition = connection.execute('SELECT pg_get_viewdef(to_regclass(?),true)',
                                    (identifier(schema) + '.model_requests',)).fetchone()[0]
    if state is None or (
        state[0] != 'consolidated' or state[1] != schema or state[2] != native_schema
        or not isinstance(state[3], dict) or state[3].get('version') != 1
        or state[3].get('legacy', {}).get('definition') != definition
    ):
        raise DatabaseError('The billing compatibility view differs from its cutover receipt.')
    return True
