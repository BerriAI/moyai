import React, { useId, useRef } from "react"
import { Check, Pencil } from "lucide-react"
import { Checkbox as CheckboxPrimitive } from "radix-ui"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import "./question-choices.css"

export type Question = {
  header: string
  question: string
  multi_select?: boolean
  options: { label: string; description?: string }[]
}

type Props = {
  question: Question
  selected: string[]
  customAnswer: string
  disabled: boolean
  onSelect: (values: string[]) => void
  onCustomAnswer: (value: string) => void
}

export function QuestionChoices({ question, selected, customAnswer, disabled, onSelect, onCustomAnswer }: Props) {
  const id = useId()
  const buttons = useRef<(HTMLButtonElement | null)[]>([])
  const active = customAnswer.trim() ? [] : selected
  function choose(label: string) {
    if (disabled) return
    onSelect(question.multi_select
      ? active.includes(label) ? active.filter(value => value !== label) : [...active, label]
      : [label])
  }
  function shortcut(event: React.KeyboardEvent<HTMLFieldSetElement>) {
    if (disabled || event.altKey || event.ctrlKey || event.metaKey || event.nativeEvent.isComposing) return
    if ((event.target as HTMLElement).closest("input, textarea, [contenteditable=true]")) return
    const row = (event.target as HTMLElement).closest<HTMLButtonElement>(".question-choice-row")
    if (!row) return
    if (question.multi_select && event.key === "Enter") {
      event.preventDefault()
      event.currentTarget.form?.requestSubmit()
      return
    }
    if (!question.multi_select && ["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault()
      const index = buttons.current.indexOf(row)
      const count = question.options.length
      const next = event.key === "Home" ? 0 : event.key === "End" ? count - 1
        : (index + (event.key === "ArrowDown" ? 1 : -1) + count) % count
      buttons.current[next]?.focus()
      return
    }
    const index = /^[1-9]$/.test(event.key) ? Number(event.key) - 1 : -1
    if (index < 0 || index >= question.options.length) return
    event.preventDefault()
    buttons.current[index]?.focus()
    choose(question.options[index].label)
  }
  const options = question.options.map((option, index) => {
    const description = option.description ? `${id}-description-${index}` : undefined
    const content = <>
      <span className="question-choice-number" aria-hidden="true">{index + 1}</span>
      <span className="question-choice-copy">
        <span>{option.label}</span>
        {option.description && <small id={description}>{option.description}</small>}
      </span>
    </>
    const button = <Button key={option.label}
      type="button" variant="ghost" className="question-choice-row"
      ref={element => { buttons.current[index] = element }}
      aria-label={option.label} aria-describedby={description} aria-keyshortcuts={String(index + 1)}
      aria-pressed={question.multi_select ? undefined : active.includes(option.label)}
      disabled={disabled}
      data-state={active.includes(option.label) ? "checked" : "unchecked"}
      onClick={question.multi_select ? undefined : () => choose(option.label)}
    >
      {content}
      {question.multi_select
        ? <CheckboxPrimitive.Indicator className="question-choice-check"><Check aria-hidden="true" /></CheckboxPrimitive.Indicator>
        : active.includes(option.label) && <span className="question-choice-check"><Check aria-hidden="true" /></span>}
    </Button>
    return question.multi_select
      ? <CheckboxPrimitive.Root key={option.label} asChild checked={active.includes(option.label)} disabled={disabled} onCheckedChange={() => choose(option.label)}>{button}</CheckboxPrimitive.Root>
      : button
  })
  return <fieldset className="question-choice-field" disabled={disabled} onKeyDown={shortcut}>
    <legend id={`${id}-question`}>{question.question}</legend>
    {question.multi_select && <p className="question-choice-hint">Choose any that apply, then press Enter.</p>}
    <div className="question-choice-list">{options}</div>
    <div className="question-custom-answer">
      <Pencil size={14} aria-hidden="true" />
      <Textarea
        className="question-custom-input" rows={1} disabled={disabled}
        aria-label={`Custom answer for ${question.header}`} placeholder="Or write your own response…"
        value={customAnswer}
        onChange={event => onCustomAnswer(event.target.value)}
        onKeyDown={event => {
          if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault()
            event.stopPropagation()
            event.currentTarget.form?.requestSubmit()
          }
        }}
      />
    </div>
  </fieldset>
}
