import React from "react"
import { Box } from "lucide-react"
import { transcriptSkills, type SkillReference } from "./skill-mentions"
import "./skill-composer.css"

export type ComposerSkill = SkillReference & { id: string; description: string; scope: string }
export const skillScopeName = (scope: string) => ({ personal: "Personal", organization: "Organization", org: "Organization", shared: "Shared", bundled: "Built in" }[scope] || scope)
const displayWords: Record<string, string> = { litellm: "LiteLLM", api: "API", mcp: "MCP", pr: "PR", ui: "UI", e2e: "E2E", pdf: "PDF" }

export function skillDisplayName(name: string): string {
  return name.split(/[-_\s]+/).filter(Boolean).map(word => displayWords[word.toLowerCase()] || word[0].toUpperCase() + word.slice(1)).join(" ")
}

export function SkillIcon() {
  return <Box aria-hidden="true" size={16} strokeWidth={1.7} />
}

export function SkillMention({ skill }: { skill: SkillReference }) {
  return <span className="skill-mention" title={`/${skill.reference} · ${skillScopeName(skill.reference.split(":")[0])}`}
    aria-label={`/${skill.name} skill`}><SkillIcon /><span>{skillDisplayName(skill.name)}</span></span>
}

export function SkillMessage({ content }: { content: string }) {
  const parts: React.ReactNode[] = []
  let offset = 0
  for (const skill of transcriptSkills(content)) {
    parts.push(content.slice(offset, skill.start), <SkillMention key={skill.start} skill={skill} />)
    offset = skill.end
  }
  parts.push(content.slice(offset))
  return <>{parts}</>
}
