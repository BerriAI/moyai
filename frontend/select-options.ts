import { Element, type DOMNode } from "html-react-parser"

// Native options are controller-owned, never children of a second React root.
export function selectOptions(nodes: DOMNode[], doc = document): DocumentFragment {
  const fragment = doc.createDocumentFragment()
  for (const node of nodes) {
    if (node.type === "text") {
      fragment.append(doc.createTextNode(node.data))
    } else if (node instanceof Element && !["script", "style", "template"].includes(node.name)) {
      const children = selectOptions(node.children as DOMNode[], doc)
      if (node.name !== "option" && node.name !== "optgroup") {
        fragment.append(children)
        continue
      }
      const element = doc.createElement(node.name)
      for (const [name, value] of Object.entries(node.attribs)) {
        if (!/^on/i.test(name)) element.setAttribute(name, value)
      }
      element.dataset.slot = `native-select-${node.name}`
      element.append(children)
      fragment.append(element)
    }
  }
  return fragment
}
