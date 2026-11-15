<script setup lang="ts">
import { reactive } from 'vue'
import { ArrowRight } from '@element-plus/icons-vue'
import type { KnowledgeCitation } from '@/components/knowledgeCitations'

withDefaults(
  defineProps<{
    citations: KnowledgeCitation[]
    title?: string
    compact?: boolean
  }>(),
  { title: '知识引用', compact: false },
)

const expanded = reactive(new Set<number>())

function toggle(index: number) {
  if (expanded.has(index)) expanded.delete(index)
  else expanded.add(index)
}

function scoreText(score: number | null): string {
  return score === null ? '' : score.toFixed(3)
}

function preview(content: string, limit = 160): string {
  if (content.length <= limit) return content
  return `${content.slice(0, limit)}…`
}
</script>

<template>
  <div class="knowledge-block" :class="{ compact }">
    <div class="knowledge-head">
      <div class="knowledge-title">
        <span class="knowledge-mark">KB</span>
        {{ title }}
        <span class="knowledge-count">{{ citations.length }}</span>
      </div>
      <span class="knowledge-sub">来自知识检索的引用片段</span>
    </div>

    <div class="citation-list">
      <div
        v-for="(citation, index) in citations"
        :key="citation.chunk_id || `${citation.label}-${index}`"
        class="citation"
      >
        <button type="button" class="citation-head" @click="toggle(index)">
          <span class="citation-label">{{ citation.label }}</span>
          <span class="citation-doc">
            {{ citation.document_title || citation.document_id || '未知文档' }}
          </span>
          <span v-if="citation.score !== null" class="citation-score">
            score {{ scoreText(citation.score) }}
          </span>
          <el-icon class="citation-arrow" :class="{ open: expanded.has(index) }">
            <ArrowRight />
          </el-icon>
        </button>
        <div v-if="citation.chunk_id" class="citation-chunk mono">
          chunk: {{ citation.chunk_id }}
        </div>
        <div
          v-if="citation.content"
          class="citation-content"
          :class="{ clamped: !expanded.has(index) }"
        >
          {{ expanded.has(index) ? citation.content : preview(citation.content) }}
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.knowledge-block {
  border: 1px solid var(--app-border);
  border-radius: 12px;
  background: #f8fafc;
  padding: 12px 14px;
}

.knowledge-block.compact {
  padding: 10px 12px;
  border-radius: 10px;
}

.knowledge-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.knowledge-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  font-weight: 650;
}

.knowledge-mark {
  width: 24px;
  height: 24px;
  border-radius: 7px;
  display: grid;
  place-items: center;
  font-size: 9.5px;
  font-weight: 700;
  letter-spacing: 0.4px;
  color: #fff;
  background: linear-gradient(135deg, #0f766e, #0d9488);
}

.knowledge-count {
  min-width: 20px;
  height: 18px;
  border-radius: 9px;
  padding: 0 7px;
  display: inline-grid;
  place-items: center;
  font-size: 10.5px;
  font-weight: 650;
  color: #0f766e;
  background: rgba(13, 148, 136, 0.12);
}

.knowledge-sub {
  color: var(--app-muted);
  font-size: 10.5px;
}

.citation-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.citation {
  border: 1px solid var(--app-border);
  border-radius: 9px;
  padding: 8px 10px;
  background: #fff;
}

.citation-head {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  border: 0;
  background: transparent;
  padding: 0;
  font: inherit;
  color: inherit;
  text-align: left;
  cursor: pointer;
}

.citation-label {
  flex: 0 0 auto;
  font-size: 10.5px;
  font-weight: 650;
  color: #0f766e;
  background: rgba(13, 148, 136, 0.1);
  border-radius: 5px;
  padding: 2px 6px;
}

.citation-doc {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.citation-score {
  flex: 0 0 auto;
  font-family: 'JetBrains Mono', 'SFMono-Regular', Consolas, monospace;
  font-size: 10.5px;
  color: var(--app-muted);
}

.citation-arrow {
  flex: 0 0 auto;
  font-size: 11px;
  color: var(--app-muted);
  transition: transform 0.15s ease;
}

.citation-arrow.open {
  transform: rotate(90deg);
}

.citation-chunk {
  margin-top: 3px;
  font-size: 10px;
  color: var(--app-muted);
  word-break: break-all;
}

.citation-content {
  margin-top: 6px;
  font-size: 11.5px;
  line-height: 1.65;
  color: #475569;
  white-space: pre-wrap;
  word-break: break-word;
}

.citation-content.clamped {
  color: var(--app-muted);
}

.mono {
  font-family: 'JetBrains Mono', 'SFMono-Regular', Consolas, monospace;
}
</style>
