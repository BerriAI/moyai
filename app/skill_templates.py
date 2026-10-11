"""Install release-owned skill templates into the existing organization library.

Runs once at application startup, never on the inference path. The stored source
version and digest allow upgrades without replacing an administrator's edits.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from uuid import NAMESPACE_URL, uuid5

import yaml

from .db import now
from .skill_tools import bundle, checked_text, file_manifest, safe_path


ROOT = Path(__file__).resolve().parents[1] / 'agent' / 'skill_templates'
OWNER = 'system:bundled-coding-skills'


def digest(name, description, icon, instructions, files):
    data = [name, description, icon, instructions, files]
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class BundledSkill:
    name: str
    description: str
    instructions: str
    icon: str
    version: int
    files: dict[str, str]

    @property
    def id(self):
        # Keep the original stable identity when template source locations move.
        return uuid5(NAMESPACE_URL, 'https://github.com/BerriAI/moyai/agent/skills/' + self.name).hex

    @property
    def client_id(self):
        return f'bundled-{self.version}-' + digest(self.name, self.description, self.icon, self.instructions, self.files)


def load_pack(root=ROOT):
    # Only checked-in, release-owned files are read. No repo scanning, network,
    # script execution, or user-provided installation paths on the request path.
    from .skills import SkillForm

    result = []
    for entry in sorted(root.glob('*/SKILL.md')):
        if entry.is_symlink() or entry.parent.is_symlink():
            raise ValueError('Bundled skills must be regular files.')
        parts = checked_text(entry.read_bytes(), instructions=True).split('---', 2)
        if len(parts) != 3 or parts[0].strip():
            raise ValueError(f'Missing skill frontmatter: {entry.parent.name}')
        header = yaml.safe_load(parts[1])
        metadata = header.pop('metadata')['moyai']
        version = metadata['version']
        if type(version) is not int or not 1 <= version <= 999999:
            raise ValueError('Bundled skill versions must be positive integers.')
        form = SkillForm(**header, icon=metadata['icon'], instructions=parts[2].strip(),
                         scope='organization', client_id='bundled-template')
        if form.name != entry.parent.name:
            raise ValueError('Bundled skill name must match its directory.')
        files = {}
        for path in sorted(entry.parent.rglob('*')):
            if path.is_symlink():
                raise ValueError('Bundled supporting files cannot be symlinks.')
            if path.is_file() and path != entry:
                files[safe_path(path.relative_to(entry.parent).as_posix())] = checked_text(path.read_bytes())
        result.append(BundledSkill(form.name, form.description, form.instructions, form.icon, version, files))
    return result


def install_pack(skills, pack=None):
    templates = load_pack() if pack is None else pack
    result = {'installed': [], 'updated': [], 'preserved': []}
    with skills.store.connect() as conn:
        # SQLite IMMEDIATE / PostgreSQL advisory transaction lock protects the
        # check-and-install across API, broker and coordinator processes.
        conn.begin_write()
        for template in templates:
            old = conn.execute('SELECT * FROM skills WHERE id=?', (template.id,)).fetchone()
            if old:
                source = re.fullmatch(r'bundled-(\d+)-([a-f0-9]{64})', old['client_id'])
                if (old['owner_id'] != OWNER or old['scope'] != 'organization' or old['archived'] or not source
                        or int(source[1]) >= template.version):
                    result['preserved'].append(template.name)
                    continue
                current = digest(old['name'], old['description'], old['icon'], skills.security.decrypt(old['encrypted']),
                                 bundle(skills, conn, old['id'], old['revision']))
                if current != source[2]:
                    result['preserved'].append(template.name)
                    continue
                revision = old['revision'] + 1
                conn.execute('''UPDATE skills SET description=?,encrypted=?,icon=?,revision=?,updated_at=?,client_id=?
                    WHERE id=?''', (template.description, skills.security.encrypt(template.instructions), template.icon,
                                   revision, now(), template.client_id, template.id))
                action = 'updated'
            else:
                collision = conn.execute('SELECT 1 FROM skills WHERE namespace=? AND name=?',
                                         ('organization', template.name)).fetchone()
                if collision:
                    result['preserved'].append(template.name)
                    continue
                revision = 1
                conn.execute('''INSERT INTO skills(id,name,description,encrypted,scope,owner_id,namespace,
                    created_at,updated_at,client_id,icon) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                    (template.id, template.name, template.description, skills.security.encrypt(template.instructions),
                     'organization', OWNER, 'organization', now(), now(), template.client_id, template.icon))
                action = 'installed'
            conn.execute('INSERT INTO skill_bundles VALUES(?,?,?,?)',
                         (template.id, revision, skills.security.encrypt(json.dumps(template.files)),
                          json.dumps(file_manifest(template.files))))
            skills.audit(conn, template.id, OWNER, 'bundled ' + action)
            result[action].append(template.name)
    return result
