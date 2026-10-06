"""Agent-authored skills: trusted ownership, attachment imports and private references."""
import hashlib
import json
import re
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .db import now

MAX_FILE = 1024 * 1024
MAX_BUNDLE = 4 * MAX_FILE
MAX_FILES = 20


def safe_path(value):
    if (len(value) > 200 or not re.fullmatch(r'[A-Za-z0-9_.\-/]+', value)
            or any(part in {'', '.', '..'} or part.startswith('.') for part in value.split('/'))
            or value.lower() == 'skill.md'):
        raise ValueError('Use a relative supporting-file path such as references/runbook.md.')
    return value


class SkillFile(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str
    attachment_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    content: str | None = Field(default=None, max_length=MAX_FILE)

    _path = field_validator('path')(safe_path)

    @model_validator(mode='after')
    def one_source(self):
        if (self.attachment_id is None) == (self.content is None):
            raise ValueError('Choose attachment_id or content for each supporting file.')
        return self


class SaveSkill(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(pattern=r'^[a-z0-9]+(?:-[a-z0-9]+)*$', max_length=64)
    description: str = Field(min_length=3, max_length=320)
    scope: Literal['personal', 'organization'] | None = Field(default=None,
        description='The user must choose personal or organization. Omit when they have not chosen; the tool returns a scope question without saving. Never infer a preference.')
    instructions: str | None = Field(default=None, min_length=3, max_length=32000)
    instructions_attachment_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    files: list[SkillFile] = Field(default_factory=list, max_length=MAX_FILES)
    remove_files: list[str] = Field(default_factory=list, max_length=MAX_FILES)
    expected_revision: int = Field(default=0, ge=0)
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,80}$')

    @model_validator(mode='after')
    def sources(self):
        if self.instructions is not None and self.instructions_attachment_id is not None:
            raise ValueError('Choose instructions or instructions_attachment_id.')
        paths = [file.path for file in self.files] + [safe_path(path) for path in self.remove_files]
        if len(paths) != len(set(paths)):
            raise ValueError('Each file path must occur only once.')
        return self


class ReadSkillFile(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(pattern=r'^(?:(?:personal|org):)?[a-z0-9]+(?:-[a-z0-9]+)*$', max_length=73)
    path: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=16000, ge=1, le=24000)
    search: str = Field(default='', max_length=200)

    _path = field_validator('path')(safe_path)


SAVE_TOOL = {
    'name': 'skills_save',
    'description': 'Create or update a reusable skill when the current user asks to save/install one. Ask the user to choose Personal (their requests only) or Organization (all teammates) before saving, unless they already specified the scope. Never infer a default. Omit scope when no choice was made: this returns scope_required without saving; present the question and wait for their reply. Organization publishing requires an admin. Ownership is set by the server. Use instructions_attachment_id to import an uploaded SKILL.md exactly, and files [{path: "references/runbook.md", attachment_id: "..."}] to copy supporting text files without reproducing their content. Only sent attachments from this session through the current turn are eligible. Inline instructions/content also work. On updates omit instructions to keep them; files are merged and remove_files explicitly deletes paths. Use expected_revision=0 to create, or the current revision from skills_search to update (skills_load stays pinned for a turn). Reuse request_id for identical retries. Never save secrets, claim success before this tool succeeds, or publish personally scoped content without the owner asking.',
    'inputSchema': SaveSkill.model_json_schema(),
    'annotations': {'readOnlyHint': False},
}
READ_TOOL = {
    'name': 'skills_read_file',
    'description': 'Read a supporting file from an authorized skill. Use an exact reference from skills_search or the user request and a path returned by skills_load. Optional search finds an exact text occurrence at/after offset; limit bounds the excerpt. The excerpt is privately inserted into the next model call; this tool returns offsets and metadata only. The four most recently read excerpts remain available; use next_offset to continue. This does not copy files into the sandbox or execute scripts.',
    'inputSchema': ReadSkillFile.model_json_schema(),
    'annotations': {'readOnlyHint': True},
}


def require_turn(run):
    if not run['chat_enabled'] or not run['active_user_id'] or not run['active_message_id']:
        raise HTTPException(403, 'Skills require an authenticated chat turn.')


def writing_actor(skills, run):
    require_turn(run)
    actor = run['active_user_id']
    if skills.security.local_preview() and actor == 'shared:local:admin':
        return actor, True
    settings = skills.security.settings
    # Only independently verified Google profiles and fresh matching Slack
    # profiles qualify. Accounting links never grant access or admin status.
    users = skills.store.rows("SELECT * FROM users WHERE kind='google'")
    matches = [u for u in users if skills.same_requester(u['id'], actor)]
    if len(matches) != 1 or not settings.google_enabled():
        raise HTTPException(403, 'Sign in with Google first. Slack also needs a fresh verified matching profile to save skills.')
    user = matches[0]
    if user['email'].rpartition('@')[2] not in settings.google_domains():
        raise HTTPException(403, 'This account is no longer eligible to manage skills.')
    return user['id'], skills.security.google_role(user['email']) == 'admin'


def bundle_row(conn, skill_id, revision):
    return conn.execute('SELECT * FROM skill_bundles WHERE skill_id=? AND revision<=? ORDER BY revision DESC LIMIT 1',
                        (skill_id, revision)).fetchone()


def bundle(skills, conn, skill_id, revision):
    row = bundle_row(conn, skill_id, revision)
    return json.loads(skills.security.decrypt(row['encrypted'])) if row else {}


def file_manifest(files):
    return [{'path': path, 'size': len(content.encode()), 'characters': len(content),
             'sha256': hashlib.sha256(content.encode()).hexdigest()} for path, content in sorted(files.items())]


def checked_text(raw, *, instructions=False):
    limit = 128000 if instructions else MAX_FILE
    if len(raw) > limit:
        raise HTTPException(413, 'Skill instructions must be at most 32,000 characters; supporting files at most 1 MiB each.')
    try:
        value = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise HTTPException(422, 'Skills support UTF-8 text files only.') from None
    if '\x00' in value or (instructions and not 3 <= len(value.strip()) <= 32000):
        raise HTTPException(422, 'Use text without NUL characters and 3–32,000 characters for instructions.')
    return value


def attachment_text(skills, run, attachment_id, *, instructions=False):
    raw = skills.store.attachments.broker_file(run, attachment_id).body
    return checked_text(raw, instructions=instructions)


def save_skill(skills, run, args):
    from .skills import SkillForm
    actor, admin = writing_actor(skills, run)
    if args.scope is None:
        return {'saved': False, 'status': 'scope_required',
                'question': 'Where should I save this skill: Personal (only for your requests) or Organization (shared with teammates)?',
                'choices': [{'scope': 'personal', 'label': 'Personal · my requests only', 'available': True},
                            {'scope': 'organization', 'label': 'Organization · all teammates', 'available': admin}],
                'instructions': 'Nothing has been saved. Ask the current user to choose and wait for their reply. Do not pick a default or retry with an inferred scope. Organization saves require an admin; explain that if unavailable.'}
    if args.scope == 'organization' and not admin:
        raise HTTPException(403, 'Only an administrator can publish organization skills.')
    fingerprint = hashlib.sha256(json.dumps(args.model_dump(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    operation = (run['id'], run['active_message_id'], args.request_id)
    with skills.store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        prior = conn.execute('SELECT * FROM skill_saves WHERE run_id=? AND message_id=? AND request_id=?', operation).fetchone()
        if prior:
            if prior['actor_id'] != actor or prior['fingerprint'] != fingerprint:
                raise HTTPException(409, 'This request_id was already used for a different save. Use a new request_id.')
            current = conn.execute('SELECT * FROM skills WHERE id=?', (prior['skill_id'],)).fetchone()
            if not current or not skills.manageable(current, actor, admin):
                raise HTTPException(404, 'Skill not found.')
            return json.loads(prior['result'])
        namespace = 'organization' if args.scope == 'organization' else actor
        old = conn.execute('SELECT * FROM skills WHERE namespace=? AND name=?', (namespace, args.name)).fetchone()
        if (old and old['revision'] != args.expected_revision) or (not old and args.expected_revision):
            raise HTTPException(409, f"Skill revision changed. Use expected_revision={old['revision'] if old else 0} after reviewing the current skill.")
        if old and old['archived']:
            raise HTTPException(409, 'Restore the archived skill from the Skills library before updating it.')
        instructions = args.instructions
        if args.instructions_attachment_id:
            instructions = attachment_text(skills, run, args.instructions_attachment_id, instructions=True)
        if instructions is None and old:
            instructions = skills.security.decrypt(old['encrypted'])
        if instructions is None:
            raise HTTPException(422, 'Provide instructions or an uploaded SKILL.md to create a skill.')
        checked_text(instructions.encode(), instructions=True)
        files = bundle(skills, conn, old['id'], old['revision']) if old else {}
        for path in args.remove_files:
            files.pop(path, None)
        for file in args.files:
            files[file.path] = (attachment_text(skills, run, file.attachment_id) if file.attachment_id
                                else checked_text(file.content.encode()))
        if len(files) > MAX_FILES or sum(len(text.encode()) for text in files.values()) > MAX_BUNDLE:
            raise HTTPException(413, 'A skill can contain at most 20 supporting files and 4 MiB of supporting text.')
        body = SkillForm(name=args.name, description=args.description, scope=args.scope, instructions=instructions,
                         revision=args.expected_revision, client_id=hashlib.sha256(json.dumps(operation).encode()).hexdigest())
        skill_id = skills.save(body, actor, admin, old['id'] if old else '', conn=conn)
        row = conn.execute('SELECT * FROM skills WHERE id=?', (skill_id,)).fetchone()
        manifest = file_manifest(files)
        if args.files or args.remove_files:
            conn.execute('INSERT INTO skill_bundles VALUES(?,?,?,?)',
                         (skill_id, row['revision'], skills.security.encrypt(json.dumps(files, ensure_ascii=False)), json.dumps(manifest)))
        result = {'saved': True, 'id': skill_id, 'reference': ('org:' if args.scope == 'organization' else 'personal:') + args.name,
                  'scope': args.scope, 'revision': row['revision'], 'files': manifest,
                  'message': 'Saved to the Skills library. This revision is available on future turns; already loaded turns keep their pinned revision.'}
        conn.execute('INSERT INTO skill_saves VALUES(?,?,?,?,?,?,?,?)', (*operation, actor, fingerprint, skill_id, json.dumps(result), now()))
    skills.store.event(run['id'], 'skill', 'Saved '+args.scope+' skill: '+args.name, {'revision': result['revision']})
    return result


def read_file(skills, run, args):
    require_turn(run)
    loaded = skills.load(run, args.name)
    skill = skills.find(args.name, run['active_user_id'])
    with skills.store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        files = bundle(skills, conn, skill['id'], loaded['revision'])
        if args.path not in files:
            raise HTTPException(404, 'Supporting file not found in the loaded skill revision.')
        content, offset = files[args.path], args.offset
        if args.search:
            found = content.find(args.search, offset)
            if found < 0:
                return {'found': False, 'path': args.path, 'characters': len(content)}
            offset = max(offset, found - 1000)
        end = min(len(content), offset + args.limit)
        if offset > len(content):
            raise HTTPException(422, 'The offset is past the end of this file.')
        conn.execute('DELETE FROM skill_file_reads WHERE run_id=? AND message_id=? AND skill_id=? AND path=? AND offset=?',
                     (run['id'], run['active_message_id'], skill['id'], args.path, offset))
        conn.execute('INSERT INTO skill_file_reads(run_id,message_id,actor_id,skill_id,path,offset,length) VALUES(?,?,?,?,?,?,?)',
                     (run['id'], run['active_message_id'], run['active_user_id'], skill['id'], args.path, offset, end - offset))
        conn.execute('DELETE FROM skill_file_reads WHERE run_id=? AND message_id=? AND id NOT IN (SELECT id FROM skill_file_reads WHERE run_id=? AND message_id=? ORDER BY id DESC LIMIT 4)',
                     (run['id'], run['active_message_id'], run['id'], run['active_message_id']))
    return {'loaded': True, 'reference': skills.metadata(skill)['reference'], 'revision': loaded['revision'],
            'path': args.path, 'offset': offset, 'next_offset': end if end < len(content) else None,
            'characters': len(content), 'excerpt_characters': end - offset}
