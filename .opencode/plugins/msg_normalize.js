/**
 * msg_normalize.js — OpenCode chat-message normalizer
 *
 * Part of the dynamic_model_loader project. This plugin sits between
 * opencode's chat pipeline and the model, cleaning up message arrays
 * before they are sent.
 *
 * Why it exists:
 *   When the config watcher reloads a model mid-session (e.g. to fix a
 *   drifted load config), opencode can be left with stale or malformed
 *   messages in its context — empty assistant turns, or consecutive user
 *   messages that should have been merged. Some models reject or
 *   misinterpret these, producing degraded output or errors.
 *
 * What it does:
 *   1. Removes assistant messages that contain no text or tool parts.
 *   2. Merges consecutive user messages into a single message.
 *   3. Appends metadata-only log entries (role, part count, tools) to
 *      <tmpdir>/opencode/msg_normalize.log — no message content is logged.
 *
 * This is a workaround for opencode not normalizing its own message
 * array after external model lifecycle events. It should be removed
 * once opencode handles this natively.
 *
 * Location: .opencode/plugins/ (project-level, auto-loaded by opencode)
 */

const { join } = await import("node:path")
const { tmpdir } = await import("node:os")
const LOG = join(tmpdir(), "opencode", "msg_normalize.log")

export default async function () {
  return {
    "experimental.chat.messages.transform": async (_input, output) => {
      const msgs = output.messages
      if (!Array.isArray(msgs)) return

      const role = (m) => m && m.info && m.info.role
      const hasText = (p) => p && p.type === "text" && p.text
      const hasTool = (p) => p && p.type === "tool"
      const describe = (m) => {
        const parts = Array.isArray(m.parts) ? m.parts : []
        return {
          role: role(m),
          n: parts.length,
          hasContent: parts.some((p) => hasText(p) || hasTool(p)),
          tools: parts.filter((p) => hasTool(p)).length,
        }
      }

      const before = msgs.map(describe)

      const next = []
      let changed = false

      for (const m of msgs) {
        const r = role(m)
        const parts = Array.isArray(m.parts) ? m.parts : []
        const hasContent = parts.some((p) => hasText(p) || hasTool(p))

        if (r === "assistant" && !hasContent) {
          changed = true
          continue
        }

        const last = next[next.length - 1]
        if (r === "user" && role(last) === "user") {
          last.parts.push(...parts)
          changed = true
          continue
        }

        next.push(m)
      }

      if (changed) {
        msgs.splice(0, msgs.length, ...next)
      }

      try {
        const { appendFileSync } = await import("node:fs")
        appendFileSync(
          LOG,
          JSON.stringify({
            t: Date.now(),
            changed,
            beforeCount: before.length,
            afterCount: msgs.length,
            rolesBefore: before.map((b) => b.role),
            rolesAfter: msgs.map(role),
            before,
            after: msgs.map(describe),
          }) + "\n",
        )
      } catch {}
    },
  }
}
