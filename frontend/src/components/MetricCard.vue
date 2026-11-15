<script setup lang="ts">
defineProps<{
  label: string
  value: string | number
  hint?: string
  accent?: 'blue' | 'green' | 'amber' | 'violet'
}>()
</script>

<template>
  <div class="metric-card" :class="accent || 'blue'">
    <div class="label">{{ label }}</div>
    <div class="value">{{ value }}</div>
    <div v-if="hint" class="hint">{{ hint }}</div>
  </div>
</template>

<style scoped>
.metric-card {
  border-radius: 14px;
  padding: 18px 20px;
  background: #fff;
  border: 1px solid var(--app-border);
  position: relative;
  overflow: hidden;
}

.metric-card::after {
  content: '';
  position: absolute;
  right: -24px;
  top: -24px;
  width: 84px;
  height: 84px;
  border-radius: 50%;
  opacity: 0.1;
}

.metric-card.blue::after { background: #2f6bff; }
.metric-card.green::after { background: #16a34a; }
.metric-card.amber::after { background: #d97706; }
.metric-card.violet::after { background: #7c3aed; }

.label {
  color: var(--app-muted);
  font-size: 12px;
}

.value {
  margin-top: 8px;
  font-size: 26px;
  font-weight: 680;
  letter-spacing: -0.6px;
}

.hint {
  margin-top: 6px;
  color: var(--app-muted);
  font-size: 11px;
}
</style>
