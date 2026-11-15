import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import client from '@/api/client'

export interface UserProfile {
  id: string
  username: string
  display_name: string
  email: string | null
  is_active: boolean
  is_superuser: boolean
  department_id: string | null
  roles: string[]
  created_at: string
}

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string>(localStorage.getItem('access_token') || '')
  const user = ref<UserProfile | null>(null)
  const loading = ref(false)

  const isAuthenticated = computed(() => Boolean(token.value))
  const permissions = computed(() => {
    // Backend exposes effective permission checks per endpoint; the UI mirrors
    // role-based visibility and always relies on server-side enforcement.
    const rolePermissions: Record<string, string[]> = {
      admin: ['*'],
      developer: [
        'agents:read', 'agents:write', 'agents:publish',
        'prompts:read', 'prompts:write',
        'models:read', 'models:write',
        'tools:read', 'tools:write', 'tools:execute',
        'executions:read', 'executions:write',
        'approvals:read', 'approvals:write',
        'dashboard:read', 'audit:read',
      ],
      viewer: [
        'agents:read', 'prompts:read', 'models:read', 'tools:read',
        'executions:read', 'approvals:read', 'dashboard:read', 'audit:read',
      ],
    }
    const set = new Set<string>()
    if (user.value?.is_superuser) set.add('*')
    for (const role of user.value?.roles || []) {
      for (const permission of rolePermissions[role] || []) set.add(permission)
    }
    return set
  })

  function has(permission: string): boolean {
    return permissions.value.has('*') || permissions.value.has(permission)
  }

  function setSession(nextToken: string, profile: UserProfile) {
    token.value = nextToken
    user.value = profile
    localStorage.setItem('access_token', nextToken)
  }

  async function login(username: string, password: string) {
    loading.value = true
    try {
      const { data } = await client.post('/auth/login', { username, password })
      setSession(data.access_token, data.user)
      return data.user as UserProfile
    } finally {
      loading.value = false
    }
  }

  async function bootstrap(payload: {
    username: string
    password: string
    display_name?: string
    token?: string
  }) {
    loading.value = true
    try {
      const { data } = await client.post('/auth/bootstrap', payload)
      setSession(data.access_token, data.user)
      return data.user as UserProfile
    } finally {
      loading.value = false
    }
  }

  async function fetchMe() {
    if (!token.value) return null
    try {
      const { data } = await client.get('/auth/me')
      user.value = data
      return data as UserProfile
    } catch {
      logout()
      return null
    }
  }

  function logout() {
    token.value = ''
    user.value = null
    localStorage.removeItem('access_token')
  }

  return {
    token,
    user,
    loading,
    isAuthenticated,
    permissions,
    has,
    login,
    bootstrap,
    fetchMe,
    logout,
    setSession,
  }
})
