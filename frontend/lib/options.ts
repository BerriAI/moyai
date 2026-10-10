// FormSelect takes native <option> nodes; build them from typed choices.
export function optionFragment(choices: ReadonlyArray<readonly [value: string, label: string]>, selected?: string) {
  const fragment = document.createDocumentFragment()
  for (const [value, label] of choices) fragment.append(new Option(label, value, value === selected, value === selected))
  return fragment
}
