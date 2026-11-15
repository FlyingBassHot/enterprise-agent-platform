<script setup lang="ts">
withDefaults(
  defineProps<{
    title: string
    subtitle?: string
  }>(),
  { subtitle: '' },
)
</script>

<template>
  <div class="page-header">
    <div>
      <h2>{{ title }}</h2>
      <p v-if="subtitle">{{ subtitle }}</p>
    </div>
    <div class="actions">
      <slot name="actions" />
    </div>
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 4px;
}

h2 {
  margin: 0;
  font-size: 22px;
  font-weight: 680;
  letter-spacing: -0.2px;
}

p {
  margin: 6px 0 0;
  color: var(--app-muted);
  font-size: 12.5px;
}

.actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
</style>
