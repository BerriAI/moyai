import React, { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react"
import {
  $createLineBreakNode, $createParagraphNode, $createTextNode, $getNodeByKey, $getRoot, $getSelection,
  $isNodeSelection, $isRangeSelection, $isTextNode, COMMAND_PRIORITY_HIGH, COPY_COMMAND, CUT_COMMAND,
  HISTORY_PUSH_TAG, KEY_DOWN_COMMAND, PASTE_COMMAND, SKIP_DOM_SELECTION_TAG, type LexicalNode,
} from "lexical"
import { LexicalComposer } from "@lexical/react/LexicalComposer"
import { useLexicalComposerContext } from "@lexical/react/LexicalComposerContext"
import { ContentEditable } from "@lexical/react/LexicalContentEditable"
import { PlainTextPlugin } from "@lexical/react/LexicalPlainTextPlugin"
import { HistoryPlugin } from "@lexical/react/LexicalHistoryPlugin"
import { LexicalErrorBoundary } from "@lexical/react/LexicalErrorBoundary"
import { mergeRegister } from "@lexical/utils"
import { Button } from "@/components/ui/button"
import { Popover, PopoverAnchor, PopoverContent } from "@/components/ui/popover"
import { csrfToken } from "./api"
import { listInvocableSkills } from "./generated/api"
import { SkillIcon, skillDisplayName, skillScopeName, type ComposerSkill } from "./skill-content"
import { $createSkillNode, SkillNode } from "./skill-node"
import { skillDisplayText, skillMentions, skillQueryAllowed, transcriptSkills } from "./skill-mentions"
import "./skill-composer.css"

type Query = { key: string; start: number; end: number; text: string }
type Props = {
  value: string; onChange: (value: string) => void; onSend: (value: string) => void
  onPaste?: React.ClipboardEventHandler<HTMLElement>
  placeholder: string; shared?: boolean; disabled?: boolean; sessionId?: string; focusRequest?: number
}
const CLIPBOARD_TYPE = "application/x-moyai-message"

function $textNodes(text: string): LexicalNode[] {
  return text.split("\n").flatMap((line, index) => [
    ...(index ? [$createLineBreakNode()] : []), ...(line ? [$createTextNode(line)] : []),
  ])
}
function $contentNodes(value: string, legacy = false): LexicalNode[] {
  const nodes: LexicalNode[] = []
  let offset = 0
  for (const skill of legacy ? transcriptSkills(value) : skillMentions(value)) {
    nodes.push(...$textNodes(value.slice(offset, skill.start)), $createSkillNode(skill))
    offset = skill.end
  }
  nodes.push(...$textNodes(value.slice(offset)))
  return nodes
}
function $setContent(value: string) { $getRoot().clear().append($createParagraphNode().append(...$contentNodes(value, true))) }
function $query(): Query | null {
  const selection = $getSelection()
  if (!$isRangeSelection(selection) || !selection.isCollapsed() || selection.anchor.type !== "text") return null
  const node = selection.anchor.getNode()
  if (!$isTextNode(node)) return null
  const offset = selection.anchor.offset, text = node.getTextContent()
  const match = /(?:^|[\s([{])\/([A-Za-z0-9_.:-]*)$/.exec(text.slice(0, offset))
  if (!match) return null
  const preceding = selection.clone()
  preceding.anchor.set($getRoot().getKey(), 0, "element")
  if (!skillQueryAllowed(preceding.getTextContent())) return null
  return { key: node.getKey(), start: offset - match[1].length - 1,
    end: offset + (/^[A-Za-z0-9_.:-]*/.exec(text.slice(offset))?.[0].length || 0), text: match[1] }
}

export const SkillComposer = React.forwardRef<HTMLDivElement, Props>(function SkillComposer(props, ref) {
  return <LexicalComposer initialConfig={{
    namespace: "moyai-composer", nodes: [SkillNode], editable: !props.disabled,
    onError: error => { throw error }, editorState: () => $setContent(props.value),
  }}><InlineComposer {...props} inputRef={ref} /><HistoryPlugin /></LexicalComposer>
})

function InlineComposer({ value, onChange, onSend, onPaste, placeholder, shared = false, disabled = false, sessionId, focusRequest = 0, inputRef }: Props & { inputRef: React.ForwardedRef<HTMLDivElement> }) {
  const [editor] = useLexicalComposerContext()
  const input = useRef<HTMLDivElement | null>(null), latest = useRef({ onChange, onSend })
  const bindInput = useCallback((node: HTMLDivElement | null) => {
    input.current = node
    if (typeof inputRef === "function") inputRef(node)
    else if (inputRef) inputRef.current = node
  }, [inputRef])
  latest.current = { onChange, onSend }
  const id = useId(), auth = csrfToken(), scope = `${auth}:${shared}:${sessionId || ""}`
  const [focused, setFocused] = useState(false), [dismissed, setDismissed] = useState(false)
  const [query, setQuery] = useState<Query | null>(null), [version, setVersion] = useState(0), [active, setActive] = useState(0)
  const [empty, setEmpty] = useState(!value)
  const emitted = useRef(value), loadedCatalog = useRef(""), appliedFocus = useRef(focusRequest)
  const [state, setState] = useState<{ scope: string; rows: ComposerSkill[]; loading: boolean; error: string }>({ scope: "", rows: [], loading: false, error: "" })
  const wantsPicker = focused && query !== null && !disabled

  useEffect(() => editor.registerUpdateListener(({ editorState, tags }) => {
    editorState.read(() => {
      const next = $getRoot().getTextContent()
      setEmpty(!next)
      const nextQuery = $query()
      setQuery(previous => previous?.key === nextQuery?.key && previous?.start === nextQuery?.start
        && previous?.end === nextQuery?.end && previous?.text === nextQuery?.text ? previous : nextQuery)
      if (!tags.has("external-value") && emitted.current !== next) {
        emitted.current = next; latest.current.onChange(next)
      }
    })
  }), [editor])
  useLayoutEffect(() => {
    const focus = focusRequest !== appliedFocus.current
    const changed = value !== emitted.current
    if (!changed && !focus) return
    emitted.current = value
    appliedFocus.current = focusRequest
    // Commit the starter and its caret together before another input event can
    // replace the selection. A later animation frame could interrupt typing.
    editor.update(() => {
      if (changed) $setContent(value)
      if (focus) $getRoot().selectEnd()
    }, { tag: focus ? ["external-value"] : ["external-value", SKIP_DOM_SELECTION_TAG], discrete: true })
  }, [editor, value, focusRequest])
  useEffect(() => { editor.setEditable(!disabled) }, [editor, disabled])
  useEffect(() => {
    if (!wantsPicker) return
    const cacheKey = `${scope}:${version}`
    if (loadedCatalog.current === cacheKey) return
    const controller = new AbortController()
    setState({ scope, rows: [], loading: true, error: "" })
    void (async () => {
      const rows = new Map<string, ComposerSkill>()
      let offset = 0
      do {
        const { data: page } = await listInvocableSkills({ query: { shared, offset, limit: 100, ...(sessionId ? { session_id: sessionId } : {}) }, signal: controller.signal })
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
  }, [scope, wantsPicker, version])
  useEffect(() => { setActive(0); setDismissed(false) }, [query?.key, query?.start, query?.text])
  useEffect(() => {
    const clear = () => { setState({ scope: "", rows: [], loading: false, error: "" }); setVersion(value => value + 1) }
    window.addEventListener("moyai:access-changed", clear)
    return () => window.removeEventListener("moyai:access-changed", clear)
  }, [])
  const rows = state.scope === scope ? state.rows : []
  const loading = state.scope !== scope || state.loading
  const matches = rows.filter(skill => `${skill.name} ${skill.reference} ${skill.description}`.toLowerCase().includes((query?.text || "").toLowerCase()))
    .sort((a, b) => Number(b.name === query?.text) - Number(a.name === query?.text) || a.name.localeCompare(b.name)).slice(0, 30)
  const selectedIndex = Math.min(active, Math.max(matches.length - 1, 0))
  const open = wantsPicker && !dismissed
  function pick(skill: ComposerSkill) {
    if (!query) return
    editor.update(() => {
      const node = $getNodeByKey(query.key)
      if (!$isTextNode(node)) return
      node.select(query.start, query.end)
      const selection = $getSelection()
      if ($isRangeSelection(selection)) selection.insertNodes([$createSkillNode(skill), $createTextNode(" ")])
    }, { tag: HISTORY_PUSH_TAG })
    setDismissed(true); editor.focus()
  }
  useEffect(() => editor.registerCommand(KEY_DOWN_COMMAND, event => {
    if (event.isComposing || editor.isComposing()) return false
    if (open && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
      event.preventDefault(); setActive(index => matches.length ? (index + (event.key === "ArrowDown" ? 1 : -1) + matches.length) % matches.length : 0); return true
    }
    if (open && event.key === "Escape") { event.preventDefault(); setDismissed(true); return true }
    if (open && !event.shiftKey && (event.key === "Enter" || event.key === "Tab") && matches.length) {
      event.preventDefault(); pick(matches[selectedIndex]); return true
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault()
      if (!open || (!loading && !state.error)) latest.current.onSend($getRoot().getTextContent())
      return true
    }
    return false
  }, COMMAND_PRIORITY_HIGH), [editor, open, matches, selectedIndex, query, loading, state.error])
  useEffect(() => {
    function copy(event: ClipboardEvent | KeyboardEvent | null, cut: boolean) {
      const selection = $getSelection()
      if (!event || !("clipboardData" in event) || !event.clipboardData || !selection) return false
      const text = selection.getTextContent()
      if (!skillMentions(text).length) return false
      event.preventDefault()
      event.clipboardData.setData("text/plain", skillDisplayText(text))
      event.clipboardData.setData(CLIPBOARD_TYPE, text)
      if (cut && $isRangeSelection(selection)) selection.removeText()
      else if (cut && $isNodeSelection(selection)) selection.getNodes().forEach(node => node.remove())
      return true
    }
    return mergeRegister(
      editor.registerCommand(COPY_COMMAND, event => copy(event, false), COMMAND_PRIORITY_HIGH),
      editor.registerCommand(CUT_COMMAND, event => copy(event, true), COMMAND_PRIORITY_HIGH),
      editor.registerCommand(PASTE_COMMAND, event => {
        if (!("clipboardData" in event) || !event.clipboardData) return false
        const text = event.clipboardData.getData(CLIPBOARD_TYPE) || event.clipboardData.getData("text/plain")
        const selection = $getSelection()
        if (!$isRangeSelection(selection) || !skillMentions(text).length) return false
        event.preventDefault(); selection.insertNodes($contentNodes(text)); return true
      }, COMMAND_PRIORITY_HIGH),
    )
  }, [editor])
  useEffect(() => {
    if (open) document.getElementById(`${id}-${selectedIndex}`)?.scrollIntoView({ block: "nearest" })
  }, [open, selectedIndex, id])
  return <div className="skill-composer">
    <Popover open={open} onOpenChange={next => { if (!next) setDismissed(true) }}>
      <PopoverAnchor asChild>
        <div className="skill-editor-wrap">
          <PlainTextPlugin ErrorBoundary={LexicalErrorBoundary} contentEditable={
            <ContentEditable ref={bindInput}
              className="skill-editor" aria-label="Message Moyai" aria-multiline="true" aria-disabled={disabled}
              aria-controls={open ? id : undefined} aria-expanded={open} aria-autocomplete="list"
              aria-describedby={open ? `${id}-help` : undefined}
              aria-activedescendant={open && matches.length ? `${id}-${selectedIndex}` : undefined}
              onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
              onPasteCapture={event => { onPaste?.(event); if (event.defaultPrevented) event.stopPropagation() }} />
          } />
          {empty && <span className="skill-editor-placeholder" aria-hidden="true">{placeholder}</span>}
        </div>
      </PopoverAnchor>
      <PopoverContent className="skill-picker" side="top" align="start" sideOffset={8}
        onOpenAutoFocus={event => event.preventDefault()} onCloseAutoFocus={event => event.preventDefault()}
        onInteractOutside={event => { if (event.target instanceof Node && input.current?.contains(event.target)) event.preventDefault() }}>
        <div className="skill-picker-heading">Skills</div>
        <span className="visually-hidden" id={`${id}-help`}>Use the arrow keys to browse skills, Enter to select, or Escape to close.</span>
        {loading ? <p role="status">Loading skills…</p> : state.error ? <div role="alert"><p>Couldn’t load your skills.</p><Button type="button" variant="outline" size="sm" onMouseDown={event => event.preventDefault()} onClick={() => setVersion(value => value + 1)}>Retry skills</Button></div>
          : matches.length ? <div id={id} role="listbox" aria-label="Skills" className="skill-options">{matches.map((skill, index) =>
            <button type="button" role="option" aria-selected={selectedIndex === index} tabIndex={-1}
              id={`${id}-${index}`} key={skill.reference} className="skill-option"
              title={`${skillDisplayName(skill.name)} · ${skillScopeName(skill.scope)}\n${skill.description}`}
              onMouseDown={event => event.preventDefault()} onClick={() => pick(skill)}>
              <span className="skill-option-icon"><SkillIcon /></span>
              <span className="skill-option-copy"><span className="skill-option-title">{skillDisplayName(skill.name)}</span><span className="skill-option-description">{skill.description}</span></span>
              <span className="skill-option-scope">{skillScopeName(skill.scope)}</span>
            </button>)}</div>
          : <p role="status">No matching skills{shared ? " available in shared sessions" : ""}. <a href="#settings/skills">View skills</a></p>}
      </PopoverContent>
    </Popover>
  </div>
}
