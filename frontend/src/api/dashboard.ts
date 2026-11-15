import client from './client'

export interface ExecutionSummary {
  id: string
  agent_id: string
  agent_name: string | null
  user_id: string
  status: string
  input_text: string
  created_at: string
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
}

export interface DashboardStats {
  agents_total: number
  agents_published: number
  tools_total: number
  tools_enabled: number
  executions_total: number
  executions_completed: number
  executions_failed: number
  executions_running: number
  success_rate: number
  tokens_total: number
  avg_duration_ms: number | null
  status_breakdown: Record<string, number>
  recent_executions: ExecutionSummary[]
}

export const dashboardApi = {
  stats: () => client.get<DashboardStats>('/dashboard/stats').then((r) => r.data),
}
