export interface KnowledgeCitation {
  label: string
  chunk_id: string
  document_id: string
  document_title: string
  knowledge_base_id: string
  score: number | null
  content: string
}

function asText(value: unknown): string {
  if (value === null || value === undefined) return ''
  return String(value)
}

function asScore(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value))) {
    return Number(value)
  }
  return null
}

/**
 * Accepts the `citations` payload of `knowledge_retrieved` or the
 * `output_payload.knowledge_citations` array. Unknown shapes yield `[]`.
 */
export function parseKnowledgeCitations(value: unknown): KnowledgeCitation[] {
  let raw = value
  if (
    !Array.isArray(raw) &&
    typeof raw === 'object' &&
    raw !== null &&
    Array.isArray((raw as Record<string, unknown>).citations)
  ) {
    raw = (raw as Record<string, unknown>).citations
  }
  if (!Array.isArray(raw)) return []
  const citations: KnowledgeCitation[] = []
  for (const entry of raw) {
    if (typeof entry !== 'object' || entry === null) continue
    const item = entry as Record<string, unknown>
    const chunkId = asText(item.chunk_id)
    const documentId = asText(item.document_id)
    const label = asText(item.label) || chunkId || `引用 ${citations.length + 1}`
    const content = asText(item.content)
    citations.push({
      label,
      chunk_id: chunkId,
      document_id: documentId,
      document_title: asText(item.document_title),
      knowledge_base_id: asText(item.knowledge_base_id),
      score: asScore(item.score),
      content,
    })
  }
  return citations
}

export function citationKey(citation: KnowledgeCitation): string {
  if (citation.chunk_id) return `chunk:${citation.chunk_id}`
  if (citation.document_id || citation.label) {
    return `doc:${citation.document_id}|${citation.label}|${citation.content.slice(0, 80)}`
  }
  return `text:${citation.content.slice(0, 120)}`
}

/** Merge citation groups in order, keeping the first occurrence of a chunk. */
export function mergeKnowledgeCitations(
  ...groups: KnowledgeCitation[][]
): KnowledgeCitation[] {
  const seen = new Set<string>()
  const merged: KnowledgeCitation[] = []
  for (const group of groups) {
    for (const citation of group) {
      const key = citationKey(citation)
      if (seen.has(key)) continue
      seen.add(key)
      merged.push(citation)
    }
  }
  return merged
}
