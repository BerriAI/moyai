import React, { useId, useRef } from "react"
import { Check, Pencil } from "lucide-react"
import { Checkbox as CheckboxPrimitive, RadioGroup } from "radix-ui"
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
    onCustomAnswer("")
    onSelect(question.multi_select
      ? active.includes(label) ? active.filter(value => value !== label) : [...active, label]
      : [label])
  }
  function shortcut(event: React.KeyboardEvent<HTMLFieldSetElement>) {
    if (disabled || event.altKey || event.ctrlKey || event.metaKey || event.nativeEvent.isComposing) return
    if ((event.target as HTMLElement).closest("input, textarea, [contenteditable=true]")) return
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
    const button = <Button
      type="button" variant="ghost" className="question-choice-row"
      ref={element => { buttons.current[index] = element }}
      aria-label={option.label} aria-describedby={description} aria-keyshortcuts={String(index + 1)}
    >
      {content}
      {question.multi_select
        ? <CheckboxPrimitive.Indicator className="question-choice-check"><Check aria-hidden="true" /></CheckboxPrimitive.Indicator>
        : <RadioGroup.Indicator className="question-choice-check"><Check aria-hidden="true" /></RadioGroup.Indicator>}
    </Button>
    return question.multi_select
      ? <CheckboxPrimitive.Root key={option.label} asChild checked={active.includes(option.label)} disabled={disabled} onCheckedChange={() => choose(option.label)}>{button}</CheckboxPrimitive.Root>
      : <RadioGroup.Item key={option.label} asChild value={option.label}>{button}</RadioGroup.Item>
  })
  return <fieldset className="question-choice-field" disabled={disabled} onKeyDown={shortcut}>
    <legend id={`${id}-question`}>{question.question}</legend>
    {question.multi_select && <p className="question-choice-hint">Choose any that apply.</p>}
    {question.multi_select
      ? <div className="question-choice-list">{options}</div>
      : <RadioGroup.Root className="question-choice-list" aria-labelledby={`${id}-question`} orientation="vertical"
        value={active[0] ?? ""} disabled={disabled} onValueChange={choose}>{options}</RadioGroup.Root>}
    <div className="question-custom-answer">
      <Pencil size={18} aria-hidden="true" />
      <Textarea
        className="question-custom-input" rows={1} disabled={disabled}
        aria-label={`Custom answer for ${question.header}`} placeholder="Or write your own response…"
        value={customAnswer}
        onChange={event => {
          onCustomAnswer(event.target.value)
          if (event.target.value.trim()) onSelect([])
        }}
        onKeyDown={event => {
          if (event.key === "Enter" && (event.metaKey || event.ctrlKey) && !event.nativeEvent.isComposing) {
            event.preventDefault()
            event.currentTarget.form?.requestSubmit()
          }
        }}
      />
    </div>
  </fieldset>
}
