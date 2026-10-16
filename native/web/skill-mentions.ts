export type SkillReference = { reference: string; name: string; icon?: string }
export type InlineSkill = SkillReference & { start: number; end: number }
export const skillReferencePattern = /^(?:personal|org|shared|bundled):[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/
const markerPattern = /^\[\/([A-Za-z0-9][A-Za-z0-9_.-]{0,99})\]\(moyai-skill:([A-Za-z0-9%_.~-]+)\?icon=([A-Za-z0-9%_.~-]*)\)/

export function skillMarker(skill: SkillReference): string {
  const encode = (value: string) => encodeURIComponent(value).replace(/[!'()*]/g, char => `%${char.charCodeAt(0).toString(16).toUpperCase()}`)
  return `[/${skill.name}](moyai-skill:${encode(skill.reference)}?icon=${encode(skill.icon || "auto")})`
}

/** Code/escaped references stay literal in the editor, transcript and backend. */
export function skillMentions(text: string): InlineSkill[] {
  const result: InlineSkill[] = []
  let index = 0
  while (index < text.length) {
    if ((index === 0 || text[index - 1] === "\n") && (text.startsWith("    ", index) || text[index] === "\t")) {
      const newline = text.indexOf("\n", index)
      index = newline < 0 ? text.length : newline + 1
      continue
    }
    if (text[index] === "\\") { index += 2; continue }
    if (text[index] === "`") {
      let end = index + 1
      while (text[end] === "`") end++
      const close = text.indexOf(text.slice(index, end), end)
      index = close < 0 ? text.length : close + end - index
      continue
    }
    if (text.startsWith("~~~", index) && !text.slice(text.lastIndexOf("\n", index - 1) + 1, index).trim()) {
      const close = /^ {0,3}~{3,}[ \t]*(?:\n|$)/m.exec(text.slice(index + 3))
      index = close ? index + 3 + close.index + close[0].length : text.length
      continue
    }
    if (text[index] !== "[") { index++; continue }
    const match = markerPattern.exec(text.slice(index))
    if (match) {
      try {
        const reference = decodeURIComponent(match[2]), icon = decodeURIComponent(match[3])
        if (skillReferencePattern.test(reference) && reference.split(":")[1] === match[1]) {
          result.push({ start: index, end: index + match[0].length, reference, name: match[1],
            icon: [...icon].length <= 32 && !/[\u0000-\u001f]/.test(icon) ? icon : "auto" })
        }
      } catch { /* Malformed pasted markers remain literal text. */ }
      index += match[0].length
    } else index++
  }
  return result
}

export function skillDisplayText(value: string): string {
  for (const skill of skillMentions(value).reverse()) value = value.slice(0, skill.start) + `/${skill.name}` + value.slice(skill.end)
  return value
}

/** Whether a caret at the end of this prefix is inside a code literal. */
export function skillQueryAllowed(prefix: string): boolean {
  const probe = skillMarker({ reference: "personal:probe", name: "probe" })
  return skillMentions(prefix + probe).some(mention => mention.start === prefix.length)
}

export function transcriptSkills(value: string): InlineSkill[] {
  const result = skillMentions(value)
  // Messages sent by the first slash-picker release retain their scoped text.
  const legacy = /^\s*\/((?:personal|org|shared|bundled):[A-Za-z0-9][A-Za-z0-9_.-]{0,99})(?=\s|$)/
  let offset = 0, match
  while (!value.slice(offset).startsWith("    ") && !value.slice(offset).startsWith("\t") && (match = legacy.exec(value.slice(offset)))) {
    const start = offset + match[0].indexOf("/")
    result.push({ start, end: offset + match[0].length, reference: match[1], name: match[1].split(":")[1], icon: "auto" })
    offset += match[0].length
  }
  return result.sort((a, b) => a.start - b.start)
}
