import React from "react"
import { createRoot, type Root } from "react-dom/client"
import { flushSync } from "react-dom"
import parse, { attributesToProps, domToReact, Element, type DOMNode, type HTMLReactParserOptions } from "html-react-parser"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { NativeSelect, NativeSelectOption, NativeSelectOptGroup } from "@/components/ui/native-select"
import { Label } from "@/components/ui/label"
import { Badge } from "@/components/ui/badge"
import { Table, TableHeader, TableBody, TableFooter, TableRow, TableHead, TableCell, TableCaption } from "@/components/ui/table"
import { Separator } from "@/components/ui/separator"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Card } from "@/components/ui/card"
import { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider } from "@/components/ui/tooltip"
import { FormCheckbox } from "./form-checkbox"
import { FormSelect } from "./form-select"
import { Collapsible, CollapsibleTrigger, CollapsibleContent } from "@/components/ui/collapsible"

type MountedRegion = { root: Root; nodes: ChildNode[]; connected: boolean }
type RenderOptions = { preserve?: Iterable<HTMLElement> }
const regions = new Map<HTMLElement, MountedRegion>()

// Controllers replace regions rather than reconcile them. Dispose nested React
// roots first, including regions in streamed messages and transient panels.
export function dispose(host: HTMLElement, preserved: readonly HTMLElement[] = []) {
  for (const [node, region] of [...regions].reverse()) {
    if (!(node instanceof HTMLElement) || (node !== host && !host.contains(node))) continue
    if (preserved.some(owner => owner === node || owner.contains(node))) {
      // A caller will reattach these independently mounted subtrees this turn.
      region.connected ||= node.isConnected
      continue
    }
    // Controllers may have replaced a top-level loading node or moved a panel.
    // Restore React's direct children before asking it to release their effects.
    for (const child of region.nodes) if (child.parentNode !== node) node.appendChild(child)
    flushSync(() => region.root.unmount())
    regions.delete(node)
  }
}

function hasClass(node: Element, names: string[]) {
  return (node.attribs.class || "").split(/\s+/).some(name => names.includes(name))
}

function Disclosure({ node }: { node: Element }) {
  const [open, setOpen] = React.useState("open" in node.attribs)
  const props = attributesToProps(node.attribs)
  const summary = node.children.find(child => child instanceof Element && child.name === "summary") as Element | undefined
  if (!summary) return <details {...props}>{domToReact(node.children as DOMNode[], options)}</details>
  return <Collapsible open={open} onOpenChange={setOpen} asChild>
    <details {...props} open={open} onToggle={event => setOpen(event.currentTarget.open)}>
      <CollapsibleTrigger asChild>
        <summary {...attributesToProps(summary.attribs)} onClick={event => { event.preventDefault(); setOpen(current => !current) }}>
          {domToReact(summary.children as DOMNode[], options)}
        </summary>
      </CollapsibleTrigger>
      <CollapsibleContent forceMount style={{ display: "contents" }}>
        {domToReact(node.children.filter(child => child !== summary) as DOMNode[], options)}
      </CollapsibleContent>
    </details>
  </Collapsible>
}

