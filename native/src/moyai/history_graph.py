"""Statement-local equivalents of the canonical direct-parent history views."""


def ancestry_query(runs: str) -> str:
    # Callers supply their validated/quoted table name, never request input.
    # A primary-key parent lookup keeps recursive fan-out estimates bounded.
    # Joining both nodes excludes missing parents; UNION terminates cycles.
    return f"""WITH RECURSIVE tree(ancestor_id,run_id) AS (
        SELECT id,id FROM {runs} UNION
        SELECT parent.id,tree.run_id FROM tree
        JOIN {runs} child ON child.id=tree.ancestor_id
        JOIN {runs} parent ON parent.id=child.parent_run_id
    ) SELECT ancestor_id,run_id FROM tree"""
