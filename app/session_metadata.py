"""Answer unambiguous requests for this session's ID without an agent turn."""
import json
import re

from .database import Connection


_SESSION = r"(?:session|chat|conversation|run)"
_SUBJECT = rf"(?:(?:this|current|my|your|our|the|this current|the current) )?{_SESSION}"
_ID = rf"(?:{_SUBJECT}(?:'s)? (?:id|identifier)|(?:the )?(?:id|identifier) (?:of|for) {_SUBJECT})"
_REQUEST = re.compile(
    rf"(?:{_ID}|what(?: is|'s) {_ID}|"
    rf"(?:(?:can|could|would) you )?(?:show|give|tell|send)(?: me| us)? {_ID}|"
    rf"(?:get|return|copy) {_ID})"
)


def is_session_id_request(content: str) -> bool:
    # Full-string matching is deliberate: quoted examples, other sessions and
    # requests that include real work must still reach the agent.
    text = ' '.join(content.lower().replace('’', "'").split()).rstrip('?.!')
    if text in {'/session-id', '/session_id'}:
        return True
    text = re.sub(r'^please[, ]+', '', text)
    text = re.sub(r',? please$', '', text)
    return _REQUEST.fullmatch(text) is not None


def session_id_response(run_id: str) -> str:
    return f'Session ID: `{run_id}`'


def complete_in(conn: Connection, run_id: str, message_id: int, stamp: str, *, initial: bool = False) -> None:
    """Save the input, answer and browser notification in the caller's transaction.

    Existing agent state (including approvals, steering and the current result)
    belongs to its active turn and is deliberately not updated here.
    """
    response = session_id_response(run_id)
    conn.execute("UPDATE messages SET status='completed',started_at=? WHERE id=? AND run_id=?",
                 (stamp, message_id, run_id))
    conn.execute("""INSERT INTO messages(run_id,role,content,status,created_at,model,user_id,response_to_id)
        SELECT run_id,'assistant',?,'completed',?,'',user_id,id FROM messages WHERE id=? AND run_id=?""",
                 (response, stamp, message_id, run_id))
    conn.execute("UPDATE runs SET updated_at=? WHERE id=?", (stamp, run_id))
    if initial:
        conn.execute("UPDATE runs SET status='idle',summary=?,display_title='Session ID',title_attempted_at=? WHERE id=?",
                     (response, stamp, run_id))
    conn.execute("INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,'chat','Session ID returned',?,?)",
                 (run_id, json.dumps({'message_id': message_id, 'source': 'session_metadata'}), stamp))