const options: HTMLReactParserOptions = {
  replace(node) {
    if (!(node instanceof Element)) return
    const props = attributesToProps(node.attribs, node.name)
    // Event attributes are never application handlers. API/Markdown content is
    // escaped/sanitized upstream; React must not turn strings into executable JS.
    for (const name of Object.keys(props)) if (/^on/i.test(name)) delete props[name]
    // Templates seed defaults; controllers own subsequent edits. Keep this
    // explicit rather than relying on the parser's controlled-prop conversion.
    if (["input", "textarea", "select"].includes(node.name)) {
      delete props.value
      delete props.checked
      delete props.defaultValue
      delete props.defaultChecked
    }
    const children = () => domToReact(node.children as DOMNode[], options)
    switch (node.name) {
      case "script": return <></>
      case "details": return <Disclosure node={node} />
      case "button": {
        const variant = hasClass(node, ["danger"]) ? "destructive"
          : hasClass(node, ["quiet", "icon-button", "nav-button", "new-task", "session-link", "session-move", "agent-disclosure", "folder-toggle", "folder-menu", "folder-add", "panel-icon", "header-icon", "dialog-close", "session-section-heading", "settings-back"]) ? "ghost"
          : hasClass(node, ["primary", "send-button"]) || node.attribs.type === "submit" ? "default" : "outline"
        const title = node.attribs.title
        const button = <Button {...props} title={undefined} variant={variant}>{children()}</Button>
        return title ? <Tooltip><TooltipTrigger asChild>{button}</TooltipTrigger><TooltipContent sideOffset={6}>{title}</TooltipContent></Tooltip> : button
      }
      case "input": {
        const type = (node.attribs.type || "text").toLowerCase()
        const value = node.attribs.value
        const defaultChecked = "checked" in node.attribs
        if (type === "checkbox") return <FormCheckbox {...props} value={value} defaultChecked={defaultChecked} />
        // A choice's value is its submitted identity, not its checked state.
        if (type === "radio") return <Input {...props} value={value} defaultChecked={defaultChecked} />
        if (type === "file") return <Input {...props} />
        if (["button", "submit", "reset", "image"].includes(type)) return <Input {...props} value={value} />
        return <Input {...props} defaultValue={value} />
      }
      case "textarea": return <Textarea {...props} defaultValue={node.children.map(child => child.type === "text" ? child.data : "").join("")} />
      case "select": {
        const selected = (node.children as Element[]).flatMap(child => child.name === "optgroup" ? child.children as Element[] : [child])
          .filter(child => child.attribs && "selected" in child.attribs).map(child => child.attribs.value ?? (child.children[0]?.type === "text" ? child.children[0].data : ""))
        const Component = props.multiple || Number(node.attribs.size) > 1 ? NativeSelect : FormSelect
        return <Component {...props} defaultValue={props.multiple ? selected : selected[0]}>{children()}</Component>
      }
      case "option": {
        delete props.selected
        return <NativeSelectOption {...props}>{children()}</NativeSelectOption>
      }
      case "optgroup": return <NativeSelectOptGroup {...props}>{children()}</NativeSelectOptGroup>
      case "label": return <Label {...props}>{children()}</Label>
      case "table": return <Table {...props}>{children()}</Table>
      case "thead": return <TableHeader {...props}>{children()}</TableHeader>
      case "tbody": return <TableBody {...props}>{children()}</TableBody>
      case "tfoot": return <TableFooter {...props}>{children()}</TableFooter>
      case "tr": return <TableRow {...props}>{children()}</TableRow>
      case "th": return <TableHead {...props}>{children()}</TableHead>
      case "td": return <TableCell {...props}>{children()}</TableCell>
      case "caption": return <TableCaption {...props}>{children()}</TableCaption>
      case "hr": return <Separator {...props} />
      case "span":
        if (hasClass(node, ["status", "badge", "settings-badge", "scope-badge", "secret-scope", "skill-scope", "memory-kind", "connection-state", "environment-status"])) return <Badge {...props} variant="secondary">{children()}</Badge>
        break
      case "article":
        if (hasClass(node, ["automation-card", "environment-card", "memory-card", "skill-card", "connection-card"])) return <Card {...props} className={`${props.className || ""} block gap-0`}>{children()}</Card>
        break
      case "div":
        if (node.attribs.id === "toast") return <Alert {...props} className="block">{children()}</Alert>
        if (node.attribs.role === "alert") return <Alert {...props}><AlertDescription>{children()}</AlertDescription></Alert>
    }
  },
}

export function render(host: HTMLElement, html: string, { preserve = [] }: RenderOptions = {}) {
  const select = host instanceof HTMLSelectElement ? host : null
  let selected: string[] = []
  if (select) {
    const template = document.createElement("template")
    template.innerHTML = html
    selected = [...template.content.querySelectorAll("option[selected]")].map(option => (option as HTMLOptionElement).value)
  }
  dispose(host, [...preserve].filter(node => node !== host && host.contains(node)))
  host.replaceChildren()
  if (!html) return
  const root = createRoot(host)
  flushSync(() => root.render(<TooltipProvider delayDuration={500}>{parse(String(html), options)}</TooltipProvider>))
  regions.set(host, { root, nodes: [...host.childNodes], connected: host.isConnected })
  if (select && selected.length) {
    for (const option of select.options) option.selected = selected.includes(option.value)
  }
}

export function insert(host: HTMLElement, position: InsertPosition, html: string) {
  // A display:contents region owns only this insertion. Sibling handlers, focus,
  // drafts and streamed message nodes remain untouched.
  const region = document.createElement("moyai-region")
  region.style.display = "contents"
  host.insertAdjacentElement(position, region)
  render(region, html)
}

export function replace(host: HTMLElement, html: string) {
  insert(host, "beforebegin", html)
  dispose(host)
  host.remove()
}

export function createElement(tag: string, doc = document): HTMLElement {
  const host = doc.createElement("moyai-region")
  render(host, `<${tag}></${tag}>`)
  const element = host.firstElementChild as HTMLElement
  // The detached region remains the owner; disposal restores its direct child.
  element.dataset.uiCreated = "true"
  return element
}

export function observeRemovals() {
  const observer = new MutationObserver(() => {
    for (const [node, region] of [...regions]) {
      if (!(node instanceof HTMLElement)) continue
      if (node.isConnected || region.nodes.some(child => child.isConnected)) region.connected = true
      else if (region.connected) dispose(node)
    }
  })
  observer.observe(document.body, { childList: true, subtree: true })
}
