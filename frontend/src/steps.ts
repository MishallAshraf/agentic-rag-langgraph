import type { StatusEvent } from './api'

const SEARCH_TOOLS = new Set(['search_knowledge', 'search_course_material', 'search_documents'])

/** A status event as a short sentence for the "steps" list, or null to hide it. */
export function describeStep(e: StatusEvent): string | null {
  const args = (e.args ?? {}) as Record<string, unknown>
  switch (e.stage) {
    case 'received':
      return 'Message received'
    case 'guard':
      return e.result === 'ok' ? 'Safety check passed' : 'Safety check: answered with a fixed reply'
    case 'compact':
      return 'Summarised older messages to save space'
    case 'tool_call':
      if (SEARCH_TOOLS.has(String(e.name))) return `Searching the documents: "${args.query ?? args.question}"`
      if (e.name === 'read_page') return `Reading ${args.source}, page ${args.page}`
      if (e.name === 'save_note') return 'Asking to save a note'
      if (e.name === 'list_notes') return 'Listing saved notes'
      return `Using ${e.name}`
    case 'tool_result':
      return SEARCH_TOOLS.has(String(e.name)) ? 'Found relevant passages' : `${e.name} finished`
    case 'fact_check':
      return e.result === 'supported'
        ? 'Checked the answer against the sources'
        : 'The answer failed the fact check; writing it again'
    case 'output_guard':
      return 'Reply replaced by the output check'
    // Step 14: the research team
    case 'plan': {
      const tasks = (e.tasks ?? []) as { question: string; source: string }[]
      return `Planned ${tasks.length} research ${tasks.length === 1 ? 'task' : 'tasks'}: ` +
        tasks.map((t) => `"${t.question}" (${t.source})`).join('; ')
    }
    case 'research':
      return `${e.found ? 'Found' : 'Found nothing'} in ${e.source}: "${e.question}"`
    case 'review':
      return e.result === 'approved' ? 'Reviewer approved the answer'
        : e.result === 'more_research' ? 'Reviewer found gaps; researching them'
        : 'Reviewer found unsupported claims; rewriting'
    default:
      return null
  }
}
