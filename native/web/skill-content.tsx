import React from "react"
import { BookOpen, Bug, Code2, FileText, GitPullRequest, Globe, Image, Rocket, Search, Shield, Sparkles, Terminal, Users, Video } from "lucide-react"
import { transcriptSkills, type SkillReference } from "./skill-mentions"
import "./skill-composer.css"

export type ComposerSkill = SkillReference & { id: string; description: string; scope: string }
export const skillScopeName = (scope: string) => ({ personal: "Personal", organization: "Organization", org: "Organization", shared: "Shared", bundled: "Built in" }[scope] || scope)
const icons = { code: Code2, terminal: Terminal, users: Users, team: Users, search: Search, research: Search, document: FileText, file: FileText, review: GitPullRequest, "git-pull-request": GitPullRequest, shield: Shield, globe: Globe, bug: Bug, rocket: Rocket, image: Image, video: Video, book: BookOpen, sparkles: Sparkles }

export function SkillIcon({ skill }: { skill: Pick<SkillReference, "name" | "icon"> }) {
  if (skill.icon && /\p{Extended_Pictographic}/u.test(skill.icon) && [...skill.icon].length <= 12) return <span className="skill-emoji" aria-hidden="true">{skill.icon}</span>
  const hint = skill.icon && skill.icon !== "auto" ? skill.icon : /team/.test(skill.name) ? "team" : /review|pr/.test(skill.name) ? "review" : /research/.test(skill.name) ? "search" : "sparkles"
  const Icon = icons[hint as keyof typeof icons] || Sparkles
  return <Icon aria-hidden="true" size={17} />
}

export function SkillMention({ skill }: { skill: SkillReference }) {
  return <span className="skill-mention" title={`/${skill.reference} · ${skillScopeName(skill.reference.split(":")[0])}`}
    aria-label={`/${skill.name} skill`}><SkillIcon skill={skill} /><span>/{skill.name}</span></span>
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
