import React, { useId, useLayoutEffect, useRef, useState } from "react"
import { flushSync } from "react-dom"
import { NativeSelect } from "@/components/ui/native-select"
import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectTrigger, SelectValue } from "@/components/ui/select"

type Props = React.ComponentProps<typeof NativeSelect>
type Choice = { value: string; text: string; disabled: boolean; group: string }
type Snapshot = { index: number; disabled: boolean; required: boolean; label: string; choices: Choice[] }

// Keep the native element as the controller/form contract and sizing reference.
// Only the shadcn trigger is exposed to pointer, keyboard and assistive technology.
export function FormSelect(props: Props) {
  const source = useRef<HTMLSelectElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const control = useRef<HTMLSpanElement>(null)
  const errorId = useId()
  const [snapshot, setSnapshot] = useState<Snapshot>({ index: -1, disabled: !!props.disabled, required: !!props.required, label: "", choices: [] })
  const [error, setError] = useState("")
  const [open, setOpen] = useState(false)

  useLayoutEffect(() => {
    const node = source.current!
    const sync = () => {
      const choices = [...node.options].map(option => ({
        value: option.value, text: option.label,
        disabled: option.disabled || (option.parentElement instanceof HTMLOptGroupElement && option.parentElement.disabled),
        group: option.parentElement instanceof HTMLOptGroupElement ? option.parentElement.label : "",
      }))
      const labels = new Set(node.labels || [])
      const enclosing = node.closest("label")
      if (enclosing) labels.add(enclosing)
      const label = [...labels].map(label => {
        const copy = label.cloneNode(true) as HTMLElement
        copy.querySelectorAll('[data-slot="native-select-wrapper"],svg').forEach(child => child.remove())
        return copy.textContent?.trim()
      }).filter(Boolean).join(" ")
      setSnapshot({ index: node.selectedIndex, disabled: node.disabled, required: node.required, label, choices })
      if (node.validity.valid) setError("")
    }
    // Controllers assign values directly, including rollback, without events.
    const properties = ["value", "selectedIndex"] as const
    for (const key of properties) {
      const descriptor = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, key)!
      Object.defineProperty(node, key, { configurable: true, get: () => descriptor.get!.call(node), set: value => { descriptor.set!.call(node, value); sync() } })
    }
    const observer = new MutationObserver(sync)
    observer.observe(node, { attributes: true, childList: true, subtree: true, characterData: true })
    const focus = () => trigger.current?.focus()
    const invalid = (event: Event) => { event.preventDefault(); setError(node.validationMessage); focus() }
    const reset = () => queueMicrotask(sync)
    node.addEventListener("input", sync)
    node.addEventListener("change", sync)
    node.addEventListener("focus", focus)
    node.addEventListener("invalid", invalid)
    const form = node.form
    form?.addEventListener("reset", reset)
    // Radix's unnamed autofill input must not emit a second form change.
    const stop = (event: Event) => event.stopPropagation()
    const controls = control.current!
    controls.addEventListener("input", stop)
    controls.addEventListener("change", stop)
    sync()
    return () => {
      observer.disconnect()
      for (const key of properties) delete (node as unknown as Record<string, unknown>)[key]
      node.removeEventListener("input", sync)
      node.removeEventListener("change", sync)
      node.removeEventListener("focus", focus)
      node.removeEventListener("invalid", invalid)
      form?.removeEventListener("reset", reset)
      controls.removeEventListener("input", stop)
      controls.removeEventListener("change", stop)
    }
  }, [])

  // Radix reserves the empty string for its placeholder. Index keys also handle
  // native empty-valued filter choices and duplicate option values faithfully.
  const selected = snapshot.index
  const change = (key: string) => {
    const node = source.current!
    if (!/^\d+$/.test(key) || node.selectedIndex === Number(key)) return
    const choice = snapshot.choices[Number(key)]
    if (!choice || choice.disabled || node.disabled) return
    flushSync(() => { node.selectedIndex = Number(key); setError("") })
    node.dispatchEvent(new Event("input", { bubbles: true }))
    node.dispatchEvent(new Event("change", { bubbles: true }))
  }
  const items = snapshot.choices.map((choice, index) => <SelectItem key={index} value={String(index)} disabled={choice.disabled}>{choice.text}</SelectItem>)
  const groups: React.ReactNode[] = []
  for (let index = 0; index < items.length;) {
    const group = snapshot.choices[index].group
    let end = index + 1
    while (group && end < items.length && snapshot.choices[end].group === group) end++
    groups.push(group ? <SelectGroup key={index}><SelectLabel>{group}</SelectLabel>{items.slice(index, end)}</SelectGroup> : items[index])
    index = end
  }

  return <><NativeSelect {...props} ref={source} aria-hidden="true" tabIndex={-1} control={
    <span ref={control} style={{ display: "contents" }} onKeyDownCapture={event => {
      if (open && event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setOpen(false) }
    }}>
    <Select value={selected < 0 ? "" : String(selected)} disabled={snapshot.disabled} open={open && !snapshot.disabled} onOpenChange={setOpen} onValueChange={change}>
      <SelectTrigger ref={trigger} aria-label={props["aria-label"] || snapshot.label || props.title}
        aria-labelledby={props["aria-labelledby"]} aria-description={props["aria-description"]}
        aria-describedby={[props["aria-describedby"], error ? errorId : ""].filter(Boolean).join(" ") || undefined}
        aria-required={snapshot.required || undefined} aria-invalid={error ? true : props["aria-invalid"]}>
        <SelectValue>{snapshot.choices[selected]?.text || "Choose an option"}</SelectValue>
      </SelectTrigger>
      <SelectContent aria-label={props["aria-label"] || snapshot.label || props.title}>{groups}</SelectContent>
    </Select>
    </span>
  } />
  {error && <span id={errorId} data-slot="select-error" role="alert">{error}</span>}
  </>
}
