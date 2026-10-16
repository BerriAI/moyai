import type { SkillReference } from "./skill-mentions"

// Keep this pool and its order stable: unfamiliar names use a deterministic slot.
export const SKILL_ICON_NAMES = [
  "team", "tasks", "search", "pull-request", "palette", "code", "bug", "test",
  "shield", "document", "table", "presentation", "image", "video", "music",
  "voice", "globe", "database", "cloud", "rocket", "workflow", "plug", "chart",
  "terminal", "mail", "chat", "calendar", "languages", "book", "wrench",
] as const
export type SkillIconName = typeof SKILL_ICON_NAMES[number]

const nameRules: ReadonlyArray<readonly [RegExp, SkillIconName]> = [
  [/\b(pr|pull request|pullrequest|code review|review code)\b/, "pull-request"],
  [/\b(brand|branding|design|palette|visuals|figma)\b/, "palette"],
  [/\b(security|secure|permission|permissions|auth|credential|credentials|vpn)\b/, "shield"],
  [/\b(test|tests|testing|testwriter|e2e|qa|eval|evals|evaluation)\b/, "test"],
  [/\b(bug|bugs|debug|debugging|repro|rca|incident)\b/, "bug"],
  [/\b(team|squad|collaboration|collaborate)\b/, "team"],
  [/\b(calendar|schedule|scheduling|daily|weekly|morning|gm|meeting|meetings|reminder)\b/, "calendar"],
  [/\b(automation|automate|workflow|pipeline|orchestrate|plan|planning|planner|spec)\b/, "workflow"],
  [/\b(issue|issues|task|tasks|todo|triage)\b/, "tasks"],
  [/\b(research|search|investigate|investigation|find|lookup)\b/, "search"],
  [/\b(spreadsheet|spreadsheets|sheets|excel|csv|table)\b/, "table"],
  [/\b(slide|slides|slideshow|deck|presentation|presentations|pptx)\b/, "presentation"],
  [/\b(image|images|imagegen|photo|photos|thumbnail|banner|illustration)\b/, "image"],
  [/\b(voice|voiceover|speech|tts|transcription|transcribe)\b/, "voice"],
  [/\b(audio|music|sound|bgm|sfx)\b/, "music"],
  [/\b(video|videos|animation|animate|motion|caption|captions)\b/, "video"],
  [/\b(analytics|metrics|benchmark|dashboard|performance|spend|cost|budget|expense|expenses|reimbursement|payment|billing)\b/, "chart"],
  [/\b(database|sql|postgres|mysql|redis|data)\b/, "database"],
  [/\b(deploy|deployment|release|launch|publish)\b/, "rocket"],
  [/\b(cloud|aws|azure|gcp|docker|kubernetes|k8s|infrastructure)\b/, "cloud"],
  [/\b(connector|connectors|integration|integrate|plugin|plugins|mcp)\b/, "plug"],
  [/\b(terminal|shell|bash|zsh|cli)\b/, "terminal"],
  [/\b(code|coding|python|javascript|typescript|sdk|api|implement|implementation|build|builder)\b/, "code"],
  [/\b(email|mail|gmail|outlook|inbox|newsletter)\b/, "mail"],
  [/\b(chat|slack|message|messages|discord|social|linkedin|post)\b/, "chat"],
  [/\b(translate|translation|language|languages|localization|i18n)\b/, "languages"],
  [/\b(travel|flight|hotel)\b/, "globe"],
  [/\b(docs|documentation|guide|learn|learning|knowledge|book|read)\b/, "book"],
  [/\b(document|documents|docx|word|pdf|report|notes|write|writing)\b/, "document"],
  [/\b(web|website|sites|site|browser)\b/, "globe"],
  [/\b(tool|tools|maintenance|cleanup|clean|clear|repair|worktree|setup|install)\b/, "wrench"],
  [/\bhyperframes\b/, "video"],
]

const iconAliases: Record<string, SkillIconName> = {
  users: "team", people: "team", "👥": "team",
  issue: "tasks", task: "tasks", "list-plus": "tasks", "📝": "tasks",
  research: "search", "🔍": "search", "🔎": "search",
  review: "pull-request", "git-pull-request": "pull-request",
  design: "palette", "🎨": "palette", "💻": "code", "🐛": "bug",
  flask: "test", "🧪": "test", security: "shield", "🛡": "shield",
  file: "document", "file-text": "document", "📄": "document",
  spreadsheet: "table", slides: "presentation", "🖼": "image",
  "🎬": "video", "🎥": "video", audio: "music", "🎵": "music", "🎧": "music",
  mic: "voice", "🎤": "voice", web: "globe", "🌐": "globe",
  "🗃": "database", "🗄": "database", "☁": "cloud", deploy: "rocket", "🚀": "rocket",
  automation: "workflow", "🔌": "plug", analytics: "chart", "📊": "chart", "📈": "chart",
  email: "mail", "✉": "mail", "📧": "mail", message: "chat", "💬": "chat",
  "📅": "calendar", translation: "languages", "📚": "book", "📖": "book",
  tools: "wrench", "🔧": "wrench", "🛠": "wrench",
}

/** A pure resolver shared by the picker, editor and saved-message renderer. */
export function skillIconName(skill: Pick<SkillReference, "name" | "icon">): SkillIconName {
  const name = skill.name.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim()
  for (const [pattern, icon] of nameRules) if (pattern.test(name)) return icon
  const hint = (skill.icon || "").replace(/\uFE0F/g, "").toLowerCase()
  const configured = Object.hasOwn(iconAliases, hint) ? iconAliases[hint] : SKILL_ICON_NAMES.find(icon => icon === hint)
  if (configured) return configured
  let hash = 2166136261
  for (let index = 0; index < name.length; index++) hash = Math.imul(hash ^ name.charCodeAt(index), 16777619)
  return SKILL_ICON_NAMES[(hash >>> 0) % SKILL_ICON_NAMES.length]
}
