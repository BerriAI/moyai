// Curated local artwork only. Never interpolate skill-provided SVG or image URLs.
const skillIconChoices = [
  ['cube', 'Skill', 'cube'], ['team', 'Team', 'participants'],
  ['code', 'Code', 'code'], ['review', 'Review', 'pull-request'],
  ['search', 'Research', 'search'], ['document', 'Document', 'file'],
  ['chart', 'Analysis', 'chart'], ['design', 'Design', 'design'],
  ['video', 'Video', 'video'], ['chat', 'Communication', 'chat'],
  ['automation', 'Automation', 'bolt'], ['target', 'Goal', 'target'],
];
const skillIconRules = [
  ['team', /\b(team|agents|collaborate|parallel)\b/],
  ['video', /\b(video|film|animation)\b/],
  ['chat', /\b(slack|email|linkedin|reply|replies|communication)\b/],
  ['design', /\b(design|figma|ui|presentation|slides)\b/],
  ['review', /\b(review|pr|pull request)\b/],
  ['chart', /\b(benchmark|analysis|analytics|metrics|data)\b/],
  ['document', /\b(docs|document|documentation|pdf|writing|report|memo)\b/],
  ['search', /\b(research|search|investigate|explore)\b/],
  ['automation', /\b(automation|automate|deploy|install|schedule)\b/],
  ['code', /\b(code|coding|debug|fix|test|tests|refactor)\b/],
  ['target', /\b(goal|plan|planning)\b/],
];
function skillIconKey(skill = {}) {
  if (skill.builtin && skill.reference === 'goal') return 'target';
  if (skillIconChoices.some(([key]) => key === skill.icon)) return skill.icon;
  // Names are stable identity hints; descriptions often mention unrelated tasks.
  const name = String(skill.name || '').toLowerCase().replaceAll('-', ' ');
  return skillIconRules.find(([, pattern]) => pattern.test(name))?.[0] || 'cube';
}
function skillIcon(skill) {
  const key = skillIconKey(skill);
  const artwork = skillIconChoices.find(([name]) => name === key)[2];
  return `<span class="skill-symbol" data-skill-icon="${key}" aria-hidden="true">${MoyaiIcon(artwork, 16)}</span>`;
}
function skillIconField(skill, canEdit) {
  const selected = skill?.icon || 'auto';
  return `<div class="field"><label for="skill-icon">Icon</label><div class="skill-icon-field"><select id="skill-icon" ${canEdit ? '' : 'disabled'}><option value="auto" ${selected === 'auto' ? 'selected' : ''}>Automatic · based on name</option>${skillIconChoices.map(([key, label]) => `<option value="${key}" ${selected === key ? 'selected' : ''}>${label}</option>`).join('')}</select><span id="skill-icon-preview">${skillIcon(skill || {})}</span></div><small>Shown in the library, skill picker, and message composer.</small></div>`;
}
