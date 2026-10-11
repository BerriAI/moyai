import React, { useLayoutEffect, useRef, useState } from "react"
import { flushSync } from "react-dom"
import { Checkbox } from "@/components/ui/checkbox"
import { Switch } from "@/components/ui/switch"

type Props = React.ComponentProps<"input">

// Expose the existing controllers' checked/change contract on the shadcn control.
// Radix supplies the hidden checkbox for native FormData and form reset behavior.
export function FormCheckbox({ defaultChecked, checked, type: _type, role, ...props }: Props) {
  const [value, setValue] = useState(Boolean(defaultChecked ?? checked))
  const ref = useRef<HTMLButtonElement>(null)
  const current = useRef(value)
  current.current = value
  useLayoutEffect(() => {
    const node = ref.current!
    Object.defineProperty(node, "checked", { configurable: true, get: () => current.current, set: next => { current.current = Boolean(next); setValue(Boolean(next)) } })
    const reset = () => setValue(Boolean(defaultChecked ?? checked))
    node.form?.addEventListener("reset", reset)
    return () => node.form?.removeEventListener("reset", reset)
  }, [checked, defaultChecked])
  const change = (next: boolean | "indeterminate") => {
    current.current = next === true
    flushSync(() => setValue(current.current))
    ref.current?.dispatchEvent(new Event("input", { bubbles: true }))
    ref.current?.dispatchEvent(new Event("change", { bubbles: true }))
  }
  const { onChange: _onChange, onInput: _onInput, size: _size, ...buttonProps } = props
  const shared = buttonProps as React.ComponentProps<typeof Checkbox>
  return role === "switch"
    ? <Switch {...buttonProps as React.ComponentProps<typeof Switch>} ref={ref} checked={value} onCheckedChange={change} />
    : <Checkbox {...shared} ref={ref} checked={value} onCheckedChange={change} />
}
