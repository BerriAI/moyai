import React, { useEffect, useId, useRef, useState } from "react"
import { BookOpen, Bug, Code2, FileText, GitPullRequest, Globe, Image, Rocket, Search, Shield, Sparkles, Terminal, Users, Video, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { Popover, PopoverAnchor, PopoverContent } from "@/components/ui/popover"
import { api, csrfToken } from "./api"
import "./skill-composer.css"

export type ComposerSkill = { id: string; name: string; description: string; scope: string; reference: string; icon?: string }
type SkillPage = { skills: ComposerSkill[]; has_more: boolean; next_offset: number | null }
const scopeName = (scope: string) => ({ personal: "Personal", organization: "Organization", org: "Organization", shared: "Shared", bundled: "Built in" }[scope] || scope)
const icons = { code: Code2, terminal: Terminal, users: Users, team: Users, search: Search, research: Search, document: FileText, file: FileText, review: GitPullRequest, "git-pull-request": GitPullRequest, shield: Shield, globe: Globe, bug: Bug, rocket: Rocket, image: Image, video: Video, book: BookOpen, sparkles: Sparkles }

export function SkillIcon({ skill }: { skill: Pick<ComposerSkill, "name" | "icon"> }) {
  if (skill.icon && /\p{Extended_Pictographic}/u.test(skill.icon) && [...skill.icon].length <= 12) {
    return <span className="skill-emoji" aria-hidden="true">{skill.icon}</span>
  }
  const hint = skill.icon && skill.icon !== "auto" ? skill.icon : /team/.test(skill.name) ? "team" : /review|pr/.test(skill.name) ? "review" : /research/.test(skill.name) ? "search" : "sparkles"
  const Icon = icons[hint as keyof typeof icons] || Sparkles
  return <Icon aria-hidden="true" size={17} />
}

export function skillDraft(value: string): { references: string[]; text: string } {
  let text = value
  const references: string[] = []
  // A terminating space commits a selection. An unfinished scoped name stays editable.
  for (let match; (match = text.match(/^\s*\/((?:personal|org|shared|bundled):[A-Za-z0-9][A-Za-z0-9_.-]{0,99})\s+/));) {
    references.push(match[1]); text = text.slice(match[0].length)
  }
  return { references, text }
}

function compose(references: string[], text: string) { return references.map(reference => `/${reference} `).join("") + text }

export const SkillComposer = React.forwardRef<HTMLTextAreaElement, {
  value: string; onChange: (value: string) => void; onSend: () => void
  onPaste?: React.ClipboardEventHandler<HTMLTextAreaElement>
  placeholder: string; shared?: boolean; disabled?: boolean
}>(function SkillComposer({ value, onChange, onSend, onPaste, placeholder, shared = false, disabled = false }, forwardedRef) {
  const input = useRef<HTMLTextAreaElement | null>(null)
  const id = useId(), auth = csrfToken(), scope = `${auth}:${shared}`
  const [focused, setFocused] = useState(false), [dismissed, setDismissed] = useState(false)
  const [version, setVersion] = useState(0), [active, setActive] = useState(0)
  const loadedCatalog = useRef("")
  const [state, setState] = useState<{ scope: string; rows: ComposerSkill[]; loading: boolean; error: string }>({ scope: "", rows: [], loading: false, error: "" })
  const draft = skillDraft(value)
  const query = draft.text.match(/^\/([^\s/]*)$/)?.[1]
  const wantsPicker = focused && query !== undefined && !disabled
  const needsCatalog = wantsPicker || draft.references.length > 0
  useEffect(() => {
    if (!needsCatalog) return
    const cacheKey = `${scope}:${version}`
    if (loadedCatalog.current === cacheKey) return
    const controller = new AbortController()
    setState({ scope, rows: [], loading: true, error: "" })
    void (async () => {
      const rows = new Map<string, ComposerSkill>()
      let offset = 0
      do {
        const page = await api<SkillPage>(`/skills?shared=${shared}&offset=${offset}&limit=100`, "GET", undefined, controller.signal)
        if (controller.signal.aborted || csrfToken() !== auth) return
        for (const skill of page.skills) rows.set(skill.reference, skill)
        if (!page.has_more) break
        if (page.next_offset === null || page.next_offset <= offset || page.next_offset > 10000) throw new Error("The skill list could not be loaded. Retry.")
        offset = page.next_offset
      } while (true)
      loadedCatalog.current = cacheKey
      setState({ scope, rows: [...rows.values()], loading: false, error: "" })
    })().catch(reason => { if (!controller.signal.aborted && csrfToken() === auth) setState({ scope, rows: [], loading: false, error: String(reason) }) })
    return () => controller.abort()
  }, [scope, needsCatalog, version])
  useEffect(() => { setActive(0); setDismissed(false) }, [query])
  useEffect(() => {
    const clear = () => { setState({ scope: "", rows: [], loading: false, error: "" }); setVersion(value => value + 1) }
    window.addEventListener("moyai:access-changed", clear)
    return () => window.removeEventListener("moyai:access-changed", clear)
  }, [])
  const rows = state.scope === scope ? state.rows : []
  const loading = state.scope !== scope || state.loading
  const matches = rows.filter(skill => !draft.references.includes(skill.reference) && `${skill.name} ${skill.reference} ${skill.description}`.toLowerCase().includes((query || "").toLowerCase()))
    .sort((a, b) => Number(b.name === query) - Number(a.name === query) || a.name.localeCompare(b.name)).slice(0, 30)
  const selectedIndex = Math.min(active, Math.max(matches.length - 1, 0))
  const open = wantsPicker && !dismissed
  function pick(skill: ComposerSkill) {
    onChange(compose([...new Set([...draft.references, skill.reference])], ""))
    setDismissed(true); input.current?.focus()
  }
  function remove(index: number) {
    onChange(compose(draft.references.filter((_, item) => item !== index), draft.text))
    input.current?.focus()
  }
  useEffect(() => {
    if (open) document.getElementById(`${id}-${selectedIndex}`)?.scrollIntoView({ block: "nearest" })
  }, [open, selectedIndex, id])
  return <div className="skill-composer">
    {draft.references.length > 0 && <div className="skill-chips" aria-label="Selected skills">{draft.references.map((reference, index) => {
      const skill = rows.find(row => row.reference === reference) || { name: reference.split(":").slice(1).join(":"), scope: reference.split(":")[0], icon: "auto" }
      return <span className="skill-chip" key={`${reference}-${index}`} title={`/${reference} · ${scopeName(skill.scope)}`}>
        <SkillIcon skill={skill} /><span>{skill.name}</span>
        <Button type="button" variant="ghost" size="icon-sm" disabled={disabled} aria-label={`Remove ${skill.name} skill`} onClick={() => remove(index)}><X size={13} /></Button>
      </span>
    })}</div>}
    <Popover open={open} onOpenChange={next => { if (!next) setDismissed(true) }}>
      <PopoverAnchor asChild>
        <Textarea ref={node => { input.current = node; if (typeof forwardedRef === "function") forwardedRef(node); else if (forwardedRef) forwardedRef.current = node }}
          aria-label="Message Moyai" placeholder={draft.references.length ? "Add instructions for this skill…" : placeholder}
          value={draft.text} disabled={disabled}
          aria-controls={open ? id : undefined} aria-expanded={open} aria-autocomplete="list"
          aria-activedescendant={open && matches.length ? `${id}-${selectedIndex}` : undefined}
          onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
          onChange={event => onChange(compose(draft.references, event.target.value))} onPaste={onPaste}
          onKeyDown={event => {
            if (event.nativeEvent.isComposing) return
            if (open && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
              event.preventDefault(); setActive(index => matches.length ? (index + (event.key === "ArrowDown" ? 1 : -1) + matches.length) % matches.length : 0); return
            }
            if (open && event.key === "Escape") { event.preventDefault(); setDismissed(true); return }
            if (open && !event.shiftKey && (event.key === "Enter" || event.key === "Tab") && matches.length) {
              event.preventDefault(); pick(matches[selectedIndex]); return
            }
            if (event.key === "Backspace" && !draft.text && draft.references.length) { event.preventDefault(); remove(draft.references.length - 1); return }
            if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); if (!open || (!loading && !state.error)) onSend() }
          }} />
      </PopoverAnchor>
      <PopoverContent className="skill-picker" side="top" align="start" sideOffset={8}
        onOpenAutoFocus={event => event.preventDefault()} onCloseAutoFocus={event => event.preventDefault()}
        onInteractOutside={event => { if (event.target === input.current) event.preventDefault() }}>
        <div className="skill-picker-heading"><span>Skills</span><small>↑ ↓ to browse · Enter to select</small></div>
        {loading ? <p role="status">Loading skills…</p> : state.error ? <div role="alert"><p>Couldn’t load your skills.</p><Button type="button" variant="outline" size="sm" onMouseDown={event => event.preventDefault()} onClick={() => setVersion(value => value + 1)}>Retry skills</Button></div>
          : matches.length ? <div id={id} role="listbox" aria-label="Skills" className="skill-options">{matches.map((skill, index) =>
            <button type="button" role="option" aria-selected={selectedIndex === index} tabIndex={-1}
              id={`${id}-${index}`} key={skill.reference} className="skill-option" onMouseDown={event => event.preventDefault()} onClick={() => pick(skill)}>
              <span className="skill-option-icon"><SkillIcon skill={skill} /></span>
              <span className="skill-option-copy"><span className="skill-option-title">{skill.name}<small>{scopeName(skill.scope)}</small></span><span className="skill-option-description">{skill.description}</span></span>
            </button>)}</div>
          : <p role="status">No matching skills{shared ? " available in shared sessions" : ""}. <a href="#settings/skills">View skills</a></p>}
      </PopoverContent>
    </Popover>
  </div>
})
