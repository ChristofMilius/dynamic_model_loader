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
