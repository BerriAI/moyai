const test = require("node:test")
const assert = require("node:assert/strict")
const path = require("node:path")
const { root, web, config, shared, inside, sources, imports } = require("./fixtures/frontend-imports.cjs")

// Existing cross-feature interfaces are deliberate: renderers share the same
// session state and file references, without importing application composition.
const features = {
  workspace: {
    packages: ["react", "react-dom", "react-markdown", "remark-gfm", "lucide-react", "radix-ui", "@novnc/novnc"],
    modules: ["api.ts", "file-links.ts", "polling.ts", "conversations/session-list.ts", "messages/code-block.tsx"],
  },
  navigation: {
    packages: ["react", "react-dom", "lucide-react"],
    modules: ["api.ts", "debug-trace.ts", "conversations/session-list.ts", "conversations/session-updates.ts",
      "workspace/review-labels.ts", "assets/slack-logo.svg"],
  },
  messages: {
    packages: ["react", "react-dom", "react-markdown", "remark-gfm", "lucide-react", "highlight.js", "html-react-parser",
      "lexical", "@lexical/react", "@lexical/plain-text", "@lexical/history", "@lexical/utils",
      "micromark-util-decode-string", "radix-ui", "mdast"],
    modules: ["api.ts", "debug-trace.ts", "attachment-files.ts", "attachment-uploads.ts",
      "conversations/conversation-state.ts", "conversations/live-transcript.ts", "conversations/work-timeline.ts",
      "workspace/workspace.tsx", "workspace/workspace-types.ts", "settings/settings.css"],
  },
}

for (const [name, rule] of Object.entries(features)) {
  const feature = path.join(web, name)
  const allowed = new Set(rule.modules.map(file => path.join(web, file)))
  test(`${name} sources and declarations are included in typechecking`, () => {
    assert.deepEqual(config.errors, [])
    for (const file of sources(feature).filter(file => /\.tsx?$/.test(file))) {
      assert.ok(config.fileNames.includes(file), `${file} is missing from tsconfig`)
    }
  })
  test(`${name} imports only its documented interfaces`, () => {
    for (const file of sources(feature)) for (const dependency of imports(file)) {
      assert.ok(inside(dependency.file, feature) || allowed.has(dependency.file)
        || inside(dependency.file, path.join(web, "generated"))
        || inside(dependency.file, path.join(root, "frontend/components/ui"))
        || dependency.file === path.join(root, "frontend/lib/utils.ts")
        || rule.packages.some(pkg => dependency.specifier === pkg || dependency.specifier.startsWith(pkg + "/")),
      `${path.relative(root, file)} imports ${dependency.specifier}; keep feature dependencies explicit`)
    }
  })
  test(`shared transport and controls never load ${name}`, () => {
    for (const file of [...shared, ...sources(path.join(web, "generated")),
      ...sources(path.join(root, "frontend/components/ui")), ...sources(path.join(root, "frontend/lib"))]) for (const dependency of imports(file)) {
      assert.ok(!inside(dependency.file, feature), `${path.relative(root, file)} imports ${name}`)
    }
  })
}
