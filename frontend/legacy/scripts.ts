/// <reference types="vite/client" />
// Classic scripts share globals, so Vite only fingerprints them; they still load in order.
const urls: Record<string, string> = import.meta.glob(["../../app/static/*.js", "../../app/static/vendor/*.js"], { query: "?url", import: "default", eager: true })

// Ordered compatibility controllers; removed as each feature moves to React.
export const controllerScripts = [
  "space-field", "icons", "vendor/marked", "vendor/purify", "goal-status", "provider-logos", "computer", "pull-request",
  "workspace-panel", "automation-triggers", "automations", "automation-builder", "settings", "analytics", "adoption",
  "environments", "users", "skill-icons", "skill-editor", "saved-files", "markdown", "infrastructure", "spend-prs",
  "spend-analytics", "spend", "credentials", "skills", "message-history", "skill-composer", "attachment-transport",
  "audio-recorder", "attachments", "activity", "message-queue", "session-folders", "sandbox-settings", "session-actions",
  "regions", "command-palette", "navigation-cache", "app",
].map(name => {
  const url = urls[`../../app/static/${name}.js`]
  if (!url) throw new Error(`Missing workspace controller: ${name}`)
  return url
})
