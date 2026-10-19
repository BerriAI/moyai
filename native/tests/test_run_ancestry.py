"""The read query preserves canonical graph semantics without changing the source view."""
import random

from app.database import sqlite_connection

from moyai.history_graph import ancestry_query

OLD_ANCESTRY_QUERY = '''WITH RECURSIVE tree(ancestor_id,run_id) AS (
    SELECT id,id FROM runs UNION
    SELECT tree.ancestor_id,r.id FROM tree JOIN runs r ON r.parent_run_id=tree.run_id
) SELECT ancestor_id,run_id FROM tree'''


def test_ancestry_matches_reference_for_branches_cycles_orphans_and_unicode(tmp_path):
    rng = random.Random(41)
    parents = {'root': '', 'child': 'root', 'sibling': 'root', 'grandchild': 'child',
               'orphan': 'absent', 'cycle-a': 'cycle-b', 'cycle-b': 'cycle-a',
               'cycle-child': 'cycle-a', 'self': 'self', '日本語🗿': 'child'}
    for i in range(200):
        parents[str(i)] = rng.choice(['', 'absent', *parents])
    expected = set()
    for run in parents:
        ancestor = run
        seen = set()
        while ancestor in parents and ancestor not in seen:
            expected.add((ancestor, run))
            seen.add(ancestor)
            ancestor = parents[ancestor]
    with sqlite_connection(tmp_path / 'graph.db') as db:
        db.execute('CREATE TABLE runs(id TEXT PRIMARY KEY,parent_run_id TEXT)')
        db.executemany('INSERT INTO runs VALUES(?,?)', parents.items())
        for query in (OLD_ANCESTRY_QUERY, ancestry_query('runs')):
            rows = [tuple(row) for row in db.execute(query)]
            assert len(rows) == len(expected) and set(rows) == expected
