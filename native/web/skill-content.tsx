import React from "react"
import {
  BookOpen, Bug, CalendarDays, ChartNoAxesCombined, Cloud, Code2, Database, FileText,
  FlaskConical, GitPullRequest, Globe2, Image, Languages, ListPlus, Mail, MessageSquare,
  Mic2, Music2, Palette, Plug, Presentation, Rocket, Search, ShieldCheck, Table2, Terminal,
  Users, Video, Workflow, Wrench, type LucideIcon,
} from "lucide-react"
import { transcriptSkills, type SkillReference } from "./skill-mentions"
import { skillIconName, type SkillIconName } from "./skill-icon-map"
import "./skill-composer.css"

export type ComposerSkill = SkillReference & { id: string; description: string; scope: string }
export const skillScopeName = (scope: string) => ({ personal: "Personal", organization: "Organization", org: "Organization", shared: "Shared", bundled: "Built in" }[scope] || scope)
const displayWords: Record<string, string> = { litellm: "LiteLLM", api: "API", mcp: "MCP", pr: "PR", ui: "UI", e2e: "E2E", pdf: "PDF" }

export function skillDisplayName(name: string): string {
  return name.split(/[-_\s]+/).filter(Boolean).map(word => displayWords[word.toLowerCase()] || word[0].toUpperCase() + word.slice(1)).join(" ")
}

const iconComponents: Record<SkillIconName, LucideIcon> = {
  team: Users, tasks: ListPlus, search: Search, "pull-request": GitPullRequest, palette: Palette,
  code: Code2, bug: Bug, test: FlaskConical, shield: ShieldCheck, document: FileText,
  table: Table2, presentation: Presentation, image: Image, video: Video, music: Music2,
  voice: Mic2, globe: Globe2, database: Database, cloud: Cloud, rocket: Rocket,
  workflow: Workflow, plug: Plug, chart: ChartNoAxesCombined, terminal: Terminal, mail: Mail,
  chat: MessageSquare, calendar: CalendarDays, languages: Languages, book: BookOpen, wrench: Wrench,
}

export function SkillIcon({ skill }: { skill: Pick<SkillReference, "name" | "icon"> }) {
  const name = skillIconName(skill), Icon = iconComponents[name]
  return <Icon data-skill-icon={name} aria-hidden="true" size={16} strokeWidth={1.7} />
}

export function SkillMention({ skill }: { skill: SkillReference }) {
  return <span className="skill-mention" title={`/${skill.reference} · ${skillScopeName(skill.reference.split(":")[0])}`}
    aria-label={`/${skill.name} skill`}><SkillIcon skill={skill} /><span>{skillDisplayName(skill.name)}</span></span>
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
